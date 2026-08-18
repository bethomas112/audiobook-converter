"""Chapter resolution, in priority order:

  1. Embedded chapters already present in an M4B input -> left untouched,
     signalled here by returning None (caller must not modify chapters).
  2. Official audnexus chapter timestamps for the matched title, if the
     input didn't already have its own chapters.
  3. Source-file boundaries, when the input was multiple discrete audio
     files and audnexus had no chapter data.
  4. Silence-detection, as the last-resort fallback for a single,
     undifferentiated audio stream with no better chapter source.
"""
import re
from pathlib import Path

from app.config import config
from app.pipeline import ffutil, metadata


def resolve_chapters(
    source_type: str,
    source_audio_files: list[Path],
    output_path: Path,
    has_embedded_chapters: bool,
    asin: str | None,
) -> list[dict] | None:
    if source_type == "m4b_single" and has_embedded_chapters:
        return None

    if asin:
        audnexus_chapters = metadata.get_chapters(asin)
        if audnexus_chapters:
            return _clamp_to_duration(audnexus_chapters, ffutil.get_duration_sec(output_path))

    if source_type == "mp3_multi":
        return _chapters_from_source_boundaries(source_audio_files)

    return _chapters_from_silence_detection(output_path)


def _clamp_to_duration(chapters: list[dict], duration_sec: float) -> list[dict]:
    """audnexus's chapter timestamps are for Audible's own official release
    and can run slightly past a given rip's actual duration (different
    encode, trimmed silence, a different edition). Drop any chapter that
    starts beyond the actual audio, and clamp the last remaining one's end
    so we never write chapter metadata past the end of the file.
    """
    clamped = [c for c in chapters if c["start_sec"] < duration_sec]
    if clamped and clamped[-1]["end_sec"] > duration_sec:
        clamped[-1] = {**clamped[-1], "end_sec": duration_sec}
    return clamped


def _chapters_from_source_boundaries(source_audio_files: list[Path]) -> list[dict]:
    chapters = []
    offset = 0.0
    for f in source_audio_files:
        duration = ffutil.get_duration_sec(f)
        chapters.append(
            {
                "start_sec": offset,
                "end_sec": offset + duration,
                "title": f.stem,
            }
        )
        offset += duration
    return chapters


_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def _parse_silence_gaps(stderr: str) -> list[tuple[float, float]]:
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(stderr)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(stderr)]
    return list(zip(starts, ends))


def _chapters_from_silence_detection(output_path: Path) -> list[dict]:
    stderr = ffutil.run_silencedetect(
        output_path, config.SILENCE_THRESHOLD_DB, config.SILENCE_MIN_DURATION_SEC
    )
    gaps = _parse_silence_gaps(stderr)
    total_duration = ffutil.get_duration_sec(output_path)

    # Break at the end of each silent gap (start of the next chapter).
    candidate_breaks = [end for _, end in gaps]

    breaks = [0.0]
    for candidate in candidate_breaks:
        if candidate - breaks[-1] >= config.SILENCE_MIN_CHAPTER_SEC:
            breaks.append(candidate)

    if total_duration - breaks[-1] < config.SILENCE_MIN_CHAPTER_SEC and len(breaks) > 1:
        breaks.pop()
    breaks.append(total_duration)

    chapters = []
    for i in range(len(breaks) - 1):
        chapters.append(
            {
                "start_sec": breaks[i],
                "end_sec": breaks[i + 1],
                "title": f"Chapter {i + 1}",
            }
        )
    return chapters
