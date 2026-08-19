"""Thin wrappers around ffmpeg/ffprobe subprocess calls."""
import json
import re
import subprocess
from pathlib import Path


class FFError(RuntimeError):
    pass


class CancelledError(RuntimeError):
    """Raised when a caller-supplied should_cancel() check trips mid-run."""


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


def has_quicktime_chapter_track(path: Path) -> bool:
    """Does this M4B carry a QuickTime-style chapter track, not just the
    legacy Nero-style 'chpl' atom?

    MP4/M4B has two independent, coexisting ways to store chapters:
    - a Nero-style 'chpl' atom under moov/udta (a flat list of offset+title
      pairs) - what ffprobe's -show_chapters reads regardless of which
      format(s) are present, and what every other player (VLC, Kodi,
      mp4chaps) reads fine.
    - a QuickTime-style chapter *track*: a text-sample trak referenced from
      the audio trak via <tref><chap>. Apple's own apps (Books, Music,
      Podcasts, QuickTime) need this specifically to show real chapter
      *titles* - lacking it, they still see the right chapter *count* and
      *boundaries* (from other duration/index metadata) but fall back to
      generic "1", "2", "3" numbering for the titles.

    ffutil.inject_chapters_ffmetadata() (below) already writes both when it
    runs - this only matters for a source that already had chapters written
    by something else (e.g. an older ffmpeg build, or other audiobook
    tooling) before ever reaching this pipeline.

    ffprobe can tell the two cases apart even though -show_chapters can't:
    a QuickTime chapter track shows up as its own extra stream, distinct
    from the audio stream, carrying the MP4 'text' sample-description tag.
    """
    info = probe(path)
    return any(
        stream.get("codec_type") != "audio" and stream.get("codec_tag_string") == "text"
        for stream in info.get("streams", [])
    )


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


_OUT_TIME_RE = re.compile(r"out_time=(\d+):(\d+):([\d.]+)")


def _run_with_progress(cmd: list[str], total_duration_sec, on_progress, should_cancel):
    """Run an ffmpeg command (already carrying -progress pipe:1), streaming
    progress as it goes rather than blocking until the whole thing finishes.

    on_progress(pct) is called (at most) whenever ffmpeg reports a new
    out_time; should_cancel() is polled on every progress line, and a True
    result terminates the subprocess and raises CancelledError.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            if should_cancel is not None and should_cancel():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise CancelledError("Conversion cancelled")
            if on_progress is not None and total_duration_sec:
                match = _OUT_TIME_RE.search(line)
                if match:
                    hours, minutes, seconds = match.groups()
                    out_time_sec = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                    pct = max(0, min(99, round(out_time_sec / total_duration_sec * 100)))
                    on_progress(pct)
    finally:
        stderr_output = proc.stderr.read() if proc.stderr else ""
        proc.wait()

    if proc.returncode != 0:
        raise FFError(f"ffmpeg failed: {stderr_output.strip()}")


def transcode_to_aac_m4b(
    input_paths: list[Path],
    output_path: Path,
    bitrate_kbps: int,
    total_duration_sec: float | None = None,
    on_progress=None,
    should_cancel=None,
):
    """Concatenate (if multiple) and transcode MP3 source(s) to AAC in an M4B container.

    Source MP3s often carry a per-track embedded cover image (an ID3 APIC
    frame, which ffprobe reports as a "video" stream). -map 0:a restricts
    output to audio only, so those per-track images - inconsistent or
    absent across a multi-file source - can't leak into the muxed output
    or confuse the concat demuxer's stream matching. Cover art is added
    properly afterward, from the confirmed metadata match, in tag.py.

    total_duration_sec/on_progress/should_cancel are all optional together:
    pass them to get live progress reporting and cooperative cancellation
    (used by the job dispatcher); omit them for a plain, silent run.
    """
    if len(input_paths) == 1:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_paths[0]),
            "-map", "0:a",
            "-c:a", "aac", "-b:a", f"{bitrate_kbps}k",
            "-progress", "pipe:1", "-nostats",
            "-f", "mp4",
            str(output_path),
        ]
        _run_with_progress(cmd, total_duration_sec, on_progress, should_cancel)
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
            "-map", "0:a",
            "-c:a", "aac", "-b:a", f"{bitrate_kbps}k",
            "-progress", "pipe:1", "-nostats",
            "-f", "mp4",
            str(output_path),
        ]
        _run_with_progress(cmd, total_duration_sec, on_progress, should_cancel)
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
