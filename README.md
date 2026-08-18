# Audiobook Pipeline

A self-hosted tool that turns a downloaded audiobook — an M4B file, a
folder of chapter/CD-split MP3s, or a single monolithic MP3 — into a
properly tagged, chaptered M4B, with a web UI to confirm each step along
the way.

## What it does

1. **Drop-off** — drop an audiobook into a watched inbox folder.
2. **Detection** — once the item has stopped changing (no partial
   downloads/copies picked up mid-write), it's queued as pending.
3. **Confirm to start** — processing begins only once you confirm in the
   web UI (or automatically, if configured).
4. **Metadata search** — the tool guesses a title/author from the
   filename and searches for candidate matches, with cover art, author,
   narrator, series, and description.
5. **Metadata review** — confirm a candidate, pick a different one, or
   enter details manually. Nothing is written until you confirm (or it's
   configured to auto-confirm the top match).
6. **Conversion**:
   - An M4B source is passed through **untouched** — no re-encode, no
     remux, just a metadata-only tag patch.
   - An MP3 source (or folder of them) is transcoded to AAC/M4B at the
     source bitrate, with a configurable floor.
7. **Chapters**, resolved in priority order:
   1. Embedded chapters already in an M4B input — left as-is.
   2. Official chapter data for the matched title, if the input didn't
      already have its own chapters.
   3. Source-file boundaries, for a multi-file MP3 source with no
      official chapter data.
   4. Silence detection, as a last resort for an undifferentiated single
      audio stream.
8. **Tagging** — standard M4B/MP4 tags and embedded cover art (see
   [Tag mapping](#tag-mapping) below).
9. **Output** — either a single self-tagged M4B file (`standalone` mode)
   or a library folder structure with optional sidecar files (`library`
   mode) — see [Configuration](#configuration).
10. **Archive** — the original source is archived (default), deleted, or
    left in place, per `SOURCE_CLEANUP_MODE`.
11. **History** — the web UI shows a queue and history of jobs with
    enough detail to diagnose failures.

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

Then open `http://<host>:8000` and drop an audiobook into your configured
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
| `ARCHIVE_RETENTION_DAYS` | Auto-purge window for archived originals; unset = keep forever | unset |
| `AUTO_START_PROCESSING` | Skip the manual "confirm to start" step | `false` |
| `AUTO_CONFIRM_METADATA` | Skip metadata review, auto-apply the top match | `false` |
| `MIN_BITRATE_KBPS` | Informational floor (kbps); sources below it still convert (always at their own bitrate), just flagged in the log | `128` |
| `METADATA_SOURCE` | Named for future alternate sources; only one exists today | `audnexus` |
| `SILENCE_THRESHOLD_DB` | Noise floor for silence-based chapter detection | `-30dB` |
| `SILENCE_MIN_DURATION_SEC` | Minimum quiet-gap length to count as a chapter break | `1.5` |
| `SILENCE_MIN_CHAPTER_SEC` | Minimum resulting chapter length | `120` |
| `SETTLE_WINDOW_SEC` | How long a drop-off must be unchanged before it's queued | `10` |
| `WEB_UI_AUTH` | `none` or `basic` | `none` |
| `WEB_UI_USERNAME` / `WEB_UI_PASSWORD` | Only used when `WEB_UI_AUTH=basic` | unset |

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
