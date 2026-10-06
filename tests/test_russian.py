import asyncio
import json
import re
from datetime import timedelta
import httpx
import pytest
from fastapi.testclient import TestClient
from fast_parser.adapters import SchemaError
from fast_parser.app import create_app
from fast_parser.collector import Collector, Deferred, SourceFailure
from fast_parser.domain import stamp
from fast_parser.russian import active_season, catalog, parse_page, page_url, RPL_REASON
from fast_parser.sources import FNL_SOURCE, RPL_SOURCE


def page(**changes):
    row=dict(matchId=1,leagueId=100,seasonId=1066,season="2026/2027",date="2026-10-09 17:00",
             status="FUTURE",statusExtended="FUTURE",matchTime="FUTURE",matchTimeStr=None,
             home=dict(teamId=1,name="Home",score=0),guest=dict(teamId=2,name="Away",score=0))
    row.update(changes)
    return dict(location=dict(limit=10,offset=0,count=1),matches=[dict(date="2026-10-09",matches=[row])])


def parse(data):
    return parse_page(data,"2026",1066,10,2026)


def test_season_and_moscow_clock_and_future_zero_placeholder():
    assert active_season(dict(leagueId=100,seasonId=1066,title="2026/2027"),"2026")==1066
    with pytest.raises(SchemaError):active_season(dict(leagueId=200,seasonId=1066,title="2026/2027"),"2026")
    comp,rows,nxt=parse(page());m=rows[0]
    assert comp["country"]=="Россия" and nxt is None
    assert m.kickoff_at=="2026-10-09T14:00:00+00:00" and m.home_team["name"]=="Home"
    assert m.score_home is None and m.score_away is None
    assert m.source_url=="https://fnl.pro/pari/matches"


@pytest.mark.parametrize("value",["2026-10-09","2026-10-09 00:00",None])
def test_unknown_clock_does_not_invent_midnight(value):
    m=parse(page(date=value))[1][0]
    assert m.kickoff_at is None
    assert m.scheduled_date == ("2026-10-09" if value else None)


@pytest.mark.parametrize("phase,status,period",[("START_FIRST_HALF","live","first_half"),
    ("END_FIRST_HALF","paused","half_time"),("START_SECOND_HALF","live","second_half")])
def test_explicit_live_phases_only(phase,status,period):
    m=parse(page(status="BEGIN",statusExtended=phase,matchTimeStr="47"))[1][0]
    assert (m.status,m.period,m.elapsed_minutes)==(status,period,47)
    m=parse(page(status="BEGIN",statusExtended="NEW",matchTimeStr="999"))[1][0]
    assert m.status=="unknown" and m.period is None and m.elapsed_minutes is None


@pytest.mark.parametrize("changes",[dict(leagueId=200),dict(seasonId=979),dict(season="2025/2026"),
    dict(date="2026-11-09 17:00"),dict(date="bad"),dict(matchId=True),
    dict(status="END",home=dict(teamId=1,name="Home",score=None))])
def test_wrong_contract_rejected(changes):
    with pytest.raises(SchemaError):parse(page(**changes))


def test_final_missing_exact_end_and_bad_paging():
    m=parse(page(status="END",statusExtended="END",home=dict(teamId=1,name="Home",score=2),guest=dict(teamId=2,name="Away",score=1)))[1][0]
    assert m.status=="finished" and m.score_home==2 and m.finished_at is None
    d=page();d["location"]["count"]=2
    with pytest.raises(SchemaError):parse(d)
    d=page();d["matches"][0]["matches"]*=2;d["location"]["count"]=2
    with pytest.raises(SchemaError):parse(d)


def test_worker_window_and_persistent_quota(store,now,monkeypatch):
    monkeypatch.setattr("fast_parser.collector.utcnow",lambda:now)
    store.set_meta("fnl_season",json.dumps(dict(year="2026",season=1066,refresh_after=now.timestamp()+86400)))
    async def run():
        c=Collector(store);await c.client.aclose()
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=page())))
        try:
            assert await c.sync_russian()==1
            m=store.match("fnl:2026:1",now)
            assert m["official_source"] and m["away_team"]["name"]=="Away"
            assert json.loads(store.get_meta("fnl_plan"))["2026-10"]["due"]>now.timestamp()
            assert not store.reserve_request(FNL_SOURCE,30)
        finally:await c.client.aclose()
    asyncio.run(run())


def test_captcha_redirect_blocks_without_following(store):
    async def run():
        c=Collector(store);await c.client.aclose();seen=[]
        def response(r):
            seen.append(str(r.url));return httpx.Response(302,headers={"Location":"/tmgrdfrend/showcaptcha?token=hidden"})
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(response))
        try:
            with pytest.raises(SourceFailure,match="CAPTCHA"):await c.fetch(RPL_SOURCE,"https://premierliga.ru/matches/")
            assert len(seen)==1 and store.source(RPL_SOURCE)["state"]=="blocked"
            assert "hidden" not in store.source(RPL_SOURCE)["error"]
            with pytest.raises(Deferred):await c.fetch(RPL_SOURCE,"https://premierliga.ru/matches/")
        finally:await c.client.aclose()
    asyncio.run(run())


def test_rpl_catalog_is_explicitly_unavailable_not_empty_success(tmp_path):
    app=create_app(tmp_path/"app.sqlite3",start_worker=False)
    app.state.store.catalog(catalog("2026"))
    with TestClient(app) as client:
        rows=client.get("/api/v1/competitions").json()["items"]
        rpl=next(r for r in rows if r["id"].startswith("rpl:"))
        assert not rpl["collection_supported"] and not rpl["collection_allowed"]
        assert rpl["collection_reason"]==RPL_REASON
        fnl=next(r for r in rows if r["id"].startswith("fnl:"))
        assert fnl["collection_allowed"] and fnl["period_supported"]
        token=re.search(r'content="([^"]+)"',client.get("/").text).group(1)
        # Use the actual CSRF meta tag rather than the first page meta value.
        token=re.search(r'name="csrf-token" content="([^"]+)"',client.get("/").text).group(1)
        headers={"origin":"http://testserver","x-csrf-token":token}
        assert client.post("/api/v1/sync/rpl:premier-league:2026",headers=headers).status_code==422
        assert client.post("/api/v1/sources/rpl_official/reset",headers=headers).status_code==422


def test_paginated_month_has_strict_continuation_and_empty_month():
    d=page();d["location"]["count"]=11
    from copy import deepcopy
    row=d["matches"][0]["matches"][0]
    d["matches"][0]["matches"]=[dict(deepcopy(row),matchId=i) for i in range(1,11)]
    assert parse(d)[2]==10
    d=page();d["location"].update(offset=10,count=11)
    assert parse_page(d,"2026",1066,10,2026,10)[2] is None
    d=dict(location=dict(limit=10,offset=0,count=0),matches=[])
    assert parse(d)[1:]==([],None)


def test_future_date_only_outside_window_not_imported(store,now,monkeypatch):
    monkeypatch.setattr("fast_parser.collector.utcnow",lambda:now)
    store.set_meta("fnl_season",json.dumps(dict(year="2026",season=1066,refresh_after=now.timestamp()+86400)))
    async def run():
        c=Collector(store);await c.client.aclose()
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=page(date="2026-10-25"))))
        try:
            assert await c.sync_russian()==0
            assert store.match("fnl:2026:1",now) is None
        finally:await c.client.aclose()
    asyncio.run(run())
