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
