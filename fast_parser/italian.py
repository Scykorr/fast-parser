"""Official public Lega Serie A JSON and server-rendered Lega B calendar."""
import re
from datetime import datetime, date as calendar_date
from urllib.parse import quote
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from .adapters import SchemaError
from .domain import Match, parse_time, stamp
from .english import score
from .sources import SA_SOURCE, SB_SOURCE

SA_BASE = "https://api-sdp.legaseriea.it/v1/serie-a/football"
SA_COMP = "serie-a::Football_Competition::ec93b94f74294dc98ab5bcfd67fc0d88"
SA_SEASONS = SA_BASE+"/competitions/"+quote(SA_COMP,safe="")+"/seasons?locale=it-IT"
SB_BASE = "https://www.legab.it"
MONTHS = {v:i+1 for i,v in enumerate(['gennaio','febbraio','marzo','aprile','maggio','giugno','luglio','agosto','settembre','ottobre','novembre','dicembre'])}

def catalog(year):
    return [dict(id=f"sa:serie-a:{year}",shortcut="serie-a",season=str(year),name="Серия A · Lega Serie A",country="Италия",category="league"),
            dict(id=f"sb:serie-b:{year}",shortcut="serie-b",season=str(year),name="Серия B · Lega B",country="Италия",category="league")]

def select_season(data,year):
    try:
        candidates=[s for s in data['seasons'] if s['seasonName']==f"{year}/{int(year)+1}" and s['competitionId']==SA_COMP]
        if len(candidates)!=1 or not re.fullmatch(r'serie-a::Football_Season::[a-f0-9]{32}',candidates[0]['seasonId']):
            raise SchemaError("Serie A: официальный сезон не найден или неоднозначен")
        return candidates[0]['seasonId']
    except (KeyError,TypeError,ValueError) as exc:
        raise SchemaError("Serie A: изменился список сезонов") from exc

def sa_url(season):
    return SA_BASE+"/seasons/"+quote(season,safe="")+"/matches?matchDayId=&locale=it-IT"

def parse_sa(data,year,season):
    try:
        compdata=data['competition']
        if compdata['competitionId']!=SA_COMP or compdata['seasonId']!=season or compdata['seasonName']!=f"{year}/{int(year)+1}":
            raise SchemaError("Serie A: чужое соревнование или сезон")
        rows=data['matches']
        if not isinstance(rows,list) or not rows or len({r['matchId'] for r in rows})!=len(rows):
            raise SchemaError("Serie A: пустой ответ или повтор ID")
        comp=catalog(year)[0];matches=[]
        for r in rows:
            if r['seasonId']!=season:
                raise SchemaError("Serie A: смешение сезонов")
            teams=[dict(id=r[k]['teamId'],name=(r[k].get('shortName') or r[k]['officialName']).strip()) for k in ('home','away')]
            if any(not t['name'] for t in teams) or any(r[k].get('isTeamFake') for k in ('home','away')):
                raise SchemaError("Serie A: отсутствует подтвержденная команда")
            value=r.get('matchDateUtc')
            date_only=isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}Z',value)
            if date_only and r.get('isUnknownKickOffTime') is not True:
                raise SchemaError("Serie A: дата без времени противоречит признаку источника")
            kickoff=None if date_only else parse_time(value)
            day=calendar_date.fromisoformat(value[:-1]) if date_only else kickoff.date() if kickoff else None
            if day and day.year-int(day.month<7)!=int(year):
                raise SchemaError("Serie A: дата вне сезона")
            status={'FINISHED':'finished','UPCOMING':'scheduled'}.get(r['status'],'unknown')
            scores=[score(r.get('homeScorePush')),score(r.get('awayScorePush'))]
            if status=='finished' and None in scores:
                raise SchemaError("Serie A: отсутствует итоговый счет")
            external=f"{year}:{r['matchId']}"
            matches.append(Match(id='sa:'+external,external_id=external,competition_id=comp['id'],source=SA_SOURCE,
                home_team=teams[0],away_team=teams[1],kickoff_at=stamp(kickoff) if kickoff and not r.get('isUnknownKickOffTime') else None,
                status=status,score_home=scores[0],score_away=scores[1],raw_status=r['status']+'/'+str(r.get('phase')),
                scheduled_date=day.isoformat() if day else None,
                verification='official_source',source_url=sa_url(season)))
        return comp,matches
    except (KeyError,TypeError,ValueError) as exc:
        raise SchemaError("Serie A: изменился контракт матчей") from exc

def sb_url(year,week=None):
    return f"{SB_BASE}/seriebkt/calendario/{year}-{int(year)+1}/stagione-regolare"+(f"/{week}" if week else "")

def parse_sb(html,year,week=None):
    try:
        soup=BeautifulSoup(html,'html.parser')
        pattern=rf'/seriebkt/calendario/{year}-{int(year)+1}/stagione-regolare/(\d+)'
        options=[(int(m[1]),o.has_attr('selected')) for o in soup.select('select option[value]') if (m:=re.fullmatch(pattern,o['value']))]
        selected={number for number,checked in options if checked}
        if len(selected)!=1 or week is not None and selected!={week}:
            raise SchemaError("Lega B: неверный сезон/тур или нет текущей страницы")
        current=next(iter(selected));available={n for n,_ in options}
        cards=soup.select('.matchday-list');matches=[];ids=set();dates=[]
        if not cards:
            raise SchemaError("Lega B: отсутствуют карточки; пустой ответ не подтверждает покрытие")
        comp=catalog(year)[1]
        for card in cards:
            link=card.select_one('a[data-match-id]')
            if link is None or not link.get('href','').startswith(f'/seriebkt/partita/{year}-{int(year)+1}/stagione-regolare/'):
                raise SchemaError("Lega B: неверный ID/сезон матча")
            key=link['data-match-id']
            if not key or key in ids:
                raise SchemaError("Lega B: повтор ID")
            ids.add(key)
            teams=[]
            for side in ('home','away'):
                name=card.select_one(f'.club-{side} .club-label span')
                if name is None or not name.get_text(strip=True):
                    raise SchemaError("Lega B: отсутствует название команды")
                teams.append(dict(name=name.get_text(strip=True)))
            label=card.select_one('.stato .visually-hidden')
            if label is None:
                raise SchemaError("Lega B: отсутствует явная дата")
            text=label.get_text(' ',strip=True).lower()
            date=re.search(r'\b(\d{1,2})\s+([a-z]+)\s+(\d{4})\b',text)
            if not date or date[2] not in MONTHS:
                raise SchemaError("Lega B: неизвестная дата/месяц")
            local=datetime(int(date[3]),MONTHS[date[2]],int(date[1]),tzinfo=ZoneInfo('Europe/Rome'))
            if local.year-int(local.month<7)!=int(year):
                raise SchemaError("Lega B: дата вне сезона")
            dates.append(local)
            time=re.search(r'ore:\s*(\d{1,2}):(\d{2})\b',text)
            kickoff=local.replace(hour=int(time[1]),minute=int(time[2])) if time else None
            state=card.select_one('.stato');state_label=state.select_one('.stato-label')
            raw=state_label.get_text(' ',strip=True) if state_label else ''
            status='finished' if 'giocata' in state.get('class',[]) and raw=='Risultato' else 'scheduled' if raw=='Ore' else 'unknown'
            scores=[]
            for side in ('home','away'):
                tag=state.select_one('.gol-'+side)
                value=tag.get_text(strip=True) if tag else ''
                if value and not value.isdigit():
                    raise SchemaError("Lega B: неверный счет")
                scores.append(int(value) if value else None)
            if status=='finished' and None in scores:
                raise SchemaError("Lega B: отсутствует итоговый счет")
            external=f'{year}:{key}'
            matches.append(Match(id='sb:'+external,external_id=external,competition_id=comp['id'],source=SB_SOURCE,
                home_team=teams[0],away_team=teams[1],kickoff_at=stamp(kickoff) if kickoff else None,
                scheduled_date=local.date().isoformat(),
                status=status,score_home=scores[0],score_away=scores[1],raw_status=raw,verification='official_source',source_url=sb_url(year,current)))
        return comp,matches,current,available,min(dates),max(dates)
    except (KeyError,TypeError,ValueError) as exc:
        raise SchemaError("Lega B: изменился календарь") from exc
