import asyncio
from copy import deepcopy
from datetime import timedelta
from urllib.parse import urlencode
import httpx
import pytest
from fast_parser.adapters import SchemaError
from fast_parser.collector import Collector, Deferred
from fast_parser.domain import stamp
from fast_parser.english import bootstrap, parse_pl, parse_efl, safe_next, efl_url, EFL_BASE, PL_SOURCE, EFL_SOURCE

def reference(now):
    return bootstrap(dict(events=[dict(deadline_time=stamp(now))],teams=[dict(id=i,name=f"Team {i}") for i in range(1,21)]))

def pl(now, **changes):
    row=dict(id=1, kickoff_time=stamp(now), team_h=1,team_a=2,team_h_score=2,team_a_score=1,
             finished=True,finished_provisional=True,started=True,minutes=90)
    row.update(changes)
    return row

def efl(now, **changes):
    attrs=dict(kickOffDateUTC=now.strftime('%Y-%m-%d %H:%M:%S'),TBC=None,competitionID=10,
               matchPeriod='FullTime',homeTeam=dict(name='Home',score=3),awayTeam=dict(name='Away',score=1))
    attrs.update(changes)
    return dict(data=[dict(type='match',id='g1',attributes=attrs)],meta=dict(totalCount=1,count=1),links={})

def test_pl_names_season_final_and_live_without_invented_end(now):
    ref=reference(now)
    comp, rows=parse_pl([pl(now)],ref)
    assert comp['country']=='Англия' and rows[0].home_team['name']=='Team 1'
    assert rows[0].id=='pl:2026:1' and rows[0].finished_at is None and rows[0].elapsed_minutes is None
    _,live=parse_pl([pl(now,finished=False,finished_provisional=False,minutes=32)],ref)
    assert live[0].status=='live' and live[0].elapsed_minutes==32 and live[0].period is None
    _,provisional=parse_pl([pl(now,finished=False)],ref)
    assert provisional[0].status=='unknown'

@pytest.mark.parametrize('changes',[{'team_a':99},{'team_a_score':None},{'team_h_score':True},{'finished':'yes'},{'kickoff_time':'2026-01-01T12:00:00Z'}])
def test_pl_rejects_bad_teams_scores_status_and_season(now,changes):
    with pytest.raises(SchemaError):
        parse_pl([pl(now,**changes)],reference(now))

def test_pl_duplicate_and_reference_mismatch(now):
    with pytest.raises(SchemaError):parse_pl([pl(now),pl(now)],reference(now))
    ref=reference(now);ref['season']='2025'
    with pytest.raises(SchemaError):parse_pl([pl(now)],ref)

def test_efl_explicit_utc_names_scores_tbc_unknown(now):
    url=efl_url('2026',now,now+timedelta(days=7))
    comp,rows,nxt=parse_efl(efl(now),'2026',url)
    assert comp['country']=='Англия' and rows[0].kickoff_at==stamp(now)
    assert rows[0].score_home==3 and rows[0].source_url==url and nxt is None
    _,rows,_=parse_efl(efl(now,TBC=True,matchPeriod='NewStatus'),'2026',url)
    assert rows[0].status=='unknown' and rows[0].kickoff_at is None and rows[0].elapsed_minutes is None
    with pytest.raises(SchemaError):parse_efl(efl(now,competitionID=11),'2026',url)
    d=efl(now);d['data'][0]['attributes']['awayTeam']['score']=None
    with pytest.raises(SchemaError):parse_efl(d,'2026',url)

def test_pagination_rejects_other_host_scope_same_page_and_missing_continuation(now):
    url=efl_url('2026',now,now+timedelta(days=7))
    nxt=url+'&page.number=2'
    assert safe_next(nxt,url)==nxt
    for wrong in [nxt.replace('https://multi-club-matches.webapi.gc.eflservices.co.uk','https://evil.invalid'),nxt.replace('competitionID=10','competitionID=11'),url+'&page.number=1']:
        with pytest.raises(SchemaError):safe_next(wrong,url)
    d=efl(now);d['meta']['totalCount']=101
    with pytest.raises(SchemaError):parse_efl(d,'2026',url)

def test_efl_imports_final_with_72h_and_provenance(store,now,monkeypatch):
    monkeypatch.setattr('fast_parser.collector.utcnow',lambda:now)
    async def run():
        c=Collector(store);await c.client.aclose()
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=efl(now))))
        try:
            assert await c.sync_english(EFL_SOURCE)==1
            m=store.match('efl:2026:g1',now)
            assert m['official_source'] and m['home_team']['name']=='Home'
            assert m['expires_at']==stamp(now+timedelta(hours=72))
            assert not store.reserve_request(EFL_SOURCE,30)
            assert await c.sync_english(EFL_SOURCE) is False
        finally:await c.client.aclose()
    asyncio.run(run())

def test_pl_bootstrap_cache_then_fixtures_and_no_player_payload_saved(store,now,monkeypatch):
    monkeypatch.setattr('fast_parser.collector.utcnow',lambda:now)
    async def run():
        calls=[]
        def respond(r):
            calls.append(r.url.path)
            d=dict(events=[dict(deadline_time=stamp(now))],teams=[dict(id=i,name=f'Team {i}') for i in range(1,21)],elements=['must not persist']) if 'bootstrap' in r.url.path else [pl(now)]
            return httpx.Response(200,json=d)
        c=Collector(store);await c.client.aclose();c.client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await c.sync_english(PL_SOURCE) is False
            assert 'must not persist' not in store.get_meta('pl_teams')
            with pytest.raises(Deferred):await c.sync_english(PL_SOURCE)
            # Advance the test quota without sleeping or changing production timing.
            with store.connect() as db:db.execute('UPDATE source_state SET next_allowed=0 WHERE name=?',(PL_SOURCE,))
            assert await c.sync_english(PL_SOURCE)==1
            assert store.match('pl:2026:1',now)['official_source']
            assert calls==['/api/bootstrap-static/','/api/fixtures/']
        finally:await c.client.aclose()
    asyncio.run(run())
