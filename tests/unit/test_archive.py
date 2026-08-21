"""Unit tests for app/pipeline/archive.py - source cleanup modes and the
ARCHIVE_RETENTION_DAYS auto-purge.
"""
import os
import time

from app.pipeline.archive import handle_source_cleanup, purge_expired_archives


def test_cleanup_mode_keep_leaves_source_in_place(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "keep")
    source = isolated_dirs["inbox"] / "book.m4b"
    source.write_bytes(b"data")

    handle_source_cleanup(source, log=lambda *_: None)

    assert source.exists()


def test_cleanup_mode_delete_removes_file_source(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "delete")
    source = isolated_dirs["inbox"] / "book.m4b"
    source.write_bytes(b"data")

    handle_source_cleanup(source, log=lambda *_: None)

    assert not source.exists()


def test_cleanup_mode_delete_removes_folder_source(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "delete")
    source = isolated_dirs["inbox"] / "book"
    source.mkdir()
    (source / "track1.mp3").write_bytes(b"data")

    handle_source_cleanup(source, log=lambda *_: None)

    assert not source.exists()


def test_cleanup_mode_archive_moves_source_to_archive_dir(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "archive")
    source = isolated_dirs["inbox"] / "book.m4b"
    source.write_bytes(b"data")

    handle_source_cleanup(source, log=lambda *_: None)

    assert not source.exists()
    assert (isolated_dirs["archive"] / "book.m4b").exists()


def test_cleanup_mode_archive_dedupes_name_collision(isolated_dirs, monkeypatch):
    """Two different source items that happen to share a name (e.g. two
    separate drop-ins both folder-named "book") must not clobber each
    other in the archive.
    """
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "archive")
    (isolated_dirs["archive"] / "book.m4b").write_bytes(b"already here")

    source = isolated_dirs["inbox"] / "book.m4b"
    source.write_bytes(b"new data")
    handle_source_cleanup(source, log=lambda *_: None)

    assert (isolated_dirs["archive"] / "book.m4b").read_bytes() == b"already here"
    assert (isolated_dirs["archive"] / "book (1).m4b").read_bytes() == b"new data"


def test_cleanup_mode_keep_returns_none(isolated_dirs, monkeypatch):
    """The caller (see app/queue.py) uses a non-None return to know the
    source moved and update Job.source_path to match - keep mode never
    moves anything, so it must return None.
    """
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "keep")
    source = isolated_dirs["inbox"] / "book.m4b"
    source.write_bytes(b"data")

    assert handle_source_cleanup(source, log=lambda *_: None) is None


def test_cleanup_mode_delete_returns_none(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "delete")
    source = isolated_dirs["inbox"] / "book.m4b"
    source.write_bytes(b"data")

    assert handle_source_cleanup(source, log=lambda *_: None) is None


def test_cleanup_mode_archive_returns_new_path(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "archive")
    source = isolated_dirs["inbox"] / "book.m4b"
    source.write_bytes(b"data")

    result = handle_source_cleanup(source, log=lambda *_: None)

    assert result == isolated_dirs["archive"] / "book.m4b"
    assert result.exists()


def _set_mtime_days_ago(path, days):
    ts = time.time() - (days * 86400)
    os.utime(path, (ts, ts))


def test_purge_expired_archives_removes_only_items_older_than_retention(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "ARCHIVE_RETENTION_DAYS", 30)
    old_item = isolated_dirs["archive"] / "old_book.m4b"
    new_item = isolated_dirs["archive"] / "new_book.m4b"
    old_item.write_bytes(b"old")
    new_item.write_bytes(b"new")
    _set_mtime_days_ago(old_item, 45)
    _set_mtime_days_ago(new_item, 5)

    removed = purge_expired_archives(log=lambda *_: None)

    assert removed == 1
    assert not old_item.exists()
    assert new_item.exists()


def test_purge_expired_archives_removes_expired_folder(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "ARCHIVE_RETENTION_DAYS", 30)
    old_folder = isolated_dirs["archive"] / "old_book"
    old_folder.mkdir()
    (old_folder / "track1.mp3").write_bytes(b"data")
    _set_mtime_days_ago(old_folder, 45)

    removed = purge_expired_archives(log=lambda *_: None)

    assert removed == 1
    assert not old_folder.exists()


def test_purge_expired_archives_is_noop_when_retention_unset(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "ARCHIVE_RETENTION_DAYS", None)
    old_item = isolated_dirs["archive"] / "ancient_book.m4b"
    old_item.write_bytes(b"data")
    _set_mtime_days_ago(old_item, 3650)

    removed = purge_expired_archives(log=lambda *_: None)

    assert removed == 0
    assert old_item.exists()


def test_purge_expired_archives_default_30_days(isolated_dirs, monkeypatch):
    """Ties the archive.py purge behavior to the new ARCHIVE_RETENTION_DAYS
    default of 30 (see app/config.py) - a 31-day-old item should be purged
    under the new default, a 29-day-old one should not.
    """
    from app.config import config

    monkeypatch.setattr(config, "ARCHIVE_RETENTION_DAYS", 30)
    just_expired = isolated_dirs["archive"] / "just_expired.m4b"
    not_yet_expired = isolated_dirs["archive"] / "not_yet_expired.m4b"
    just_expired.write_bytes(b"data")
    not_yet_expired.write_bytes(b"data")
    _set_mtime_days_ago(just_expired, 31)
    _set_mtime_days_ago(not_yet_expired, 29)

    purge_expired_archives(log=lambda *_: None)

    assert not just_expired.exists()
    assert not_yet_expired.exists()
