"""Helpers for testing app/pipeline/chapter_aligner.py, ported from achew
(https://github.com/SirGibblets/achew, MIT licensed, (c) 2025 Sir
Gibblets), backend/tests/realignment/realignment_helpers.py at commit
5e8e249 (v1.12.0) - see /NOTICE.md for the full attribution and license
text this port carries.

Differences from upstream, both driven by the same fact: this pipeline's
own _align_audnexus_chapters (app/pipeline/chapters.py) always runs
silencedetect over the WHOLE converted file, never a padding-windowed
region around each expected timestamp (that windowing exists in achew
purely for its interactive tool's scan latency, which doesn't apply to a
background batch job here - see chapter_aligner.py's attribution header):

  - production_padding()/extraction_regions()/cues_in_regions() are not
    ported - they exist upstream to simulate a padding-windowed first
    pass. Real-fixture tests here use each fixture's full, unfiltered
    detected_cues list, matching what this pipeline's whole-file scan
    would actually produce.
  - Fixture.padding is kept in the loaded dataclass (the field is present
    in the source JSON) but nothing here reads it.

Fixture schema (see tests/unit/chapter_aligner_fixtures/README.md)::

    {
      "ref_duration": float,
      "book_duration": float,
      "ref_chapters":  [float, ...],            # source chapter timestamps
      "detected_cues": [[float, float], ...],   # [timestamp, gap] per cue
      "ground_truth":  [float | None, ...],     # user-corrected timestamp, null if deleted
      "padding": float,                         # unused here - see module docstring
    }

``ref_chapters`` and ``ground_truth`` are index-aligned - one entry per reference chapter.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.pipeline.chapter_aligner import BasicChapter, DetectedCue

FIXTURE_DIR = Path(__file__).resolve().parent / "chapter_aligner_fixtures"

# A ground-truth timestamp set via achew's editor jump-to-cue feature is quantized to
# 1/100 s, while detected_cues keep full precision. Synthetic fixtures mirror this so the
# test harness exercises the same rounding, and tolerances stay comfortably above it.
GROUND_TRUTH_QUANTUM = 0.01

# A chapter counts as correctly placed when the aligner lands within this many seconds
# of the ground-truth boundary. Far tighter than chapter editing needs, but well clear
# of the 1/100 s quantization above.
CORRECT_TOLERANCE = 0.05


@dataclass
class Fixture:
    """A realignment test case: aligner inputs plus the expected answer."""

    name: str
    ref_duration: float
    book_duration: float
    ref_chapters: List[BasicChapter]
    detected_cues: List[DetectedCue]
    # Per-chapter correct boundary; None where the chapter has no ground truth
    # (e.g. the user deleted it, or it has no real boundary in the audio).
    ground_truth: List[Optional[float]]
    # Extraction padding achew captured the cues with. Unused by this port's
    # tests (see module docstring) - kept only because it's in the source JSON.
    padding: Optional[float] = None

    @classmethod
    def from_json(cls, path: Path) -> "Fixture":
        data = json.loads(path.read_text())
        return cls(
            name=path.stem,
            ref_duration=float(data["ref_duration"]),
            book_duration=float(data["book_duration"]),
            ref_chapters=[BasicChapter(timestamp=float(t), title="") for t in data["ref_chapters"]],
            detected_cues=[DetectedCue(timestamp=float(t), gap=float(g)) for t, g in data["detected_cues"]],
            ground_truth=[(None if t is None else float(t)) for t in data["ground_truth"]],
            padding=(float(data["padding"]) if data.get("padding") is not None else None),
        )


@dataclass
class AlignmentScore:
    """Outcome of scoring an aligner result against a fixture's ground truth."""

    total: int  # chapters that have a ground truth (the scorable ones)
    correct: int  # placed within tolerance
    wrong: List[int]  # chapter indices placed outside tolerance
    confident_wrong: List[int]  # wrong AND reported as a confident (non-guess) result

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 1.0


def score_alignment(
    result: Sequence[Dict[str, Any]],
    ground_truth: Sequence[Optional[float]],
    tolerance: float = CORRECT_TOLERANCE,
) -> AlignmentScore:
    """Compare an aligner result (list of dicts with ``timestamp``/``is_guess``) to
    ground truth. Chapters whose ground truth is ``None`` are skipped."""
    correct = 0
    wrong: List[int] = []
    confident_wrong: List[int] = []
    total = 0
    for i, (res, gt) in enumerate(zip(result, ground_truth)):
        if gt is None:
            continue
        total += 1
        if abs(float(res["timestamp"]) - gt) <= tolerance:
            correct += 1
        else:
            wrong.append(i)
            if not res.get("is_guess", False):
                confident_wrong.append(i)
    return AlignmentScore(total=total, correct=correct, wrong=wrong, confident_wrong=confident_wrong)


def load_real_fixtures() -> List[Fixture]:
    """Load every fixture from tests/unit/chapter_aligner_fixtures (may be empty)."""
    if not FIXTURE_DIR.is_dir():
        return []
    return [Fixture.from_json(p) for p in sorted(FIXTURE_DIR.glob("*.json"))]


# ──────────────────────────────────────────────────────────────────────────────
# Synthetic fixture generation
#
# A synthetic book has N reference chapters at times R_i. The audio places each
# true boundary at A_i = warp(R_i) for some misalignment model. We emit a cue at
# every true boundary (a strong silence) plus scattered decoy cues (weaker
# silences that are not chapter boundaries), then ask the aligner to recover A_i.
# ──────────────────────────────────────────────────────────────────────────────


def _round_gt(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value / GROUND_TRUTH_QUANTUM) * GROUND_TRUTH_QUANTUM


def _make_ref_chapters(rng: random.Random, n: int) -> List[float]:
    """N reference chapter start times beginning at 0 with realistic lengths."""
    times = [0.0]
    for _ in range(n - 1):
        length = rng.uniform(450.0, 2400.0)  # 7.5-40 min chapters
        times.append(times[-1] + length)
    return times


def build_fixture(
    name: str,
    *,
    rng: random.Random,
    ref_times: List[float],
    true_times: List[float],
    ref_duration: float,
    book_duration: float,
    missing_cue_indices: Sequence[int] = (),
    boundary_gap: Tuple[float, float] = (1.6, 4.0),
    decoys_per_chapter: Tuple[int, int] = (1, 3),
    decoy_window: float = 45.0,
    decoy_gap: Tuple[float, float] = (0.3, 1.8),
    forced_decoys: Sequence[Tuple[float, float]] = (),
) -> Fixture:
    """Assemble a Fixture from a warp (ref_times -> true_times) plus cue noise.

    ``missing_cue_indices`` chapters get no real boundary cue (they are missing in
    the audio); ``forced_decoys`` are explicit (time, gap) lures.
    """
    cues: List[Tuple[float, float]] = []

    for i, a in enumerate(true_times):
        # A missing chapter has no boundary in the audio at all - no real cue and no
        # decoys in its neighbourhood, so the aligner genuinely finds nothing there.
        if i in missing_cue_indices:
            continue
        if a > 0.0:
            cues.append((a, rng.uniform(*boundary_gap)))
        # Scatter weak decoy silences around each chapter's neighbourhood.
        for _ in range(rng.randint(*decoys_per_chapter)):
            offset = rng.uniform(-decoy_window, decoy_window)
            t = a + offset
            if t <= 0.5 or t >= book_duration:
                continue
            cues.append((t, rng.uniform(*decoy_gap)))

    for t, g in forced_decoys:
        cues.append((t, g))

    cues.sort(key=lambda c: c[0])
    detected = [DetectedCue(timestamp=t, gap=g) for t, g in cues]

    ground_truth = [_round_gt(a) for a in true_times]

    return Fixture(
        name=name,
        ref_duration=ref_duration,
        book_duration=book_duration,
        ref_chapters=[BasicChapter(timestamp=t, title="") for t in ref_times],
        detected_cues=detected,
        ground_truth=ground_truth,
    )


def make_clean(rng: random.Random, n: int = 20) -> Fixture:
    """Audio matches the reference exactly - sanity baseline."""
    ref = _make_ref_chapters(rng, n)
    dur = ref[-1] + rng.uniform(600, 1800)
    return build_fixture("clean", rng=rng, ref_times=ref, true_times=list(ref), ref_duration=dur, book_duration=dur)


def make_linear_drift(rng: random.Random, n: int = 20) -> Fixture:
    """Uniform clock drift: A = scale * R."""
    ref = _make_ref_chapters(rng, n)
    scale = rng.uniform(0.97, 1.03)
    true = [r * scale for r in ref]
    ref_dur = ref[-1] + rng.uniform(600, 1800)
    return build_fixture(
        "linear_drift",
        rng=rng,
        ref_times=ref,
        true_times=true,
        ref_duration=ref_dur,
        book_duration=ref_dur * scale,
    )


def make_per_chapter_jitter(rng: random.Random, n: int = 20) -> Fixture:
    """Each boundary independently nudged a few seconds - no global pattern."""
    ref = _make_ref_chapters(rng, n)
    true = [0.0] + [r + rng.uniform(-6.0, 6.0) for r in ref[1:]]
    dur = ref[-1] + rng.uniform(600, 1800)
    return build_fixture(
        "per_chapter_jitter",
        rng=rng,
        ref_times=ref,
        true_times=true,
        ref_duration=dur,
        book_duration=dur,
    )


def make_step_shift(rng: random.Random, n: int = 20) -> Fixture:
    """One chapter runs long/short, shifting every later boundary by a fixed amount.

    Magnitude is capped at a substantial-but-realistic ~40 s. Beyond that (one chapter
    off by ~a minute), the single chapter immediately after the step is a known hard
    case: its expected position is interpolated across the discontinuity and the nearest
    post-step anchor is itself unresolved, so it may be mismatched and need manual review.
    See the matching notes in chapter_aligner.py."""
    ref = _make_ref_chapters(rng, n)
    step_at = rng.randint(n // 3, 2 * n // 3)
    shift = rng.choice([-1, 1]) * rng.uniform(15.0, 40.0)
    true = [r + (shift if i >= step_at else 0.0) for i, r in enumerate(ref)]
    true[0] = 0.0
    dur = ref[-1] + rng.uniform(600, 1800)
    return build_fixture(
        "step_shift",
        rng=rng,
        ref_times=ref,
        true_times=true,
        ref_duration=dur,
        book_duration=dur + shift,
    )


def make_front_matter_decoy(rng: random.Random, n: int = 20) -> Fixture:
    """Models the real 'chapter 2' failure: a strong decoy silence ~6 s from the
    first real chapter boundary, which absolute-gap weighting alone would fall for."""
    ref = _make_ref_chapters(rng, n)
    true = list(ref)
    dur = ref[-1] + rng.uniform(600, 1800)
    # A loud decoy (dedication/epigraph pause) ~6 s before chapter 1's true start,
    # with a bigger gap than the true boundary.
    decoy_t = true[1] - 6.0
    forced = [(decoy_t, 5.0)]
    return build_fixture(
        "front_matter_decoy",
        rng=rng,
        ref_times=ref,
        true_times=true,
        ref_duration=dur,
        book_duration=dur,
        forced_decoys=forced,
    )


def make_offsetting_shift(rng: random.Random, n: int = 20) -> Fixture:
    """Total durations match, but a mid-book run of boundaries is shifted well beyond a
    typical narrow scan window - content inserted at one point and an equal amount
    removed later. Upstream (achew) this is the case its expansion-retry signal exists
    for; here it's a plain accuracy case, since this pipeline's whole-file scan already
    covers wherever the shifted boundaries actually are."""
    ref = _make_ref_chapters(rng, n)
    k1 = rng.randint(n // 4, n // 2)
    k2 = rng.randint(k1 + 3, min(n - 2, k1 + 7))
    shift = rng.choice([-1, 1]) * rng.uniform(45.0, 80.0)
    true = [r + (shift if k1 < i <= k2 else 0.0) for i, r in enumerate(ref)]
    dur = ref[-1] + rng.uniform(600, 1800)
    return build_fixture(
        "offsetting_shift",
        rng=rng,
        ref_times=ref,
        true_times=true,
        ref_duration=dur,
        book_duration=dur,
    )


def make_tail_shift(rng: random.Random, n: int = 20) -> Fixture:
    """Total durations match, but every boundary from some point on is shifted well
    beyond a typical narrow scan window (extra content mid-book, offset by shorter
    trailing content after the last chapter). Same rationale as offsetting_shift."""
    ref = _make_ref_chapters(rng, n)
    k = rng.randint(n // 2, n - 3)
    shift = rng.uniform(45.0, 80.0)
    true = [r + (shift if i > k else 0.0) for i, r in enumerate(ref)]
    dur = ref[-1] + shift + rng.uniform(600, 1800)
    return build_fixture(
        "tail_shift",
        rng=rng,
        ref_times=ref,
        true_times=true,
        ref_duration=dur,
        book_duration=dur,
    )


def make_missing_chapter(rng: random.Random, n: int = 20) -> Fixture:
    """A reference chapter that has no real boundary in the audio (e.g. two
    chapters were narrated as one) - it must be left as a guess, not stolen from a
    neighbour."""
    ref = _make_ref_chapters(rng, n)
    true = list(ref)
    missing = rng.randint(n // 3, 2 * n // 3)
    dur = ref[-1] + rng.uniform(600, 1800)
    fx = build_fixture(
        "missing_chapter",
        rng=rng,
        ref_times=ref,
        true_times=true,
        ref_duration=dur,
        book_duration=dur,
        missing_cue_indices=[missing],
    )
    # The missing chapter has no recoverable boundary; drop its ground truth so the
    # scorer judges only its neighbours, and stash the index for is_guess checks.
    fx.ground_truth[missing] = None
    return fx


# Builders keyed by name, for parametrized tests.
SYNTHETIC_BUILDERS = {
    "clean": make_clean,
    "linear_drift": make_linear_drift,
    "per_chapter_jitter": make_per_chapter_jitter,
    "step_shift": make_step_shift,
    "front_matter_decoy": make_front_matter_decoy,
    "missing_chapter": make_missing_chapter,
    "offsetting_shift": make_offsetting_shift,
    "tail_shift": make_tail_shift,
}
