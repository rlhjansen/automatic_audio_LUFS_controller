# Real-Time Audio Level Controller

A Linux system-tray application that monitors system audio via PulseAudio / PipeWire monitor sources and automatically adjusts the master volume to maintain consistent perceived loudness across songs, podcasts, videos, and applications.

Think of it as a system-wide loudness normaliser that runs in real time — no need to pre-process files.

> **Note:** The original Windows 10/11 version is preserved as `audio_level_controller_windows10.py` (with `requirements_windows10.txt` and `README_windows10.md`).

## How it works

```
desired_volume_dB = target_LUFS − source_LUFS   (clamped to device range)
```

1. **PulseAudio / PipeWire monitor source** reads the system audio mix (the monitor signal is independent of the volume slider on most setups).
2. Loudness is integrated over a **configurable sliding window** (default 10 s) of per-block mean-square energy, with silence gating (ITU-R BS.1770-style).
3. A **feed-forward controller** sets the master volume via `pulsectl` so that `source + volume ≈ target`. No feedback loop — the monitor signal is independent of the volume slider.
4. **Slew-rate limiting** (8 dB/s) and **hold-before-release** (1.5 s) smooth out transient dips so it doesn't chase every beat.
5. **Manual override detection** — if you move the volume slider, the controller pauses for 30 s before resuming.

## Features

- **System tray icon** with colour-coded status (green = tracking, yellow = adjusting, grey = silent/disabled)
- **Settings window** (double-click tray icon) with:
  - Target loudness slider (−60 to 0 LUFS, 0.5 dB steps)
  - Window duration slider (5 to 120 seconds)
  - Enable/disable checkbox
  - Live status display
- **Single-instance guard** using a PID file — prevents duplicate launches
- **Config persistence** to `~/.audio_level_controller.json`
- **XDG autostart installer** (`--install`) — copies everything to `~/.local/share/AudioLevelController/` with a dedicated venv
- **Console mode** (`--console`) for debugging

## Installation

### Prerequisites

- **Ubuntu 22.04+** or any Linux with PulseAudio / PipeWire (with PulseAudio compat)
- **Python 3.10+**
- **tkinter** (for the settings window)
- A working audio output device

### Quick start

```bash
# Clone the repo
git clone https://github.com/rlhjansen/automatic_audio_LUFS_controller.git
cd automatic_audio_LUFS_controller

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# If tkinter is not installed:
sudo apt install python3-tk
```

### System dependencies

The `soundcard` library requires PulseAudio headers on some systems:

```bash
sudo apt install libpulse-dev python3-tk ffmpeg
```

## Usage

### System tray mode (default)

```bash
python3 audio_level_controller.py
```

Runs as a background app with a system tray icon. Double-click the icon to open Settings, right-click for the quick menu.

### Console mode

```bash
python3 audio_level_controller.py --console
```

Shows a single-line live readout for debugging:
```
[ON ] Src: -18.3  Vol:  -7.7dB  Heard: -26.0  T:  -26
```

### Set target from CLI

```bash
python3 audio_level_controller.py --target -30
```

Saves the target to config and exits.

### Install to autostart

```bash
python3 audio_level_controller.py --install
```

Creates a self-contained copy under `~/.local/share/AudioLevelController/` with its own venv and an XDG autostart `.desktop` file. The controller will auto-start on login.

```bash
python3 audio_level_controller.py --uninstall
```

Removes the autostart entry and installed files.

### List audio devices

```bash
python3 audio_level_controller.py --list
```

## Configuration

Settings are saved to `~/.audio_level_controller.json` and can be edited by hand:

| Key | Default | Description |
|-----|---------|-------------|
| `target_lufs` | −26.0 | Desired loudness in LUFS |
| `enabled` | true | Whether the controller is active |
| `window_seconds` | 10.0 | Sliding window for loudness integration (seconds) |
| `slew_rate` | 8.0 | Maximum volume change rate (dB/s) |
| `hold_time` | 1.5 | Seconds to hold before releasing after a transient dip |
| `manual_pause` | 30.0 | Seconds to pause after detecting a manual volume change |

### Recommended settings

| Content | Target LUFS | Window |
|---------|-------------|--------|
| Music (loud) | −20 to −14 | 10 s |
| Music (background) | −30 to −26 | 10 s |
| Podcasts / interviews | −26 to −20 | 30–60 s |
| Quiet listening / speakers | −40 to −30 | 20 s |

## Environment & assumptions

This Linux version was adapted for and tested on:

- **Ubuntu 22.04** with **PipeWire** (PulseAudio compatibility layer)
- **Python 3.10.12**
- **numpy**, **soundcard 0.4.x**, **pulsectl**, **pystray**, **Pillow**

### Critical assumption: monitor source is pre-volume

On most PulseAudio / PipeWire setups, monitor sources capture audio **before** the sink volume is applied. This means the captured signal level doesn't change when you move the volume slider, which is what makes the feed-forward design work.

**If your system's monitor source is post-volume** (i.e. the captured signal *does* change with the slider), the controller will create a feedback loop and will not work correctly. You would need to switch to a PI/PID feedback controller design instead.

You can test this by running `--console`, playing audio at a fixed level, and moving the volume slider. If the `Src` reading changes, your monitor is post-volume.

### Batch normalization

The `audio_level_targeting.py` script scans `~/Music` for audio files (`.mp3`, `.flac`, `.ogg`, `.opus`, `.wav`, `.m4a`, `.aac`) and normalises them using ffmpeg's two-pass loudnorm filter:

```bash
# Make sure ffmpeg is installed
sudo apt install ffmpeg

python3 audio_level_targeting.py
```

Normalised files are written to `~/Music/normalized/`.

## Dependencies

```
numpy
soundcard
pulsectl
pystray
Pillow
```

Also uses `tkinter` (install with `sudo apt install python3-tk` if needed) and optionally `ffmpeg` (for the batch normalisation script).

## Repo contents

| File | Description |
|------|-------------|
| `audio_level_controller.py` | Main application — **Linux** version |
| `audio_level_targeting.py` | Batch offline normalisation — **Linux** version |
| `requirements.txt` | Python dependencies (Linux) |
| `README.md` | This file |
| `audio_level_controller_windows10.py` | Original Windows 10/11 controller |
| `audio_level_targeting_windows10.py` | Original Windows batch normalisation |
| `requirements_windows10.txt` | Python dependencies (Windows) |
| `README_windows10.md` | Original Windows README |

## License

MIT
