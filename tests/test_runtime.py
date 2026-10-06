import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from fast_parser.app import create_app
from fast_parser.collector import Collector, Deferred, SourceFailure
from fast_parser.config import Settings
from fast_parser.domain import Match, stamp, utcnow


def test_source_limit_and_lease_survive_restart(store):
    assert store.reserve_request("openligadb", 100)
    assert not store.reserve_request("openligadb", 100)
    assert store.lease("collector", "a", 60)
    assert not store.lease("collector", "b", 60)
    from fast_parser.storage import Store
    restarted = Store(store.path)
    assert not restarted.reserve_request("openligadb", 100)
    assert not restarted.lease("collector", "b", 60)
    store.release("a")
    assert store.lease("collector", "b", 60)


@pytest.mark.parametrize("response,blocked", [(httpx.Response(429, headers={"Retry-After": "600"}), False),
                                             (httpx.Response(403), True), (httpx.Response(200, text="captcha-container"), True),
                                             (httpx.Response(503), False)])
def test_http_failures_and_retry_after(store, response, blocked):
    async def run():
        collector = Collector(store)
        await collector.client.aclose()
        collector.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response))
        with pytest.raises(SourceFailure):
            await collector.fetch("openligadb", "https://api.openligadb.de/test")
        state = store.source("openligadb")
        assert state["state"] == ("blocked" if blocked else "open")
        if response.status_code == 429:
            assert state["next_allowed"] >= utcnow().timestamp() + 590
        with pytest.raises(Deferred):
            await collector.fetch("openligadb", "https://api.openligadb.de/test")
        await collector.client.aclose()
    asyncio.run(run())


def test_batch_league_one_http_request(store, comp, openliga_row, now):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        rows = [{**openliga_row, "matchID": i, "matchIsFinished": False, "matchDateTimeUTC": stamp(utcnow() + timedelta(hours=1))} for i in range(100)]
        return httpx.Response(200, json=rows)
    async def run():
        c = Collector(store)
        await c.client.aclose()
        c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert await c.sync_league(comp) == 100
        await c.client.aclose()
    asyncio.run(run())
    assert count == 1


def test_settings_timezone_absolute_and_midnight_window():
    with pytest.raises(ValueError):
        Settings(timezone="wrong-zone")
    with pytest.raises(ValueError):
        Settings(mode="absolute", date_from="2026-10-06", date_to="2026-10-07")
    s = Settings(timezone="UTC", active_windows=["23:00-01:00"])
    from datetime import UTC, datetime
    assert s.in_window(datetime(2026, 10, 6, 23, 30, tzinfo=UTC))
    assert s.in_window(datetime(2026, 10, 7, 0, 30, tzinfo=UTC))
    assert not s.in_window(datetime(2026, 10, 7, 2, tzinfo=UTC))


def test_api_csrf_names_filter_validation_and_expiration(tmp_path, comp):
    app = create_app(tmp_path / "api.sqlite3", start_worker=False)
    store = app.state.store
    store.catalog([comp])
    now = utcnow()
    m = Match(id="ol:1", competition_id=comp["id"], source="openligadb", external_id="1", home_team={"id": "1", "name": "<script>"},
              away_team={"id": "2", "name": "B"}, kickoff_at=stamp(now), status="scheduled")
    store.upsert_matches([m], now)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/docs").status_code == 200
        assert "/api/v1/matches" in client.get("/openapi.json").json()["paths"]
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/countries").json()["items"][0]["id"] == "Германия"
        result = client.get("/api/v1/matches", params={"competition_id": comp["id"]}).json()
        assert result["items"][0]["home_team"]["name"] == "<script>"
        assert client.get("/api/v1/matches?limit=201").status_code == 422
        assert client.get("/api/v1/matches?date_from=2026-10-06").status_code == 422
        assert client.get("/api/v1/matches?cursor=bad").status_code == 422
        data = store.settings().model_dump()
        assert client.put("/api/v1/settings", json=data).status_code == 403
        headers = {"Origin": "http://testserver", "X-CSRF-Token": app.state.csrf}
        data["enabled_leagues"] = ["bl1"]
        assert client.put("/api/v1/settings", json=data, headers=headers).status_code == 200
        assert client.post("/api/v1/sync/ol:bl1:2026", headers=headers).status_code == 202
        headers["Origin"] = "https://evil.example"
        assert client.put("/api/v1/settings", json=data, headers=headers).status_code == 403
        m.status, m.finished_at = "finished", stamp(now - timedelta(hours=73))
        store.upsert_matches([m], now)
        assert client.get("/api/v1/matches/ol:1").status_code == 404
        assert client.get("/api/v1/matches").json()["items"] == []


def test_invalid_batch_keeps_last_good_data(store, comp, openliga_row):
    async def run():
        c = Collector(store)
        await c.client.aclose()
        c.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"login": True})))
        with pytest.raises(SourceFailure):
            await c.sync_league(comp)
        assert "Карантин" in store.source("openligadb")["error"]
        await c.client.aclose()
    asyncio.run(run())
