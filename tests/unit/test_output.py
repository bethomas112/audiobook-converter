"""Unit tests for app/pipeline/output.py - naming-template rendering and
destination-path building. This is the piece Section 7 of the planning doc
explicitly left open ("exact placeholder syntax... left to the build
session to design") and Section 5 gave a worked example to check against:

    <Author>/[<Series>/]<Year> - <Title> [<Series> #]/<Title> (<Year>) [<Series> #].m4b
"""
import pytest

from app.pipeline.output import (
    build_library_destination,
    build_standalone_destination,
    place_output,
    render_template,
    sanitize_path_component,
)


def test_render_template_fills_placeholders():
    result = render_template("{author} - {title}", {"author": "Andy Weir", "title": "Project Hail Mary"})
    assert result == "Andy Weir - Project Hail Mary"


def test_bracketed_section_included_when_placeholder_present():
    result = render_template(
        "{title}[ ({series} #{series_index})]",
        {"title": "The Final Empire", "series": "Mistborn", "series_index": "1"},
    )
    assert result == "The Final Empire (Mistborn #1)"


def test_bracketed_section_dropped_when_placeholder_empty():
    """The book-with-no-series case Section 7 explicitly asked to handle
    gracefully.
    """
    result = render_template(
        "{title}[ ({series} #{series_index})]",
        {"title": "Some Standalone Book", "series": "", "series_index": ""},
    )
    assert result == "Some Standalone Book"


def test_bracketed_section_dropped_when_only_one_inner_placeholder_empty():
    """A bracket with two placeholders drops entirely if *either* is empty -
    a series with no index shouldn't render "Mistborn #" with a dangling
    separator.
    """
    result = render_template(
        "{title}[ ({series} #{series_index})]",
        {"title": "T", "series": "Mistborn", "series_index": ""},
    )
    assert result == "T"


def test_full_library_folder_template_matches_brady_worked_example():
    """The exact worked example from the planning doc, Section 3's README
    docstring: a book in a series renders with the series segment; see the
    companion no-series case below.
    """
    values = {
        "author": "Brandon Sanderson", "title": "The Final Empire", "year": "2006",
        "series": "Mistborn", "series_index": "1",
    }
    template = "{author}/[{series}/]{year} - {title}[ ({series} #{series_index})]"
    result = render_template(template, values)
    assert result == "Brandon Sanderson/Mistborn/2006 - The Final Empire (Mistborn #1)"


def test_full_library_folder_template_no_series():
    values = {
        "author": "Brandon Sanderson", "title": "Some Standalone Book", "year": "2010",
        "series": "", "series_index": "",
    }
    template = "{author}/[{series}/]{year} - {title}[ ({series} #{series_index})]"
    result = render_template(template, values)
    assert result == "Brandon Sanderson/2010 - Some Standalone Book"


def test_escaped_brackets_render_as_literal_outside_optional_section():
    """\\[ and \\] outside any optional section are literal characters, not
    the optional-section syntax.
    """
    result = render_template("{title} \\[literal\\]", {"title": "Some Book"})
    assert result == "Some Book [literal]"


def test_escaped_brackets_nested_inside_optional_section_when_present():
    """The Cradle-library case: literal brackets wrapping series/index,
    nested inside the real optional-drop brackets.
    """
    result = render_template(
        "{title}[  \\[{series} {series_index}\\]]",
        {"title": "Blackflame", "series": "Cradle", "series_index": "3"},
    )
    assert result == "Blackflame  [Cradle 3]"


def test_escaped_brackets_nested_inside_optional_section_dropped_when_empty():
    """When the referenced placeholder is empty, the whole bracketed span -
    escaped literal brackets included - is dropped, matching the existing
    "drop the whole span" semantics.
    """
    result = render_template(
        "{title}[  \\[{series} {series_index}\\]]",
        {"title": "Blackflame", "series": "", "series_index": ""},
    )
    assert result == "Blackflame"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('Title: The "Best" Book?', "Title The Best Book"),
        ("A/B\\C", "ABC"),
        ("  Trailing dot.  ", "Trailing dot"),
        ("", "Untitled"),
        ("....", "Untitled"),
    ],
)
def test_sanitize_path_component_strips_invalid_chars(raw, expected):
    assert sanitize_path_component(raw) == expected


def test_build_standalone_destination_under_output_dir(monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_DIR", __import__("pathlib").Path("/data/output"))
    monkeypatch.setattr(
        config, "STANDALONE_FILENAME_TEMPLATE", "{author} - {title}[ ({series} #{series_index})]"
    )
    dest = build_standalone_destination(
        {"author": "Andy Weir", "title": "Project Hail Mary", "series": "", "series_index": ""}
    )
    assert dest == config.OUTPUT_DIR / "Andy Weir - Project Hail Mary.m4b"


def test_build_library_destination_folder_and_file(monkeypatch):
    from pathlib import Path

    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_DIR", Path("/data/output"))
    monkeypatch.setattr(
        config, "LIBRARY_FOLDER_TEMPLATE", "{author}/[{series}/]{year} - {title}[ ({series} #{series_index})]"
    )
    monkeypatch.setattr(config, "LIBRARY_FILENAME_TEMPLATE", "{title} ({year})[ ({series} #{series_index})]")

    folder, file_path = build_library_destination(
        {
            "author": "Brandon Sanderson", "title": "The Final Empire", "year": "2006",
            "series": "Mistborn", "series_index": "1",
        }
    )
    assert folder == Path("/data/output/Brandon Sanderson/Mistborn/2006 - The Final Empire (Mistborn #1)")
    assert file_path == folder / "The Final Empire (2006) (Mistborn #1).m4b"


def test_build_library_destination_sanitizes_each_path_segment(monkeypatch):
    """A colon in the author name (e.g. "Sub: Title") must not be
    interpreted as introducing a new path segment or corrupting the path -
    each rendered path component is sanitized independently.
    """
    from pathlib import Path

    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_DIR", Path("/data/output"))
    monkeypatch.setattr(config, "LIBRARY_FOLDER_TEMPLATE", "{author}/{year} - {title}")
    monkeypatch.setattr(config, "LIBRARY_FILENAME_TEMPLATE", "{title}")

    folder, file_path = build_library_destination(
        {"author": "A: B", "title": 'Weird "Title"', "year": "2020", "series": "", "series_index": ""}
    )
    assert "A: B" not in str(folder)
    assert folder == Path("/data/output/A B/2020 - Weird Title")


def test_place_output_library_mode_disambiguates_filename_on_collision(tmp_path, monkeypatch):
    """Guards against place_output() silently overwriting an unrelated
    existing file - a real risk now that OUTPUT_DIR can point directly at
    an already-populated library rather than a throwaway staging folder
    (e.g. re-converting a book you already own, or two sources matching
    the same author/title/year). The job's destination_path is exactly
    this return value (app/queue.py), so a disambiguated name here is what
    shows up in the "Done" queue's path box too.
    """
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "OUTPUT_MODE", "library")
    monkeypatch.setattr(config, "LIBRARY_FOLDER_TEMPLATE", "{author}/{year} - {title}")
    monkeypatch.setattr(config, "LIBRARY_FILENAME_TEMPLATE", "{title} ({year})")
    monkeypatch.setattr(config, "WRITE_SIDECAR_FILES", False)

    meta = {"author": "Andy Weir", "title": "Project Hail Mary", "year": "2021", "series": "", "series_index": ""}

    existing_folder = tmp_path / "Andy Weir" / "2021 - Project Hail Mary"
    existing_folder.mkdir(parents=True)
    existing_file = existing_folder / "Project Hail Mary (2021).m4b"
    existing_file.write_bytes(b"already here")

    work_m4b = tmp_path / "work.m4b"
    work_m4b.write_bytes(b"newly converted")

    dest = place_output(work_m4b, meta)

    assert dest == existing_folder / "Project Hail Mary (2021) (2).m4b"
    assert existing_file.read_bytes() == b"already here"
    assert dest.read_bytes() == b"newly converted"
    assert not work_m4b.exists()


def test_place_output_library_mode_keeps_incrementing_past_first_collision(tmp_path, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "OUTPUT_MODE", "library")
    monkeypatch.setattr(config, "LIBRARY_FOLDER_TEMPLATE", "{author}/{year} - {title}")
    monkeypatch.setattr(config, "LIBRARY_FILENAME_TEMPLATE", "{title} ({year})")
    monkeypatch.setattr(config, "WRITE_SIDECAR_FILES", False)

    meta = {"author": "Andy Weir", "title": "Project Hail Mary", "year": "2021", "series": "", "series_index": ""}

    folder = tmp_path / "Andy Weir" / "2021 - Project Hail Mary"
    folder.mkdir(parents=True)
    (folder / "Project Hail Mary (2021).m4b").write_bytes(b"1")
    (folder / "Project Hail Mary (2021) (2).m4b").write_bytes(b"2")

    work_m4b = tmp_path / "work.m4b"
    work_m4b.write_bytes(b"3")

    dest = place_output(work_m4b, meta)

    assert dest == folder / "Project Hail Mary (2021) (3).m4b"


def test_place_output_standalone_mode_disambiguates_filename_on_collision(tmp_path, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{author} - {title}")

    meta = {"author": "Andy Weir", "title": "Project Hail Mary", "series": "", "series_index": ""}

    existing = tmp_path / "Andy Weir - Project Hail Mary.m4b"
    existing.write_bytes(b"already here")

    work_m4b = tmp_path / "work.m4b"
    work_m4b.write_bytes(b"newly converted")

    dest = place_output(work_m4b, meta)

    assert dest == tmp_path / "Andy Weir - Project Hail Mary (2).m4b"
    assert existing.read_bytes() == b"already here"


def test_place_output_no_collision_uses_plain_name(tmp_path, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{author} - {title}")

    meta = {"author": "Andy Weir", "title": "Project Hail Mary", "series": "", "series_index": ""}
    work_m4b = tmp_path / "work.m4b"
    work_m4b.write_bytes(b"data")

    dest = place_output(work_m4b, meta)

    assert dest == tmp_path / "Andy Weir - Project Hail Mary.m4b"
