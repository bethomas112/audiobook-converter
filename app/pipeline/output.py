"""Naming-template rendering and output-mode handling (standalone vs
library), plus optional sidecar file writing.
"""
import re
import shutil
from pathlib import Path

from app.config import config
from app.pipeline import metadata

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")

INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_path_component(value: str) -> str:
    value = INVALID_CHARS_RE.sub("", value)
    value = value.strip().strip(".")
    return value or "Untitled"


def render_template(template: str, values: dict) -> str:
    """Fills {placeholder} tokens from values. A [bracketed section] drops
    out entirely if any placeholder inside it is empty - e.g. a book with
    no series - rather than leaving stray separators behind. See the
    LIBRARY_*_TEMPLATE / STANDALONE_FILENAME_TEMPLATE docs in .env.example
    for the placeholder list and worked examples.
    """
    def render_bracket(match: re.Match) -> str:
        inner = match.group(1)
        placeholders_in_inner = _PLACEHOLDER_RE.findall(inner)
        if placeholders_in_inner and any(not values.get(p) for p in placeholders_in_inner):
            return ""
        return _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), "")), inner)

    result = _BRACKET_RE.sub(render_bracket, template)
    result = _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), "")), result)
    return result


def _template_values(meta: dict) -> dict:
    return {
        "author": meta.get("author", ""),
        "title": meta.get("title", ""),
        "year": meta.get("year", ""),
        "series": meta.get("series", ""),
        "series_index": meta.get("series_index", ""),
        "narrator": meta.get("narrator", ""),
        "genre": meta.get("genre", ""),
    }


def build_standalone_destination(meta: dict) -> Path:
    values = _template_values(meta)
    filename = render_template(config.STANDALONE_FILENAME_TEMPLATE, values)
    safe_segments = [sanitize_path_component(seg) for seg in filename.split("/")]
    return config.OUTPUT_DIR.joinpath(*safe_segments).with_suffix(".m4b")


def build_library_destination(meta: dict) -> tuple[Path, Path]:
    """Returns (folder_path, m4b_file_path), both absolute under OUTPUT_DIR."""
    values = _template_values(meta)
    folder_rendered = render_template(config.LIBRARY_FOLDER_TEMPLATE, values)
    filename_rendered = render_template(config.LIBRARY_FILENAME_TEMPLATE, values)

    folder_segments = [sanitize_path_component(seg) for seg in folder_rendered.split("/") if seg]
    folder_path = config.OUTPUT_DIR.joinpath(*folder_segments)

    filename_segments = [sanitize_path_component(seg) for seg in filename_rendered.split("/") if seg]
    file_path = folder_path.joinpath(*filename_segments[:-1], filename_segments[-1]).with_suffix(".m4b")

    return folder_path, file_path


def place_output(work_m4b: Path, meta: dict) -> Path:
    """Move the finished M4B (and, in library mode, any sidecar files) to
    its final destination. Returns the final .m4b path.
    """
    if config.OUTPUT_MODE == "library":
        folder_path, dest_file = build_library_destination(meta)
        folder_path.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work_m4b), str(dest_file))

        if config.WRITE_SIDECAR_FILES:
            _write_sidecar_files(folder_path, meta)

        return dest_file

    dest_file = build_standalone_destination(meta)
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(work_m4b), str(dest_file))
    return dest_file


def _write_sidecar_files(folder_path: Path, meta: dict):
    description = meta.get("description", "")
    if description:
        (folder_path / "desc.txt").write_text(description, encoding="utf-8")

    narrator = meta.get("narrator", "")
    if narrator:
        (folder_path / "reader.txt").write_text(narrator, encoding="utf-8")

    cover_bytes = metadata.fetch_cover_bytes(meta.get("cover_url", ""))
    if cover_bytes:
        (folder_path / "cover.jpg").write_bytes(cover_bytes)
