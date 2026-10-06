import asyncio
import json
from datetime import timedelta
import httpx
import pytest
from fastapi.testclient import TestClient
from fast_parser.app import create_app
from fast_parser.collector import Collector
from fast_parser.domain import stamp
from fast_parser.official import parse_page
from fast_parser.adapters import SchemaError

def html(now, status="FullTime", score=1):
    page = dict(competition="primera-division", season="2026", gameweek={"week":8},
        gameweekList=[dict(week=8, date=stamp(now)), dict(week=9, date=stamp(now+timedelta(days=7)))],
        matches=[dict(id=99, status=status, date=stamp(now-timedelta(hours=1)),
            home_team=dict(id=1,name="Home"), away_team=dict(id=2,name="Away"), home_score=2, away_score=score)])
    return '<script id="__NEXT_DATA__">'+json.dumps(dict(props=dict(pageProps=page)))+'</script>'

def test_official_unknown_status_does_not_invent_live_or_final(now):
    _, rows, _, _ = parse_page(html(now,"UnexpectedLive"),"primera-division")
    assert rows[0].status == "unknown" and rows[0].raw_status == "UnexpectedLive"
    assert rows[0].finished_at is None and rows[0].elapsed_minutes is None
    with pytest.raises(SchemaError):
        parse_page(html(now,score=None),"primera-division")

def test_official_sync_imports_with_provenance_retention_and_shared_quota(store, now, monkeypatch):
    monkeypatch.setattr("fast_parser.collector.utcnow", lambda:now)
    async def run():
        c = Collector(store)
        await c.client.aclose()
        urls=[]
        def respond(request):
            urls.append(str(request.url))
            return httpx.Response(200,text=html(now))
        c.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await c.sync_official() == 1
            m=store.match("ll:99",now)
            assert m["official_source"] and m["source_url"] == urls[0]
            assert m["score_home"] == 2 and m["expires_at"] == stamp(now+timedelta(hours=72))
            assert not store.reserve_request("laliga_reference",30)
            assert any(c["id"] == "ll:primera-division:2026" for c in store.competitions())
        finally:
            await c.client.aclose()
    asyncio.run(run())

def test_official_only_worker_never_requests_legacy(store):
    async def run():
        c=Collector(store)
        await c.client.aclose()
        def reject(request):
            raise AssertionError("Legacy source requested")
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(reject))
        try:
            await c.source_cycle("openligadb")
            await c.source_cycle("skysports_html")
        finally:
            await c.client.aclose()
    asyncio.run(run())

def test_official_only_api_keeps_catalog_but_rejects_legacy_sync(tmp_path, comp):
    app=create_app(tmp_path/"official.sqlite3",start_worker=False)
    app.state.store.catalog([comp])
    with TestClient(app) as client:
        c=client.get("/api/v1/competitions").json()["items"][0]
        assert not c["official_source"] and not c["collection_allowed"]
        assert client.post("/api/v1/sync/"+comp["id"],headers={"Origin":"http://testserver", "X-CSRF-Token":app.state.csrf}).status_code == 422
