"""Free public services used by the official Premier League and EFL websites."""
from urllib.parse import urlencode, urlparse, parse_qs
from datetime import timedelta
from .adapters import SchemaError
from .domain import Match, parse_time, stamp

from .sources import PL_SOURCE, EFL_SOURCE, OFFICIAL_SOURCES, OFFICIAL_PREFIXES
PL_BASE = "https://fantasy.premierleague.com/api"
EFL_BASE = "https://multi-club-matches.webapi.gc.eflservices.co.uk/v2/matches"

def catalog(year):
    return [dict(id=f"pl:premier-league:{year}", shortcut="premier-league", season=str(year), name="Премьер-лига · Premier League", country="Англия", category="league"),
            dict(id=f"efl:championship:{year}", shortcut="championship", season=str(year), name="Чемпионшип · EFL", country="Англия", category="league")]

def score(value):
    if value is not None and (type(value) is not int or value < 0):
        raise SchemaError("Официальный источник: неверный счет")
    return value

def bootstrap(data):
    try:
        events = data["events"]
        year = min(parse_time(e["deadline_time"]) for e in events).year
        teams = {str(t["id"]): {"id":str(t["id"]), "name":t["name"].strip()} for t in data["teams"]}
        if len(teams) != len(data["teams"]) or len(teams) != 20 or any(not t["name"] for t in teams.values()):
            raise SchemaError("Premier League: неверный справочник команд")
        return {"season":str(year), "teams":teams}
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError("Premier League: изменился справочник") from exc

def parse_pl(rows, reference):
    try:
        if not isinstance(rows, list) or not rows or len({r["id"] for r in rows}) != len(rows):
            raise SchemaError("Premier League: пустой ответ или повтор ID")
        year = reference["season"]
        dates = [parse_time(r["kickoff_time"]) for r in rows if r.get("kickoff_time")]
        if not dates or min(dates).year != int(year):
            raise SchemaError("Premier League: сезон матчей не совпал со справочником")
        comp = catalog(year)[0]
        matches = []
        for r in rows:
            if type(r["finished"]) is not bool or type(r["started"]) not in (bool, type(None)):
                raise SchemaError("Premier League: неверный статус")
            status = "finished" if r["finished"] else "unknown" if r.get("finished_provisional") else "live" if r["started"] else "scheduled"
            scores = [score(r.get("team_h_score")), score(r.get("team_a_score"))]
            if status == "finished" and None in scores:
                raise SchemaError("Premier League: отсутствует итоговый счет")
            minutes = r.get("minutes") if status == "live" else None
            if minutes is not None and (type(minutes) is not int or not 0 <= minutes <= 240):
                raise SchemaError("Premier League: неверная минута")
            kickoff = parse_time(r.get("kickoff_time"))
            if kickoff and kickoff.year-int(kickoff.month < 7) != int(year):
                raise SchemaError("Premier League: дата вне сезона")
            matches.append(Match(id=f"pl:{year}:{r['id']}", external_id=f"{year}:{r['id']}",
                competition_id=comp["id"], source=PL_SOURCE, home_team=reference["teams"][str(r["team_h"])],
                away_team=reference["teams"][str(r["team_a"])], kickoff_at=stamp(kickoff) if kickoff else None,
                status=status, score_home=scores[0], score_away=scores[1], elapsed_minutes=minutes,
                raw_status=f"finished={r['finished']};started={r['started']};provisional={r.get('finished_provisional')}",
                verification="official_source", source_url=PL_BASE+"/fixtures/"))
        return comp, matches
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError("Premier League: изменился контракт матчей") from exc

def efl_url(year, start, end):
    return EFL_BASE+"?"+urlencode({"page.size":100, "seasonID":year, "competitionID":10,
                                  "from":start.date().isoformat(), "to":(end+timedelta(days=1)).date().isoformat()})

def safe_next(url, expected):
    if not url:
        return None
    a, b = urlparse(url), urlparse(expected)
    q, original = parse_qs(a.query), parse_qs(b.query)
    if (a.scheme != "https" or a.netloc != b.netloc or a.path != b.path
            or any(q.get(k) != original.get(k) for k in ("seasonID","competitionID","from","to","page.size"))
            or not q.get("page.number", [""])[0].isdigit()
            or int(q["page.number"][0]) != int(original.get("page.number",["1"])[0])+1):
        raise SchemaError("EFL: небезопасная или чужая ссылка следующей страницы")
    return url

def parse_efl(data, year, url):
    try:
        rows = data["data"]
        if not isinstance(rows,list) or len({r["id"] for r in rows}) != len(rows):
            raise SchemaError("EFL: неверная страница или повтор ID")
        comp = catalog(year)[1]
        matches = []
        for r in rows:
            a = r["attributes"]
            if r["type"] != "match" or a["competitionID"] != 10:
                raise SchemaError("EFL: чужое соревнование")
            # Field is explicitly named UTC by EFL; its wire format omits the offset.
            value = a.get("kickOffDateUTC")
            kickoff = parse_time(value.replace(" ","T")+"+00:00") if value else None
            if kickoff and kickoff.year-int(kickoff.month < 7) != int(year):
                raise SchemaError("EFL: неверный сезон даты")
            status = {"PreMatch":"scheduled", "FullTime":"finished"}.get(a["matchPeriod"],"unknown")
            teams = [{"name":a[k]["name"].strip()} for k in ("homeTeam","awayTeam")]
            scores = [score(a[k].get("score")) for k in ("homeTeam","awayTeam")]
            if any(not t["name"] for t in teams) or status == "finished" and None in scores:
                raise SchemaError("EFL: нет команд или итогового счета")
            matches.append(Match(id=f"efl:{year}:{r['id']}", external_id=f"{year}:{r['id']}", competition_id=comp["id"],
                source=EFL_SOURCE, home_team=teams[0], away_team=teams[1], kickoff_at=stamp(kickoff) if kickoff and not a.get("TBC") else None,
                status=status, score_home=scores[0], score_away=scores[1], raw_status=a["matchPeriod"],
                verification="official_source", source_url=url))
        nxt = safe_next(data.get("links",{}).get("next"),url)
        total = data.get("meta",{}).get("totalCount",len(rows))
        if total > len(rows) and not nxt and "page.number" not in parse_qs(urlparse(url).query):
            raise SchemaError("EFL: неполная первая страница без продолжения")
        return comp, matches, nxt
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError("EFL: изменился контракт матчей") from exc
