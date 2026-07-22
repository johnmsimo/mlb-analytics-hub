from __future__ import annotations

import os

os.environ.setdefault("AUTH_DISABLED", "1")

from app import app  # noqa: E402


def test_data_status_and_sources():
    client = app.test_client()
    status = client.get("/api/data/status")
    assert status.status_code == 200
    body = status.get_json()
    assert body["counts"]["teams"] >= 32
    assert "raw_ingest_records" in body["counts"]

    sources = client.get("/api/data/sources")
    assert sources.status_code == 200
    assert sources.get_json()["count"] >= 1


def test_quality_and_profiles():
    client = app.test_client()
    assert client.get("/api/data/quality").status_code == 200
    assert client.get("/api/data/teams/BUF/profile").status_code == 200
    assert client.get("/api/data/players/1/profile").status_code == 200
    assert client.get("/api/data/teams/NOPE/profile").status_code == 404
