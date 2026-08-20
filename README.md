# Audiobook Converter

A self-hosted tool that turns a downloaded audiobook — an M4B file, a
folder of chapter/CD-split MP3s, or a single monolithic MP3 — into a
properly tagged, chaptered M4B. A web UI lets you confirm each step,
watch live conversion progress, and manage a queue of several books at
once.

For how it's built internally — the job lifecycle, the conversion
queue's design, the web UI's fragment/polling model — see
[ARCHITECTURE.md](ARCHITECTURE.md). This README covers what it does and
how to run it.

## What it does

1. **Drop-off** — drop an audiobook into a watched inbox folder. Once
   it's stopped changing (no partial downloads/copies picked up
   mid-write), it shows up in the **Needs Input** queue as "Waiting".
2. **Look it up** — click "Find this book"; the tool guesses a
   title/author from the filename and searches for candidate matches,
   each with cover art, author, narrator, series, description, and the
   book's official runtime. Didn't find the right one? Search again with
   your own title/author instead of the filename-derived guess.
3. **Review** — pick a candidate, or click "None of these" to clear the
   fields back to the filename-derived guess and type your own (every
   field is always editable regardless of what's selected), and confirm.
   The candidate's official runtime is shown next to
   the source files' actual total duration, so a mismatched edition
   (e.g. an abridged match against an unabridged source) is easy to spot
   before confirming. A preview of the chapters that will be written is
   also shown (expandable to see the full list, not just the first few).
   Nothing is written until you confirm (or it's configured to
   auto-confirm the top match).
4. **Convert** — confirming queues the book; one book converts at a
   time, and you can reorder or cancel anything still waiting its turn.
   The book actually converting shows live progress in a persistent bar
   at the top of the page, visible no matter what else you're looking
   at, and can be cancelled mid-conversion.
   - An M4B source's audio is passed through **untouched** — no
     re-encode, ever. Its own embedded chapters and tags are normally
     left alone too (just a metadata-only tag patch), *except* when the
     source has chapters but is missing the QuickTime-style chapter
     track Apple's own apps (Books, Music, Podcasts) need to show real
     chapter titles — some other/older tools only write the legacy
     format. In that one case the existing chapter data is rewritten
     (still a `-codec copy` remux, no audio re-encode) to add the
     missing track.
   - An MP3 source (or folder of them) is transcoded to AAC/M4B, always
     at the source's own bitrate (re-encoding higher can't add back
     quality that isn't there).
5. **Chapters**, resolved in priority order:
   1. Embedded chapters already in an M4B input — left as-is (see the
      QuickTime-chapter-track repair note above for the one exception).
   2. Official chapter data for the matched title, if the input didn't
      already have its own chapters — **realigned to this rip's actual
      audio** before being written, rather than trusted verbatim. The
      official timestamps are for Audible's own release, which commonly
      has different front/back matter than a given rip, so each chapter
      is individually matched against a real pause in the converted audio
      (a ported, credited algorithm from the
      [achew](https://github.com/SirGibblets/achew) project - see
      `NOTICE.md`) instead of just writing the official offset as-is. A
      few real-world wrinkles are handled before trusting that match: a
      handful of very short "chapters" (a couple of seconds - typically a
      narrator-name or part-break marker rather than a real chapter) are
      folded into whichever real chapter follows them instead of getting
      their own entry; for a multi-file source, a chapter the audio match
      isn't confident about can instead be placed at its own source
      file's real boundary, when that turns out to be a reliable
      alternative for this particular book; and a chapter that ends up
      with neither a confident audio match nor a reliable file boundary
      is folded into a neighboring chapter rather than written at a
      guessed position. This all happens automatically; the outcome (how
      many chapters were folded, matched confidently, placed via file
      boundaries, or folded for lacking any of the above) is recorded in
      the job's log.
   3. Source-file boundaries, for a multi-file MP3 source with no
      official chapter data.
   4. Silence detection, as a last resort for an undifferentiated single
      audio stream.
6. **Tagging** — standard M4B/MP4 tags and embedded cover art (see
   [Tag mapping](#tag-mapping) below).
7. **Output** — either a single self-tagged M4B file (`standalone` mode)
   or a library folder structure with optional sidecar files (`library`
   mode) — see [Configuration](#configuration).
8. **Archive** — the original source is archived (default), deleted, or
   left in place, per `SOURCE_CLEANUP_MODE`.
9. **Done** — finished books stay listed with their full log and where
   they were saved, for as long as you keep them (there's no automatic
   history cleanup).

## Quick start

Requires Docker and Docker Compose.

```bash
git clone <this-repo-url>
cd audiobook-pipeline
cp .env.example .env
```

Edit `.env` — at minimum you don't need to change anything to try it out
with the bundled `./data/*` bind mounts in `docker-compose.yml`. For a
real deployment, point the volumes at wherever you want the inbox,
working space, archive, output, and app config to actually live.

```bash
docker compose up -d --build
```

Then open `http://<host>:2012` and drop an audiobook into your configured
inbox folder.

## Configuration

All variables go in `.env` (see `.env.example` for the full, documented
list with defaults). The `*_DIR` variables are container-internal mount
points — set the actual host-side locations via the `volumes:` section of
`docker-compose.yml`.

| Variable | Purpose | Default |
|---|---|---|
| `INBOX_DIR` | Watched drop folder | `/data/inbox` |
| `WORK_DIR` | Scratch space during conversion | `/data/work` |
| `ARCHIVE_DIR` | Where source files land after processing | `/data/archive` |
| `OUTPUT_DIR` | Where finished audiobooks land | `/data/output` |
| `CONFIG_DIR` | App database/settings, persisted across restarts | `/data/config` |
| `OUTPUT_MODE` | `standalone` or `library` | `standalone` |
| `STANDALONE_FILENAME_TEMPLATE` | Filename template, `standalone` mode only | `{author} - {title}[ ({series} #{series_index})]` |
| `LIBRARY_FOLDER_TEMPLATE` | Folder-naming template, `library` mode only | `{author}/[{series}/]{year} - {title}[ ({series} #{series_index})]` |
| `LIBRARY_FILENAME_TEMPLATE` | Filename template, `library` mode only | `{title} ({year})[ ({series} #{series_index})]` |
| `WRITE_SIDECAR_FILES` | Write `desc.txt`/`reader.txt`/`cover.jpg`; `library` mode only | `false` |
| `SOURCE_CLEANUP_MODE` | `archive`, `delete`, or `keep` | `archive` |
| `ARCHIVE_RETENTION_DAYS` | Auto-purge window for archived originals; unset/`0` = keep forever | `30` |
| `AUTO_START_PROCESSING` | Skip the manual "look it up" step and start detection/metadata search immediately | `false` |
| `AUTO_CONFIRM_METADATA` | Skip metadata review, auto-apply the top match | `false` |
| `MIN_BITRATE_KBPS` | Informational floor (kbps); sources below it still convert (always at their own bitrate), just flagged in the log | `128` |
| `METADATA_SOURCE` | Named for future alternate sources; only one exists today | `audnexus` |
| `SILENCE_THRESHOLD_DB` | Noise floor for silence-based chapter detection | `-30dB` |
| `SILENCE_MIN_DURATION_SEC` | Minimum quiet-gap length to count as a chapter break | `1.5` |
| `SILENCE_MIN_CHAPTER_SEC` | Minimum resulting chapter length | `120` |
| `SETTLE_WINDOW_SEC` | How long a drop-off must be unchanged before it's queued | `10` |
| `WEB_UI_AUTH` | `none` or `basic` | `none` |
| `WEB_UI_USERNAME` / `WEB_UI_PASSWORD` | Only used when `WEB_UI_AUTH=basic` | unset |
| `PORT` | Port the web UI is served on, both inside the container and on the host | `2012` |

`WEB_UI_AUTH=none` is intended for a trusted LAN only — don't expose this
tool directly to the internet.

### Naming templates

Placeholders: `{author}` `{title}` `{year}` `{series}` `{series_index}`
`{narrator}` `{genre}`. Wrap a section in square brackets to have it drop
out entirely when the placeholder(s) inside are empty — e.g. a book with
no series:

```
{author}/[{series}/]{year} - {title}[ ({series} #{series_index})]
```

renders as `Brandon Sanderson/Mistborn/2006 - The Final Empire (Mistborn #1)`
for a book in a series, or `Brandon Sanderson/2006 - Some Standalone Book`
for one that isn't.

## Tag mapping

MP4 has no dedicated "series" atom, so this follows the convention used
by most self-hosted audiobook tooling:

| Field | MP4 atom |
|---|---|
| Title | `©nam` |
| Author | `©ART`, `aART` |
| Narrator | `©wrt` (composer atom — the de facto convention) |
| Series | `©alb` (album atom; falls back to title if there's no series) |
| Series index | `trkn` (track number) |
| Year | `©day` |
| Genre | `©gen` |
| Description | `desc`, `©cmt` |
| Cover art | `covr` |

If your media server/app expects a different mapping, it's all in one
place: `app/pipeline/tag.py`.

## Metadata source

Candidate search queries Audible's own unauthenticated catalog search
(no login required); the matched title's chapter data comes from
[audnexus](https://api.audnex.us), a harmonized, ASIN-keyed metadata API
built on top of the same catalog. Both are unofficial and could change
or go away — `METADATA_SOURCE` exists so an alternate source could be
added later.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The app is one process that also needs a Huey consumer running
alongside it:

```bash
uvicorn app.main:app --reload &
python -m huey.bin.huey_consumer app.queue.huey -w 1
```

Both need `ffmpeg`/`ffprobe` on `PATH` to actually convert anything —
inside the Docker image they're already there.

Run the test suite with `pytest` from the repo root (`pip install -r
requirements-dev.txt` first) — it also needs `ffmpeg`/`ffprobe` on `PATH`,
since most tests run real conversions against small synthetic audio files
rather than mocking ffmpeg.

See `NOTICE.md` for third-party code included in this project (the
chapter-realignment algorithm, ported from achew).
