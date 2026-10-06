import asyncio
from datetime import timedelta

import httpx

from fast_parser.collector import Collector
from fast_parser.domain import Match, stamp, utcnow
from fast_parser.storage import Store


def test_worker_can_process_durable_pending_task_after_restart(store, comp, openliga_row):
    settings = store.settings()
    settings.enabled_leagues = ["bl1"]
    settings.html_enabled = False
    settings.official_only = False  # this test exercises the optional legacy adapter
    store.save_settings(settings)
    store.set_meta("catalog_updated_at", stamp(utcnow()))
    openliga_row["matchDateTimeUTC"] = stamp(utcnow() + timedelta(hours=1))
    openliga_row["matchIsFinished"] = False
    restarted = Store(store.path)

    async def run():
        c = Collector(restarted)
        await c.client.aclose()
        c.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[openliga_row])))
        task = asyncio.create_task(c.run())
        try:
            for _ in range(100):
                if restarted.match("ol:10") and restarted.competitions()[0]["state"] == "verified":
                    break
                await asyncio.sleep(0.02)
            assert restarted.match("ol:10") is not None
            assert restarted.competitions()[0]["state"] == "verified"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    asyncio.run(run())


def test_postponed_kickoff_updates_same_match(store, comp):
    now = utcnow()
    m = Match(id="ol:1", competition_id=comp["id"], source="openligadb", external_id="1", home_team={"id": "a", "name": "A"},
              away_team={"id": "b", "name": "B"}, kickoff_at=stamp(now), status="scheduled")
    store.upsert_matches([m], now)
    m.status, m.kickoff_at = "postponed", None
    store.upsert_matches([m], now)
    m.status, m.kickoff_at = "scheduled", stamp(now + timedelta(days=2))
    store.upsert_matches([m], now)
    page = store.matches(now=now)
    assert len(page["items"]) == 1
    assert page["items"][0]["kickoff_at"] == m.kickoff_at
    assert page["items"][0]["expires_at"] is None
