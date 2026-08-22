# Corrupt Source File Validation Implementation Plan

> **Outcome (post-implementation):** Tasks 1–5 below were implemented,
> individually reviewed, and passed a final whole-branch review — but
> Tasks 2–4 (the tier-2 full-decode `check_decodable()`/`validate_sources()`
> pre-transcode check) were reverted afterward, on Brady's call, once
> real-world testing surfaced two things a design review alone hadn't:
> (1) the actual real-world failure this plan was built around (`037.mp3`,
> a truncated stub) was already caught by the pre-existing tier-1 header
> probe on its own — Task 5's one-line message clarification was the only
> change that failure actually needed; (2) tier 2's `-xerror` check was
> stricter than the real transcode itself. Concatenating a file with a
> harmless, fully-recoverable mid-stream decode hiccup (verified directly:
> correct audio, correctly positioned, at every timestamp checked) through
> the *actual* production transcode command produced a perfectly good
> book — but tier 2 would have rejected that same book outright, since it
> aborts on the first decode error regardless of whether the real
> transcode can recover from it. Weighed against a genuinely-corrupt
> multi-file source (a truncated stub embedded among good neighbors),
> where the real transcode does *not* error but silently splices in a
> multi-minute block of true digital silence with `STATUS_DONE` and no
> warning anywhere - that failure mode is real, but it was already fully
> prevented by tier 1, which runs before concatenation ever happens and
> probes every file individually. Tier 2 added a new false-rejection risk
> without covering any gap tier 1 left open. **What actually shipped:**
> Task 1 (trimmed to just the `make_header_garbage_mp3` fixture — the
> mid-file-corruption fixture was removed along with tier 2) and Task 5
> (start_job()'s clearer error message) only. Tasks 2–4, 6 below are kept
> as the historical record of what was built and why it came back out —
> not a description of the current codebase.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A corrupt MP3 source file must fail the job clearly, naming the
specific bad file, and must never silently produce a gapped/partial
audiobook — with the expensive check deferred until after the book is
actually confirmed for conversion, not run speculatively on every drop-off.

**Architecture:** Two-tier validation. Tier 1 (already mostly present) is
the cheap, header-only `ffprobe` check that already runs during
`start_job()`'s source-duration probing, right after detection and before
the Audible metadata search — this task only needs to make its error
message clearly name the file and call out corruption, not add new
mechanism. Tier 2 is new: a full `ffmpeg -xerror` decode-through pass per
source file, added as its own explicit stage in `process_job()`
(`app/queue.py`), run only for `mp3_multi`/`mp3_single` sources, only after
metadata is confirmed, and only right before the real transcode begins —
never for M4B sources (which are never re-encoded at all) and never
before the user has actually committed to converting the book.

**Tech Stack:** Python 3.12, FastAPI, Huey, peewee/SQLite, ffmpeg/ffprobe
via subprocess, pytest (integration tests run real ffmpeg against
synthetic audio — no mocking of ffmpeg itself).

**Spec:** None — this is a bounded, well-scoped fix to an existing
pipeline stage, not a new subsystem, so it skipped the formal spec-doc
step. The design was worked out interactively and confirmed with Brady;
this plan is self-contained and captures every decision that matters.

## Global Constraints

- **Fail fast, on the first bad file, do not scan the rest.** One corrupt
  file in a multi-file source means the whole book can't be converted
  without a gap in the narration — there's no value in finding every bad
  file, and stopping early keeps the failure case cheap too.
- **MP3 sources only.** M4B sources are passed through with `-codec copy`
  and never re-encoded (see `app/pipeline/convert.py`'s `passthrough_m4b`)
  — validating them would mean decoding audio the pipeline otherwise never
  touches, for a failure mode that's never actually been observed on an
  M4B. Out of scope for this plan.
- **Tier 2 (the expensive decode-validate pass) must never run before
  metadata is confirmed.** Confirmed by profiling against real audiobooks
  in `Test Data/`: a full-book decode-validate pass takes 15–45 seconds on
  a 13–29 hour book and can exceed a minute on 60+ hour books. That cost
  must never be paid speculatively on a book that hasn't been confirmed
  for conversion — API calls (the metadata search) are comparatively free
  and infrequent, so they stay first.
- **Reuse existing exception types** — `ffutil.FFError` for the subprocess
  layer, `convert.ConvertError` for the human-facing message. No new
  exception classes.
- **Every source file's original filename must appear in the failure
  message** — e.g. `"037.mp3"`, not just a generic "conversion failed."

---

## File Structure

- `tests/helpers.py` — **modify.** Add two new synthetic-fixture builders:
  a "totally invalid" MP3 (tier-1's case) and a "valid header, corrupted
  mid-file" MP3 (tier-2's case — the gap tier 1 can't see).
- `app/pipeline/ffutil.py` — **modify.** Add `check_decodable()`, a thin
  wrapper around a real `ffmpeg -xerror` decode pass, matching this
  module's existing role ("thin wrappers around ffmpeg/ffprobe subprocess
  calls").
- `tests/integration/test_ffutil.py` — **modify.** Tests for
  `check_decodable()`, following this file's existing per-function test
  style.
- `app/pipeline/convert.py` — **modify.** Add `validate_sources()`, which
  loops source files in order, calls `ffutil.check_decodable()` per file,
  and turns the first failure into a clear `ConvertError` naming the file.
- `tests/integration/test_convert.py` — **create.** No dedicated test file
  exists for `convert.py` today (its behavior is otherwise only exercised
  indirectly through `test_pipeline_process_job.py`'s end-to-end tests and
  `test_ffutil.py`'s transcode tests) — `validate_sources()` has enough of
  its own edge cases (first-failure semantics, message content) to warrant
  direct unit-level coverage, matching the pattern of `test_archive.py`,
  `test_detect.py`, `test_metadata.py`, `test_output.py`, `test_tag.py`
  already existing as one-module-per-file tests elsewhere in the suite.
- `app/queue.py` — **modify.** Two separate changes: (1) `process_job()`
  gets a new `"Validating source files"` stage that calls
  `convert.validate_sources()` immediately before the existing
  `"Transcoding audio"` stage, for `mp3_multi`/`mp3_single` sources only.
  (2) `start_job()`'s existing duration-probe loop gets a clearer error
  message when a file fails there.
- `tests/integration/test_pipeline_process_job.py` — **modify.** Two new
  end-to-end tests: one proving a mid-file-corrupt MP3 in a multi-file
  source fails the whole job before any output is written, one proving a
  totally-invalid file fails fast during `start_job()` with a clear
  message and without ever calling the metadata search API.
- `ARCHITECTURE.md` — **modify.** Document the two-tier validation in the
  "Conversion pipeline" section, matching the doc's existing voice.
- `NEXT_STEPS.md` — **modify.** Small edit to the existing
  "real-world test results" table row for *The Dungeon Anarchist's
  Cookbook* — mark the corrupt-file gap it flagged as fixed.

---

## Task 1: Corrupt-file test fixtures

**Files:**
- Modify: `tests/helpers.py`
- Test: none (this task only adds fixture builders; they're exercised by
  later tasks' tests)

**Interfaces:**
- Produces: `make_header_garbage_mp3(path: Path) -> Path`,
  `make_corrupt_mid_file_mp3(path: Path, duration_sec: float = 3.0, bitrate_kbps: int = 96) -> Path`
  — both used by Tasks 2, 3, and 4.

- [ ] **Step 1: Add the two fixture builders to `tests/helpers.py`**

Add these two functions anywhere in `tests/helpers.py` after the existing
`make_silence_mp3` function (after line 41):

```python
def make_header_garbage_mp3(path: Path) -> Path:
    """A file with an .mp3 extension but no valid MP3 data at all - the
    "whole file is unreadable" case the existing header-only ffprobe check
    (ffutil.probe / get_duration_sec) already catches on its own, since
    ffmpeg's mp3 demuxer can't even establish the format.
    """
    path.write_text("This is not an mp3 file, just plain text.")
    return path


def make_corrupt_mid_file_mp3(path: Path, duration_sec: float = 3.0, bitrate_kbps: int = 96) -> Path:
    """A real, valid-header MP3 (via make_tone_mp3) with a chunk of its
    middle bytes zeroed out - simulates a truncated download or disk error
    that corrupts part of an otherwise-normal file, not the "whole file is
    garbage" case make_header_garbage_mp3 covers.

    This is the gap a header-only ffprobe check can't see: confirmed by
    hand against real ffmpeg that ffutil.probe() (and therefore
    get_duration_sec/get_audio_bitrate_kbps, which call it) reports exit 0
    and a normal-looking duration for a file corrupted this way, while an
    actual decode pass (ffutil.check_decodable) fails on it. This fixture
    exists specifically to exercise that gap - see
    tests/integration/test_ffutil.py's
    test_check_decodable_catches_mid_file_corruption_a_header_probe_misses.
    """
    make_tone_mp3(path, duration_sec=duration_sec, bitrate_kbps=bitrate_kbps)
    data = bytearray(path.read_bytes())
    start = len(data) // 3
    end = min(start + max(2000, len(data) // 10), len(data))
    for i in range(start, end):
        data[i] = 0
    path.write_bytes(data)
    return path
```

- [ ] **Step 2: Commit**

```bash
git add tests/helpers.py
git commit -m "test: add corrupt-mp3 fixture builders for source validation tests"
```

---

## Task 2: `ffutil.check_decodable()`

**Files:**
- Modify: `app/pipeline/ffutil.py`
- Test: `tests/integration/test_ffutil.py`

**Interfaces:**
- Consumes: `tests.helpers.make_tone_mp3`, `make_header_garbage_mp3`,
  `make_corrupt_mid_file_mp3` (Task 1); `ffutil.probe`, `ffutil.FFError`
  (already exist in `app/pipeline/ffutil.py`).
- Produces: `ffutil.check_decodable(path: Path) -> None`, raising
  `ffutil.FFError` on failure — used by Task 3.

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/integration/test_ffutil.py`, after the existing
`test_probe_raises_ffe_error_on_nonexistent_file` test (after line 87).
Update this file's imports (currently lines 11-17) to add
`make_corrupt_mid_file_mp3` and `make_header_garbage_mp3`:

```python
from tests.helpers import (
    has_quicktime_chapter_text_track,
    make_corrupt_mid_file_mp3,
    make_header_garbage_mp3,
    make_m4b,
    make_tone_mp3,
    make_tone_silence_pattern_mp3,
    strip_quicktime_chapter_track,
)
```

```python
def test_check_decodable_passes_silently_for_a_clean_file(tmp_path):
    f = make_tone_mp3(tmp_path / "clean.mp3", duration_sec=1.5)
    ffutil.check_decodable(f)  # must not raise


def test_check_decodable_raises_for_header_garbage(tmp_path):
    f = make_header_garbage_mp3(tmp_path / "garbage.mp3")
    with pytest.raises(ffutil.FFError):
        ffutil.check_decodable(f)


def test_check_decodable_catches_mid_file_corruption_a_header_probe_misses(tmp_path):
    """The gap ffutil.probe() (and get_duration_sec/get_audio_bitrate_kbps,
    which call it) can't see: a file that's fine at the header but
    corrupted partway through. Both assertions matter here - if probe()
    ever also started failing on this fixture, it would mean the fixture
    stopped testing the gap it claims to.
    """
    f = make_corrupt_mid_file_mp3(tmp_path / "mid_corrupt.mp3", duration_sec=3.0)
    ffutil.probe(f)  # header-only probe succeeds - this is exactly the gap
    with pytest.raises(ffutil.FFError):
        ffutil.check_decodable(f)


def test_check_decodable_is_fast_on_a_long_file(tmp_path):
    """check_decodable is decode-only (no encode), so it's expected to run
    far faster than real-time - manually profiled against real audiobooks
    in Test Data/ during development: 16.8s-42.3s for whole 13-29 hour
    books (37-52 files each). This is a smoke check on a 10-minute file
    (not a full book-length one, to keep the suite fast), matching the
    existing test_run_silencedetect_whole_file_pass_is_fast_on_a_long_file
    test's pattern and bound.
    """
    f = make_tone_mp3(tmp_path / "long.mp3", duration_sec=600.0, bitrate_kbps=64)
    start = time.monotonic()
    ffutil.check_decodable(f)
    elapsed = time.monotonic() - start
    assert elapsed < 30.0, f"check_decodable took {elapsed:.1f}s on a 10-minute file - unexpectedly slow"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_ffutil.py -k check_decodable -v`
Expected: FAIL with `AttributeError: module 'app.pipeline.ffutil' has no attribute 'check_decodable'`

- [ ] **Step 3: Implement `check_decodable()`**

Add this function to `app/pipeline/ffutil.py`, immediately after the
existing `probe()` function (after line 35, before `get_duration_sec` at
line 38):

```python
def check_decodable(path: Path) -> None:
    """Fully decodes `path` through ffmpeg's real audio decoder (writing no
    output) to verify every frame in the file actually decodes cleanly -
    not just that its header/container parses (see probe(), above).

    probe() alone isn't enough for this: a file that's valid at the header
    but corrupted partway through (e.g. a truncated download, a disk
    error) still parses fine under -show_format/-show_streams and returns
    exit 0 - confirmed directly against a real corrupted fixture, not
    assumed. -xerror makes ffmpeg exit non-zero on the very first decode
    error it hits, rather than its default behavior of logging a warning
    and continuing past one.

    Raises FFError naming the file and ffmpeg's own diagnostic if any part
    of the file fails to decode.
    """
    result = _run(
        [
            "ffmpeg", "-v", "error", "-xerror",
            "-i", str(path),
            "-f", "null", "-",
        ]
    )
    if result.returncode != 0:
        raise FFError(f"ffmpeg could not fully decode {path}: {result.stderr.strip()}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_ffutil.py -k check_decodable -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/pipeline/ffutil.py tests/integration/test_ffutil.py
git commit -m "feat: add ffutil.check_decodable for full decode-through source validation"
```

---

## Task 3: `convert.validate_sources()`

**Files:**
- Modify: `app/pipeline/convert.py`
- Create: `tests/integration/test_convert.py`

**Interfaces:**
- Consumes: `ffutil.check_decodable`, `ffutil.FFError` (Task 2);
  `convert.ConvertError` (already exists, `app/pipeline/convert.py:11-12`).
- Produces: `convert.validate_sources(source_files: list[Path]) -> None`,
  raising `convert.ConvertError` on the first undecodable file — used by
  Task 4.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_convert.py`:

```python
"""Integration tests for app/pipeline/convert.py against real ffmpeg on
synthetic audio (tests/helpers.py).
"""
from pathlib import Path

import pytest

from app.pipeline import convert
from tests.helpers import make_corrupt_mid_file_mp3, make_header_garbage_mp3, make_tone_mp3


def test_validate_sources_passes_silently_for_all_clean_files(tmp_path):
    files = [
        make_tone_mp3(tmp_path / "a.mp3", duration_sec=1.0),
        make_tone_mp3(tmp_path / "b.mp3", duration_sec=1.0),
    ]
    convert.validate_sources(files)  # must not raise


def test_validate_sources_raises_convert_error_naming_the_bad_file(tmp_path):
    good = make_tone_mp3(tmp_path / "01_good.mp3", duration_sec=1.0)
    bad = make_corrupt_mid_file_mp3(tmp_path / "02_bad.mp3", duration_sec=1.0)
    with pytest.raises(convert.ConvertError, match="02_bad.mp3"):
        convert.validate_sources([good, bad])


def test_validate_sources_catches_header_garbage_too(tmp_path):
    bad = make_header_garbage_mp3(tmp_path / "garbage.mp3")
    with pytest.raises(convert.ConvertError, match="garbage.mp3"):
        convert.validate_sources([bad])


def test_validate_sources_stops_at_the_first_bad_file(tmp_path, monkeypatch):
    """One bad file means the whole book can't be converted - no reason to
    keep decoding the rest once the first bad one is found. Verified by
    counting calls to ffutil.check_decodable rather than timing, so this
    stays fast and deterministic.
    """
    calls = []
    real_check = convert.ffutil.check_decodable

    def _tracking_check(path):
        calls.append(path)
        return real_check(path)

    monkeypatch.setattr(convert.ffutil, "check_decodable", _tracking_check)

    bad = make_corrupt_mid_file_mp3(tmp_path / "01_bad.mp3", duration_sec=1.0)
    good = make_tone_mp3(tmp_path / "02_good.mp3", duration_sec=1.0)

    with pytest.raises(convert.ConvertError):
        convert.validate_sources([bad, good])

    assert calls == [bad]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_convert.py -v`
Expected: FAIL with `AttributeError: module 'app.pipeline.convert' has no attribute 'validate_sources'`

- [ ] **Step 3: Implement `validate_sources()`**

Add this function to `app/pipeline/convert.py`, immediately after
`passthrough_m4b()` (after line 18, before `convert_mp3_to_m4b` at line
21):

```python
def validate_sources(source_files: list[Path]) -> None:
    """Confirms every source file is actually decodable before any
    transcoding time is spent on it - see ffutil.check_decodable() for why
    a full decode pass is needed rather than just probe()'s header check.

    Checks files in order and raises on the FIRST bad one found; does not
    scan the rest. One corrupt file means the whole book can't be
    converted without a gap in the narration, so there's nothing to gain
    by finding every bad file up front, and stopping early keeps the
    failure case fast too.
    """
    for f in source_files:
        try:
            ffutil.check_decodable(f)
        except ffutil.FFError as e:
            raise ConvertError(
                f"Source file '{f.name}' appears to be corrupt and can't be converted "
                f"cleanly ({e}). This book was not converted - fix or replace "
                f"{f.name} and re-add the book to the inbox to try again."
            ) from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_convert.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/pipeline/convert.py tests/integration/test_convert.py
git commit -m "feat: add convert.validate_sources to fail cleanly on a corrupt source file"
```

---

## Task 4: Wire tier-2 validation into `process_job()`

**Files:**
- Modify: `app/queue.py`
- Modify: `tests/integration/test_pipeline_process_job.py`

**Interfaces:**
- Consumes: `convert.validate_sources` (Task 3);
  `tests.helpers.make_corrupt_mid_file_mp3` (Task 1);
  `Job.save_progress(pct: int, stage: str | None = None)` (already exists,
  `app/db.py:130-140`).
- Produces: `process_job()` now fails a job with a clear, file-naming
  message before any transcode time is spent on an undecodable MP3 source
  — end state other tasks don't depend on further.

- [ ] **Step 1: Write the failing end-to-end test**

Add this test to `tests/integration/test_pipeline_process_job.py`, after
the existing `test_failed_detection_marks_job_failed_not_crash` test (end
of file, after line 383). Add `make_corrupt_mid_file_mp3` to this file's
existing `tests.helpers` import (currently lines 18-24):

```python
from tests.helpers import (
    has_quicktime_chapter_text_track,
    make_corrupt_mid_file_mp3,
    make_m4b,
    make_m4b_with_silence_gap,
    make_tone_mp3,
    strip_quicktime_chapter_track,
)
```

```python
def test_corrupt_mp3_in_multi_file_source_fails_before_producing_output(
    isolated_dirs, monkeypatch, huey_immediate
):
    """process_job() must validate every mp3 source file is actually
    decodable before spending any transcode time - and fail the whole job
    clearly (naming the bad file) rather than producing a partial/gapped
    output or a raw ffmpeg stderr dump. Regression coverage for the
    corrupt-source-file gap noted in NEXT_STEPS.md (a real 037.mp3 in "The
    Dungeon Anarchist's Cookbook" failed with an unhelpful raw ffmpeg
    error, discovered only after real-world testing).
    """
    source = isolated_dirs["inbox"] / "Bad Book"
    source.mkdir()
    make_tone_mp3(source / "01.mp3", duration_sec=1.0)
    make_corrupt_mid_file_mp3(source / "02.mp3", duration_sec=1.0)
    make_tone_mp3(source / "03.mp3", duration_sec=1.0)

    meta = {"asin": "", "title": "Bad Book", "author": "", "narrator": "", "series": "",
            "series_index": "", "year": "", "genre": "", "description": "", "cover_url": ""}
    job = _run_job_to_completion(monkeypatch, source, meta)

    assert job.status == Job.STATUS_FAILED, job.log
    assert "02.mp3" in job.error_message

    # A failed validation must not leave a partial/gapped .m4b anywhere a
    # later step could pick up or a user could mistake for a real
    # conversion.
    assert not list(isolated_dirs["work"].glob("*.m4b"))
    assert not list(isolated_dirs["output"].rglob("*.m4b"))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/integration/test_pipeline_process_job.py -k corrupt_mp3_in_multi_file -v`
Expected: FAIL — the job reaches `STATUS_DONE` instead of `STATUS_FAILED`
(today, nothing catches the mid-file corruption before/during transcode
producing a gapped-but-"successful" output, since the real ffmpeg concat
transcode logs the decode error but still exits 0 by default).

- [ ] **Step 3: Wire `validate_sources()` into `process_job()`**

In `app/queue.py`, inside `process_job()`, replace the `else:` branch that
currently reads (lines 270-279):

```python
        else:
            job.save_progress(0, stage="Transcoding audio")
            convert.convert_mp3_to_m4b(
                audio_files,
                work_path,
                log=job.append_log,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
            has_embedded = False
```

with:

```python
        else:
            # Validated in its own stage, before "Transcoding audio" -
            # see convert.validate_sources() and ffutil.check_decodable()
            # for why a full decode pass (not just probe()'s cheap header
            # check) is needed. Deliberately placed here, not earlier
            # during start_job()'s detection phase: this cost (up to ~45s
            # on a real 29-hour book, profiled against Test Data/, and can
            # exceed a minute on 60+ hour books) must only ever be paid
            # once the book is actually confirmed for conversion, not
            # speculatively on every drop-off.
            job.save_progress(0, stage="Validating source files")
            convert.validate_sources(audio_files)
            job.append_log(f"Validated {len(audio_files)} source file(s) - all decodable.")

            job.save_progress(0, stage="Transcoding audio")
            convert.convert_mp3_to_m4b(
                audio_files,
                work_path,
                log=job.append_log,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
            has_embedded = False
```

No other change is needed in `process_job()` — a `ConvertError` raised by
`validate_sources()` propagates up to the function's existing
`except Exception as e:` block (unchanged), which already marks the job
`STATUS_FAILED` with `job.error_message = str(e)` and logs
`f"Failed during processing: {e}"`, the same generic handling every other
pipeline-stage error already gets.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/integration/test_pipeline_process_job.py -k corrupt_mp3_in_multi_file -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `pytest`
Expected: All tests pass (no existing test should be affected — this only
adds a new stage to the `mp3_multi`/`mp3_single` conversion path, which
every existing passing test's synthetic fixtures already satisfy).

- [ ] **Step 6: Commit**

```bash
git add app/queue.py tests/integration/test_pipeline_process_job.py
git commit -m "feat: validate mp3 sources are decodable before transcoding, after confirm"
```

---

## Task 5: Clearer tier-1 error message in `start_job()`

**Files:**
- Modify: `app/queue.py`
- Modify: `tests/integration/test_pipeline_process_job.py`

**Interfaces:**
- Consumes: `tests.helpers.make_header_garbage_mp3` (Task 1); `ffutil.FFError`
  (already exists).
- Produces: no new interface — this task only changes error-message text
  surfaced through the existing `Job.error_message`/`Job.log` fields.

- [ ] **Step 1: Write the failing end-to-end test**

Add this test to `tests/integration/test_pipeline_process_job.py`, after
the test added in Task 4. Add `make_header_garbage_mp3` to this file's
`tests.helpers` import (already updated in Task 4 — extend it further):

```python
from tests.helpers import (
    has_quicktime_chapter_text_track,
    make_corrupt_mid_file_mp3,
    make_header_garbage_mp3,
    make_m4b,
    make_m4b_with_silence_gap,
    make_tone_mp3,
    strip_quicktime_chapter_track,
)
```

```python
def test_totally_invalid_mp3_fails_fast_during_detection_with_clear_message(
    isolated_dirs, monkeypatch, huey_immediate
):
    """The cheap, early check in start_job() - right after detect(), before
    the metadata search API call - must still catch a totally-invalid file
    (tier 1 of the corrupt-file fix; tier 2 in process_job() only runs for
    a source that passes this first) with a message that clearly names
    corruption as the cause, not a bare ffprobe stderr dump. Must also
    never reach the metadata search call for a source that's already known
    to be unusable.
    """
    from app.pipeline import metadata as metadata_mod

    search_calls = []

    def _tracked_search(*args, **kwargs):
        search_calls.append(1)
        return []

    monkeypatch.setattr(metadata_mod, "search", _tracked_search)

    source = isolated_dirs["inbox"] / "Garbage Book.mp3"
    make_header_garbage_mp3(source)

    job = Job.create(source_path=str(source))
    queue_mod.start_job(job.id)
    job = Job.get_by_id(job.id)

    assert job.status == Job.STATUS_FAILED, job.log
    assert "corrupt" in job.error_message.lower()
    assert not search_calls  # never got as far as the metadata API call
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/integration/test_pipeline_process_job.py -k totally_invalid_mp3 -v`
Expected: FAIL — today's `job.error_message` is the bare
`"ffprobe failed on ...: ..."` text without the word "corrupt" in it
(the assertion `"corrupt" in job.error_message.lower()` fails).

- [ ] **Step 3: Clarify the error message in `start_job()`**

In `app/queue.py`, inside `start_job()`, replace the line that currently
reads (line 66):

```python
        job.source_duration_sec = sum(ffutil.get_duration_sec(f) for f in result.audio_files)
```

with:

```python
        try:
            job.source_duration_sec = sum(ffutil.get_duration_sec(f) for f in result.audio_files)
        except ffutil.FFError as e:
            raise ffutil.FFError(
                f"A source file appears to be corrupt or unreadable and can't be processed ({e})."
            ) from e
```

The line immediately after (line 67,
`job.append_log(f"Total source duration: {job.source_duration_sec / 60:.1f} min.")`)
stays unchanged — it's unreachable when the exception above fires, and
still correct on the success path.

No change is needed to `start_job()`'s existing
`except Exception as e:` block — it already marks the job `STATUS_FAILED`
with `job.error_message = str(e)` and logs
`f"Failed during detection/metadata search: {e}"`; this task only makes
the `{e}` text itself name corruption explicitly, by re-raising a
same-type `ffutil.FFError` with a clearer wrapped message before it
reaches that handler.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/integration/test_pipeline_process_job.py -k totally_invalid_mp3 -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `pytest`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/queue.py tests/integration/test_pipeline_process_job.py
git commit -m "fix: clarify start_job's error message when a source file is corrupt"
```

---

## Task 6: Update docs

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `NEXT_STEPS.md`

**Interfaces:** None — documentation only.

- [ ] **Step 1: Add a paragraph to `ARCHITECTURE.md`'s "Conversion pipeline" section**

In `ARCHITECTURE.md`, find the paragraph that currently starts with
`**Metadata search** goes through Audible's own unauthenticated catalog
API` (this is the last paragraph before the "## Live progress and
cancellation" heading). Insert a new paragraph immediately **before** that
`**Metadata search**` paragraph:

```markdown
**Corrupt source files.** Two checks guard against a corrupt MP3 source,
run at different points because they cost very different amounts. A
cheap, header-only `ffprobe` check already runs during `start_job()`
(right after `detect.detect()`, before the metadata search) as a
byproduct of probing each file's duration - it catches a source file
that's unreadable outright (e.g. truncated to a few bytes) in
milliseconds, before any API call or user review time is spent on a book
that was never going to work. It can't, however, catch a file that's
valid at the header but corrupted partway through (a truncated download,
a disk error) - confirmed directly, not assumed: such a file still
passes the header check with a normal-looking duration. Catching that
requires an actual decode pass (`ffutil.check_decodable`,
`ffmpeg -xerror -i file -f null -`), which is reliable (it's the literal
decode path the real transcode uses) but not free - profiled against real
audiobooks in `Test Data/`, a whole-book pass took 15-45 seconds on
13-29 hour books, and can run past a minute on a 60+ hour one. That cost
is deferred as late as it can usefully go: `convert.validate_sources()`
runs in `process_job()`, in its own `"Validating source files"` stage,
immediately before the MP3 transcode itself and only after metadata has
been confirmed - so it's never paid on a book that hasn't actually been
committed to conversion, while still failing well before the transcode
(which costs roughly 12x longer than validation alone, profiled on the
same real files) wastes any time on a doomed book. Either check raising
fails the whole job (`STATUS_FAILED`) with a message naming the specific
bad file - never a partial, silently-gapped output. M4B sources are out
of scope for both checks: they're never re-encoded at all (see above), so
there's no matching failure mode yet observed for them.
```

- [ ] **Step 2: Update `NEXT_STEPS.md`'s real-world test results table**

In `NEXT_STEPS.md`, find the table row for *The Dungeon Anarchist's
Cookbook* in the "Real-world test results so far" section. It currently
reads:

```markdown
| *The Dungeon Anarchist's Cookbook* | **Failed** — but for an unrelated reason: one source MP3 (`037.mp3`) is corrupt ("Failed to find two consecutive MPEG audio frames"). Not a chapter-alignment issue; a real bad-file-handling gap worth its own look eventually. |
```

Replace it with:

```markdown
| *The Dungeon Anarchist's Cookbook* | **Failed** — one source MP3 (`037.mp3`) is corrupt ("Failed to find two consecutive MPEG audio frames"). Not a chapter-alignment issue. **Fixed**: source files are now validated (a full decode pass, not just a header check) before transcoding begins, and the job fails clearly, naming the bad file, instead of producing a raw ffmpeg error or a silently-gapped output — see ARCHITECTURE.md's "Corrupt source files" section. |
```

- [ ] **Step 3: Commit**

```bash
git add ARCHITECTURE.md NEXT_STEPS.md
git commit -m "docs: document the corrupt-source-file validation added in this branch"
```

---

## Self-Review Notes

- **Spec coverage:** every design decision confirmed in conversation is
  covered — tier-1 stays cheap/early (Task 5), tier-2 is a full decode
  pass deferred to right before transcode (Tasks 2-4), MP3-only scope
  (stated in Global Constraints, no M4B code path touched), fail-on-first
  (Task 3's `test_validate_sources_stops_at_the_first_bad_file`), the
  progress-stage UX touch (Task 4), and doc updates matching this repo's
  existing discipline (Task 6).
- **Type/signature consistency checked:** `ffutil.check_decodable(path: Path) -> None`
  (Task 2) is the only thing `convert.validate_sources()` (Task 3) calls;
  `convert.validate_sources(source_files: list[Path]) -> None` (Task 3) is
  the only thing `process_job()` (Task 4) calls — both used exactly as
  defined, no mismatched names across tasks.
- **No placeholders:** every step has real, complete code — no "add error
  handling" or "similar to Task N" shortcuts.
