"""Loads every env var listed in .env.example into a single Config object,
applying the same defaults. See .env.example for what each one does -
this module is deliberately just plumbing, not documentation.
"""
import os
from pathlib import Path


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int_or_none(value: str) -> int | None:
    value = value.strip()
    if not value or value == "0":
        return None
    return int(value)


class Config:
    def __init__(self):
        # Resolved to absolute paths: watchdog reports absolute event paths,
        # so INBOX_DIR must be absolute too or every event fails the
        # "is this under the inbox?" comparison in the watcher.
        self.INBOX_DIR = Path(os.environ.get("INBOX_DIR", "/data/inbox")).resolve()
        self.WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/work")).resolve()
        self.ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/data/archive")).resolve()
        self.OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output")).resolve()
        self.CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/data/config")).resolve()

        self.OUTPUT_MODE = os.environ.get("OUTPUT_MODE", "standalone")
        self.LIBRARY_FOLDER_TEMPLATE = os.environ.get(
            "LIBRARY_FOLDER_TEMPLATE",
            "{author}/[{series}/]{year} - {title}[ ({series} #{series_index})]",
        )
        self.LIBRARY_FILENAME_TEMPLATE = os.environ.get(
            "LIBRARY_FILENAME_TEMPLATE",
            "{title} ({year})[ ({series} #{series_index})]",
        )
        self.STANDALONE_FILENAME_TEMPLATE = os.environ.get(
            "STANDALONE_FILENAME_TEMPLATE",
            "{author} - {title}[ ({series} #{series_index})]",
        )
        self.WRITE_SIDECAR_FILES = _bool(os.environ.get("WRITE_SIDECAR_FILES", "false"))

        self.SOURCE_CLEANUP_MODE = os.environ.get("SOURCE_CLEANUP_MODE", "archive")
        self.ARCHIVE_RETENTION_DAYS = _int_or_none(os.environ.get("ARCHIVE_RETENTION_DAYS", ""))

        self.AUTO_START_PROCESSING = _bool(os.environ.get("AUTO_START_PROCESSING", "false"))
        self.AUTO_CONFIRM_METADATA = _bool(os.environ.get("AUTO_CONFIRM_METADATA", "false"))

        self.MIN_BITRATE_KBPS = int(os.environ.get("MIN_BITRATE_KBPS", "128"))

        self.METADATA_SOURCE = os.environ.get("METADATA_SOURCE", "audnexus")

        self.SILENCE_THRESHOLD_DB = os.environ.get("SILENCE_THRESHOLD_DB", "-30dB")
        self.SILENCE_MIN_DURATION_SEC = float(os.environ.get("SILENCE_MIN_DURATION_SEC", "1.5"))
        self.SILENCE_MIN_CHAPTER_SEC = float(os.environ.get("SILENCE_MIN_CHAPTER_SEC", "120"))

        self.WEB_UI_AUTH = os.environ.get("WEB_UI_AUTH", "none")
        self.WEB_UI_USERNAME = os.environ.get("WEB_UI_USERNAME", "")
        self.WEB_UI_PASSWORD = os.environ.get("WEB_UI_PASSWORD", "")

        self.SETTLE_WINDOW_SEC = float(os.environ.get("SETTLE_WINDOW_SEC", "10"))

    def ensure_dirs(self):
        for d in (self.INBOX_DIR, self.WORK_DIR, self.ARCHIVE_DIR, self.OUTPUT_DIR, self.CONFIG_DIR):
            d.mkdir(parents=True, exist_ok=True)


config = Config()
