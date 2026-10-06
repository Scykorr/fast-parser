import asyncio
import json
from copy import deepcopy
import httpx
import pytest
from fast_parser.adapters import SchemaError
from fast_parser.collector import Collector, Deferred
from fast_parser.german import parse_page, page_url
from fast_parser.sources import DFL_SOURCE, source_for_comp


def state(division="bundesliga", week=5, **changes):
    cid = "DFL-COM-000001" if division == "bundesliga" else "DFL-COM-000002"
    row = dict(matchId="DFL-MAT-TEST1", dflDatalibraryCompetitionId=cid,
               dflDatalibrarySeasonId="season", matchday=week, matchDateFixed=True,
               plannedKickOff="2026-10-09T18:30:00+0000", matchStatus="PRE_MATCH",
               teams={side:dict(dflDatalibraryClubId=side,nameFull=name)
                      for side,name in [("home","FC Home"),("away","FC Away")]})
    row.update(changes)
    return {"config": {"b": {cid:dict(season=dict(name="2026-2027",seasonId="season"),
                                    matchday=dict(matchdayNumber=week))}},
            f"_getDataFromFirebase-/all/{cid}/seasons/season/matchesmatchday{week}99":[row]}


def html(data):
    return '<script id="ng-state" type="application/json">'+json.dumps(data)+'</script>'


@pytest.mark.parametrize("division",["bundesliga","2bundesliga"])
def test_official_names_and_utc(division):
    comp, rows, *rest = parse_page(html(state(division)),division,"2026")
    m = rows[0]
    assert source_for_comp(comp["id"]) == DFL_SOURCE
    assert m.home_team["name"] == "FC Home" and m.away_team["name"] == "FC Away"
    assert m.kickoff_at == "2026-10-09T18:30:00+00:00"
    assert m.finished_at is None and m.elapsed_minutes is None
    assert m.source_url == page_url(division,"2026",5)


@pytest.mark.parametrize("changes",[
    dict(matchday=6),dict(dflDatalibraryCompetitionId="other"),
    dict(dflDatalibrarySeasonId="other"),dict(plannedKickOff="2026-10-09T18:30:00"),
    dict(matchStatus="FINAL_WHISTLE"),
    dict(matchStatus="FINAL_WHISTLE",score=dict(home=dict(fulltime=True),away=dict(fulltime=0)))])
def test_invalid_contract_quarantined(changes):
    with pytest.raises(SchemaError):parse_page(html(state(**changes)),"bundesliga","2026")


def test_final_and_unknown_are_not_invented():
    d = state(matchStatus="FINAL_WHISTLE",score=dict(home=dict(fulltime=2),away=dict(fulltime=1)))
    m = parse_page(html(d),"bundesliga","2026")[1][0]
    assert (m.status,m.score_home,m.score_away)==("finished",2,1)
    assert m.finished_at is None and m.period is None
    m = parse_page(html(state(matchStatus="NEW_PHASE",matchDateFixed=False)),"bundesliga","2026")[1][0]
    assert m.status == "unknown" and m.kickoff_at is None and m.scheduled_date == "2026-10-09"


def test_wrong_page_season_and_duplicate_rejected():
    d=state();key=next(k for k in d if k.startswith("_get"));d[key].append(deepcopy(d[key][0]))
    with pytest.raises(SchemaError):parse_page(html(d),"bundesliga","2026")
    for division,year,week in [("2bundesliga","2026",None),("bundesliga","2025",None),("bundesliga","2026",6)]:
        with pytest.raises(SchemaError):parse_page(html(state()),division,year,week)


def test_worker_persists_plan_shared_quota_and_official_origin(store,now,monkeypatch):
    monkeypatch.setattr("fast_parser.collector.utcnow",lambda:now)
    async def run():
        c=Collector(store);await c.client.aclose()
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=html(state()))))
        try:
            assert await c.sync_german()==1
            plan=json.loads(store.get_meta("german_plan"))
            assert plan["2bundesliga"]["due"]==0 and "bundesliga:4" in plan
            m=store.match("dfl:2026:DFL-MAT-TEST1",now)
            assert m["official_source"] and m["home_team"]["name"]=="FC Home"
            with pytest.raises(Deferred):await c.sync_german()
        finally:await c.client.aclose()
    asyncio.run(run())
