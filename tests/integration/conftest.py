"""Shared fixtures for tests/integration/ - see individual fixtures for
what each provides.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.routes import router


@pytest.fixture
def client(isolated_dirs):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_real_audnexus(monkeypatch):
    """confirm_metadata() refreshes the chapter preview for the confirmed
    asin, so any integration test confirming an asin would otherwise hit
    the real audnexus API. Tests that care monkeypatch get_chapters
    themselves, which overrides this default.
    """
    from app.pipeline import metadata as metadata_mod

    monkeypatch.setattr(metadata_mod, "get_chapters", lambda asin, region=None: [])
