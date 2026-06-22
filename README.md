# HDHomeRun Archiver

A Python script that automatically moves completed DVR recordings from an **HDHomeRun Flex 4K**'s attached external storage to a local Mac drive (e.g. for Plex), then deletes them from the HDHomeRun only after the local copy has been verified byte-for-byte complete. Designed to run unattended on a schedule via `launchd` or `cron`.

---

## Features

- **Atomic, verified downloads** — files download to a `.partial` temp name and are only renamed to their final destination once the transferred byte count matches the server-reported `Content-Length` exactly. A dropped connection or mid-transfer failure leaves no trace at the final path and will be retried on the next run.
- **Safe deletion** — the HDHomeRun recording is never deleted until the local file exists, is complete, and has passed verification.
- **Single-instance lock** — uses `fcntl` to prevent two overlapping runs (e.g. if a large download takes longer than the scheduled interval) from racing each other.
- **Rotating log file** — logs to `~/Library/Logs/hdhomerun_archiver/archiver.log` with up to 25 MB of history retained across five rotating files.
- **Pushover alerts** — optional push notifications via [Pushover](https://pushover.net) for:
  - Target drive not mounted
  - HDHomeRun device unreachable
  - An episode failing to download after 3 consecutive runs
  - An episode failing to delete from the HDHomeRun after 3 consecutive runs
  - Successful archive of each episode (optional, independently toggled)
- **Alert deduplication** — each problem fires a Pushover notification exactly once, not once per hour while the problem persists. Alerts re-arm automatically once the problem resolves.
- **Plex-friendly file layout** — organises recordings into `Show Name / Season XX / Show Name - SXXEXX - Episode Title.mpg`.

---

## Requirements

- macOS (uses `fcntl` for locking and `~/Library/Logs` for state; not tested on Linux/Windows)
- Python 3.9 or later
- No third-party packages — stdlib only
- An HDHomeRun device with a DVR subscription and attached external storage
- A [Pushover](https://pushover.net) account (optional, for alerts)

---

## Setup

**1. Download the script**

Save `hdhomerun_archiver.py` somewhere permanent on your Mac, e.g.:

```
~/Scripts/hdhomerun_archiver.py
```

**2. Edit the configuration block** near the top of the script:

```python
HDHOMERUN_IP = "192.168.1.xxx"          # Your HDHomeRun's local IP address
TARGET_DIR = Path("/Volumes/YourDrive/TV Shows")  # Where to archive recordings
```

To find your HDHomeRun's IP address, open the HDHomeRun app on your Mac or check your router's device list.

**3. Configure Pushover** (optional)

If you want push notifications, create a free [Pushover](https://pushover.net) account:

1. Note your **User Key** on the Pushover dashboard.
2. Go to **pushover.net/apps/build**, register an application (e.g. "HDHomeRun Archiver"), and note its **API Token**.
3. Paste both into the script:

```python
PUSHOVER_ENABLED = True
PUSHOVER_APP_TOKEN = "your-app-token-here"
PUSHOVER_USER_KEY  = "your-user-key-here"
```

Set `PUSHOVER_ENABLED = False` to disable all notifications, or set `NOTIFY_ON_SUCCESSFUL_DOWNLOAD = False` to suppress the per-episode success notification while keeping error alerts active.

**4. Test manually**

```bash
python3 ~/Scripts/hdhomerun_archiver.py
```

On a successful run with no new recordings you should see:

```
2026-01-01 12:00:00 [INFO] === Archiver run started ===
2026-01-01 12:00:00 [INFO] === Archiver run finished (0 episode(s) seen) ===
```

**5. Schedule with launchd**

Create a launchd plist to run the script hourly. Save the following as `~/Library/LaunchAgents/com.hdhomerun.archiver.plist`, substituting your username and script path:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hdhomerun.archiver</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/yourusername/Scripts/hdhomerun_archiver.py</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/yourusername</string>
    </dict>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.hdhomerun.archiver.plist
```

---

## File layout

Recordings are organised to match Plex's expected library structure:

```
TV Shows/
└── The Nanny/
    └── Season 04/
        └── The Nanny - S04E23 - You Bette Your Life.mpg
```

Recordings without a season/episode number (e.g. one-off specials) are placed in a `Specials/` folder and disambiguated using the HDHomeRun's recording ID.

---

## State files

All state is written to `~/Library/Logs/hdhomerun_archiver/`:

| File | Purpose |
|---|---|
| `archiver.log` | Human-readable run log, rotates at 5 MB, 5 backups kept |
| `archiver.lock` | Prevents concurrent runs |
| `alert_state.json` | Tracks per-episode failure counts and which alerts have already fired |

---

## Alert behaviour summary

| Event | Alert sent | Frequency |
|---|---|---|
| Target drive not mounted | Yes (priority 1) | Once per occurrence |
| HDHomeRun unreachable | Yes (priority 1) | Once per occurrence |
| Episode download fails 3× in a row | Yes (priority 0) | Once per stuck episode |
| Episode delete fails 3× in a row | Yes (priority 0) | Once per stuck episode |
| Episode archived successfully | Yes (priority −1, no sound) | Every episode (if enabled) |
| Run completes with nothing to do | No | — |

---

## License

MIT License. See [LICENSE](LICENSE) for details.
