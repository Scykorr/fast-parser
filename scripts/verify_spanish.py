"""Bounded, free-source verification. Historical match bodies stay in memory only.

Run while the managed worker is stopped. --apply updates only coverage labels;
it does not import old matches or alter the configured collection window.
"""
import argparse
import asyncio
import json
import re
import unicodedata
from collections import defaultdict
from datetime import date

from bs4 import BeautifulSoup

from fast_parser.adapters import HTML_BASE, MONTHS, SchemaError, openliga_url, parse_openliga, parse_sky
from fast_parser.collector import Collector, Deferred
from fast_parser.config import ROOT
from fast_parser.domain import parse_time, stamp, utcnow
from fast_parser.storage import Store


def normalized(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                           if not unicodedata.combining(c)).split())


# Explicit comparison-only aliases. Never used to merge production IDs or teams.
ALIASES = {
    "FC Malaga": "Malaga", "Espanyol Barcelona": "Espanyol",
    "Deportivo Alaves": "Alaves", "FC Barcelona": "Barcelona", "FC Getafe": "Getafe",
    "FC Villareal": "Villarreal", "Elche CF": "Elche", "Deportivo A Coruña": "Deportivo La Coruna",
    "RC Deportivo": "Deportivo La Coruna", "CA Osasuna": "Osasuna",
    "FC Valencia": "Valencia", "Levante UD": "Levante", "FC Sevilla": "Sevilla",
}
ALIAS_MAP = {normalized(key): normalized(value) for key, value in ALIASES.items()}


def team_key(name):
    key = normalized(name)
    return ALIAS_MAP.get(key, key)


def match_key(match):
    kickoff = parse_time(match.kickoff_at)
    if not kickoff:
        raise ValueError("Cannot compare a match without kickoff")
    return kickoff.date().isoformat(), team_key(match.home_team["name"]), team_key(match.away_team["name"])


def compare(left, right):
    def index(rows):
        result = {}
        for match in rows:
            key = match_key(match)
            if key in result:
                raise ValueError("Ambiguous fixture key: comparison aborted")
            result[key] = match
        return result
    a, b = index(left), index(right)
    common = a.keys() & b.keys()
    times = sum(a[k].kickoff_at != b[k].kickoff_at for k in common)
    status = sum(a[k].status != b[k].status for k in common)
    finals = [k for k in common if a[k].status == b[k].status == "finished"]
    missing_scores = sum(None in (a[k].score_home, a[k].score_away, b[k].score_home, b[k].score_away) for k in finals)
    score_mismatches = sum((a[k].score_home, a[k].score_away) != (b[k].score_home, b[k].score_away) for k in finals)
    fixtures = sum(a[k].status == b[k].status == "scheduled" for k in common)
    offsets = defaultdict(int)
    for key in common:
        seconds = int((parse_time(a[key].kickoff_at)-parse_time(b[key].kickoff_at)).total_seconds())
        if seconds:
            offsets[str(seconds)] += 1
    return {"left": len(a), "right": len(b), "paired": len(common), "left_only": len(a.keys()-b.keys()),
            "right_only": len(b.keys()-a.keys()), "kickoff_mismatches": times, "status_mismatches": status,
            "finished_pairs": len(finals), "scheduled_pairs": fixtures, "missing_final_scores": missing_scores,
            "score_mismatches": score_mismatches, "kickoff_offset_seconds": dict(sorted(offsets.items()))}


def parse_month(html, year, month, now):
    """Verification reference pages are monthly; production collection remains daily."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-component-name="ui-sport-match-score"][data-state]')
    if not cards:
        raise SchemaError("Reference page has no fixture cards; do not verify an empty response")
    days = defaultdict(list)
    for card in cards:
        row = json.loads(card["data-state"])
        label = row["start"]["date"].lower()
        if MONTHS[month-1] not in label:
            raise SchemaError("Wrong month in verification reference")
        explicit_year = re.search(r"\b(20\d{2})\b", label)
        if explicit_year and int(explicit_year[1]) != year:
            raise SchemaError("Wrong year in verification reference")
        found = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", label)
        if not found:
            raise SchemaError("Reference fixture has no explicit date")
        days[int(found[1])].append(str(card))
    comps, matches = {}, {}
    for day, group in days.items():
        competitions, parsed = parse_sky("".join(group), date(year, month, day), now)
        comps.update({c["id"]: c for c in competitions})
        for m in parsed:
            if m.id in matches:
                raise SchemaError("Duplicate reference ID across days")
            matches[m.id] = m
    return list(comps.values()), list(matches.values())


async def main(apply=False):
    store = Store(ROOT / "data" / "football.sqlite3")
    collector = Collector(store)
    if not store.lease("collector", collector.owner, 60):
        await collector.client.aclose()
        raise SystemExit("Stop the managed worker before running verification")
    now = utcnow()
    previous = date(now.year-1, 12, 1) if now.month == 1 else date(now.year, now.month-1, 1)
    current = date(now.year, now.month, 1)
    report = {"checked_at": stamp(now), "product_version": "0.8.1", "free_only": True,
              "historical_payloads_saved": False, "checks": {}, "references": []}

    async def fetch(source, url):
        for _ in range(120):
            if not store.lease("collector", collector.owner, 60):
                raise RuntimeError("Collector lease lost")
            try:
                body = await collector.fetch(source, url, reference=True)
                report["references"].append(url)
                return body
            except Deferred:
                state = store.source(source)
                if state["state"] == "blocked":
                    raise RuntimeError(f"Source blocked: {source}")
                await asyncio.sleep(1)
        raise RuntimeError(f"Source cooldown exceeded verification wait: {source}")

    try:
        spanish = [c for c in store.competitions() if c["country"] == "Испания"]
        openliga = next(c for c in spanish if c["shortcut"] == "la1")
        body = await fetch("openligadb", openliga_url(openliga))
        ol_matches = parse_openliga(json.loads(body), openliga, now)
        report["openliga_season_matches"] = len(ol_matches)
        sky_matches, reference_comps = [], {}
        for month in (previous, current):
            url = f"{HTML_BASE}/la-liga-scores-fixtures/{month.isoformat()}"
            html = await fetch("skysports_html", url)
            comps, parsed = parse_month(html, month.year, month.month, now)
            reference_comps.update({c["id"]: c for c in comps})
            sky_matches.extend(parsed)
        # Reference pages must contain only the requested league.
        if {c["name"] for c in reference_comps.values()} != {"Spanish La Liga"}:
            raise SchemaError("Unexpected competitions in La Liga reference")
        ol_selected = [m for m in ol_matches if m.kickoff_at and previous <= parse_time(m.kickoff_at).date()
                       and parse_time(m.kickoff_at).date().replace(day=1) <= current]
        report["checks"]["laliga_cross_source"] = compare(ol_selected, sky_matches)
        print(json.dumps(report["checks"]["laliga_cross_source"], indent=2), flush=True)
        left_keys = {match_key(m) for m in ol_selected}
        right_keys = {match_key(m) for m in sky_matches}
        print("Unpaired team labels (transient):", sorted({team for key in left_keys ^ right_keys for team in key[1:]}), flush=True)
        for name, slug in [("Women", "womens-spanish-primera-division"), ("Copa", "spanish-copa-del-rey")]:
            url = f"{HTML_BASE}/{slug}-scores-fixtures/{current.isoformat()}"
            html = await fetch("skysports_html", url)
            comps, parsed = parse_month(html, current.year, current.month, now)
            expected = next(c for c in spanish if c["name"] == ("Women's Spanish Primera Division" if name == "Women" else "Spanish Copa del Rey"))
            relevant = [m for m in parsed if m.competition_id == expected["id"]]
            if not relevant or len(relevant) != len(parsed):
                raise SchemaError(f"Wrong competition in {name} reference")
            local = []
            # Read stored cards without importing old reference data.
            from fast_parser.domain import Match
            cursor = None
            while True:
                page = store.matches(cid=expected["id"], cursor=cursor, limit=200, now=now)
                for payload in page["items"]:
                    local.append(Match(**{k:v for k,v in payload.items() if k in Match.__dataclass_fields__}))
                cursor = page["next_cursor"]
                if not cursor:
                    break
            ids = {m.id for m in local}
            result = compare(local, [m for m in relevant if m.id in ids])
            result.update(reference_month_cards=len(relevant), independent_confirmation=False,
                          monthly_view_vs_stored_daily_cards=True)
            report["checks"][name.lower()+"_view_consistency"] = result
            print(name, json.dumps(result), flush=True)
        (ROOT / "data" / "qa").mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "qa" / "spanish-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        # Apply only after every reference fetch and comparison has succeeded.
        if apply:
            from fast_parser.verification import CoverageVerification
            cross = report["checks"]["laliga_cross_source"]
            clean = (cross["paired"] > 0 and cross["finished_pairs"] > 0 and cross["scheduled_pairs"] > 0
                     and not any(cross[k] for k in ("left_only", "right_only", "kickoff_mismatches", "status_mismatches", "missing_final_scores", "score_mismatches")))
            for c in spanish:
                if c["shortcut"] != "la1" and c["name"] not in {"Spanish La Liga", "Women's Spanish Primera Division", "Spanish Copa del Rey"}:
                    continue
                if c["shortcut"] == "la1" or c["name"] == "Spanish La Liga":
                    v = CoverageVerification(status="verified" if clean else "partial", fixtures=cross["scheduled_pairs"] > 0,
                        results=cross["finished_pairs"] > cross["score_mismatches"],
                        scope=f"{previous.isoformat()}–{current.year}-{current.month:02d}: сверка двух источников; docs/SPANISH_VERIFICATION.md",
                        evidence="OpenLigaDB la1 и открытые месячные страницы Sky Sports La Liga; scripts.verify_spanish",
                        note=f"Сопоставлено {cross['paired']}; без пары {cross['left_only']}/{cross['right_only']}; расхождения времени/статуса/счета {cross['kickoff_mismatches']}/{cross['status_mismatches']}/{cross['score_mismatches']}. Live и полнота сезона не подтверждены.")
                else:
                    key = "women_view_consistency" if c["name"] == "Women's Spanish Primera Division" else "copa_view_consistency"
                    result = report["checks"][key]
                    v = CoverageVerification(status="partial", fixtures=result["paired"] > 0,
                        results=result["finished_pairs"] > result["score_mismatches"],
                        scope=f"Сохраненные неистекшие карточки и месячная страница {current.isoformat()}; {result['paired']} пар",
                        evidence=f"Sky Sports {key}: дневной сбор и открытая месячная страница; docs/SPANISH_VERIFICATION.md",
                        note="Один источник: проверена техническая согласованность. Независимая полнота, достоверность результатов и live не подтверждены.")
                store.save_verification(c["id"], v)
        print("Applied labels" if apply else "Report only", flush=True)
    finally:
        await collector.client.aclose()
        store.release(collector.owner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
