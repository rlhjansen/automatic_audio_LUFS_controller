# Real-Time Audio Level Controller

A Windows system-tray application that monitors system audio via WASAPI loopback and automatically adjusts the master volume to maintain consistent perceived loudness across songs, podcasts, videos, and applications.

Think of it as a system-wide loudness normaliser that runs in real time — no need to pre-process files.

## How it works

```
desired_volume_dB = target_LUFS − source_LUFS   (clamped to device range)
```

1. **WASAPI loopback capture** reads the system audio mix (pre-volume on most hardware — see [Environment notes](#environment--assumptions) below).
2. Loudness is integrated over a **configurable sliding window** (default 10 s) of per-block mean-square energy, with silence gating (ITU-R BS.1770-style).
3. A **feed-forward controller** sets the Windows master volume so that `source + volume ≈ target`. No feedback loop — the loopback signal is independent of the volume slider.
4. **Slew-rate limiting** (8 dB/s) and **hold-before-release** (1.5 s) smooth out transient dips so it doesn't chase every beat.
5. **Manual override detection** — if you grab the Windows volume slider, the controller pauses for 30 s before resuming.

## Features

- **System tray icon** with colour-coded status (green = tracking, yellow = adjusting, grey = silent/disabled)
- **Settings window** (double-click tray icon) with:
  - Target loudness slider (−60 to 0 LUFS, 0.5 dB steps)
  - Window duration slider (5 to 120 seconds)
  - Enable/disable checkbox
  - Live status display
- **Single-instance guard** using a PID file — prevents duplicate launches
- **Config persistence** to `~/.audio_level_controller.json`
- **Auto-start installer** (`--install`) — copies everything to `%LOCALAPPDATA%\AudioLevelController\` with a dedicated venv, no USB drive required after installation
- **Console mode** (`--console`) for debugging

## Installation

### Prerequisites

- **Windows 10/11** (uses WASAPI and COM APIs)
- **Python 3.10+** (developed with 3.13.1)
- A working audio output device

### Quick start

```bash
# Clone the repo
git clone https://github.com/YOUR_USER/audio-level-controller.git
cd audio-level-controller

# Create a virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### soundcard / numpy compatibility patch

If you're using **numpy 2.x** (which is the default for Python 3.12+), the `soundcard` package (v0.4.x) will crash with `AttributeError: module 'numpy' has no attribute 'fromstring'`. You need to patch one line in soundcard's `mediafoundation.py`:

```
# Find the file:
#   .venv\Lib\site-packages\soundcard\mediafoundation.py
#
# Around line 761, change:
#   numpy.fromstring(_ffi.buffer(...), dtype='float32')
# To:
#   numpy.frombuffer(bytes(_ffi.buffer(...)), dtype='float32').copy()
```

The `--install` command applies this patch automatically.

## Usage

### System tray mode (default)

```bash
python audio_level_controller.py
```

Runs as a background app with a system tray icon. Double-click the icon to open Settings, right-click for the quick menu.

### Console mode

```bash
python audio_level_controller.py --console
```

Shows a single-line live readout for debugging:
```
[ON ] Src: -18.3  Vol:  -7.7dB  Heard: -26.0  T:  -26  
```

### Set target from CLI

```bash
python audio_level_controller.py --target -30
```

Saves the target to config and exits.

### Install to Windows startup

```bash
python audio_level_controller.py --install
```

Creates a self-contained copy under `%LOCALAPPDATA%\AudioLevelController\` with its own venv and a VBS startup launcher. The controller will auto-start on login — no USB drive or repo checkout needed.

```bash
python audio_level_controller.py --uninstall
```

Removes the startup entry and installed files.

### List audio devices

```bash
python audio_level_controller.py --list
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

This was developed and tested on:

- **Windows 11** (should work on Windows 10 as well)
- **Python 3.13.1** (CPython, 64-bit)
- **numpy 2.x**, **soundcard 0.4.5**, **pycaw 20240210**, **comtypes**, **pystray**, **Pillow**

### Critical assumption: WASAPI loopback is pre-volume

On the development machine, WASAPI loopback captures audio **before** the Windows volume slider is applied. This means the captured signal level doesn't change when you move the slider, which is what makes the feed-forward design work.

**If your system's loopback is post-volume** (i.e. the captured signal *does* change with the slider), the controller will create a feedback loop and will not work correctly. You would need to switch to a PI/PID feedback controller design instead.

You can test this by running `--console`, playing audio at a fixed level, and moving the Windows volume slider. If the `Src` reading changes, your loopback is post-volume.

### Windows-only

The controller depends on:
- **WASAPI** (via `soundcard`) for loopback audio capture
- **pycaw** / **comtypes** for Windows audio endpoint volume control
- **COM threading** (`CoInitialize` / `CoUninitialize`)
- Windows **named Startup folder** for auto-start

Porting to macOS or Linux would require replacing the volume control backend and loopback capture mechanism.

### COM threading

All COM-dependent libraries (`comtypes`, `soundcard`, `pycaw`) are imported at **module level** on the main thread. Each worker thread calls `comtypes.CoInitialize()` for its own COM apartment. Importing these inside a thread function causes `UnboundLocalError` due to Python's scoping rules — don't move the imports.

## Dependencies

```
numpy
soundcard
pycaw
comtypes
pystray
Pillow
```

Also uses `tkinter` (bundled with standard Python on Windows).

## Repo contents

| File | Description |
|------|-------------|
| `audio_level_controller.py` | Main application (single-file) |
| `audio_level_targeting.py` | Batch offline normalisation script (EBU R128 / LUFS, uses ffmpeg) |
| `requirements.txt` | Python dependencies |
| `README.md` | This file |

## License

MIT
