import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fast_parser.app import create_app
from fast_parser.storage import Store
from fast_parser.verification import CoverageVerification


def checked(**fields):
    return CoverageVerification(status="verified", fixtures=True, results=True,
                                scope="Rounds 1–3", evidence="Official league fixture comparison", **fields)


def test_successful_collection_does_not_verify_coverage(store, comp):
    store.mark_sync(comp["id"], True, 0, 10)
    row = store.competitions()[0]
    assert row["collection_state"] == "available"
    assert row["coverage_verification"]["status"] == "unverified"


def test_verification_survives_collection_error_catalog_and_restart(store, comp):
    store.save_verification(comp["id"], checked())
    store.mark_sync(comp["id"], False, 0, error="offline")
    store.catalog([comp])
    restarted = Store(store.path)
    row = restarted.competitions()[0]
    assert row["coverage_verification"]["status"] == "verified"
    assert row["coverage_verification"]["updated_at"]
    assert row["collection_state"] == "degraded"


def test_new_season_and_new_source_start_unverified(store, comp):
    store.save_verification(comp["id"], checked())
    store.catalog([{**comp, "id": "ol:bl1:2027", "season": "2027"},
                   {**comp, "id": "sky:bl1:2026", "shortcut": "sky:bl1"}])
    rows = {c["id"]: c for c in store.competitions()}
    assert len(rows) == 3
    assert rows[comp["id"]]["coverage_verification"]["status"] == "verified"
    assert rows["ol:bl1:2027"]["coverage_verification"]["status"] == "unverified"
    assert rows["sky:bl1:2026"]["coverage_verification"]["status"] == "unverified"


@pytest.mark.parametrize("payload", [
    {"status": "verified"},
    {"status": "partial", "fixtures": True},
    {"status": "verified", "fixtures": True, "scope": "test", "evidence": "test"},
    {"status": "unverified", "live": True},
])
def test_unsubstantiated_labels_rejected(payload):
    with pytest.raises(ValidationError):
        CoverageVerification(**payload)


def test_verification_api_csrf_and_coverage_counts(tmp_path, comp):
    app = create_app(tmp_path / "test.sqlite3", start_worker=False)
    app.state.store.catalog([comp])
    url = f"/api/v1/competitions/{comp['id']}/verification"
    headers = {"Origin": "http://testserver", "X-CSRF-Token": app.state.csrf}
    with TestClient(app) as client:
        assert client.put(url, json=checked().model_dump()).status_code == 403
        assert client.put(url, json=checked().model_dump(), headers=headers).status_code == 200
        assert client.get("/api/v1/diagnostics").json()["coverage"]["verification"]["verified"] == 1
        assert client.put(url, json={"status": "verified"}, headers=headers).status_code == 422
        assert client.put("/api/v1/competitions/absent/verification", json=checked().model_dump(), headers=headers).status_code == 404
        assert client.put(url, json={"status": "unverified"}, headers=headers).status_code == 200
        assert len(client.get("/api/v1/competitions").json()["items"]) == 1


def test_v2_migration_keeps_catalog_and_defaults_to_unverified(tmp_path, comp):
    path = tmp_path / "v2.sqlite3"
    store = Store(path)
    store.catalog([comp])
    store.mark_sync(comp["id"], True, 0, 10)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE coverage_verification")
        db.execute("PRAGMA user_version=2")
    upgraded = Store(path)
    assert path.with_suffix(".v2.bak").exists()
    row = upgraded.competitions()[0]
    assert row["count"] == 10 and row["coverage_verification"]["status"] == "unverified"
