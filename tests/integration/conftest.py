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
