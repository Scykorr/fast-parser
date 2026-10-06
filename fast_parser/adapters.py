from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .domain import Match, parse_time, stamp

API_BASE = "https://api.openligadb.de"
HTML_BASE = "https://www.skysports.com"


class SchemaError(ValueError):
    pass


def country_for(name: str, shortcut: str = "") -> str:
    lower = name.casefold()
    if shortcut in {"bl1", "bl2", "bl3", "dfb", "ffb1", "rln", "rlno", "DFBN"}:
        return "Германия"
    if shortcut == "pl":
        return "Англия"
    if shortcut == "la1":
        return "Испания"
    if "scottish" in lower:
        return "Шотландия"
    if re.match(r"^(?:english )?premier league(?:\s+\d|$)", lower):
        return "Англия"
    for patterns, country in [
        (("champions league", "europa league", "uefa", "fifa", "world cup", "international", "nations league", "european under"), "Международные"),
        (("bundesliga", "dfb", "regionalliga", "german", "deutsch"), "Германия"),
        (("english championship", "sky bet championship", "league one", "league two", "national league", "fa cup", "carabao", "isthmian", "southern premier"), "Англия"),
        (("scottish",), "Шотландия"), (("laliga", "la liga", "spanish"), "Испания"),
        (("italian", "serie a", "serie b"), "Италия"), (("ligue 1", "ligue 2", "french"), "Франция"),
        (("portugu", "liga portugal"), "Португалия"), (("eredivisie", "dutch"), "Нидерланды"),
        (("major league soccer", "mls", "usl"), "США / Канада"), (("brazil", "brasil"), "Бразилия"),
        (("argentin",), "Аргентина"), (("australian", "a-league"), "Австралия"),
        (("turkish", "super lig"), "Турция"), (("saudi",), "Саудовская Аравия"),
        (("belgian",), "Бельгия"), (("swiss",), "Швейцария"),
    ]:
        if any(p in lower for p in patterns):
            return country
    return "Страна не указана"


def category_for(name: str) -> str:
    lower = name.casefold()
    if any(w in lower for w in ("women", "frauen", "feminin")):
        return "Женские"
    if re.search(r"(?:under.?\d+|u(?:17|19|21|23)\b|jugend)", lower):
        return "Молодёжные"
    return "Не указана"


def catalog_openliga(payload, now: datetime) -> list[dict]:
    if not isinstance(payload, list):
        raise SchemaError("Каталог: ожидался список")
    latest = {}
    for item in payload:
        if not isinstance(item, dict):
            raise SchemaError("Каталог: неверная запись")
        if item.get("sport", {}).get("sportId") != 1:
            continue
        season = str(item.get("leagueSeason", ""))
        if not season.isdigit() or int(season) < now.year - 1 or int(season) > now.year + 1:
            continue
        shortcut = str(item.get("leagueShortcut", ""))
        if not shortcut or len(shortcut) > 100:
            continue
        # The newest season for each community league, without merging different shortcuts.
        if shortcut in latest and int(latest[shortcut]["season"]) >= int(season):
            continue
        name = str(item.get("leagueName", ""))
        if not name:
            continue
        latest[shortcut] = {"id": f"ol:{shortcut}:{season}", "shortcut": shortcut, "season": season,
                            "name": name, "country": country_for(name, shortcut), "category": category_for(name)}
    if not latest:
        raise SchemaError("Каталог не содержит текущих футбольных лиг")
    return list(latest.values())


def score(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaError("Неверное значение счёта")
    return value


def parse_openliga(payload, competition: dict, now: datetime) -> list[Match]:
    if not isinstance(payload, list):
        raise SchemaError("Матчи: ожидался список")
    result = []
    seen = set()
    for row in payload:
        try:
            external = str(row["matchID"])
            if external in seen:
                raise SchemaError("Дублирующийся ID в ответе")
            seen.add(external)
            if str(row["leagueShortcut"]) != competition["shortcut"] or str(row["leagueSeason"]) != competition["season"]:
                raise SchemaError("Ответ относится к другой лиге/сезону")
            kickoff = parse_time(row.get("matchDateTimeUTC"))
            if not isinstance(row["matchIsFinished"], bool):
                raise SchemaError("Некорректный флаг завершения")
            finished = row["matchIsFinished"]
            # The API does not expose an authoritative in-play status. Do not invent it.
            status = "finished" if finished else "scheduled" if kickoff and kickoff > now else "unknown"
            home, away = row["team1"], row["team2"]
            item = Match(id=f"ol:{external}", external_id=external, competition_id=competition["id"], source="openligadb",
                         home_team={"id": str(home["teamId"]), "name": home.get("teamName") or "Команда не указана источником"},
                         away_team={"id": str(away["teamId"]), "name": away.get("teamName") or "Команда не указана источником"},
                         kickoff_at=stamp(kickoff) if kickoff else None, status=status,
                         period="finished" if finished else "pre_match" if status == "scheduled" else None,
                         raw_status=f"matchIsFinished={finished}")
            for value in row.get("matchResults", []):
                # Result types are configured by each community league; trust explicit names only.
                label = str(value.get("resultName", "")).casefold()
                if finished and label in {"endergebnis", "end result", "full time", "final result"}:
                    item.score_home, item.score_away = score(value.get("pointsTeam1")), score(value.get("pointsTeam2"))
            result.append(item)
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(f"Неверная запись OpenLigaDB: {exc}") from exc
    return result


MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]


def parse_sky(html: str, requested_day: date, now: datetime) -> tuple[list[dict], list[Match]]:
    """Read server-rendered HTML data-state; no private API or browser execution."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).casefold()
    if any(x in text for x in ("verify you are human", "access denied", "enable javascript and cookies", "just a moment...")):
        raise SchemaError("Страница блокировки вместо расписания")
    cards = soup.select('[data-component-name="ui-sport-match-score"][data-state]')
    if not cards:
        if "no fixtures" in text or "no matches" in text:
            return [], []
        raise SchemaError("HTML-контракт изменился: карточки матчей не найдены")
    matches, competitions, seen = [], {}, set()
    for card in cards:
        try:
            row = json.loads(card["data-state"])
            external = str(row["id"])
            if external in seen:
                continue
            seen.add(external)
            comp = row["competition"]
            cid = "sky:" + str(comp["id"])
            name = comp["name"]["full"]
            competitions[cid] = {"id": cid, "shortcut": cid, "season": str(requested_day.year), "name": name,
                                 "country": country_for(name), "category": category_for(name)}
            start = row["start"]
            date_label = str(start.get("date", "")).casefold()
            # Detect stale/wrong-day content instead of assigning today's date to cached games.
            day_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", date_label)
            if not day_match or int(day_match[1]) != requested_day.day or MONTHS[requested_day.month - 1] not in date_label:
                raise SchemaError("Страница вернула матчи другой даты")
            kickoff = None
            if re.fullmatch(r"\d{2}:\d{2}", start.get("time", "")):
                kickoff = datetime.combine(requested_day, datetime.strptime(start["time"], "%H:%M").time(), ZoneInfo("Europe/London"))
            status = "unknown"
            for flag, state in [("isPostponed", "postponed"), ("isCancelled", "cancelled"), ("isAbandoned", "abandoned"),
                                ("isSuspended", "paused"), ("isResult", "finished"), ("isInPlay", "live"), ("isFixture", "scheduled")]:
                if row.get(flag):
                    status = state
                    break
            home, away = row["teams"]["home"], row["teams"]["away"]
            item = Match(id=f"sky:{external}", external_id=external, competition_id=cid, source="skysports_html",
                         home_team={"id": str(home["id"]), "name": home["name"]["full"] or "Команда не указана источником"},
                         away_team={"id": str(away["id"]), "name": away["name"]["full"] or "Команда не указана источником"},
                         kickoff_at=stamp(kickoff) if kickoff else None, status=status, raw_status=str(row.get("status", "")))
            if status in {"live", "paused", "finished"}:
                item.score_home = score(home.get("score", {}).get("current"))
                item.score_away = score(away.get("score", {}).get("current"))
            raw = item.raw_status.strip().upper()
            description = row.get("statusDescription", {})
            if not raw and isinstance(description, dict):
                short = description.get("short") or []
                raw = str(short[0] if short else description.get("full") or "").strip().upper()
                item.raw_status = raw
            if raw == "HT":
                item.period, item.status = "half_time", "paused"
            elif raw in {"FT", "AET"}:
                item.period = "finished"
            elif raw in {"PEN", "PENS"}:
                item.period = "penalties"
            # A minute is explicit, but a period cannot be inferred from the minute alone.
            minute = re.fullmatch(r"(\d{1,3})(?:\+(\d{1,2}))?['′]", raw)
            if minute and status == "live":
                item.elapsed_minutes = int(minute[1])
                item.added_minutes = int(minute[2]) if minute[2] else None
            qualifier = str(row.get("statusQualifier") or "").casefold()
            item.period = {"first half": "first_half", "second half": "second_half",
                           "extra time first half": "extra_first_half", "extra time second half": "extra_second_half"}.get(qualifier, item.period)
            matches.append(item)
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(f"Неверная HTML-карточка: {exc}") from exc
    return list(competitions.values()), matches


def openliga_url(comp: dict) -> str:
    return f"{API_BASE}/getmatchdata/{quote(comp['shortcut'], safe='')}/{quote(comp['season'], safe='')}"
