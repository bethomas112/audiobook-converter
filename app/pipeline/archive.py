"""Source cleanup after a successful conversion: archive, delete, or keep
the original files, per SOURCE_CLEANUP_MODE. Also handles the
ARCHIVE_RETENTION_DAYS auto-purge of old archived originals.
"""
import shutil
import time
from pathlib import Path

from app.config import config


def _unique_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    counter = 1
    while True:
        candidate = dest.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def handle_source_cleanup(source_path: Path, log) -> None:
    mode = config.SOURCE_CLEANUP_MODE

    if mode == "keep":
        log(f"SOURCE_CLEANUP_MODE=keep; leaving source in place at {source_path}.")
        return

    if mode == "delete":
        if source_path.is_dir():
            shutil.rmtree(source_path)
        else:
            source_path.unlink()
        log(f"Deleted source at {source_path}.")
        return

    # archive (default)
    dest = _unique_destination(config.ARCHIVE_DIR / source_path.name)
    shutil.move(str(source_path), str(dest))
    log(f"Archived source to {dest}.")


def purge_expired_archives(log=print) -> int:
    """Delete archived originals older than ARCHIVE_RETENTION_DAYS. Returns
    the count of items removed. No-op if retention is unset (keep forever).
    """
    if not config.ARCHIVE_RETENTION_DAYS:
        return 0

    cutoff = time.time() - (config.ARCHIVE_RETENTION_DAYS * 86400)
    removed = 0
    for item in config.ARCHIVE_DIR.iterdir():
        if item.stat().st_mtime < cutoff:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed += 1
            log(f"Purged expired archive item: {item}")
    return removed
