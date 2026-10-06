import asyncio
from copy import deepcopy
from datetime import timedelta
import httpx
import pytest
from fast_parser.adapters import SchemaError
from fast_parser.collector import Collector, Deferred
from fast_parser.domain import stamp
from fast_parser.italian import SA_COMP, parse_sa, select_season, parse_sb, sb_url
from fast_parser.sources import SA_SOURCE, SB_SOURCE, source_for_comp

SEASON='serie-a::Football_Season::'+'a'*32

def sa(now, **changes):
    row=dict(matchId='match1',seasonId=SEASON,status='FINISHED',phase='FULL_TIME',
             home=dict(teamId='h',shortName='Home',officialName='Home',isTeamFake=False),
             away=dict(teamId='a',shortName='Away',officialName='Away',isTeamFake=False),
             matchDateUtc=stamp(now-timedelta(hours=1)),isUnknownKickOffTime=False,homeScorePush=2,awayScorePush=1)
    row.update(changes)
    return dict(competition=dict(competitionId=SA_COMP,seasonId=SEASON,seasonName='2026/2027'),matches=[row])

def sb(day='9 ottobre 2026', time='20:30', finished=False, week=6, year=2026):
    state='giocata' if finished else ''
    goals='<div class="gol-home">2</div><div class="gol-away">1</div>' if finished else '<div class="gol">20:30</div>'
    return f'''<select><option value="/seriebkt/calendario/{year}-{year+1}/stagione-regolare/{week}" selected>Giornata</option>
      <option value="/seriebkt/calendario/{year}-{year+1}/stagione-regolare/{week+1}">Next</option></select>
      <div class="matchday-list"><div class="club-home"><div class="club-label"><span>Home</span><span>HOM</span></div></div>
      <div class="club-away"><div class="club-label"><span>Away</span><span>AWA</span></div></div>
      <div class="stato {state}"><div class="stato-label">{'Risultato' if finished else 'Ore'}</div>
      <span class="visually-hidden">venerdì, {day}, Home - Away, ore: {time}</span>{goals}</div>
      <a data-match-id="b1" href="/seriebkt/partita/{year}-{year+1}/stagione-regolare/home-vs-away"></a></div>'''

def test_sa_season_discovery_and_final_provenance(now):
    ref=dict(seasons=[dict(competitionId=SA_COMP,seasonId=SEASON,seasonName='2026/2027')])
    assert select_season(ref,'2026')==SEASON
    comp,rows=parse_sa(sa(now),'2026',SEASON)
    assert comp['country']=='Италия' and rows[0].home_team['name']=='Home'
    assert rows[0].score_home==2 and rows[0].finished_at is None and rows[0].elapsed_minutes is None
    assert rows[0].source_url.startswith('https://api-sdp.legaseriea.it/')
    with pytest.raises(SchemaError):select_season(ref,'2025')

@pytest.mark.parametrize('changes',[dict(seasonId='other'),dict(awayScorePush=None),dict(homeScorePush=True),dict(matchDateUtc='2026-01-01T12:00:00Z')])
def test_sa_rejects_wrong_season_and_invalid_final(now,changes):
    with pytest.raises(SchemaError):parse_sa(sa(now,**changes),'2026',SEASON)

def test_sa_unknown_status_and_clock_remain_unknown(now):
    _,rows=parse_sa(sa(now,status='NEW_LIVE',isUnknownKickOffTime=True),'2026',SEASON)
    assert rows[0].status=='unknown' and rows[0].kickoff_at is None and rows[0].scheduled_date
    assert rows[0].elapsed_minutes is None and rows[0].period is None
    data=sa(now);data['matches']*=2
    with pytest.raises(SchemaError):parse_sa(data,'2026',SEASON)

def test_sa_real_date_only_z_format_does_not_quarantine_valid_round(now):
    data=sa(now)
    future=deepcopy(data['matches'][0])
    future.update(matchId='future',status='UPCOMING',phase='PRE_MATCH',matchDateUtc='2027-01-16Z',isUnknownKickOffTime=True,homeScorePush=None,awayScorePush=None)
    data['matches'].append(future)
    _,rows=parse_sa(data,'2026',SEASON)
    assert rows[0].status=='finished' and rows[1].kickoff_at is None
    assert rows[1].scheduled_date=='2027-01-16'
    future['isUnknownKickOffTime']=False
    with pytest.raises(SchemaError):parse_sa(data,'2026',SEASON)

@pytest.mark.parametrize('day,year,expected',[('9 ottobre 2026',2026,'2026-10-09T18:30:00+00:00'),('9 gennaio 2027',2026,'2027-01-09T19:30:00+00:00')])
def test_sb_rome_timezone_in_summer_and_winter(day,year,expected):
    comp,rows,week,available,first,last=parse_sb(sb(day=day,year=year),str(year),6)
    assert rows[0].kickoff_at==expected and rows[0].home_team['name']=='Home'
    assert rows[0].score_home is None and rows[0].status=='scheduled' and week==6
    assert comp['country']=='Италия' and available=={6,7}

def test_sb_final_requires_score_and_unknown_clock_retains_date():
    _,rows,*_=parse_sb(sb(finished=True),'2026',6)
    assert rows[0].status=='finished' and rows[0].score_home==2 and rows[0].score_away==1
    _,rows,*_=parse_sb(sb(time='TBD'),'2026',6)
    assert rows[0].kickoff_at is None and rows[0].scheduled_date=='2026-10-09'
    with pytest.raises(SchemaError):parse_sb(sb(finished=True).replace('class="gol-away"','class="gone"'),'2026',6)

@pytest.mark.parametrize('kind',['year','week','empty','team','duplicate'])
def test_sb_changed_scope_or_markup_does_not_silently_import(kind):
    html=sb()
    if kind=='year':html=sb(year=2025)
    if kind=='week':html=sb(week=5)
    if kind=='empty':html=html.replace('matchday-list','gone')
    if kind=='team':html=html.replace('club-home','gone')
    if kind=='duplicate':html=html+html[html.index('<div class="matchday-list">'):]
    with pytest.raises(SchemaError):parse_sb(html,'2026',6)

def test_sb_import_final_ttl_persistent_plan_and_quota(store,now,monkeypatch):
    monkeypatch.setattr('fast_parser.collector.utcnow',lambda:now)
    async def run():
        c=Collector(store);await c.client.aclose()
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=sb(day='5 ottobre 2026',finished=True))))
        try:
            assert await c.sync_italian(SB_SOURCE)==1
            m=store.match('sb:2026:b1',now)
            assert m['official_source'] and m['source_url']==sb_url('2026',6)
            assert m['expires_at']==stamp(now+timedelta(hours=72))
            assert store.get_meta('legab_plan') and not store.reserve_request(SB_SOURCE,30)
        finally:await c.client.aclose()
    asyncio.run(run())

def test_sa_date_only_fixture_outside_window_not_imported(store,now,monkeypatch):
    monkeypatch.setattr('fast_parser.collector.utcnow',lambda:now)
    import json
    store.set_meta('seriea_season',json.dumps(dict(year='2026',season=SEASON,refresh_after=now.timestamp()+86400)))
    async def run():
        c=Collector(store);await c.client.aclose()
        data=sa(now,status='UPCOMING',isUnknownKickOffTime=True,matchDateUtc='2027-02-01T00:00:00Z',homeScorePush=None,awayScorePush=None)
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=data)))
        try:
            assert await c.sync_italian(SA_SOURCE)==0
            assert store.match('sa:2026:match1',now) is None
        finally:await c.client.aclose()
    asyncio.run(run())

def test_date_only_matches_remain_visible_in_calendar_filter(store,now):
    comp,rows,*_=parse_sb(sb(time='TBD'),'2026',6)
    store.catalog([comp]);store.upsert_matches(rows,now)
    assert len(store.matches(cid=comp['id'],start='2026-10-08T00:00:00+00:00',end='2026-10-10T00:00:00+00:00',now=now)['items'])==1
    assert not store.matches(cid=comp['id'],end='2026-10-09T00:00:00+00:00',now=now)['items']
    assert source_for_comp(comp['id'])==SB_SOURCE
