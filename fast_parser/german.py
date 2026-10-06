"""Public Bundesliga SSR transfer state; no private API or credentials."""
import json
import re
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from .adapters import SchemaError
from .domain import Match, parse_time, stamp
from .english import score
from .sources import DFL_SOURCE

LEAGUES = {"bundesliga": ("DFL-COM-000001", "Бундеслига"),
           "2bundesliga": ("DFL-COM-000002", "Вторая Бундеслига")}

def catalog(year):
    return [dict(id=f"dfl:{division}:{year}", shortcut=division, season=str(year),
                 name=name+" · Bundesliga", country="Германия", category="league")
            for division, (_, name) in LEAGUES.items()]

def page_url(division, year=None, week=None):
    return f"https://www.bundesliga.com/en/{division}/matchday" + (f"/{year}-{int(year)+1}/{week}" if week else "")

def parse_page(html, division, year, week=None):
    try:
        cid = LEAGUES[division][0]
        soup = BeautifulSoup(html, "html.parser")
        tags = soup.find_all("script", id="ng-state")
        if len(tags) != 1:
            raise SchemaError("DFL: нет однозначного SSR состояния")
        data = json.loads(tags[0].get_text())
        configs = [v["b"][cid] for v in data.values() if isinstance(v, dict)
                   and isinstance(v.get("b"), dict) and cid in v["b"]
                   and isinstance(v["b"][cid], dict) and "season" in v["b"][cid]]
        if len(configs) != 1 or configs[0]["season"]["name"] != f"{year}-{int(year)+1}":
            raise SchemaError("DFL: неверный или неизвестный сезон")
        season = configs[0]["season"]["seasonId"]
        current = week if week is not None else configs[0]["matchday"]["matchdayNumber"]
        if type(current) is not int or not 1 <= current <= 34:
            raise SchemaError("DFL: неверный тур")
        pattern = rf"_getDataFromFirebase-/all/{cid}/seasons/{re.escape(season)}/matchesmatchday{current}99"
        arrays = [v for k, v in data.items() if re.fullmatch(pattern, k)]
        if len(arrays) != 1 or not isinstance(arrays[0], list) or not arrays[0]:
            raise SchemaError("DFL: нет матчей запрошенной лиги/тура")
        comp = next(c for c in catalog(year) if c["shortcut"] == division)
        matches = []; ids = set(); dates = []
        for row in arrays[0]:
            key = row["matchId"]
            if (not re.fullmatch(r"DFL-MAT-[A-Z0-9]+", key) or key in ids
                or row["dflDatalibraryCompetitionId"] != cid
                or row["dflDatalibrarySeasonId"] != season or row["matchday"] != current):
                raise SchemaError("DFL: повтор ID или смешение лиг/туров")
            ids.add(key)
            teams = [dict(id=row["teams"][side]["dflDatalibraryClubId"],
                          name=row["teams"][side]["nameFull"].strip()) for side in ("home", "away")]
            if any(not t["name"] for t in teams) or teams[0]["id"] == teams[1]["id"]:
                raise SchemaError("DFL: неверные команды")
            planned = parse_time(row["plannedKickOff"])
            if planned is None or planned.year-int(planned.month < 7) != int(year):
                raise SchemaError("DFL: дата вне сезона")
            dates.append(planned)
            raw = row["matchStatus"]
            # Only observed public states are mapped; new live phases remain explicit unknown.
            status = {"PRE_MATCH": "scheduled", "FINAL_WHISTLE": "finished"}.get(raw, "unknown")
            scores = [score(row.get("score", {}).get(side, {}).get("fulltime" if status == "finished" else "live"))
                      for side in ("home", "away")]
            if status == "finished" and None in scores:
                raise SchemaError("DFL: нет итогового счета")
            kickoff = parse_time(row.get("kickOff")) or planned if row.get("matchDateFixed") is True else None
            external = f"{year}:{key}"
            matches.append(Match(id="dfl:"+external, external_id=external, competition_id=comp["id"],
                source=DFL_SOURCE, home_team=teams[0], away_team=teams[1],
                kickoff_at=stamp(kickoff) if kickoff else None, scheduled_date=planned.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat(),
                status=status, score_home=scores[0], score_away=scores[1], raw_status=raw,
                verification="official_source", source_url=page_url(division, year, current)))
        return comp, matches, current, min(dates), max(dates)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SchemaError("DFL: изменился контракт календаря") from exc
