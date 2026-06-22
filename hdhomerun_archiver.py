#!/usr/bin/env python3
"""
HDHomeRun DVR Archiver
-----------------------
Moves completed recordings from an HDHomeRun Flex 4K's attached storage to
local/Plex storage, and deletes them from the HDHomeRun only after the
local copy has been verified byte-for-byte complete.

Designed to run unattended on a schedule (cron/launchd). Safe to run
concurrently-protected (uses a lock file) and safe to interrupt at any
point -- partial downloads never overwrite a good file and are never
mistaken for a complete one.

Exit codes:
  0 = ran successfully (whether or not there was anything to do)
  1 = aborted before doing any work (e.g. target drive not mounted, lock held)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, Optional

# --- CONFIGURATION ---------------------------------------------------------

HDHOMERUN_IP = "192.168.1.xxx"
TARGET_DIR = Path("path_to_target_directory")

# Where logs, the lock file, and alert-tracking state live. Keep this on
# local/internal storage (not the external SSD) so logging and alerting
# still work even if the drive is unmounted -- that's the scenario we most
# need to hear about.
STATE_DIR = Path.home() / "Library/Logs/hdhomerun_archiver"
LOG_FILE = STATE_DIR / "archiver.log"
LOCK_FILE = STATE_DIR / "archiver.lock"
ALERT_STATE_FILE = STATE_DIR / "alert_state.json"

# Network behavior
REQUEST_TIMEOUT_SECONDS = 30          # for JSON/API calls
DOWNLOAD_CHUNK_SIZE = 1024 * 1024     # 1 MB
DOWNLOAD_STALL_TIMEOUT_SECONDS = 60   # abort if no progress for this long

# --- ALERTING ---------------------------------------------------------
# Pushover (https://pushover.net). Create an Application/API Token at
# pushover.net/apps/build, and find your User Key on your dashboard at
# pushover.net. Both are required for alerts to send.
PUSHOVER_ENABLED = True
PUSHOVER_APP_TOKEN = "your-app-token-here"
PUSHOVER_USER_KEY = "your-user-key-here"

# How many consecutive runs a single episode can fail to download before
# we alert about it (rather than silently letting it retry next hour).
DOWNLOAD_FAILURE_ALERT_THRESHOLD = 3

# How many consecutive runs a delete-from-HDHomeRun can fail before we
# alert (this usually means HDHomeRun storage is filling up unnoticed).
DELETE_FAILURE_ALERT_THRESHOLD = 3

# Set to True to receive an informational Pushover notification each time
# an episode is successfully downloaded and verified. Set to False to
# suppress these and only receive error/warning alerts.
NOTIFY_ON_SUCCESSFUL_DOWNLOAD = True

# -----------------------------------------------------------------------

BASE_URL = f"http://{HDHOMERUN_IP}/"
RECORDED_JSON_URL = f"http://{HDHOMERUN_IP}/recorded_files.json"

logger = logging.getLogger("hdhomerun_archiver")


def setup_logging() -> None:
    """Logs to both stderr (for interactive runs) and a rotating log file
    (so a cron job's history is inspectable after the fact)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def send_pushover_alert(title: str, message: str, priority: int = 0) -> None:
    """Sends a push notification via Pushover. Failures here are logged but
    never raised -- a broken alerting path must not take down the archive
    run itself."""
    if not PUSHOVER_ENABLED:
        return
    if "your-app-token-here" in PUSHOVER_APP_TOKEN or "your-user-key-here" in PUSHOVER_USER_KEY:
        logger.warning(
            "Pushover is enabled but not configured (placeholder token/key). "
            "Skipping alert: %s",
            title,
        )
        return

    data = urllib.parse.urlencode(
        {
            "token": PUSHOVER_APP_TOKEN,
            "user": PUSHOVER_USER_KEY,
            "title": title,
            "message": message,
            "priority": priority,
        }
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json", data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                logger.warning("Pushover API returned status %s", resp.status)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        logger.warning("Failed to send Pushover alert: %s", e)


@dataclass
class AlertState:
    """Tracks consecutive per-file failure counts across runs, persisted to
    disk, so we can tell "transient hiccup, will retry fine" apart from
    "stuck and needs a human" without spamming an alert every single hour.
    Keyed by destination filename, which is stable across runs for the
    same episode.
    """

    download_failures: dict[str, int] = field(default_factory=dict)
    delete_failures: dict[str, int] = field(default_factory=dict)
    # Tracks which alert keys we've already paged on, so once a human is
    # notified we don't re-alert every run -- only when it either resolves
    # (cleared on success) or a fresh problem appears.
    already_alerted: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "AlertState":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
            return cls(
                download_failures=raw.get("download_failures", {}),
                delete_failures=raw.get("delete_failures", {}),
                already_alerted=set(raw.get("already_alerted", [])),
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load alert state from %s: %s", path, e)
            return cls()

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "download_failures": self.download_failures,
                        "delete_failures": self.delete_failures,
                        "already_alerted": sorted(self.already_alerted),
                    },
                    indent=2,
                )
            )
        except OSError as e:
            logger.warning("Could not save alert state to %s: %s", path, e)

    def record_download_failure(self, key: str) -> int:
        count = self.download_failures.get(key, 0) + 1
        self.download_failures[key] = count
        return count

    def record_download_success(self, key: str) -> None:
        self.download_failures.pop(key, None)
        self.already_alerted.discard(f"download:{key}")

    def record_delete_failure(self, key: str) -> int:
        count = self.delete_failures.get(key, 0) + 1
        self.delete_failures[key] = count
        return count

    def record_delete_success(self, key: str) -> None:
        self.delete_failures.pop(key, None)
        self.already_alerted.discard(f"delete:{key}")

    def should_alert(self, alert_key: str) -> bool:
        """Returns True (and marks as alerted) only the first time a given
        alert_key crosses its threshold, so we page once per problem, not
        once per hour the problem persists."""
        if alert_key in self.already_alerted:
            return False
        self.already_alerted.add(alert_key)
        return True


@contextmanager
def single_instance_lock(lock_path: Path) -> Iterator[None]:
    """Ensures only one copy of this script runs at a time. If a download
    takes longer than the cron interval, the next run will see the lock
    held and exit cleanly instead of starting a second concurrent download
    of the same file."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        logger.warning(
            "Another instance is already running (lock held at %s). Exiting.",
            lock_path,
        )
        sys.exit(1)
    try:
        fd.write(str(os.getpid()))
        fd.flush()
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def sanitize_name(name: str) -> str:
    """Removes filesystem-unsafe characters and collapses whitespace so
    names are clean across macOS/SMB/Plex."""
    cleaned = "".join(c for c in name if c.isalnum() or c in " ._-")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Unknown"


def fetch_json(url: str) -> Optional[object]:
    """GETs a URL and parses it as JSON. Returns None on any failure,
    logging the specifics so cron logs are actionable."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                logger.error("Unexpected status %s fetching %s", resp.status, url)
                return None
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.error("HTTP error %s fetching %s: %s", e.code, url, e.reason)
    except urllib.error.URLError as e:
        logger.error("Network error fetching %s: %s", url, e.reason)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON from %s: %s", url, e)
    return None


def absolute_url(maybe_relative: str) -> str:
    if maybe_relative.startswith("/"):
        return urllib.parse.urljoin(BASE_URL, maybe_relative)
    return maybe_relative


@dataclass
class DownloadResult:
    success: bool
    bytes_written: int = 0
    expected_bytes: Optional[int] = None
    error: Optional[str] = None


def download_with_verification(url: str, dest_path: Path) -> DownloadResult:
    """Downloads to a temporary sibling file and only renames it into place
    once the byte count matches the server-reported Content-Length. This
    makes the operation atomic from the point of view of anything else
    looking at dest_path (including this script's own "already exists"
    check on the next run) -- a half-downloaded file can never appear at
    the final filename.

    If the server doesn't report Content-Length, we fall back to "did the
    download complete without an exception," which is weaker but still far
    better than the old `size > 0` check.
    """
    tmp_path = dest_path.with_name(dest_path.name + ".partial")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return DownloadResult(success=False, error=f"HTTP status {resp.status}")

            expected_bytes = resp.length  # Content-Length, or None if absent/chunked
            written = 0

            with open(tmp_path, "wb") as f:
                while True:
                    # Per-chunk timeout is enforced by the socket timeout set
                    # above (urlopen's timeout also applies to subsequent
                    # reads on the same connection in CPython).
                    chunk = resp.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)

            if expected_bytes is not None and written != expected_bytes:
                tmp_path.unlink(missing_ok=True)
                return DownloadResult(
                    success=False,
                    bytes_written=written,
                    expected_bytes=expected_bytes,
                    error=(
                        f"Size mismatch: wrote {written} bytes, "
                        f"server reported {expected_bytes}"
                    ),
                )

            if written == 0:
                tmp_path.unlink(missing_ok=True)
                return DownloadResult(success=False, error="Downloaded file is empty")

            # Atomic on the same filesystem -- this is the step that makes
            # partial downloads invisible to everything else.
            os.replace(tmp_path, dest_path)
            return DownloadResult(
                success=True, bytes_written=written, expected_bytes=expected_bytes
            )

    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        tmp_path.unlink(missing_ok=True)
        return DownloadResult(success=False, error=str(e))


def delete_from_hdhomerun(cmd_url: str) -> bool:
    """POSTs the delete command, preserving the recording's unique ID that
    HDHomeRun embeds in CmdURL. rerecord=0 means "archive mode": don't
    re-record this episode just because it was deleted."""
    try:
        parts = urllib.parse.urlparse(cmd_url)
        params = urllib.parse.parse_qs(parts.query)
        params["cmd"] = "delete"
        params["rerecord"] = "0"
        new_query = urllib.parse.urlencode(params, doseq=True)
        final_url = urllib.parse.urlunparse(parts._replace(query=new_query))

        req = urllib.request.Request(final_url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            resp.read()
            if resp.status != 200:
                logger.warning(
                    "Delete request to %s returned status %s", final_url, resp.status
                )
                return False
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        logger.warning("Failed to delete remote file via %s: %s", cmd_url, e)
        return False


def build_dest_path(ep: dict) -> Path:
    series_name = sanitize_name(ep.get("Title", "Unknown Show"))
    ep_string = ep.get("EpisodeNumber", "")

    season_match = re.search(r"S(\d+)", ep_string, re.IGNORECASE)
    if season_match:
        season_num = int(season_match.group(1))
        season_folder = f"Season {season_num:02d}"
        base_name = f"{series_name} - {ep_string.upper()}"
    else:
        season_folder = "Specials"
        base_name = Path(ep.get("Filename", f"{series_name}_recording")).stem
        base_name = sanitize_name(base_name)

    # Disambiguate same-named specials/episodes using HDHomeRun's own
    # recording ID when available, so two different episodes never collide
    # on disk and silently overwrite one another.
    rec_id = ep.get("RecordingID") or ep.get("RecordEndTime")
    episode_title = ep.get("EpisodeTitle")
    if episode_title:
        base_name += f" - {sanitize_name(episode_title)}"
    elif rec_id:
        base_name += f" - {rec_id}"

    return TARGET_DIR / series_name / season_folder / f"{base_name}.mpg"


def process_episode(ep: dict, alert_state: AlertState) -> None:
    download_url = ep.get("PlayURL")
    cmd_url = ep.get("CmdURL")
    if not download_url:
        logger.warning("Skipping entry with no PlayURL: %r", ep.get("Title"))
        return

    download_url = absolute_url(download_url)
    if cmd_url:
        cmd_url = absolute_url(cmd_url)

    dest_path = build_dest_path(ep)
    key = str(dest_path)  # stable identity for this episode across runs

    if dest_path.exists():
        logger.debug("Already archived, skipping: %s", dest_path.name)
        return

    logger.info("Downloading: %s", dest_path.name)
    result = download_with_verification(download_url, dest_path)

    if not result.success:
        fail_count = alert_state.record_download_failure(key)
        logger.error(
            "Download failed for %s (%d consecutive failure(s)): %s",
            dest_path.name,
            fail_count,
            result.error,
        )
        if (
            fail_count >= DOWNLOAD_FAILURE_ALERT_THRESHOLD
            and alert_state.should_alert(f"download:{key}")
        ):
            send_pushover_alert(
                title="HDHomeRun Archiver: download stuck",
                message=(
                    f"'{dest_path.name}' has failed to download "
                    f"{fail_count} times in a row.\nLatest error: {result.error}"
                ),
                priority=0,
            )
        return

    alert_state.record_download_success(key)
    logger.info(
        "Download verified complete: %s (%s bytes)",
        dest_path.name,
        result.bytes_written,
    )

    if NOTIFY_ON_SUCCESSFUL_DOWNLOAD:
        series_name = ep.get("Title", "Unknown Show")
        ep_number = ep.get("EpisodeNumber", "")
        ep_title = ep.get("EpisodeTitle", "")
        size_mb = result.bytes_written / (1024 * 1024)

        # Build a compact, readable description line, e.g.:
        #   The Nanny  S04E23 – You Bette Your Life  (281.8 MB)
        details = series_name
        if ep_number:
            details += f"  {ep_number.upper()}"
        if ep_title:
            details += f" \u2013 {ep_title}"
        details += f"  ({size_mb:.1f} MB)"

        send_pushover_alert(
            title="HDHomeRun Archiver: episode archived",
            message=details,
            priority=-1,  # lowest priority: no sound, no vibration
        )

    if not cmd_url:
        logger.warning(
            "No CmdURL provided for %s -- file archived locally but NOT "
            "deleted from HDHomeRun. Remove it manually.",
            dest_path.name,
        )
        return

    if delete_from_hdhomerun(cmd_url):
        alert_state.record_delete_success(key)
        logger.info("Deleted from HDHomeRun: %s", dest_path.name)
    else:
        fail_count = alert_state.record_delete_failure(key)
        logger.warning(
            "Could not delete %s from HDHomeRun (%d consecutive failure(s)). "
            "It will be skipped on the next run (already archived locally).",
            dest_path.name,
            fail_count,
        )
        if (
            fail_count >= DELETE_FAILURE_ALERT_THRESHOLD
            and alert_state.should_alert(f"delete:{key}")
        ):
            send_pushover_alert(
                title="HDHomeRun Archiver: can't free up storage",
                message=(
                    f"'{dest_path.name}' was archived successfully but has "
                    f"failed to delete from the HDHomeRun {fail_count} times "
                    f"in a row. Its storage may be filling up -- consider "
                    f"deleting it manually from the HDHomeRun web UI."
                ),
                priority=0,
            )


def process_archive() -> None:
    logger.info("=== Archiver run started ===")
    alert_state = AlertState.load(ALERT_STATE_FILE)

    # Refuse to do anything if the external drive isn't actually mounted --
    # writing into an unmounted mount point's parent directory on macOS
    # would otherwise silently write to the boot drive instead.
    if not TARGET_DIR.exists():
        logger.error(
            "Aborting: target directory is unavailable (drive not mounted?): %s",
            TARGET_DIR,
        )
        if alert_state.should_alert("target_dir_missing"):
            send_pushover_alert(
                title="HDHomeRun Archiver: target drive missing",
                message=(
                    f"The archive target {TARGET_DIR} is not available. "
                    f"Check that the external SSD is connected and mounted "
                    f"on the Mac. No files were archived this run."
                ),
                priority=1,
            )
        alert_state.save(ALERT_STATE_FILE)
        sys.exit(1)
    else:
        alert_state.already_alerted.discard("target_dir_missing")

    recordings = fetch_json(RECORDED_JSON_URL)
    if recordings is None:
        logger.error("Aborting: could not retrieve recordings list from HDHomeRun.")
        if alert_state.should_alert("hdhomerun_unreachable"):
            send_pushover_alert(
                title="HDHomeRun Archiver: device unreachable",
                message=(
                    f"Could not reach the HDHomeRun at {HDHOMERUN_IP}. "
                    f"Check that it's powered on and the IP address hasn't "
                    f"changed. No files were archived this run."
                ),
                priority=1,
            )
        alert_state.save(ALERT_STATE_FILE)
        sys.exit(1)
    else:
        alert_state.already_alerted.discard("hdhomerun_unreachable")

    episode_count = 0
    for item in recordings:
        episodes_url = item.get("EpisodesURL")
        if not episodes_url:
            continue
        episodes_url = absolute_url(episodes_url)

        episodes = fetch_json(episodes_url)
        if episodes is None:
            logger.error("Skipping series, could not fetch episodes: %s", episodes_url)
            continue

        for ep in episodes:
            episode_count += 1
            process_episode(ep, alert_state)

    alert_state.save(ALERT_STATE_FILE)
    logger.info("=== Archiver run finished (%d episode(s) seen) ===", episode_count)


def main() -> None:
    setup_logging()
    with single_instance_lock(LOCK_FILE):
        process_archive()


if __name__ == "__main__":
    main()