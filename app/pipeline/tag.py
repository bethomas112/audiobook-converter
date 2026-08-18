"""Write standard M4B/MP4 metadata atoms via mutagen (native, no subprocess,
so this works equally for the M4B passthrough path and the freshly
transcoded MP3->M4B path).

MP4 has no dedicated "series" atom, so this follows the convention used by
most self-hosted audiobook tooling (m4b-tool, AAXtoMP3, and others):

| Field         | MP4 atom         | Notes                                   |
|---------------|------------------|------------------------------------------|
| Title         | \\xa9nam         |                                          |
| Author        | \\xa9ART, aART   | artist + album artist                   |
| Narrator      | \\xa9wrt         | composer atom, the de facto convention  |
| Series        | \\xa9alb         | album atom; falls back to title if none |
| Series index  | trkn             | track number atom, (index, 0)           |
| Year          | \\xa9day         |                                          |
| Genre         | \\xa9gen         | defaults to "Audiobook"                 |
| Description   | desc, \\xa9cmt   | native podcast-description atom + comment |
| Cover art     | covr             |                                          |

If a deployer's media server/agent expects a different mapping, adjust
this module — it's the single place tags are written.
"""
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover

from app.pipeline import metadata


def apply_tags(m4b_path: Path, meta: dict):
    audio = MP4(m4b_path)

    title = meta.get("title", "")
    author = meta.get("author", "")
    narrator = meta.get("narrator", "")
    series = meta.get("series", "")
    series_index = meta.get("series_index", "")
    year = meta.get("year", "")
    genre = meta.get("genre") or "Audiobook"
    description = meta.get("description", "")

    if title:
        audio["\xa9nam"] = [title]
    if author:
        audio["\xa9ART"] = [author]
        audio["aART"] = [author]
    if narrator:
        audio["\xa9wrt"] = [narrator]
    audio["\xa9alb"] = [series or title]
    if series_index:
        try:
            audio["trkn"] = [(int(float(series_index)), 0)]
        except ValueError:
            pass
    if year:
        audio["\xa9day"] = [str(year)]
    audio["\xa9gen"] = [genre]
    if description:
        audio["desc"] = [description[:255]]
        audio["\xa9cmt"] = [description]

    cover_bytes = metadata.fetch_cover_bytes(meta.get("cover_url", ""))
    if cover_bytes:
        audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()
