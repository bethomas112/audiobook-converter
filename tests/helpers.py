"""Synthetic audio generation for tests. Everything here shells out to
ffmpeg's own signal-generator inputs (`sine`, `anullsrc`) so no binary
fixture files need to be checked into the repo - a test asking for "3
seconds of 44.1kHz tone at 96kbps" gets exactly that, deterministically,
on any machine with ffmpeg on PATH.
"""
import struct
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


def _read_top_level_boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int, int]]:
    """[(boxtype, box_start, box_end, header_len), ...] for boxes in data[start:end]."""
    boxes = []
    pos = start
    while pos < end:
        size, boxtype = struct.unpack(">I4s", data[pos:pos + 8])
        header_len = 8
        if size == 1:
            (size,) = struct.unpack(">Q", data[pos + 8:pos + 16])
            header_len = 16
        if size == 0:
            size = end - pos
        boxes.append((boxtype, pos, pos + size, header_len))
        pos += size
    return boxes


def strip_quicktime_chapter_track(path: Path):
    """Removes the QuickTime-style chapter track (the audio trak's <tref><chap>
    reference, plus the companion text-handler trak it points at) from an
    M4B in place, leaving only the legacy Nero-style 'chpl' atom under
    moov/udta - readable by ffprobe/most players, but not by Apple's own
    apps (Books, Music, Podcasts, QuickTime), which need the QuickTime
    track to show real chapter titles instead of falling back to generic
    "1", "2", "3" numbering.

    This simulates what a source .m4b produced by some other/older
    ffmpeg-based tool might look like feeding into this pipeline's
    m4b_single passthrough path - a real, historically common gap in some
    audiobook-conversion tooling. Pure struct-based MP4 box surgery (no
    external tools), since ffmpeg itself has a `-movflags disable_chpl`
    switch to omit the *Nero* atom but no equivalent to omit the
    QuickTime track, so this is the only way to construct such a fixture.
    """
    data = path.read_bytes()

    def rewrite_size(data: bytes, box_start: int, new_size: int) -> bytes:
        return data[:box_start] + struct.pack(">I", new_size) + data[box_start + 4:]

    moov = next(b for b in _read_top_level_boxes(data, 0, len(data)) if b[0] == b"moov")
    _, moov_start, moov_end, moov_hlen = moov
    traks = [b for b in _read_top_level_boxes(data, moov_start + moov_hlen, moov_end) if b[0] == b"trak"]

    # Remove the first trak's <tref> (its reference to the chapter track).
    _, trak_start, trak_end, trak_hlen = traks[0]
    tref = next(
        (b for b in _read_top_level_boxes(data, trak_start + trak_hlen, trak_end) if b[0] == b"tref"),
        None,
    )
    if tref is not None:
        _, tref_start, tref_end, _ = tref
        removed = tref_end - tref_start
        data = data[:tref_start] + data[tref_end:]
        data = rewrite_size(data, trak_start, (trak_end - trak_start) - removed)
        data = rewrite_size(data, moov_start, (moov_end - moov_start) - removed)
        moov_end -= removed

    # Remove the second trak entirely (the chapter text track itself).
    moov = next(b for b in _read_top_level_boxes(data, 0, len(data)) if b[0] == b"moov")
    _, moov_start, moov_end, moov_hlen = moov
    traks = [b for b in _read_top_level_boxes(data, moov_start + moov_hlen, moov_end) if b[0] == b"trak"]
    if len(traks) >= 2:
        _, t2_start, t2_end, _ = traks[1]
        removed = t2_end - t2_start
        data = data[:t2_start] + data[t2_end:]
        data = rewrite_size(data, moov_start, (moov_end - moov_start) - removed)

    path.write_bytes(data)
    return path


def has_quicktime_chapter_text_track(path: Path) -> bool:
    """Test-side atom check: does this MP4 have a second (non-audio) trak
    whose mdia/hdlr handler_type is 'text', reachable from the first
    trak's <tref><chap> box? This is the structural marker Apple's apps
    require to show chapter *titles* - ffprobe's -show_chapters can't
    distinguish this from a chpl-only file (both report the same chapter
    list), which is exactly why this checks the raw box tree instead.
    """
    data = path.read_bytes()
    moov = next(b for b in _read_top_level_boxes(data, 0, len(data)) if b[0] == b"moov")
    _, moov_start, moov_end, moov_hlen = moov
    traks = [b for b in _read_top_level_boxes(data, moov_start + moov_hlen, moov_end) if b[0] == b"trak"]
    if len(traks) < 2:
        return False
    _, trak_start, trak_end, trak_hlen = traks[0]
    has_tref_chap = any(
        b[0] == b"tref" for b in _read_top_level_boxes(data, trak_start + trak_hlen, trak_end)
    )
    if not has_tref_chap:
        return False

    def handler_type_of(trak_box):
        _, t_start, t_end, t_hlen = trak_box
        mdia = next(b for b in _read_top_level_boxes(data, t_start + t_hlen, t_end) if b[0] == b"mdia")
        _, m_start, m_end, m_hlen = mdia
        hdlr = next(b for b in _read_top_level_boxes(data, m_start + m_hlen, m_end) if b[0] == b"hdlr")
        _, h_start, h_end, h_hlen = hdlr
        body = data[h_start + h_hlen: h_end]
        return body[8:12]  # version(1) flags(3) pre_defined(4) handler_type(4)

    return handler_type_of(traks[1]) == b"text"
