"""
Audio Level Analysis & Normalization (EBU R128 / LUFS)
=======================================================
Analyses volume levels of all MP3 files in the current directory using
ffmpeg's loudnorm filter (EBU R128 standard), then normalizes them to a
consistent target loudness so there are no jarring jumps between songs.

Two-pass normalization:
  1. Measure each song's integrated loudness (LUFS), true peak, and LRA
  2. Apply loudnorm filter with measured values for accurate normalization

Normalized copies are saved to a 'normalized/' subdirectory.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ─── Locate the bundled ffmpeg binary ────────────────────────────────────────

import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# ─── Configuration ───────────────────────────────────────────────────────────

AUDIO_DIR = Path(r"F:\\")
OUTPUT_DIR = AUDIO_DIR / "normalized"
# Integrated loudness target (Spotify uses -14, YouTube -14)
TARGET_LUFS = -14.0
TARGET_TP = -1.0    # True peak ceiling in dBTP
TARGET_LRA = 11.0    # Loudness range target
EXTENSIONS = {".mp3"}

# ─── Helpers ─────────────────────────────────────────────────────────────────


def discover_songs(directory: Path) -> list[Path]:
    """Find all audio files in the directory (non-recursive)."""
    songs = [
        f for f in directory.iterdir()
        if f.suffix.lower() in EXTENSIONS
        and f.is_file()
        and OUTPUT_DIR not in f.parents
        and f.parent != OUTPUT_DIR
    ]
    songs.sort(key=lambda p: p.name.lower())
    return songs


def get_duration(path: Path) -> float:
    """Get duration in seconds using ffmpeg."""
    cmd = [
        FFMPEG, "-i", str(path),
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", result.stderr)
    if match:
        h, m, s, cs = match.groups()
        return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0
    return 0.0


def analyse_loudness(path: Path) -> dict | None:
    """
    First pass: measure song loudness using ffmpeg's loudnorm filter.
    Returns integrated loudness (LUFS), true peak (dBTP), LRA, and threshold.
    """
    cmd = [
        FFMPEG, "-hide_banner", "-i", str(path),
        "-af", f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    stderr = result.stderr

    # Extract the JSON block that loudnorm prints
    json_match = re.search(r'\{[^{}]*"input_i"[^{}]*\}', stderr, re.DOTALL)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    duration = get_duration(path)

    return {
        "path":           path,
        "name":           path.stem,
        "duration_s":     duration,
        "input_i":        float(data.get("input_i", 0)),
        "input_tp":       float(data.get("input_tp", 0)),
        "input_lra":      float(data.get("input_lra", 0)),
        "input_thresh":   float(data.get("input_thresh", 0)),
        "target_offset":  float(data.get("target_offset", 0)),
        "_loudnorm_data": data,
    }


def format_duration(seconds: float) -> str:
    """Format seconds as M:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def print_analysis(results: list[dict], target: float) -> None:
    """Pretty-print a volume analysis table sorted by loudness."""
    bar_width = 35

    all_lufs = [r["input_i"] for r in results]
    min_lufs = min(all_lufs)
    max_lufs = max(all_lufs)
    spread = max_lufs - min_lufs if max_lufs != min_lufs else 1.0

    print()
    print("=" * 110)
    print("  AUDIO VOLUME ANALYSIS  (EBU R128 / LUFS)")
    print("=" * 110)
    print(
        f"\n  {'Song':<50} {'LUFS':>7}  {'Peak':>7}  {'LRA':>5}  {'Dur':>6}  Level")
    print("  " + "─" * 106)

    for r in results:
        bar_len = int((r["input_i"] - min_lufs) / spread *
                      bar_width) if spread else bar_width // 2
        bar_len = max(1, bar_len)
        bar = "█" * bar_len

        diff = r["input_i"] - target
        if abs(diff) < 1.0:
            marker = " ≈"
        elif diff > 0:
            marker = " ▲"
        else:
            marker = " ▼"

        name = r["name"][:48]
        dur = format_duration(r["duration_s"])
        print(
            f"  {name:<50} {r['input_i']:>+7.1f}  {r['input_tp']:>+7.1f}  {r['input_lra']:>5.1f}  {dur:>6}  {bar}{marker}")

    avg_lufs = sum(all_lufs) / len(all_lufs)
    print()
    print("  " + "─" * 106)
    print(f"  {'STATISTICS':<50}")
    print(f"    Loudest song:    {max_lufs:+.1f} LUFS")
    print(f"    Quietest song:   {min_lufs:+.1f} LUFS")
    print(f"    Average:         {avg_lufs:+.1f} LUFS")
    print(
        f"    Spread:          {spread:.1f} LU   <- this is the volume jump range you currently experience")
    print(f"    Target:          {target:+.1f} LUFS")
    print(f"    Songs analysed:  {len(results)}")
    print()
    print("  Legend:  ^ = louder than target (will be turned down)")
    print("           v = quieter than target (will be turned up)")
    print("           ~ = already close to target (within +/-1 LU)")
    print()


def normalize_song(info: dict, target_lufs: float, target_tp: float,
                   target_lra: float, output_dir: Path) -> bool:
    """
    Second pass: normalize a song using the measured loudnorm values.
    Two-pass loudnorm produces better quality than single-pass.
    """
    d = info["_loudnorm_data"]
    out_path = output_dir / info["path"].name

    af_filter = (
        f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}"
        f":measured_I={d['input_i']}"
        f":measured_TP={d['input_tp']}"
        f":measured_LRA={d['input_lra']}"
        f":measured_thresh={d['input_thresh']}"
        f":offset={d['target_offset']}"
        f":linear=true"
        f":print_format=summary"
    )

    cmd = [
        FFMPEG, "-hide_banner", "-y",
        "-i", str(info["path"]),
        "-af", af_filter,
        "-ar", "44100",
        "-b:a", "320k",
        str(out_path),
    ]

    result = subprocess.run(cmd, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.returncode == 0


def normalize_all(results: list[dict], target_lufs: float, target_tp: float,
                  target_lra: float, output_dir: Path) -> None:
    """Normalize all songs with a progress display."""
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 110)
    print("  NORMALIZATION  (two-pass EBU R128)")
    print("=" * 110)
    print(
        f"\n  Target:  {target_lufs:+.1f} LUFS  |  True peak ceiling: {target_tp:+.1f} dBTP  |  LRA: {target_lra:.1f} LU")
    print(f"  Output:  {output_dir}\n")

    ok = 0
    for i, r in enumerate(results, 1):
        gain = target_lufs - r["input_i"]
        direction = "+" if gain >= 0 else ""
        name = r["name"][:55]

        sys.stdout.write(
            f"  [{i:>2}/{len(results)}] {name:<57} {r['input_i']:>+6.1f} -> {target_lufs:>+6.1f}  ({direction}{gain:.1f} LU) ")
        sys.stdout.flush()

        success = normalize_song(
            r, target_lufs, target_tp, target_lra, output_dir)
        if success:
            print(" ok")
            ok += 1
        else:
            print(" FAILED")

    print(
        f"\n  Done! {ok}/{len(results)} songs normalized to {target_lufs:+.1f} LUFS")
    print(f"  Output: {output_dir}")
    print("=" * 110 + "\n")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print(f"\n  Scanning: {AUDIO_DIR}")
    songs = discover_songs(AUDIO_DIR)

    if not songs:
        print("  No audio files found!")
        sys.exit(1)

    print(f"  Found {len(songs)} songs.\n")

    # ── Phase 1: Analyse ──
    print("  -- Pass 1: Measuring loudness (EBU R128) --\n")
    results = []
    for i, song in enumerate(songs, 1):
        print(f"  [{i:>2}/{len(songs)}] {song.name}")
        info = analyse_loudness(song)
        if info:
            results.append(info)
        else:
            print(f"         ! Could not analyse")

    if not results:
        print("\n  No songs could be analysed!")
        sys.exit(1)

    results.sort(key=lambda r: r["input_i"])

    print_analysis(results, TARGET_LUFS)

    # ── Phase 2: Normalize ──
    print("  -- Pass 2: Normalizing --\n")
    normalize_all(results, TARGET_LUFS, TARGET_TP, TARGET_LRA, OUTPUT_DIR)


if __name__ == "__main__":
    main()
