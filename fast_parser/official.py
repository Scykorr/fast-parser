"""Public organiser pages are the production authority; no paid API or fallback."""
import json
from bs4 import BeautifulSoup
from .adapters import SchemaError
from .domain import Match, parse_time, stamp

LEAGUES = {"primera-division": ("laliga-easports", "Примера · LaLiga"),
           "segunda-division": ("laliga-hypermotion", "Сегунда · LaLiga")}
SOURCE = "laliga_reference"  # shares persistent quota with the verification tool

def parse_page(html, division, season=None, week=None):
    tag = BeautifulSoup(html, "html.parser").select_one("script#__NEXT_DATA__")
    if tag is None:
        raise SchemaError("LaLiga: отсутствуют публичные данные матчей")
    try:
        page = json.loads(tag.get_text())["props"]["pageProps"]
        year = str(page["season"])
        current = page["gameweek"]["week"]
        if not year.isdigit() or len(year) != 4 or page["competition"] != division or season is not None and year != str(season) or week is not None and current != week:
            raise SchemaError("LaLiga: неверный дивизион, сезон или тур")
        cid = f"ll:{division}:{year}"
        comp = dict(id=cid, shortcut=division, season=year, name=LEAGUES[division][1], country="Испания", category="league")
        rows = page["matches"]
        if not rows or len({r["id"] for r in rows}) != len(rows):
            raise SchemaError("LaLiga: пустой тур или повтор ID")
        matches = []
        for r in rows:
            status = {"PreMatch":"scheduled", "FullTime":"finished"}.get(r["status"])
            if status is None:
                status = "unknown"  # raw status is retained; never invent live minutes or a final
            teams = [dict(id=str(r[k]["id"]), name=r[k].get("nickname") or r[k]["name"]) for k in ("home_team", "away_team")]
            scores = [r.get("home_score"), r.get("away_score")]
            if any(not t["name"].strip() for t in teams) or status == "finished" and any(type(s) is not int or s < 0 for s in scores):
                raise SchemaError("LaLiga: неверные команды или итоговый счет")
            kickoff = parse_time(r["date"])
            if kickoff is None:
                raise SchemaError("LaLiga: отсутствует дата начала")
            matches.append(Match(id="ll:"+str(r["id"]), external_id=str(r["id"]), source=SOURCE,
                competition_id=cid, home_team=teams[0], away_team=teams[1], kickoff_at=stamp(kickoff),
                status=status, score_home=scores[0], score_away=scores[1], raw_status=r["status"], verification="official_source"))
        rounds = []
        for r in page.get("gameweekList", []):
            if type(r["week"]) is not int or r["week"] <= 0:
                raise SchemaError("LaLiga: неверный список туров")
            rounds.append(dict(week=r["week"], date=stamp(parse_time(r["date"]))))
        return comp, matches, rounds, current
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError("LaLiga: изменился контракт страницы") from exc
