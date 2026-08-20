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

     Some books have no reliable silence anywhere for the aligner to
     anchor on (a real, measured property of some narrations, not a
     threshold-tuning problem), and a substantial minority of a book's
     "chapters" can actually be sub-5-second structural markers (POV-name
     dividers, part breaks) with no acoustic boundary of their own. See
     _align_audnexus_chapters for the full refined pipeline this drives:
     short-chapter folding, an achew-confidence gate, file-boundary
     anchoring as a second-line source of ground truth for multi-file MP3
     rips, and folding (never fabricating) for anything left unresolved.
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

# Step 1 of the refined design: an audnexus chapter with its OWN reported
# length (end_sec - start_sec, itself derived from audnexus's lengthMs)
# under this is treated as a structural marker (a POV-character-name
# divider, a "Part 2" break) rather than a real, independently-placeable
# chapter - it never reaches the aligner at all. 5s comfortably separates
# real short front matter (a "Dedication" chapter, observed at ~8.4s in the
# investigation this design responds to) from genuine ~2s name markers.
_MIN_CHAPTER_SEC = 5.0

# Step 4: cheap prefilter before ever probing per-file durations - only
# attempt file-boundary anchoring when the source's file count is close to
# the *cleaned* (post-short-fold) reference chapter count. Comparing
# against the raw audnexus count (which still includes name-markers) would
# almost never match even a genuine one-file-per-chapter rip.
_FILE_COUNT_TOLERANCE = 2

# Step 4's clustering check: a direction is verified only when this large a
# majority of paired chapters agree on (round to the nearest second) the
# same file-boundary-minus-reference shift. Matches the ~80% figure from
# the investigation that motivated this design. Requiring an outright
# majority this large structurally guarantees "a clear margin over the
# next-most-common bucket" too: whatever's left (<=20%) has to be split
# across the *other* buckets, each of which is then necessarily smaller
# still.
_FILE_BOUNDARY_MAJORITY_THRESHOLD = 0.8


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
            aligned_chapters = _align_audnexus_chapters(
                audnexus_chapters, output_path, duration_sec, source_type, source_audio_files, log
            )
            return _clamp_to_duration(aligned_chapters, duration_sec)

    if source_type == "mp3_multi":
        return _chapters_from_source_boundaries(source_audio_files)

    return _chapters_from_silence_detection(output_path)


def _align_audnexus_chapters(
    chapters: list[dict],
    output_path: Path,
    duration_sec: float,
    source_type: str,
    source_audio_files: list[Path],
    log,
) -> list[dict]:
    """The refined audnexus-chapter pipeline: fold out structural markers,
    realign what's left against real detected audio, then only ever write a
    marker for a chapter with a genuinely *verified* placement - folding
    (never fabricating a smooth guessed drift) for anything else.

    Step 1 - clean the reference list. Any audnexus chapter under
    _MIN_CHAPTER_SEC (by its own reported length) is dropped before ever
    reaching the aligner and its title folded onto a neighbour (see
    _fold_short_chapters). Real books can have a substantial fraction of
    "chapters" that are actually ~2s POV-name markers with no acoustic
    boundary of their own; leaving them in front of achew wastes its
    matching budget on chapters that can never be confidently placed.

    Step 2 - achew's own ChapterAligner (app/pipeline/chapter_aligner.py,
    a ported copy) runs against the cleaned list. A single whole-file ffmpeg
    silencedetect pass (ffutil.run_silencedetect, the same filter the
    priority-4 fallback uses for a different purpose) supplies the
    candidate cues; unlike achew's own interactive use - which windows
    detection to a padding around each expected timestamp, for latency
    reasons - this runs as a background batch job and can afford to scan
    the whole file, so scanned_regions is always the entire duration and
    the aligner's expansion-retry path never has anything to fire for.
    achew's `confidence` field is a fixed, small set of tier constants, not
    a continuous score (verified directly against achew's own `_build`/
    `_result` methods at commit 5e8e249, the exact commit this port is
    from: 1.0 for the forced chapter-0 anchor, 0.85 for a confident
    "skeleton" match, 0.35 for a lower-confidence "fill", 0.25 for a
    fully-interpolated guess - `is_guess` is set to exactly `not confident`
    in every case) - so `is_guess is False` (already computed and returned
    by the ported aligner) *is* the right confidence gate, with no separate
    numeric threshold needed.

    Step 3 - per cleaned chapter, in order: use achew's placement if
    `is_guess` is False; otherwise use a verified file-boundary position
    (Step 4) if this is a multi-file MP3 source and boundary anchoring
    checked out for this book; otherwise fold this chapter's title onto
    the PRECEDING resolved (achew- or file-boundary-placed) chapter - never
    the one that follows, since a chapter's span is always [its own
    position, the next resolved chapter's position), so an unresolved
    chapter's audio already falls inside whichever resolved span precedes
    it, not the one after. The folded title is collapsed to an en-dash
    range rather than chaining every folded title together, so a long run
    doesn't produce an unreadable wall of text: e.g. chapters 22-35 all
    unresolved between resolved chapters 21 and 36 folds to one marker
    titled "Chapter 21 – Chapter 35" at chapter 21's own position, spanning
    [P21, P36); chapter 36 keeps its own untouched title and position. No
    chapter is ever written from a raw, un-realigned audnexus timestamp or
    a scale-interpolated guess - a chapter with no verified placement gets
    no marker of its own (see _classify_placements / _fold_unresolved_placements).

    Step 4 - file-boundary verification (_verify_file_boundaries), computed
    once per book: only attempted for mp3_multi sources whose file count is
    close to the cleaned chapter count, and only trusted once a large
    majority of a candidate file<->chapter pairing agree on close to the
    same shift between a file's real (ffprobe-measured) start boundary and
    its paired chapter's audnexus reference timestamp - checked in both a
    front-anchored and a back-anchored pairing direction, since the extra
    unmatched files/chapters could plausibly sit at either end.
    """
    cleaned, short_folded = _fold_short_chapters(chapters, _MIN_CHAPTER_SEC)

    ref_chapters = [BasicChapter(timestamp=c["start_sec"], title=c["title"]) for c in cleaned]
    ref_duration = cleaned[-1]["end_sec"]

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

    file_boundaries = None
    if source_type == "mp3_multi":
        file_boundaries = _verify_file_boundaries(cleaned, source_audio_files)

    placements = _classify_placements(cleaned, results, file_boundaries)
    resolved = _fold_unresolved_placements(placements)

    # Safety net: the aligner is designed to keep matches monotonic (see
    # chapter_aligner.py), and file-boundary positions are cumulative sums
    # so are monotonic by construction, but nothing downstream can write
    # sane chapter metadata from a start time that regressed behind the
    # previous chapter's, so this is enforced defensively rather than
    # trusted blind.
    starts = []
    prev = 0.0
    for p in resolved:
        start = max(0.0, float(p["position"]), prev)
        starts.append(start)
        prev = start

    aligned = []
    for i, p in enumerate(resolved):
        end = starts[i + 1] if i + 1 < len(starts) else ref_duration
        aligned.append({"start_sec": starts[i], "end_sec": end, "title": p["title"]})

    _log_alignment_summary(log, chapters, cleaned, short_folded, results, file_boundaries, placements, source_type)
    return aligned


def _fold_short_chapters(chapters: list[dict], min_sec: float = _MIN_CHAPTER_SEC) -> tuple[list[dict], int]:
    """Step 1: drop any audnexus chapter whose OWN reported length
    (end_sec - start_sec, itself derived from audnexus's lengthMs) is under
    min_sec, folding its title onto the chapter immediately following it in
    the cleaned list - "{short title} — {next title}" (em dash, matching
    this codebase's existing UI copy convention - see _chapters.html's
    "Chapters — N, from Audible's official listing"). A short chapter with
    nothing after it (it's the last chapter in the book) folds onto the
    PRECEDING kept chapter instead ("{prev title} — {short title}"), so a
    title is never silently dropped.

    A chapter's own reported length is the signal used, not spacing to its
    neighbours: front matter like an 8s "Dedication" is a real chapter and
    must not be caught, while a ~2s "Meg"/"Birdie"-style POV-name marker
    must be, regardless of how far away its neighbouring chapters happen to
    sit.

    Returns (cleaned_chapters, short_chapter_count).
    """
    cleaned: list[dict] = []
    pending_titles: list[str] = []
    for ch in chapters:
        length = ch["end_sec"] - ch["start_sec"]
        if length < min_sec:
            pending_titles.append(ch["title"])
            continue
        title = " — ".join(pending_titles + [ch["title"]]) if pending_titles else ch["title"]
        cleaned.append({**ch, "title": title})
        pending_titles = []

    if pending_titles:
        if cleaned:
            cleaned[-1] = {**cleaned[-1], "title": " — ".join([cleaned[-1]["title"]] + pending_titles)}
        else:
            # Degenerate: every chapter in the book was under min_sec. Not
            # expected on any real book, but rather than silently produce
            # zero chapters, keep the last original chapter's position with
            # every title folded together.
            cleaned.append({**chapters[-1], "title": " — ".join(pending_titles)})

    short_count = len(chapters) - len(cleaned)
    return cleaned, short_count


def _classify_placements(
    cleaned_chapters: list[dict], aligned_results: list[dict], file_boundaries: dict | None
) -> list[dict]:
    """Step 3, resolution order 1-2: classify each cleaned chapter as
    achew-confident (is_guess is False), file-boundary-placed (only when
    file_boundaries was verified for this book and covers this chapter
    index), or unresolved (position=None, falls through to folding).
    """
    fb_positions = file_boundaries["positions"] if file_boundaries else {}
    placements = []
    for i, (ch, r) in enumerate(zip(cleaned_chapters, aligned_results)):
        if not r["is_guess"]:
            placements.append({"title": ch["title"], "position": float(r["timestamp"]), "source": "achew"})
        elif i in fb_positions:
            placements.append({"title": ch["title"], "position": float(fb_positions[i]), "source": "file_boundary"})
        else:
            placements.append({"title": ch["title"], "position": None, "source": None})
    return placements


def _fold_unresolved_placements(placements: list[dict]) -> list[dict]:
    """Step 3, resolution order 3: a chapter with neither an achew-confident
    nor a verified file-boundary placement gets no marker of its own - its
    title folds onto whichever resolved chapter PRECEDES it, never the one
    that follows.

    This direction is not a stylistic choice - it's the only one that keeps
    a marker's title consistent with the audio its span actually covers. A
    chapter's span is always [its own position, the next resolved chapter's
    position). An unresolved chapter's real audio therefore already falls
    inside the *preceding* resolved chapter's span (that span necessarily
    extends forward to wherever the next resolved chapter starts, swallowing
    everything in between) - so growing that preceding marker's title to
    describe it is what keeps title and span in agreement. Folding onto the
    chapter that *follows* instead (the bug this replaced - see git history
    for the incident) would attach the compound title to a marker whose
    position is the following chapter's own real start, which sits well
    past the end of everything the compound title claims to cover.

    The displayed title is collapsed to an en-dash RANGE - "{anchor's own
    title} – {most recently folded title}" - rather than chaining every
    folded title together. A run of many consecutive unresolved chapters is
    common on books with little usable silence to anchor on, and chaining
    every one of their titles onto the anchor produces an unreadably long
    wall of text (the real-world failure this replaced - see git history).
    The range is recomputed fresh from the anchor's own original title each
    time another chapter folds in, not accumulated onto whatever the title
    currently is, so each resolved chapter's own original title has to be
    tracked separately from its current (possibly already-folded) display
    title.

    Worked example: chapter 21 resolved at position P21, chapters 22-35 all
    unresolved, chapter 36 resolved at position P36. Correct output is
    exactly two markers: {title: "Chapter 21 – Chapter 35", position: P21}
    (spanning [P21, P36)) and chapter 36's own untouched {title: "Chapter
    36", position: P36}. Title and position agree for both. This applies
    even for a single fold: chapter 38 with just one unresolved chapter
    "Meg — Chapter 39" folding onto it renders as "Chapter 38 – Meg —
    Chapter 39" - an en-dash range that happens to contain a title with its
    own em-dash short-marker prefix (see _fold_short_chapters) is expected
    and fine; the en dash used for this range-folding is deliberately
    distinct from the em dash _fold_short_chapters uses for its own,
    unrelated chaining, so the two kinds of join stay visually
    distinguishable.

    Chapter 0 is always achew-confident (the aligner anchors it at 0.0
    unconditionally), so in practice there is always a resolved chapter
    already in hand by the time an unresolved one is seen; the
    pending_leading_titles path below exists only as a defensive fallback
    for the degenerate case where that assumption is somehow violated.
    """
    resolved: list[dict] = []
    anchor_titles: list[str] = []  # resolved[i]'s own original, never-folded title
    pending_leading_titles: list[str] = []  # only for unresolved chapters before ANY resolved chapter exists yet
    for p in placements:
        if p["position"] is None:
            if resolved:
                resolved[-1] = {**resolved[-1], "title": f"{anchor_titles[-1]} – {p['title']}"}
            else:
                pending_leading_titles.append(p["title"])
            continue
        title = f"{pending_leading_titles[0]} – {p['title']}" if pending_leading_titles else p["title"]
        resolved.append({"title": title, "position": p["position"], "source": p["source"]})
        anchor_titles.append(p["title"])  # the chapter's own real title, regardless of any leading fold
        pending_leading_titles = []

    if pending_leading_titles:
        # Degenerate: no resolved chapter anywhere in the book. Shouldn't
        # occur - chapter 0 is always achew-confident, per
        # chapter_aligner.py - but don't silently drop titles if it somehow
        # does.
        title = (
            f"{pending_leading_titles[0]} – {pending_leading_titles[-1]}"
            if len(pending_leading_titles) > 1
            else pending_leading_titles[0]
        )
        resolved.append({"title": title, "position": 0.0, "source": None})

    return resolved


def _cumulative_file_starts(source_audio_files: list[Path]) -> list[float]:
    """The real (ffprobe-measured) start offset of each source file, in the
    same concatenation order used elsewhere in this module - file i's start
    is the summed duration of every file before it."""
    starts = []
    offset = 0.0
    for f in source_audio_files:
        starts.append(offset)
        offset += ffutil.get_duration_sec(f)
    return starts


def _cluster_shift_check(pairs: list[tuple[int, float]], cleaned_chapters: list[dict]) -> tuple[float, float] | None:
    """For a candidate chapter<->file-boundary pairing, bucket the shift
    (file boundary time minus the chapter's audnexus reference start,
    rounded to the nearest second) across every paired chapter and return
    (majority_fraction, consensus_shift) for the most common bucket, or
    None if there are no pairs. consensus_shift is the median of the raw
    (unrounded) shifts inside the winning bucket, for sub-second precision.
    """
    if not pairs:
        return None
    shifts = [file_t - cleaned_chapters[ci]["start_sec"] for ci, file_t in pairs]
    buckets: dict[int, list[float]] = {}
    for s in shifts:
        buckets.setdefault(round(s), []).append(s)
    best_key = max(buckets, key=lambda k: len(buckets[k]))
    majority_fraction = len(buckets[best_key]) / len(shifts)
    consensus_shift = statistics.median(buckets[best_key])
    return majority_fraction, consensus_shift


def _verify_file_boundaries(cleaned_chapters: list[dict], source_audio_files: list[Path]) -> dict | None:
    """Step 4: verify, once per book, whether this mp3_multi source's files
    correspond 1:1 (or nearly so) with the cleaned reference chapters, so
    each otherwise-unconfident chapter can be anchored to its own file's
    real start boundary instead of being folded away.

    Tries both a front-anchored pairing (chapter 0 <-> file 0, walking
    forward, excess trimmed off the back) and a back-anchored pairing (the
    last chapter <-> the last file, walking backward, excess trimmed off
    the front) - the excess could plausibly sit at either end (an unripped
    intro, or unsplit back-matter). Whichever direction's pairing has its
    paired chapters agree more tightly on the same file-boundary-vs-
    reference shift wins; an exact or near-tie prefers back-anchored
    (trim-from-front), matching front-matter mismatches being the more
    commonly observed pattern in the investigation this design responds to.

    A verified chapter's position is its own real, ffprobe-measured file
    boundary - not that boundary further adjusted by the consensus shift.
    The consensus shift's role is entirely to *decide whether to trust* the
    pairing (a large, tight majority is strong evidence these files really
    do correspond 1:1 to these chapters); once trusted, each chapter's own
    measured file boundary is strictly more accurate ground truth than
    reconstructing a position from the reference timestamp plus a shared
    shift would be.

    Returns {"positions": {chapter_index: file_boundary_sec}, "direction":
    "front"|"back", "majority_fraction": float, "shift": float} if
    verified, else None (both directions failed the clustering check, or
    the prefilter skipped verification entirely).
    """
    nc, nf = len(cleaned_chapters), len(source_audio_files)
    if nc == 0 or nf == 0 or abs(nc - nf) > _FILE_COUNT_TOLERANCE:
        return None

    file_starts = _cumulative_file_starts(source_audio_files)
    n_pairs = min(nc, nf)

    front_pairs = [(i, file_starts[i]) for i in range(n_pairs)]
    back_pairs = [(nc - n_pairs + k, file_starts[nf - n_pairs + k]) for k in range(n_pairs)]

    front_check = _cluster_shift_check(front_pairs, cleaned_chapters)
    back_check = _cluster_shift_check(back_pairs, cleaned_chapters)

    candidates = []
    if front_check and front_check[0] >= _FILE_BOUNDARY_MAJORITY_THRESHOLD:
        candidates.append(("front", front_pairs, front_check[0], front_check[1]))
    if back_check and back_check[0] >= _FILE_BOUNDARY_MAJORITY_THRESHOLD:
        candidates.append(("back", back_pairs, back_check[0], back_check[1]))

    if not candidates:
        return None

    if len(candidates) == 2:
        front_c, back_c = candidates
        # Tie-break (including an exact tie): prefer back-anchored unless
        # front is STRICTLY tighter.
        winner = front_c if front_c[2] > back_c[2] else back_c
    else:
        winner = candidates[0]

    direction, pairs, majority_fraction, shift = winner
    positions = {ci: file_t for ci, file_t in pairs}
    return {"positions": positions, "direction": direction, "majority_fraction": majority_fraction, "shift": shift}


def _log_alignment_summary(
    log,
    raw_chapters: list[dict],
    cleaned: list[dict],
    short_folded: int,
    results: list[dict],
    file_boundaries: dict | None,
    placements: list[dict],
    source_type: str,
) -> None:
    """Extends the original confident-vs-guess/shift summary so job history
    alone tells you which of the four resolution paths every chapter took,
    without reading code."""
    achew_confident = sum(1 for p in placements if p["source"] == "achew")
    fb_used = sum(1 for p in placements if p["source"] == "file_boundary")
    folded_unresolved = sum(1 for p in placements if p["source"] is None)
    achew_guesses = len(results) - achew_confident if results else 0

    log(
        f"Chapter prep: {len(raw_chapters)} audnexus chapter(s), {short_folded} folded into a "
        f"neighbouring title for being under {_MIN_CHAPTER_SEC:.0f}s long (e.g. narrator-name "
        f"markers), {len(cleaned)} sent to alignment."
    )

    if results:
        shifts = [abs(float(r["timestamp"]) - cleaned[i]["start_sec"]) for i, r in enumerate(results)]
        log(
            f"Aligned {len(cleaned)} chapter(s) to detected audio cues: "
            f"{achew_confident} confidently matched, {achew_guesses} flagged as lower-confidence guesses "
            f"(median shift {statistics.median(shifts):.1f}s, max shift {max(shifts):.1f}s)."
        )

    if source_type == "mp3_multi":
        if file_boundaries:
            log(
                f"File-boundary anchoring verified ({file_boundaries['direction']}-anchored, "
                f"{file_boundaries['majority_fraction'] * 100:.0f}% of paired chapters agreed on a "
                f"~{file_boundaries['shift']:.1f}s shift): {fb_used} otherwise-unconfident chapter(s) "
                f"placed via real source-file boundaries."
            )
        else:
            log("File-boundary anchoring not verified for this source (file/chapter count mismatch, "
                "or no consistent shift across a candidate pairing) - not used.")

    log(
        f"Final chapters: {achew_confident} placed by achew, {fb_used} placed via verified file-boundary "
        f"anchoring, {folded_unresolved} folded onto a neighbouring resolved chapter for lacking a verified "
        f"placement of their own. {len(placements) - folded_unresolved} chapter(s) written."
    )


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
