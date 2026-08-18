"""Thin wrappers around ffmpeg/ffprobe subprocess calls."""
import json
import subprocess
from pathlib import Path


class FFError(RuntimeError):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path: Path) -> dict:
    """Return ffprobe's full JSON description of a media file."""
    result = _run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise FFError(f"ffprobe failed on {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def get_duration_sec(path: Path) -> float:
    info = probe(path)
    return float(info["format"]["duration"])


def get_audio_bitrate_kbps(path: Path) -> int:
    info = probe(path)
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "audio" and stream.get("bit_rate"):
            return round(int(stream["bit_rate"]) / 1000)
    if info.get("format", {}).get("bit_rate"):
        return round(int(info["format"]["bit_rate"]) / 1000)
    raise FFError(f"Could not determine audio bitrate for {path}")


def get_embedded_chapters(path: Path) -> list[dict]:
    """Return [{start_sec, end_sec, title}, ...] for chapters already in the file."""
    info = probe(path)
    chapters = []
    for ch in info.get("chapters", []):
        chapters.append(
            {
                "start_sec": float(ch["start_time"]),
                "end_sec": float(ch["end_time"]),
                "title": ch.get("tags", {}).get("title", ""),
            }
        )
    return chapters


def run_silencedetect(path: Path, threshold_db: str, min_duration_sec: float) -> str:
    """Run ffmpeg's silencedetect filter and return its stderr (where the results are logged)."""
    result = _run(
        [
            "ffmpeg",
            "-i", str(path),
            "-af", f"silencedetect=noise={threshold_db}:d={min_duration_sec}",
            "-f", "null",
            "-",
        ]
    )
    return result.stderr


def concat_audio(input_paths: list[Path], output_path: Path):
    """Concatenate audio files (same codec/format) via ffmpeg's concat demuxer."""
    list_file = output_path.with_suffix(".concat.txt")
    with open(list_file, "w") as f:
        for p in input_paths:
            escaped = str(p).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    try:
        result = _run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(output_path),
            ]
        )
        if result.returncode != 0:
            raise FFError(f"ffmpeg concat failed: {result.stderr.strip()}")
    finally:
        list_file.unlink(missing_ok=True)


def transcode_to_aac_m4b(input_paths: list[Path], output_path: Path, bitrate_kbps: int):
    """Concatenate (if multiple) and transcode MP3 source(s) to AAC in an M4B container."""
    if len(input_paths) == 1:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_paths[0]),
            "-c:a", "aac", "-b:a", f"{bitrate_kbps}k",
            "-f", "mp4",
            str(output_path),
        ]
        result = _run(cmd)
        if result.returncode != 0:
            raise FFError(f"ffmpeg transcode failed: {result.stderr.strip()}")
        return

    list_file = output_path.with_suffix(".concat.txt")
    with open(list_file, "w") as f:
        for p in input_paths:
            escaped = str(p).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:a", "aac", "-b:a", f"{bitrate_kbps}k",
            "-f", "mp4",
            str(output_path),
        ]
        result = _run(cmd)
        if result.returncode != 0:
            raise FFError(f"ffmpeg concat+transcode failed: {result.stderr.strip()}")
    finally:
        list_file.unlink(missing_ok=True)


def inject_chapters_ffmetadata(m4b_path: Path, chapters: list[dict]):
    """Write chapters into an M4B in-place via ffmpeg's ffmetadata format."""
    metadata_path = m4b_path.with_suffix(".ffmetadata.txt")
    lines = [";FFMETADATA1"]
    for ch in chapters:
        start_ms = round(ch["start_sec"] * 1000)
        end_ms = round(ch["end_sec"] * 1000)
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={ch.get('title', '')}")
    metadata_path.write_text("\n".join(lines), encoding="utf-8")

    tmp_output = m4b_path.with_suffix(".chapters_tmp.m4b")
    try:
        result = _run(
            [
                "ffmpeg", "-y",
                "-i", str(m4b_path),
                "-i", str(metadata_path),
                "-map_metadata", "1",
                "-codec", "copy",
                str(tmp_output),
            ]
        )
        if result.returncode != 0:
            raise FFError(f"ffmpeg chapter injection failed: {result.stderr.strip()}")
        tmp_output.replace(m4b_path)
    finally:
        metadata_path.unlink(missing_ok=True)
        tmp_output.unlink(missing_ok=True)
