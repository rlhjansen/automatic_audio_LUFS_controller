"""
Real-Time Audio Level Controller — System Tray Service
=======================================================
Monitors system audio via WASAPI loopback and adjusts Windows master volume
to maintain consistent loudness across songs and applications.

Key design decisions
--------------------
* WASAPI loopback on this system is **pre-volume** — the captured signal is
  independent of the Windows volume slider.  This allows a pure feed-forward
  controller with no feedback loop:

      desired_volume_dB = target_LUFS − source_LUFS   (clamped to [min, 0])

* Loudness is integrated over a **10-second sliding window** of per-block
  mean-square energy (silence-gated).  This avoids reacting to beat-level
  dynamics while still tracking song-to-song level differences.

* The Windows volume slider is monitored for **manual changes** — if you
  grab the slider the controller backs off for 30 s (but will still attenuate
  sudden loud spikes to protect your ears).

Controls
--------
System tray icon (right-click menu):
  • Enable / Disable
  • Louder (+1 dB)  /  Quieter (−1 dB)
  • Quit

Settings are persisted to  ~/.audio_level_controller.json

CLI flags
---------
  (default)          Run as system tray app
  --console          Console-only mode (for debugging)
  --target -16       Set target LUFS (saved to config)
  --install          Add to Windows startup
  --uninstall        Remove from Windows startup
  --list             List audio devices
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import warnings

warnings.filterwarnings("ignore", message="data discontinuity")

# Pre-import comtypes and soundcard on the main thread so the module-level
# CoInitializeEx() runs here once, avoiding COM threading conflicts in worker
# threads.  Each thread still calls comtypes.CoInitialize() for its own
# COM apartment.
import comtypes                         # noqa: E402
import soundcard as sc                  # noqa: E402
from pycaw.pycaw import AudioUtilities  # noqa: E402

# Handle pythonw.exe (no console) — redirect streams so prints don't crash
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()


# ─── Configuration ───────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".audio_level_controller.json"

DEFAULTS = {
    "target_lufs":   -26.0,
    "enabled":       True,
    "window_seconds": 10.0,
    "slew_rate":      8.0,     # dB / s
    "hold_time":      1.5,     # seconds before releasing after a transient dip
    "manual_pause":  30.0,     # seconds to pause after manual volume change
}


def load_config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG_PATH.read_text())}
    except Exception:
        return dict(DEFAULTS)


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ─── Loudness helpers ────────────────────────────────────────────────────────

def block_mean_square(samples: np.ndarray) -> float:
    """Per-block mean-square power (stereo sum, ITU-R BS.1770 style)."""
    if len(samples) == 0:
        return 0.0
    if samples.ndim > 1:
        return float(np.sum(np.mean(samples ** 2, axis=0)))
    return float(np.mean(samples ** 2))


def ms_to_lufs(ms: float) -> float:
    if ms < 1e-20:
        return -100.0
    return -0.691 + 10.0 * math.log10(ms)


# ─── Single-instance guard (PID file) ───────────────────────────────────────

_PID_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AudioLevelController" / "controller.pid"


def acquire_single_instance() -> bool:
    """Return True if we are the only instance (or prior one is dead)."""
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            # Check if that PID is still alive
            import ctypes as _ct
            kernel32 = _ct.windll.kernel32
            PROCESS_QUERY_LIMITED = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, old_pid)
            if h:
                kernel32.CloseHandle(h)
                return False          # another instance is genuinely running
            # PID gone → stale file, we can take over
        except Exception:
            pass                      # corrupt file, ignore
    _PID_FILE.write_text(str(os.getpid()))
    return True


def release_single_instance():
    try:
        if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(os.getpid()):
            _PID_FILE.unlink()
    except Exception:
        pass


# ─── Core Controller ────────────────────────────────────────────────────────

class AudioLevelController:
    """
    Feed-forward volume controller.

    * Capture thread  — WASAPI loopback → 10 s sliding window of mean-square
    * Control thread  — feed-forward volume adjustment via pycaw
    """

    def __init__(self, cfg: dict):
        self.target_lufs    = cfg["target_lufs"]
        self.enabled        = cfg["enabled"]
        self.window_s       = cfg["window_seconds"]
        self.slew_rate      = cfg["slew_rate"]
        self.hold_time      = cfg["hold_time"]
        self.manual_pause_s = cfg["manual_pause"]

        self.block_ms    = 200          # capture block duration
        self.sample_rate = 48000
        self.silence_thr = -50.0

        max_blocks = max(1, int(self.window_s / (self.block_ms / 1000)))
        self._ms_buf = deque(maxlen=max_blocks)

        # Observable state (read by tray / console)
        self.source_lufs    = -100.0
        self.current_vol_db = 0.0
        self.desired_vol_db = 0.0
        self.is_silent      = True
        self.vol_range      = (-65.25, 0.0, 0.5)
        self.running        = False
        self.speaker_name   = ""
        self.manual_override_until = 0.0

        # Internal
        self._lock          = threading.Lock()
        self._last_set_db   = None
        self._last_set_time = 0.0
        self._hold_counter  = 0.0

    # ── public API (thread-safe) ──

    def adjust_target(self, delta_db: float):
        with self._lock:
            self.target_lufs = max(-60.0, min(0.0, self.target_lufs + delta_db))
        cfg = load_config()
        cfg["target_lufs"] = self.target_lufs
        save_config(cfg)

    def set_target(self, value: float):
        with self._lock:
            self.target_lufs = max(-60.0, min(0.0, value))
        cfg = load_config()
        cfg["target_lufs"] = self.target_lufs
        save_config(cfg)

    def set_window(self, seconds: float):
        seconds = max(5.0, min(120.0, seconds))
        with self._lock:
            self.window_s = seconds
            max_blocks = max(1, int(self.window_s / (self.block_ms / 1000)))
            # Copy existing data into a new deque with the new maxlen
            old = list(self._ms_buf)
            self._ms_buf = deque(old[-max_blocks:], maxlen=max_blocks)
        cfg = load_config()
        cfg["window_seconds"] = seconds
        save_config(cfg)

    def set_enabled(self, on: bool):
        with self._lock:
            self.enabled = on
        cfg = load_config()
        cfg["enabled"] = on
        save_config(cfg)

    def toggle_enabled(self):
        self.set_enabled(not self.enabled)

    def start(self):
        self.running = True
        t1 = threading.Thread(target=self._capture_thread, daemon=True,
                              name="capture")
        t2 = threading.Thread(target=self._control_thread, daemon=True,
                              name="control")
        t1.start()
        t2.start()
        return t1, t2

    def stop(self):
        self.running = False

    # ── capture thread ──

    def _capture_thread(self):
        """WASAPI loopback → sliding-window mean-square buffer."""
        try:
            comtypes.CoInitialize()
        except OSError:
            pass

        while self.running:
            try:
                speaker = sc.default_speaker()
                self.speaker_name = speaker.name
                loopback = sc.get_microphone(speaker.id,
                                             include_loopback=True)
                block_n = int(self.sample_rate * self.block_ms / 1000)

                with loopback.recorder(samplerate=self.sample_rate,
                                       channels=2) as rec:
                    while self.running:
                        try:
                            data = rec.record(numframes=block_n)
                        except Exception:
                            time.sleep(0.01)
                            continue

                        ms = block_mean_square(data.astype(np.float32))

                        with self._lock:
                            self._ms_buf.append(ms)
                            # Silence: latest block only (fast detect)
                            self.is_silent = (ms < 1e-8)
                            # Integrated loudness: skip silent blocks
                            active = [v for v in self._ms_buf if v > 1e-8]
                            self.source_lufs = (
                                ms_to_lufs(float(np.mean(active)))
                                if active else -100.0
                            )
            except Exception:
                time.sleep(1.0)      # retry on device error

    # ── control thread ──

    def _control_thread(self):
        """Feed-forward volume adjustment via pycaw."""
        try:
            comtypes.CoInitialize()
        except OSError:
            pass

        speakers = AudioUtilities.GetSpeakers()
        vol_ep = speakers.EndpointVolume

        try:
            rng = vol_ep.GetVolumeRange()
            self.vol_range = (rng[0], rng[1], rng[2])
        except Exception:
            pass
        min_db, max_db, _ = self.vol_range

        try:
            self.current_vol_db = vol_ep.GetMasterVolumeLevel()
        except Exception:
            self.current_vol_db = 0.0
        self.desired_vol_db = self.current_vol_db
        self._last_set_db = self.current_vol_db
        self._last_set_time = time.monotonic()

        dt = self.block_ms / 1000.0
        last_time = time.monotonic()

        while self.running:
            time.sleep(dt * 0.5)
            now = time.monotonic()
            actual_dt = now - last_time
            last_time = now

            # ── read actual volume ──
            try:
                actual_db = vol_ep.GetMasterVolumeLevel()
            except Exception:
                continue

            # ── detect manual change ──
            if (self._last_set_db is not None
                    and now - self._last_set_time > 0.3
                    and abs(actual_db - self._last_set_db) > 1.5):
                self.manual_override_until = now + self.manual_pause_s
                self.current_vol_db = actual_db
                self._last_set_db = actual_db
                self._last_set_time = now
                continue

            with self._lock:
                L       = self.source_lufs
                target  = self.target_lufs
                enabled = self.enabled
                silent  = self.is_silent

            if not enabled or silent:
                self.current_vol_db = actual_db
                self._last_set_db = actual_db
                self._last_set_time = now
                self._hold_counter = self.hold_time
                continue

            # ── feed-forward ──
            raw_desired = target - L
            raw_desired = max(min_db, min(max_db, raw_desired))

            # Hold before releasing (don't raise vol on transient dips)
            if raw_desired > self.desired_vol_db + 0.5:
                if self._hold_counter > 0:
                    self._hold_counter -= actual_dt
                else:
                    self.desired_vol_db = raw_desired
                    self._hold_counter = self.hold_time
            else:
                self.desired_vol_db = raw_desired
                self._hold_counter = self.hold_time

            # ── manual override: allow attenuation, block increase ──
            in_manual = now < self.manual_override_until
            if in_manual and self.desired_vol_db > self.current_vol_db + 0.5:
                continue

            # ── slew-rate limited transition ──
            delta = self.desired_vol_db - self.current_vol_db
            max_step = self.slew_rate * actual_dt
            if abs(delta) > max_step:
                delta = max_step if delta > 0 else -max_step

            new_db = max(min_db, min(max_db, self.current_vol_db + delta))

            if abs(new_db - self.current_vol_db) > 0.1:
                try:
                    vol_ep.SetMasterVolumeLevel(float(new_db), None)
                    self.current_vol_db = new_db
                    self._last_set_db = new_db
                    self._last_set_time = now
                except Exception:
                    pass

        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


# ─── System Tray ─────────────────────────────────────────────────────────────

def run_tray(ctrl: AudioLevelController):
    import pystray
    from PIL import Image, ImageDraw

    def circle(color):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([4, 4, 59, 59], fill=color,
                                    outline=(40, 40, 40))
        return img

    icons = {
        "green":  circle((50, 205, 50)),
        "yellow": circle((255, 200, 0)),
        "gray":   circle((128, 128, 128)),
    }

    def pick_icon():
        if not ctrl.enabled or ctrl.is_silent:
            return icons["gray"]
        if abs(ctrl.desired_vol_db - ctrl.current_vol_db) > 1.0:
            return icons["yellow"]
        return icons["green"]

    def tooltip():
        L, V, T = ctrl.source_lufs, ctrl.current_vol_db, ctrl.target_lufs
        if not ctrl.enabled:
            return f"Audio Ctrl | Disabled | T:{T:+.0f}"
        if ctrl.is_silent:
            return f"Audio Ctrl | Silent | T:{T:+.0f}"
        manual = " | MANUAL" if time.monotonic() < ctrl.manual_override_until else ""
        return f"Audio Ctrl | Src:{L:+.0f} Vol:{V:+.0f}dB T:{T:+.0f}{manual}"

    # ── settings window (tkinter) ──

    _settings_win = None     # track a single settings window

    def open_settings(icon, item):
        threading.Thread(target=_show_settings_window, daemon=True,
                         name="settings-ui").start()

    def _show_settings_window():
        nonlocal _settings_win
        # If window already open, just bring it to front
        if _settings_win is not None:
            try:
                _settings_win.lift()
                _settings_win.focus_force()
                return
            except Exception:
                _settings_win = None

        import tkinter as tk

        win = tk.Tk()
        win.title("Audio Level Controller")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", lambda: _close_settings(win))
        _settings_win = win

        # ── Target slider + entry ──
        tk.Label(win, text="Target (LUFS):", anchor="w").grid(
            row=0, column=0, padx=(12, 4), pady=(12, 0), sticky="w")

        slider_var = tk.DoubleVar(value=ctrl.target_lufs)
        entry_var  = tk.StringVar(value=f"{ctrl.target_lufs:.1f}")

        def on_slider(val):
            v = float(val)
            ctrl.set_target(v)
            entry_var.set(f"{v:.1f}")

        def on_entry(*_):
            try:
                v = float(entry_var.get())
                v = max(-60.0, min(0.0, v))
                ctrl.set_target(v)
                slider_var.set(v)
            except ValueError:
                pass

        slider = tk.Scale(win, from_=-60, to=0, resolution=0.5,
                          orient="horizontal", length=260,
                          variable=slider_var, command=on_slider,
                          showvalue=False)
        slider.grid(row=0, column=1, padx=4, pady=(12, 0))

        entry = tk.Entry(win, textvariable=entry_var, width=7, justify="center")
        entry.grid(row=0, column=2, padx=(4, 12), pady=(12, 0))
        entry.bind("<Return>", on_entry)
        entry.bind("<FocusOut>", on_entry)

        # ── Window duration slider + entry ──
        tk.Label(win, text="Window (s):", anchor="w").grid(
            row=1, column=0, padx=(12, 4), pady=(8, 0), sticky="w")

        win_slider_var = tk.DoubleVar(value=ctrl.window_s)
        win_entry_var  = tk.StringVar(value=f"{ctrl.window_s:.0f}")

        def on_win_slider(val):
            v = float(val)
            ctrl.set_window(v)
            win_entry_var.set(f"{v:.0f}")

        def on_win_entry(*_):
            try:
                v = float(win_entry_var.get())
                v = max(5.0, min(120.0, v))
                ctrl.set_window(v)
                win_slider_var.set(v)
            except ValueError:
                pass

        win_slider = tk.Scale(win, from_=5, to=120, resolution=1,
                              orient="horizontal", length=260,
                              variable=win_slider_var, command=on_win_slider,
                              showvalue=False)
        win_slider.grid(row=1, column=1, padx=4, pady=(8, 0))

        win_entry = tk.Entry(win, textvariable=win_entry_var, width=7,
                             justify="center")
        win_entry.grid(row=1, column=2, padx=(4, 12), pady=(8, 0))
        win_entry.bind("<Return>", on_win_entry)
        win_entry.bind("<FocusOut>", on_win_entry)

        # ── Enable / disable ──
        enabled_var = tk.BooleanVar(value=ctrl.enabled)

        def on_enabled_toggle():
            ctrl.set_enabled(enabled_var.get())

        tk.Checkbutton(win, text="Enabled", variable=enabled_var,
                       command=on_enabled_toggle).grid(
            row=2, column=0, columnspan=2, padx=12, pady=(8, 4), sticky="w")

        # ── Status label (live) ──
        status_var = tk.StringVar(value="")
        status_lbl = tk.Label(win, textvariable=status_var, fg="#555555",
                              font=("Consolas", 9))
        status_lbl.grid(row=3, column=0, columnspan=3, padx=12, pady=(0, 8),
                        sticky="w")

        def refresh_status():
            if _settings_win is None:
                return
            L = ctrl.source_lufs
            V = ctrl.current_vol_db
            T = ctrl.target_lufs
            if ctrl.is_silent:
                status_var.set("Silent")
            else:
                status_var.set(f"Src: {L:+.0f}  Vol: {V:+.0f} dB  Target: {T:+.0f}")
            # Also sync sliders if changed externally
            if abs(slider_var.get() - T) > 0.1:
                slider_var.set(T)
                entry_var.set(f"{T:.1f}")
            W = ctrl.window_s
            if abs(win_slider_var.get() - W) > 0.5:
                win_slider_var.set(W)
                win_entry_var.set(f"{W:.0f}")
            win.after(500, refresh_status)

        refresh_status()

        # Center on screen
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
        win.geometry(f"+{x}+{y}")

        win.mainloop()

    def _close_settings(win):
        nonlocal _settings_win
        _settings_win = None
        win.destroy()

    # ── menu callbacks ──

    def on_toggle(icon, item):
        ctrl.toggle_enabled()

    def on_quit(icon, item):
        ctrl.stop()
        if _settings_win is not None:
            try: _settings_win.destroy()
            except Exception: pass
        icon.stop()

    def status_text(item):
        L, V, T = ctrl.source_lufs, ctrl.current_vol_db, ctrl.target_lufs
        if not ctrl.enabled:
            return f"DISABLED | Target: {T:+.0f} LUFS"
        if ctrl.is_silent:
            return f"Silent | Target: {T:+.0f} LUFS"
        err = T - L
        if err > 0.5 and ctrl.desired_vol_db >= ctrl.vol_range[1] - 0.5:
            return f"Src: {L:+.0f} | Vol: {V:+.0f} dB | AT MAX"
        return f"Src: {L:+.0f} | Vol: {V:+.0f} dB | Target: {T:+.0f}"

    def is_enabled(item):
        return ctrl.enabled

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Enabled", on_toggle, checked=is_enabled),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings...", open_settings, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("audio_level_ctrl", icons["green"],
                        "Audio Level Controller", menu)

    def updater():
        while ctrl.running:
            time.sleep(1.0)
            try:
                icon.icon = pick_icon()
                icon.title = tooltip()
            except Exception:
                pass

    threading.Thread(target=updater, daemon=True, name="tray-update").start()
    icon.run()                          # blocks on the main thread


# ─── Console Mode ────────────────────────────────────────────────────────────

def run_console(ctrl: AudioLevelController):
    time.sleep(0.8)                     # let capture thread detect device
    print(f"\n  Audio Level Controller  (10 s window, feed-forward)")
    print(f"  ---------------------------------------------------")
    print(f"  Device:    {ctrl.speaker_name or '(detecting...)'}")
    print(f"  Target:    {ctrl.target_lufs:+.1f} LUFS")
    print(f"  Window:    {ctrl.window_s:.0f} s")
    print(f"  Slew:      {ctrl.slew_rate:.0f} dB/s")
    print(f"  Press Ctrl+C to stop\n")

    try:
        while ctrl.running:
            L = ctrl.source_lufs
            V = ctrl.current_vol_db
            T = ctrl.target_lufs
            H = L + V
            en = "ON " if ctrl.enabled else "OFF"
            s  = "SIL" if ctrl.is_silent else "   "
            mo = "HOLD" if time.monotonic() < ctrl.manual_override_until else "    "
            print(
                f"\r  [{en}] Src:{L:>+6.1f}  Vol:{V:>+6.1f}dB  "
                f"Heard:{H:>+6.1f}  T:{T:>+5.0f}  {s} {mo}   ",
                end="", flush=True,
            )
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    ctrl.stop()
    print("\n  Stopped.")


# ─── Auto-Start ─────────────────────────────────────────────────────────────

STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / (
    r"Microsoft\Windows\Start Menu\Programs\Startup"
)
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "AudioLevelController"
VBS_NAME = "AudioLevelController.vbs"


def install_startup():
    """Create a self-contained install under %LOCALAPPDATA% with its own venv.

    Copies the script, creates a dedicated venv, installs dependencies, and
    writes a startup VBS.  No USB drive required after installation.
    """
    import shutil
    import subprocess as sp

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve()
    dst = INSTALL_DIR / src.name
    shutil.copy2(src, dst)
    print(f"  [1/4] Copied script to {dst}")

    # Find system Python (not the venv python)
    system_python = Path(r"C:\Python313\python.exe")
    if not system_python.exists():
        # Try to find python on PATH
        result = sp.run(["where", "python"], capture_output=True, text=True)
        for line in result.stdout.strip().splitlines():
            p = Path(line.strip())
            if p.exists() and "venv" not in str(p).lower():
                system_python = p
                break
    if not system_python.exists():
        print(f"  ERROR: Cannot find system Python at {system_python}")
        print(f"  Install Python system-wide or adjust the path.")
        return

    # Create local venv
    venv_dir = INSTALL_DIR / ".venv"
    if not (venv_dir / "Scripts" / "python.exe").exists():
        print(f"  [2/4] Creating venv at {venv_dir} ...")
        sp.run([str(system_python), "-m", "venv", str(venv_dir)], check=True)
    else:
        print(f"  [2/4] Venv already exists at {venv_dir}")

    # Install dependencies
    pip = venv_dir / "Scripts" / "pip.exe"
    deps = ["numpy", "soundcard", "pycaw", "comtypes", "pystray", "Pillow"]
    print(f"  [3/4] Installing dependencies: {', '.join(deps)} ...")
    sp.run(
        [str(pip), "install", "--quiet", "--upgrade"] + deps,
        check=True,
    )

    # Patch soundcard's numpy.fromstring bug if needed (numpy 2.x removed it)
    mf = venv_dir / "Lib" / "site-packages" / "soundcard" / "mediafoundation.py"
    if mf.exists():
        txt = mf.read_text()
        if "numpy.fromstring" in txt:
            # Only patch the ONE line that uses fromstring on audio data
            old = "numpy.fromstring(_ffi.buffer(data_ptr, nframes*4*len(set(self.channelmap))), dtype='float32')"
            new = "numpy.frombuffer(bytes(_ffi.buffer(data_ptr, nframes*4*len(set(self.channelmap)))), dtype='float32').copy()"
            if old in txt:
                txt = txt.replace(old, new)
                mf.write_text(txt)
                print("    Patched soundcard/mediafoundation.py (numpy compat)")

    # Write VBS startup launcher
    pythonw = venv_dir / "Scripts" / "pythonw.exe"
    vbs = (
        'Set s = CreateObject("WScript.Shell")\n'
        f's.Run """{pythonw}"" ""{dst}""", 0, False\n'
    )
    vbs_path = STARTUP_DIR / VBS_NAME
    vbs_path.write_text(vbs)
    print(f"  [4/4] Created startup launcher")

    print()
    print(f"  Installation complete!")
    print(f"    Script:  {dst}")
    print(f"    Venv:    {venv_dir}")
    print(f"    Startup: {vbs_path}")
    print(f"    Python:  {pythonw}")
    print()
    print(f"  The controller will auto-start on login.")
    print(f"  No USB drive needed.  Re-run --install to update.")


def uninstall_startup():
    import shutil
    vbs_path = STARTUP_DIR / VBS_NAME
    if vbs_path.exists():
        vbs_path.unlink()
        print(f"  Removed: {vbs_path}")
    else:
        print(f"  Not found: {vbs_path}")
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        print(f"  Removed: {INSTALL_DIR}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Real-time audio level controller"
    )
    parser.add_argument("--target", type=float,
                        help="Set target LUFS (saved to config)")
    parser.add_argument("--console", action="store_true",
                        help="Console mode instead of system tray")
    parser.add_argument("--install", action="store_true",
                        help="Install to Windows startup")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove from Windows startup")
    parser.add_argument("--list", action="store_true",
                        help="List audio devices")
    args = parser.parse_args()

    if args.install:
        install_startup()
        return

    if args.uninstall:
        uninstall_startup()
        return

    if args.list:
        for i, s in enumerate(sc.all_speakers()):
            m = " <-- default" if s == sc.default_speaker() else ""
            print(f"  [{i:>2}] {s.name}{m}")
        return

    # ── single-instance guard ──
    if not acquire_single_instance():
        print("  Another instance is already running.")
        sys.exit(0)

    import atexit
    atexit.register(release_single_instance)

    cfg = load_config()
    if args.target is not None:
        cfg["target_lufs"] = args.target
        save_config(cfg)

    ctrl = AudioLevelController(cfg)
    ctrl.start()

    if args.console:
        run_console(ctrl)
    else:
        try:
            run_tray(ctrl)
        except ImportError as e:
            print(f"  pystray/Pillow not available ({e}), falling back to console")
            run_console(ctrl)


if __name__ == "__main__":
    main()
