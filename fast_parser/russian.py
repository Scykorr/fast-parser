"""Public FNL website service. RPL discovery is unavailable behind CAPTCHA."""
import re
from datetime import datetime, date
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from .adapters import SchemaError
from .domain import Match, stamp
from .english import score
from .sources import FNL_SOURCE

BASE = "https://fnl-app.fnl.pro"
ACTIVE = BASE+"/api/v1/info/activeSeason?leagueId=100"
PAGE_SIZE = 10
RPL_REASON = "РПЛ: официальный сайт возвращает CAPTCHA; текущий контракт сбора не подтвержден"


def catalog(year):
    return [dict(id=f"rpl:premier-league:{year}", shortcut="rpl", season=str(year),
                 name="Российская Премьер-лига · РПЛ", country="Россия", category="league"),
            dict(id=f"fnl:first-league:{year}", shortcut="first-league", season=str(year),
                 name="Первая лига · ФНЛ", country="Россия", category="league")]


def active_season(data, year):
    if (not isinstance(data,dict) or data.get("leagueId") != 100
        or data.get("title") != f"{year}/{int(year)+1}" or type(data.get("seasonId")) is not int
        or data["seasonId"] <= 0):
        raise SchemaError("ФНЛ: неверный активный сезон/лига")
    return data["seasonId"]


def page_url(season, month, year, offset=0):
    return BASE+"/api/v1/center/calendar/matches/date?"+urlencode(dict(
        leagueId=100,seasonId=season,limit=PAGE_SIZE,offset=offset,move="ALL",mouth=month,year=year))


def parse_page(data, year, season, month, calendar_year, offset=0):
    try:
        loc = data["location"]
        total = loc["count"]
        if (loc["limit"] != PAGE_SIZE or loc["offset"] != offset or type(total) is not int
            or not 0 <= total <= 500 or offset < 0 or offset > total):
            raise SchemaError("ФНЛ: неверная пагинация")
        groups = data["matches"]
        if not isinstance(groups,list):
            raise SchemaError("ФНЛ: нет массива дней")
        rows = [row for group in groups for row in group["matches"]]
        if len(rows) != min(PAGE_SIZE,total-offset):
            raise SchemaError("ФНЛ: неполная страница")
        comp = catalog(year)[1]; matches = []; ids = set()
        for row in rows:
            key = row["matchId"]
            if (type(key) is not int or key <= 0 or key in ids or row["leagueId"] != 100
                or row["seasonId"] != season or row["season"] != f"{year}/{int(year)+1}"):
                raise SchemaError("ФНЛ: повтор ID или чужая лига/сезон")
            ids.add(key)
            teams = [dict(id=str(row[side]["teamId"]),name=row[side]["name"].strip()) for side in ("home","guest")]
            if any(not t["name"] for t in teams) or teams[0]["id"] == teams[1]["id"]:
                raise SchemaError("ФНЛ: отсутствуют команды")
            value = row.get("date")
            kickoff = None; day = None
            if value:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?",value):
                    raise SchemaError("ФНЛ: неизвестный формат даты")
                day = date.fromisoformat(value[:10])
                if day.year-int(day.month < 7) != int(year) or (day.year,day.month) != (calendar_year,month):
                    raise SchemaError("ФНЛ: матч вне запрошенного месяца")
                if len(value) > 10 and not value.endswith(" 00:00"):
                    kickoff = datetime.strptime(value,"%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Europe/Moscow"))
            raw = row["status"]; phase = row["statusExtended"]
            status = {"FUTURE":"scheduled","END":"finished"}.get(raw,"unknown")
            period = None; minute = None
            phases = {"START_FIRST_HALF":("live","first_half"),"END_FIRST_HALF":("paused","half_time"),
                      "START_SECOND_HALF":("live","second_half")}
            if raw == "BEGIN" and phase in phases:
                status,period = phases[phase]
                clock = row.get("matchTimeStr") or row.get("matchTime")
                if isinstance(clock,str) and re.fullmatch(r"\d{1,3}",clock) and int(clock) <= 150:
                    minute = int(clock)
            scores = [score(row[side].get("score")) for side in ("home","guest")]
            if status == "finished" and (None in scores or day is None):
                raise SchemaError("ФНЛ: нет подтвержденного результата/даты")
            if status == "scheduled":
                scores = [None,None]  # FUTURE uses zero placeholders, not an observed score.
            external = f"{year}:{key}"
            matches.append(Match(id="fnl:"+external,external_id=external,competition_id=comp["id"],
                source=FNL_SOURCE,home_team=teams[0],away_team=teams[1],kickoff_at=stamp(kickoff) if kickoff else None,
                scheduled_date=day.isoformat() if day else None,status=status,score_home=scores[0],score_away=scores[1],
                raw_status=raw+"/"+phase,period=period,elapsed_minutes=minute,
                verification="official_source",source_url="https://fnl.pro/pari/matches"))
        return comp,matches,(offset+PAGE_SIZE if offset+len(rows)<total else None)
    except (KeyError,TypeError,ValueError,AttributeError) as exc:
        raise SchemaError("ФНЛ: изменился контракт календаря") from exc
