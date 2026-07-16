import os
import tempfile
import pytest

# Point the app at a throwaway DB BEFORE importing anything that reads settings.
_TMP_DB = os.path.join(tempfile.gettempdir(), "atlas_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
# API acceptance must never depend on local developer credentials or network
# availability. Individual tests that exercise a provider patch it explicitly.
os.environ["ZHIPU_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # context manager triggers startup (ensure_schema + seed_test_accounts)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_client(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "atlas123"})
    assert r.status_code == 200, r.text
    return client
