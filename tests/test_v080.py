import asyncio
import sqlite3
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from fast_parser.app import create_app
from fast_parser.collector import Collector, SourceFailure
from fast_parser.domain import stamp, utcnow
from fast_parser.storage import Store
from test_retention import game


def test_final_result_and_expiry_survive_regression(store, comp, now):
    store.upsert_matches([game(comp, now, score_home=3)], now)
    store.upsert_matches([game(comp, now, status="unknown")], now + timedelta(hours=1))
    m = store.match("ol:1", now)
    assert m["status"] == "finished" and m["score_home"] == 3
    store.upsert_matches([game(comp, now, finished_at=stamp(now + timedelta(hours=2)))], now + timedelta(hours=3))
    assert store.match("ol:1", now)["expires_at"] == stamp(now + timedelta(hours=72))


def test_explicit_revision_can_retract_final(store, comp, now):
    store.upsert_matches([game(comp, now, source_updated_at=stamp(now))], now)
    store.upsert_matches([game(comp, now, status="scheduled", source_updated_at=stamp(now + timedelta(hours=1)))], now + timedelta(hours=1))
    assert store.match("ol:1", now)["status"] == "scheduled"


def test_settings_enable_latest_season_only(store, comp):
    store.catalog([{**comp, "id": "ol:bl1:2025", "season": "2025"}])
    settings = store.settings()
    settings.enabled_leagues = ["bl1"]
    store.save_settings(settings)
    assert {c["season"] for c in store.competitions() if c["enabled"]} == {"2026"}


def test_freshness_source_failure_and_age(store, comp, now):
    store.upsert_matches([game(comp, now, status="live")], now)
    assert not store.match("ol:1", now)["is_stale"]
    assert store.match("ol:1", now + timedelta(minutes=5))["is_stale"]
    store.source_error("openligadb", "offline", 60)
    assert store.match("ol:1", now)["is_stale"]


def test_change_cursor_same_second_deletion_and_expiry(store, comp, now):
    store.upsert_matches([game(comp, now)], now)
    store.upsert_matches([game(comp, now, score_home=2)], now)
    first = store.changes(limit=1, now=now)
    assert first["has_more"] and first["items"][0]["match"]["score_home"] == 2
    second = store.changes(after=first["next_after"], now=now)
    assert second["next_after"] > first["next_after"]
    # Logical expiration is visible before cleanup. Consumers must also honor expires_at.
    assert store.changes(now=now + timedelta(hours=72))["items"][0]["kind"] == "deleted"
    store.cleanup(now + timedelta(hours=72))
    assert store.changes(after=second["next_after"], now=now + timedelta(hours=72))["items"][0]["kind"] == "deleted"
    store.cleanup(now + timedelta(days=11))
    with pytest.raises(ValueError, match="истёк"):
        store.changes(after=0, now=now + timedelta(days=11))


def test_api_changes_contract_and_validation(tmp_path, comp):
    app = create_app(tmp_path / "api.sqlite3", start_worker=False)
    store = app.state.store
    store.catalog([comp])
    store.upsert_matches([game(comp, utcnow())])
    with TestClient(app) as client:
        result = client.get("/api/v1/changes").json()
        assert result["items"][0]["match"]["home_team"]["name"] == "A"
        assert client.get("/api/v1/changes?after=-1").status_code == 422
        store.set_meta("changes_floor", "1")
        assert client.get("/api/v1/changes").status_code == 410


def test_v1_migration_backup_and_future_schema_rejected(tmp_path):
    path = tmp_path / "migrate.sqlite3"
    Store(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE changes")
        db.execute("PRAGMA user_version=1")
    migrated = Store(path)
    assert path.with_suffix(".v1.bak").exists()
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="новой"):
        Store(path)


def test_unresolved_outside_window_is_rechecked(store, comp, openliga_row):
    now = utcnow()
    m = game(comp, now, status="unknown")
    m.id, m.external_id = "ol:10", "10"
    m.kickoff_at = stamp(now - timedelta(days=10))
    store.upsert_matches([m], now - timedelta(days=10))
    assert store.unresolved(now)[0]["id"] == "ol:10"
    openliga_row["matchDateTimeUTC"] = m.kickoff_at
    async def run():
        collector = Collector(store)
        await collector.client.aclose()
        collector.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[openliga_row])))
        try:
            await collector.sync_league(comp)
            assert store.match("ol:10")["status"] == "finished"
        finally:
            await collector.client.aclose()
    asyncio.run(run())


def test_total_deadline_records_failure(store):
    async def run():
        collector = Collector(store)
        collector.request_deadline_seconds = .02
        async def stalled(source, url):
            await asyncio.sleep(1)
        collector._fetch = stalled
        try:
            with pytest.raises(SourceFailure, match="таймаут"):
                await collector.fetch("openligadb", "https://example.invalid")
            assert store.source("openligadb")["failures"] == 1
        finally:
            await collector.client.aclose()
    asyncio.run(run())


def test_slow_source_does_not_block_other_source_or_shutdown(store):
    async def run():
        collector = Collector(store)
        slow_started, fast_started = asyncio.Event(), asyncio.Event()
        async def cycle(source):
            if source == "openligadb":
                slow_started.set()
                await asyncio.sleep(60)
            else:
                fast_started.set()
        collector.source_cycle = cycle
        task = asyncio.create_task(collector.run())
        try:
            await asyncio.wait_for(slow_started.wait(), 2)
            await asyncio.wait_for(fast_started.wait(), 2)
            assert store.get_meta("worker_heartbeat")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert collector.client.is_closed
    asyncio.run(run())


def test_html_unresolved_day_outside_window_is_rechecked(store, comp):
    import json
    from unittest.mock import patch
    from zoneinfo import ZoneInfo
    now = utcnow()
    sky_comp = {**comp, "id": "sky:test:2026", "shortcut": "sky:test"}
    store.catalog([sky_comp])
    m = game(sky_comp, now, status="unknown")
    m.id, m.external_id, m.source = "sky:10", "10", "skysports_html"
    m.kickoff_at = stamp(now - timedelta(days=10))
    store.upsert_matches([m], now - timedelta(days=10))
    settings = store.settings()
    settings.enabled_leagues = ["bl1"]
    settings.lookback_days, settings.lookahead_days = 0, 1
    store.save_settings(settings)
    today = now.astimezone(ZoneInfo("Europe/London")).date()
    store.set_meta("html_due", json.dumps({(today + timedelta(days=i)).isoformat(): now.timestamp()+3600 for i in range(3)}))
    m.status = "finished"
    async def run():
        collector = Collector(store)
        requested = []
        async def fetch(source, url):
            requested.append(url)
            return "synthetic fixture"
        collector.fetch = fetch
        try:
            with patch("fast_parser.collector.parse_sky", return_value=([sky_comp], [m])):
                await collector.sync_html()
            assert requested[0].endswith((today - timedelta(days=10)).isoformat())
            assert store.match(m.id)["status"] == "finished"
        finally:
            await collector.client.aclose()
    asyncio.run(run())
