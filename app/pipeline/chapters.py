"""Chapter resolution, in priority order:

  1. Embedded chapters already present in an M4B input -> left untouched,
     signalled here by returning None (caller must not modify chapters) -
     UNLESS the source is missing the QuickTime-style chapter track Apple's
     own apps (Books, Music, Podcasts, QuickTime) need to show real titles
     instead of falling back to generic "1", "2", "3" numbering (see
     ffutil.has_quicktime_chapter_track's docstring). In that case the same
     chapter data is returned for re-injection instead of None, so the
     caller rewrites it through ffutil.inject_chapters_ffmetadata() - which
     always writes both formats - picking up the missing track without
     altering the chapter times/titles the source already had.
  2. Official audnexus chapter timestamps for the matched title, if the
     input didn't already have its own chapters - REALIGNED against this
     particular rip's actual audio before being used (see
     _align_audnexus_chapters). audnexus's timestamps are anchored to
     Audible's own official release, which commonly has different
     front/back matter (branded intros, "Audible Studios presents...",
     outros) than a local rip; using them verbatim used to land chapter
     navigation slightly after a chapter had actually begun. The drift
     isn't a single constant offset per book, so a global correction can't
     fix it - each chapter is instead individually matched to a real
     silence in the converted audio.
  3. Source-file boundaries, when the input was multiple discrete audio
     files and audnexus had no chapter data.
  4. Silence-detection, as the last-resort fallback for a single,
     undifferentiated audio stream with no better chapter source.
"""
import re
import statistics
from pathlib import Path

from app.config import config
from app.pipeline import ffutil, metadata
from app.pipeline.chapter_aligner import BasicChapter, ChapterAligner, DetectedCue

# Floor for the aligner's cue-matching search window (how far a candidate
# detected silence may sit from a chapter's scale-projected position before
# the aligner won't consider it a match at all). Mirrors achew's own
# REALIGN_PADDING_DEFAULT (its floor for the *extraction* window, which
# this pipeline doesn't need - see _align_audnexus_chapters, we always scan
# the whole file rather than windowing around each reference timestamp).
# Reused here purely to size the matching window: wider when this rip's
# duration diverges further from audnexus's own reported total, with a 15s
# floor for when the two durations are close.
_ALIGNER_WINDOW_FLOOR_SEC = 15.0


def resolve_chapters(
    source_type: str,
    source_audio_files: list[Path],
    output_path: Path,
    has_embedded_chapters: bool,
    asin: str | None,
    log=None,
) -> list[dict] | None:
    log = log or (lambda _line: None)

    if source_type == "m4b_single" and has_embedded_chapters:
        if ffutil.has_quicktime_chapter_track(output_path):
            return None
        # The source's chapters are readable (ffprobe/chpl) but the file is
        # missing the QuickTime-style track Apple's apps need for real
        # titles. Re-inject the same data through our own chapter-writing
        # path (still a -codec copy remux, no audio re-encode) to add it.
        return ffutil.get_embedded_chapters(output_path)

    if asin:
        audnexus_chapters = metadata.get_chapters(asin)
        if audnexus_chapters:
            duration_sec = ffutil.get_duration_sec(output_path)
            aligned_chapters = _align_audnexus_chapters(audnexus_chapters, output_path, duration_sec, log)
            return _clamp_to_duration(aligned_chapters, duration_sec)

    if source_type == "mp3_multi":
        return _chapters_from_source_boundaries(source_audio_files)

    return _chapters_from_silence_detection(output_path)


def _align_audnexus_chapters(
    chapters: list[dict], output_path: Path, duration_sec: float, log
) -> list[dict]:
    """Realigns audnexus's chapter timestamps onto real silences detected in
    this rip's actual (converted) audio, via a ported copy of achew's
    ChapterAligner (see app/pipeline/chapter_aligner.py) - a monotonic
    duration-shape match between the *spacing* of audnexus's chapters and
    the spacing of detected silences, immune to a constant front-matter
    offset and to per-chapter jitter, rather than trusting audnexus's
    absolute timestamps.

    A single whole-file ffmpeg silencedetect pass (ffutil.run_silencedetect,
    the same filter the priority-4 fallback uses for a different purpose)
    supplies the candidate cues. Unlike achew's own interactive use - which
    windows detection to a padding around each expected timestamp, for
    latency reasons - this runs as a background batch job and can afford to
    scan the whole file, so scanned_regions is always the entire duration
    and the aligner's expansion-retry path (for when a windowed scan turns
    out too narrow) never has anything to fire for.

    Every chapter gets a placement (confident match, lower-confidence
    "fill", or - if nothing nearby was ever found - a scale-interpolated
    guess); confidence never gates whether a correction is applied, only
    what gets logged. See ARCHITECTURE.md for the algorithm's tiers.
    """
    ref_chapters = [BasicChapter(timestamp=c["start_sec"], title=c["title"]) for c in chapters]
    ref_duration = chapters[-1]["end_sec"]

    stderr = ffutil.run_silencedetect(output_path, config.SILENCE_THRESHOLD_DB, config.SILENCE_MIN_DURATION_SEC)
    gaps = _parse_silence_gaps(stderr)
    detected_cues = [DetectedCue.from_silences(start, end) for start, end in gaps]

    max_drift = max(_ALIGNER_WINDOW_FLOOR_SEC, abs(duration_sec - ref_duration) * 2.0)
    aligner = ChapterAligner(max_drift=max_drift)
    results, _stats = aligner.align(
        ref_chapters,
        detected_cues,
        ref_duration,
        duration_sec,
        scanned_regions=[(0.0, duration_sec)],
    )

    # Safety net: the aligner is designed to keep matches monotonic (see
    # chapter_aligner.py), but nothing downstream can write sane chapter
    # metadata from a start time that regressed behind the previous
    # chapter's, so this is enforced defensively rather than trusted blind.
    starts = []
    prev = 0.0
    for r in results:
        start = max(0.0, float(r["timestamp"]), prev)
        starts.append(start)
        prev = start

    aligned = []
    for i, original in enumerate(chapters):
        end = starts[i + 1] if i + 1 < len(starts) else original["end_sec"]
        aligned.append({"start_sec": starts[i], "end_sec": end, "title": original["title"]})

    shifts = [abs(aligned[i]["start_sec"] - chapters[i]["start_sec"]) for i in range(len(chapters))]
    confident = sum(1 for r in results if not r["is_guess"])
    guesses = len(results) - confident
    log(
        f"Aligned {len(chapters)} audnexus chapter(s) to detected audio cues: "
        f"{confident} confidently matched, {guesses} flagged as lower-confidence guesses "
        f"(median shift {statistics.median(shifts):.1f}s, max shift {max(shifts):.1f}s)."
    )
    return aligned


def _clamp_to_duration(chapters: list[dict], duration_sec: float) -> list[dict]:
    """Drop any chapter that starts beyond the actual audio, and clamp the
    last remaining one's end so we never write chapter metadata past the
    end of the file.

    Before _align_audnexus_chapters existed, this was the *only* correction
    applied to audnexus's chapters, and did most of the work of papering
    over audnexus's timestamps being for Audible's own official release,
    which can run slightly past a given rip's actual duration (different
    encode, trimmed silence, a different edition). Now that priority-2
    chapters are individually realigned to real audio before reaching here,
    this should rarely have anything to do - the aligner's own placements
    are bounded by real detected cues or by scale-interpolation clamped to
    the book, so a chapter starting past the file's end shouldn't occur.
    It's kept as a defensive backstop rather than removed: a hard clamp
    here costs nothing and guards against a bug or an unanticipated edge
    case in the aligner (or a future change to it) writing chapter
    metadata past the end of the file, which would otherwise corrupt the
    M4B's chapter atom. It is not the primary correction mechanism anymore.
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
