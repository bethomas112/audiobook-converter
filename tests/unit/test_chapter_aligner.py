"""Tests for app/pipeline/chapter_aligner.py - the ported achew ChapterAligner
(see that module's attribution header and /NOTICE.md for provenance/license).

Two layers, both ported from achew's own backend/tests/realignment/test_chapter_aligner.py
(see tests/unit/chapter_aligner_helpers.py's docstring for what did and didn't come over):

  - Specific designed behaviours: the "chapter 2 decoy" case, a missing chapter, and the
    no-cues/empty-reference edge cases.
  - Accuracy floors over every synthetic scenario and every real fixture in
    tests/unit/chapter_aligner_fixtures/, calibrated against a real run of this exact
    wiring (see the module docstring in chapter_aligner_helpers.py for why real fixtures
    are fed their full, unfiltered cue list rather than a padding-windowed subset - this
    pipeline always scans the whole file). At the time this suite was written, this port
    scored 97.10% overall accuracy across the 73 real fixtures (achew's own README reports
    ~97.2% on the same data upstream) - see this file's git history / the commit that added
    it for the exact calibration run. Thresholds below are set with headroom under that
    measured number so the gate catches a real regression without being flaky.

These are unit tests of the ported algorithm in isolation - see
tests/integration/test_chapter_alignment_e2e.py for a real end-to-end check that this
algorithm is correctly wired into app/pipeline/chapters.py's resolve_chapters().
"""
import random

import pytest

from app.pipeline.chapter_aligner import ChapterAligner
from tests.unit.chapter_aligner_helpers import (
    SYNTHETIC_BUILDERS,
    load_real_fixtures,
    make_front_matter_decoy,
    make_missing_chapter,
    score_alignment,
)

SEEDS = [1, 2, 3, 7, 11]
SCORE_TOLERANCE = 0.1

REAL_FIXTURES = load_real_fixtures()


def _max_drift(book_duration: float, ref_duration: float) -> float:
    """Mirrors app/pipeline/chapters.py's own max_drift formula exactly, so
    these tests measure the same window sizing production actually uses."""
    return max(15.0, abs(book_duration - ref_duration) * 2.0)


def _align(fx):
    aligner = ChapterAligner(max_drift=_max_drift(fx.book_duration, fx.ref_duration))
    return aligner.align(
        fx.ref_chapters,
        fx.detected_cues,
        fx.ref_duration,
        fx.book_duration,
        scanned_regions=[(0.0, fx.book_duration)],
    )


# ── Specific designed behaviours ────────────────────────────────────────────────


@pytest.mark.parametrize("seed", SEEDS)
def test_first_real_chapter_not_fooled_by_decoy(seed):
    """The real-world 'chapter 2' failure: a strong decoy silence ~6 s from the first
    real boundary must not capture chapter 1."""
    fx = make_front_matter_decoy(random.Random(seed))
    result, _ = _align(fx)

    gt = fx.ground_truth[1]
    assert gt is not None
    assert abs(result[1]["timestamp"] - gt) <= 0.05, (
        f"seed {seed}: chapter 1 landed on {result[1]['timestamp']:.2f}, expected {gt:.2f} (decoy at {gt - 6.0:.2f})"
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_missing_chapter_is_guessed_not_stolen(seed):
    """A reference chapter with no real boundary is flagged as a guess, and its
    neighbours stay correct."""
    fx = make_missing_chapter(random.Random(seed))
    missing = next(i for i, gt in enumerate(fx.ground_truth) if gt is None and i != 0)
    result, _ = _align(fx)
    score = score_alignment(result, fx.ground_truth)

    assert result[missing]["is_guess"], f"seed {seed}: missing chapter {missing} not flagged as a guess"
    assert missing - 1 not in score.wrong and missing + 1 not in score.wrong, (
        f"seed {seed}: a neighbour of the missing chapter was misplaced ({score.wrong})"
    )
    assert score.accuracy >= 0.95


def test_no_cues_falls_back_to_duration_scaling():
    """With no detected cues the aligner still returns one entry per chapter,
    all but chapter 0 flagged as guesses."""
    fx = SYNTHETIC_BUILDERS["linear_drift"](random.Random(0))
    aligner = ChapterAligner()
    result, _ = aligner.align(fx.ref_chapters, [], fx.ref_duration, fx.book_duration)
    assert len(result) == len(fx.ref_chapters)
    assert all(r["is_guess"] for r in result[1:])


def test_empty_reference_returns_empty():
    aligner = ChapterAligner()
    result, _ = aligner.align([], [], 100.0, 100.0)
    assert result == []


def test_whole_file_scanned_regions_never_requests_expansion():
    """This pipeline always passes scanned_regions=[(0, duration)] (the whole
    converted file - see app/pipeline/chapters.py's _align_audnexus_chapters), which
    is the genuine simplification over achew's own padding-windowed usage described in
    chapter_aligner.py's attribution header: with the entire file already scanned,
    there is no narrower region expansion could still search, so expansion_needed must
    never fire. This is exercised across every synthetic scenario (including the two
    upstream specifically designed to trigger achew's own expansion signal under a
    padding-limited scan) to confirm that claim holds for every case this port ships
    tests for, not just spot-checked ones.
    """
    for name, builder in SYNTHETIC_BUILDERS.items():
        for seed in SEEDS:
            fx = builder(random.Random(seed))
            _, stats = _align(fx)
            assert stats["expansion_needed"] is False, f"{name}/seed {seed}: unexpected expansion_needed"
    for fx in REAL_FIXTURES:
        _, stats = _align(fx)
        assert stats["expansion_needed"] is False, f"{fx.name}: unexpected expansion_needed"


# ── Accuracy floors ──────────────────────────────────────────────────────────────

# Per-scenario floor on the minimum accuracy (over SEEDS) observed at calibration time,
# with headroom so a small, non-regressive fluctuation doesn't flake the suite.
SYNTHETIC_ACCURACY_FLOOR = {
    "clean": 0.99,
    "linear_drift": 0.99,
    "per_chapter_jitter": 0.90,
    "step_shift": 0.85,
    "front_matter_decoy": 0.99,
    "missing_chapter": 0.90,
    "offsetting_shift": 0.75,
    "tail_shift": 0.65,
}


@pytest.mark.parametrize("scenario", sorted(SYNTHETIC_BUILDERS))
def test_synthetic_scenario_accuracy_floor(scenario):
    builder = SYNTHETIC_BUILDERS[scenario]
    floor = SYNTHETIC_ACCURACY_FLOOR[scenario]
    for seed in SEEDS:
        fx = builder(random.Random(seed))
        result, _ = _align(fx)
        score = score_alignment(result, fx.ground_truth, tolerance=SCORE_TOLERANCE)
        assert score.accuracy >= floor, (
            f"{scenario}/seed {seed}: accuracy {score.accuracy:.2f} below floor {floor} "
            f"(wrong={score.wrong})"
        )


def test_real_fixtures_overall_accuracy():
    """Aggregate accuracy across all 73 real fixtures must not regress below a floor
    set (with headroom) under the 97.10% measured at calibration time - see this
    file's module docstring."""
    if not REAL_FIXTURES:
        pytest.skip("no real fixtures found in tests/unit/chapter_aligner_fixtures")

    total_correct = total_total = total_confident_wrong = 0
    for fx in REAL_FIXTURES:
        result, _ = _align(fx)
        score = score_alignment(result, fx.ground_truth, tolerance=SCORE_TOLERANCE)
        total_correct += score.correct
        total_total += score.total
        total_confident_wrong += len(score.confident_wrong)

    accuracy = total_correct / total_total
    assert accuracy >= 0.94, f"real-fixture accuracy {accuracy:.4f} ({total_correct}/{total_total}) below floor"
    # A "confident_wrong" placement is worse than a flagged guess - it asserts confidence
    # while being wrong. 51 were observed at calibration time; allow some headroom.
    assert total_confident_wrong <= 70, f"{total_confident_wrong} confident-wrong placements across real fixtures"
