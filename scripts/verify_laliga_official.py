"""Official free reference; aggregate-only report, no historical match import."""
import asyncio
import argparse
import json
from bs4 import BeautifulSoup
from fast_parser.adapters import SchemaError, openliga_url, parse_openliga
from fast_parser.collector import Collector, Deferred, SourceFailure
from fast_parser.config import ROOT
from fast_parser.domain import Match, parse_time, stamp, utcnow
from fast_parser.storage import Store
from fast_parser.verification import CoverageVerification
from scripts.verify_spanish import ALIAS_MAP, normalized, compare, match_key

for a, b in {"Málaga CF":"Malaga", "R. Racing Club":"Racing Santander", "Atlético de Madrid":"Atletico Madrid", "RCD Espanyol de Barcelona":"Espanyol", "Valencia CF":"Valencia", "Sevilla FC":"Sevilla", "Getafe CF":"Getafe", "Villarreal CF":"Villarreal"}.items():
    ALIAS_MAP[normalized(a)] = normalized(b)

def parse_official(html, division, season, week):
    tag = BeautifulSoup(html, "html.parser").select_one("script#__NEXT_DATA__")
    if tag is None:
        raise SchemaError("Missing public match data")
    page = json.loads(tag.get_text())["props"]["pageProps"]
    if page.get("competition") != division or str(page.get("season")) != str(season) or page.get("gameweek", {}).get("week") != week:
        raise SchemaError("Wrong competition/season/round")
    rows = page.get("matches", [])
    if not rows or len({r["id"] for r in rows}) != len(rows):
        raise SchemaError("Empty round or duplicate IDs")
    result = []
    for row in rows:
        status = {"PreMatch":"scheduled", "FullTime":"finished"}.get(row["status"])
        if not status:
            raise SchemaError("Unsupported status")
        teams = [{"name": row[k].get("nickname") or row[k]["name"]} for k in ("home_team", "away_team")]
        scores = [row.get("home_score"), row.get("away_score")]
        if any(not t["name"].strip() for t in teams) or status == "finished" and any(type(s) is not int or s < 0 for s in scores):
            raise SchemaError("Missing teams or invalid final score")
        result.append(Match(id="reference:"+str(row["id"]), external_id=str(row["id"]), competition_id=division,
            source="official_reference", home_team=teams[0], away_team=teams[1],
            kickoff_at=stamp(parse_time(row["date"])), status=status, score_home=scores[0], score_away=scores[1]))
    return result

async def main(apply=False):
    store = Store(ROOT / "data/football.sqlite3")
    collector = Collector(store)
    if not store.lease("collector", collector.owner, 60):
        await collector.client.aclose()
        raise SystemExit("Stop worker before verification")
    report = {"checked_at":stamp(utcnow()), "free_only":True, "historical_payloads_saved":False,
              "live_verified":False, "references":[], "rounds":{}, "comparison":{}}
    async def fetch(source, url):
        failures = 0
        for _ in range(120):
            if not store.lease("collector", collector.owner, 60):
                raise RuntimeError("Lease lost")
            try:
                body = await collector.fetch(source, url, reference=True)
                store.source_success(source)
                report["references"].append(url)
                return body
            except Deferred:
                if store.source(source)["state"] == "blocked":
                    raise RuntimeError("Reference blocked; no bypass")
                await asyncio.sleep(1)
            except SourceFailure:
                failures += 1
                if failures >= 2:
                    raise
        raise RuntimeError("Source cooldown")
    try:
        comp = next(c for c in store.competitions() if c["shortcut"] == "la1")
        existing = parse_openliga(json.loads(await fetch("openligadb", openliga_url(comp))), comp, utcnow())
        for division, slug, weeks in [("primera-division","laliga-easports",(7,8)), ("segunda-division","laliga-hypermotion",(8,9))]:
            official = []
            for week in weeks:
                rows = parse_official(await fetch("laliga_reference", f"https://www.laliga.com/{slug}/resultados/2026-27/jornada-{week}"), division, 2026, week)
                official.extend(rows)
                report["rounds"][f"{division}:{week}"] = {"cards":len(rows), "finished":sum(m.status=="finished" for m in rows), "scheduled":sum(m.status=="scheduled" for m in rows)}
                print(division, week, report["rounds"][f"{division}:{week}"], flush=True)
            if division == "primera-division":
                dates = {parse_time(m.kickoff_at).date() for m in official}
                r = compare([m for m in existing if parse_time(m.kickoff_at).date() in dates], official)
                report["comparison"]["openliga_vs_official"] = r
                print(json.dumps(r), flush=True)
                keys = {match_key(m) for m in official}
                other = {match_key(m) for m in existing if parse_time(m.kickoff_at).date() in dates}
                print("Unpaired comparison labels (memory only):", sorted(keys ^ other), flush=True)
            else:
                report["comparison"]["segunda"] = {"official_structure_checked":True, "independent_confirmation":False, "production_adapter_available":False}
        r = report["comparison"]["openliga_vs_official"]
        clean = r["finished_pairs"] > 0 and r["scheduled_pairs"] > 0 and not any(r[k] for k in ("left_only","right_only","kickoff_mismatches","status_mismatches","missing_final_scores","score_mismatches"))
        verification = CoverageVerification(status="verified" if clean else "partial", fixtures=r["scheduled_pairs"]>0, results=r["finished_pairs"]>0,
            scope="Примера 2026/27: только туры 7–8, сверка 06.10.2026",
            evidence="Официальные страницы LaLiga; scripts.verify_laliga_official; docs/SPANISH_VERIFICATION.md",
            note=f"Пар {r['paired']}; время/статус/счет {r['kickoff_mismatches']}/{r['status_mismatches']}/{r['score_mismatches']}; без пары {r['left_only']}/{r['right_only']}. Live и остальные туры не подтверждены; ранее обнаружены расхождения сентября–октября.")
        if apply:
            store.save_verification(comp["id"], verification)
        target = ROOT / "data/qa/laliga-official-verification.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await collector.client.aclose()
        store.release(collector.owner)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
