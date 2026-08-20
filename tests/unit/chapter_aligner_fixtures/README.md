# Real chapter-realignment fixtures

All 73 `*.json` files here are copied verbatim from achew
(<https://github.com/SirGibblets/achew>, MIT licensed, © 2025 Sir
Gibblets), `backend/tests/realignment/fixtures/`, at commit `5e8e249`
(v1.12.0). See `/NOTICE.md` at the repository root for the full license
text and attribution this port carries.

Each file is one real audiobook: audnexus-style reference chapter
timestamps, cues detected from real audio, and a human-verified ground
truth timestamp per chapter (`null` where the chapter has no real boundary
in the audio, e.g. it was deleted or narrated together with its neighbour).
`tests/unit/chapter_aligner_helpers.py` loads them and runs them through
the ported `ChapterAligner` (`app/pipeline/chapter_aligner.py`) as a
regression check - see `tests/unit/test_chapter_aligner.py`.

## Why the whole set, unmodified

achew's own tests filter each fixture's `detected_cues` down to whatever a
*padding-windowed* detection pass would have found in production (see
achew's `production_padding`/`extraction_regions` helpers) - achew only
scans a window around each expected chapter timestamp, for interactive-UI
latency reasons.

This pipeline doesn't window: `chapters.py`'s `_align_audnexus_chapters`
always runs `silencedetect` over the *whole* converted file up front (it's
a background batch job, not an interactive tool - see
`app/pipeline/chapter_aligner.py`'s attribution header for why this is a
safe simplification). So the tests here feed each fixture's full,
unfiltered `detected_cues` list straight to the aligner - that's actually
the more accurate simulation of what this pipeline will really see, not a
simplification for convenience.

One consequence: this port does not carry over achew's own padding/
expansion-signal-specific tests (production_padding, extraction_regions,
cues_in_regions, and the `expansion_needed` retry-trigger tests) - they
test behaviour this pipeline's wiring deliberately never exercises, since
`scanned_regions` here is always the entire file. `test_chapter_aligner.py`
does include one direct check that `expansion_needed` in fact never fires
under a whole-file `scanned_regions`, to verify that simplification holds.

## Schema

```
{
  "ref_duration": float,
  "book_duration": float,
  "ref_chapters":  [float, ...],            # source (reference) chapter timestamps
  "detected_cues": [[float, float], ...],   # [timestamp, gap] per detected cue
  "ground_truth":  [float | None, ...],     # human-corrected timestamp, null if unrecoverable
  "padding": float                          # achew's own capture padding - unused by this
                                             # port's tests (see above); present only because
                                             # the files are copied unmodified from upstream.
}
```

`ref_chapters` and `ground_truth` are index-aligned: one entry per reference chapter.
