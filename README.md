# litepop
LiTePoP: Linux Terminal Podcast Player

**litepop** is a terminal-based podcast player for Linux. It supports playback control, download queue management, and full synchronization with a [Nextcloud-gPodder](https://github.com/gpodder/mygpo) or an [oPodSync](https://github.com/kd2org/opodsync) server.

Built with `curses` for a clean terminal UI and using `mpv` for audio playback, litepop allows you to manage subscriptions, queue episodes, and sync your listening progress across devices.

## Features

- Terminal UI with keyboard navigation and playback controls
- Smart download manager with concurrent downloads
- Seamless sync with Nextcloud-gPodder (subscriptions and episode actions)
- Playback position and speed control via `mpv` IPC
- Offline playback of downloaded episodes
- Episode queue with per-item progress display
- Auto-recovery of partially played episodes
- Grouped view by publication date when adding episodes

## Requirements

- Python 3.6+
- `mpv` installed and available in `$PATH`
- A Nextcloud instance with the gPodder sync app enabled (or another compatible server)
- Optional: `ffmpeg` for advanced playback features

## Installation

Clone this repository and run `litepop.py`:

```bash
git clone https://github.com/yourusername/litepop.git
cd litepop
python3 litepop.py
````

Make sure the config file is generated at `~/.config/litepop.conf` upon first run. You can manually edit your gPodder credentials there.

## Keyboard Controls

| Key         | Action                          |
| ----------- | ------------------------------- |
| ↑ / ↓       | Navigate queue                  |
| SPACE       | Play/Pause current item         |
| ENTER       | Play next episode               |
| ← / →       | Seek backward/forward 10s       |
| `d`         | Delete selected episode         |
| `D`         | Delete and mark as completed    |
| `a`         | Add new episodes                |
| `s`         | Change playback speed (cyclic)  |
| `R`         | Reset progress of selected item |
| `c`         | Clear completed items           |
| `r`         | Force sync with gPodder         |
| `ESC` / `q` | Exit                            |

## Configuration

Edit `~/.config/litepop.conf` to adjust player command, download folder, and gPodder credentials:

```ini
[gpodder]
server_url = https://sync-server.com
username = yourusername
password = yourpassword
backend = opodsync # or nextcloud
device_id = litepop
sync_interval = 300
initial_days_back = 90

[player]
temp_dir = /tmp/litepop
log_file = /tmp/litepop/litepop.log
default_speed = 1.0
player_command = mpv --no-config --no-video --af=loudnorm=i=-16:lra=11:tp=-1.5 --speed={speed} --start={start_time} --input-ipc-server={ipc_socket} {file}
```

## To Do / Open for Contributions

Contributions are very welcome! Here are some ideas where help is needed:

* ✅ **Internal MP3 player in Python**
  Replace `mpv` with an internal player for MP3 files that supports:

  * Playback progress tracking
  * Seek with ← and → keys (±10s)
  * Pitch-preserving speed variation
    Keep `mpv` as fallback for non-MP3 formats.

* ✅ **Display "Downloading, wait" message**
  When an item is selected but not yet downloaded, display a clear message to prevent user confusion.

* ✅ **Subscription management from UI**
  Add support for:

  * Adding a new subscription from an RSS URL
  * Removing an existing subscription
  * Syncing these changes to the gPodder server

* ✅ **Code optimization and cleanup**

  * Split the current monolithic script into modules
  * Improve navigation speed and reduce latency
  * Add inline comments (in English) explaining the more complex functions

* 📄 **Unit tests**

  * Add unit tests for parsing feeds, syncing actions, etc.

* 🐞 **Bug reports**

  * Open issues for any playback glitches, sync failures, or UI crashes

## License

GPL 3.0

---

Contributions, feedback, and ideas are appreciated! Feel free to open a PR or issue.


**Bitcoin Cash tips:** bitcoincash:qr5epnhfcg4qzrmtsgg7st8939afu87qsu54e6vp5r
