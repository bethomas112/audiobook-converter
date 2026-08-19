"""Synthetic audio generation for tests. Everything here shells out to
ffmpeg's own signal-generator inputs (`sine`, `anullsrc`) so no binary
fixture files need to be checked into the repo - a test asking for "3
seconds of 44.1kHz tone at 96kbps" gets exactly that, deterministically,
on any machine with ffmpeg on PATH.
"""
import subprocess
from pathlib import Path


def _run(cmd: list[str]):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


def make_tone_mp3(path: Path, duration_sec: float, bitrate_kbps: int = 96, freq: int = 440):
    """A single sine-wave tone, encoded as MP3 at the given bitrate."""
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration_sec}",
            "-b:a", f"{bitrate_kbps}k",
            str(path),
        ]
    )
    return path


def make_silence_mp3(path: Path, duration_sec: float, bitrate_kbps: int = 96):
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={duration_sec}",
            "-b:a", f"{bitrate_kbps}k",
            str(path),
        ]
    )
    return path


def make_tone_silence_pattern_mp3(
    path: Path,
    segments: list[tuple[str, float]],
    bitrate_kbps: int = 96,
):
    """Concatenates alternating ("tone", dur)/("silence", dur) segments into
    one MP3, so tests can assert silencedetect finds gaps at known offsets.
    Returns (path, [(silence_start_sec, silence_end_sec), ...]) - the exact
    silence windows that ended up in the file, computed from the segment
    list itself rather than re-detected, so tests have a ground truth to
    compare ffmpeg's own detection against.
    """
    tmp_dir = path.parent / f"{path.stem}_parts"
    tmp_dir.mkdir(exist_ok=True)
    part_paths = []
    silence_windows = []
    offset = 0.0
    for i, (kind, dur) in enumerate(segments):
        part = tmp_dir / f"part_{i}.mp3"
        if kind == "tone":
            make_tone_mp3(part, dur, bitrate_kbps=bitrate_kbps)
        elif kind == "silence":
            make_silence_mp3(part, dur, bitrate_kbps=bitrate_kbps)
            silence_windows.append((offset, offset + dur))
        else:
            raise ValueError(f"Unknown segment kind: {kind}")
        part_paths.append(part)
        offset += dur

    list_file = tmp_dir / "concat.txt"
    list_file.write_text("".join(f"file '{p}'\n" for p in part_paths))
    _run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-b:a", f"{bitrate_kbps}k",
            str(path),
        ]
    )
    return path, silence_windows


def make_m4b(
    path: Path,
    duration_sec: float,
    chapters: list[dict] | None = None,
):
    """A bare AAC/M4B file, optionally with embedded chapters (ffmetadata
    format, same shape as app/pipeline/chapters.py's chapter dicts:
    {"start_sec", "end_sec", "title"}).
    """
    if not chapters:
        _run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_sec}",
                "-c:a", "aac", "-b:a", "96k",
                "-f", "mp4",
                str(path),
            ]
        )
        return path

    plain = path.with_suffix(".plain.m4b")
    _run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_sec}",
            "-c:a", "aac", "-b:a", "96k",
            "-f", "mp4",
            str(plain),
        ]
    )

    meta_path = path.with_suffix(".ffmetadata.txt")
    lines = [";FFMETADATA1"]
    for ch in chapters:
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={round(ch['start_sec'] * 1000)}")
        lines.append(f"END={round(ch['end_sec'] * 1000)}")
        lines.append(f"title={ch.get('title', '')}")
    meta_path.write_text("\n".join(lines))

    _run(
        [
            "ffmpeg", "-y",
            "-i", str(plain),
            "-i", str(meta_path),
            "-map_metadata", "1",
            "-codec", "copy",
            str(path),
        ]
    )
    plain.unlink()
    meta_path.unlink()
    return path
