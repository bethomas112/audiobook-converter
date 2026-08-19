"""Unit tests for app/config.py's env-var parsing helpers (_bool, _int_or_none)
and the new ARCHIVE_RETENTION_DAYS=30 default.
"""
import importlib
import os

import pytest

from app.config import Config, _bool, _int_or_none


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True), ("true", True), ("True", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
        ("  true  ", True),
    ],
)
def test_bool_parsing(raw, expected):
    assert _bool(raw) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", None),
        ("0", None),
        ("30", 30),
        (" 45 ", 45),
    ],
)
def test_int_or_none_parsing(raw, expected):
    assert _int_or_none(raw) == expected


def test_archive_retention_days_defaults_to_30_when_unset(monkeypatch):
    """The generic default is now 30 days (previously unset/keep-forever) -
    see the .env.example / README change accompanying this. Only exercised
    when the env var is genuinely absent, matching how Docker Compose/.env
    actually behaves for a deployer who never sets it.
    """
    monkeypatch.delenv("ARCHIVE_RETENTION_DAYS", raising=False)
    cfg = Config()
    assert cfg.ARCHIVE_RETENTION_DAYS == 30


def test_archive_retention_days_explicit_zero_means_keep_forever(monkeypatch):
    monkeypatch.setenv("ARCHIVE_RETENTION_DAYS", "0")
    cfg = Config()
    assert cfg.ARCHIVE_RETENTION_DAYS is None


def test_archive_retention_days_explicit_blank_means_keep_forever(monkeypatch):
    """This is the shape Brady's own .env used to have (the var present but
    blank) before it was set to 30 - must still mean "keep forever" for
    anyone who deliberately wants that.
    """
    monkeypatch.setenv("ARCHIVE_RETENTION_DAYS", "")
    cfg = Config()
    assert cfg.ARCHIVE_RETENTION_DAYS is None


def test_archive_retention_days_explicit_override(monkeypatch):
    monkeypatch.setenv("ARCHIVE_RETENTION_DAYS", "7")
    cfg = Config()
    assert cfg.ARCHIVE_RETENTION_DAYS == 7


def test_paths_are_resolved_to_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INBOX_DIR", "relative/inbox")
    cfg = Config()
    assert cfg.INBOX_DIR.is_absolute()
