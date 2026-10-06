from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from .adapters import API_BASE, HTML_BASE, SchemaError, catalog_openliga, openliga_url, parse_openliga, parse_sky
from .domain import parse_time, stamp, utcnow
from .storage import Store
from .english import OFFICIAL_SOURCES, PL_SOURCE, EFL_SOURCE
from .sources import FNL_SOURCE, DFL_SOURCE, SA_SOURCE, SB_SOURCE

log = logging.getLogger("fast_parser.collector")


class Deferred(Exception):
    pass


class SourceFailure(Exception):
    pass


class ReferenceUnavailable(SourceFailure):
    """A missing verification page does not disable the production source."""


class Collector:
    def __init__(self, store: Store):
        self.store = store
        self.owner = str(uuid.uuid4())
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=False, headers={"User-Agent": "FastParser/0.8 (local football results collector)"})
        self.stopped = asyncio.Event()
        self.request_deadline_seconds = 30

    async def fetch(self, source: str, url: str, reference=False) -> str:
        try:
            async with asyncio.timeout(self.request_deadline_seconds):
                return await self._fetch(source, url, reference=True) if reference else await self._fetch(source, url)
        except TimeoutError as exc:
            await asyncio.to_thread(self.store.source_error, source, "Общий таймаут запроса 30 секунд", 60)
            raise SourceFailure("Общий таймаут запроса") from exc

    async def _fetch(self, source: str, url: str, reference=False) -> str:
        spacing = 12.5 if source == "openligadb" else 30 if source in OFFICIAL_SOURCES else 20
        if not await asyncio.to_thread(self.store.reserve_request, source, spacing):
            raise Deferred()
        state = await asyncio.to_thread(self.store.source, source)
        try:
            async with self.client.stream("GET", url) as response:
                if reference and response.status_code == 404:
                    raise ReferenceUnavailable(f"HTTP 404: контрольная страница отсутствует: {url}")
                if response.status_code in {401, 403}:
                    await asyncio.to_thread(self.store.source_error, source, f"HTTP {response.status_code}: доступ остановлен до проверки", 3600, True)
                    raise SourceFailure(f"HTTP {response.status_code}")
                if response.status_code == 429:
                    raw = response.headers.get("Retry-After", "60")
                    try:
                        delay = max(60, float(raw))
                    except ValueError:
                        try:
                            delay = max(60, (parsedate_to_datetime(raw) - utcnow()).total_seconds())
                        except (ValueError, TypeError):
                            delay = 60
                    await asyncio.to_thread(self.store.source_error, source, "HTTP 429: ограничение запросов", delay)
                    raise SourceFailure("HTTP 429")
                if response.is_redirect and "captcha" in response.headers.get("location","").lower():
                    await asyncio.to_thread(self.store.source_error,source,"CAPTCHA: источник остановлен",3600,True)
                    raise SourceFailure("CAPTCHA")
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 5_000_000:
                        raise SchemaError("Ответ превышает 5 MB")
                    chunks.append(chunk)
                body = b"".join(chunks).decode("utf-8")
                if any(s in body.lower() for s in ("verify you are human", "cf-chl-", "captcha-container")):
                    await asyncio.to_thread(self.store.source_error, source, "CAPTCHA: источник остановлен", 3600, True)
                    raise SourceFailure("CAPTCHA")
                return body
        except SourceFailure:
            raise
        except (httpx.HTTPError, UnicodeError, SchemaError) as exc:
            failures = state["failures"] + 1
            delay = min(3600, 900 * 2 ** max(0, failures - 5)) if failures >= 5 else min(300, 15 * 2 ** failures) + random.uniform(0, 5)
            if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500:
                delay = 3600
            message = str(exc) or type(exc).__name__
            await asyncio.to_thread(self.store.source_error, source, message, delay)
            raise SourceFailure(message) from exc

    async def schema_failure(self, source, exc):
        await asyncio.to_thread(self.store.source_error, source, f"Карантин: {exc}", 900)
        log.warning("invalid response source=%s error=%s", source, exc)

    async def discover(self):
        body = await self.fetch("openligadb", API_BASE + "/getavailableleagues")
        try:
            catalog = catalog_openliga(json.loads(body), utcnow())
        except (ValueError, SchemaError) as exc:
            await self.schema_failure("openligadb", exc)
            raise SourceFailure(str(exc)) from exc
        await asyncio.to_thread(self.store.catalog, catalog)
        await asyncio.to_thread(self.store.set_meta, "catalog_updated_at", stamp(utcnow()))
        await asyncio.to_thread(self.store.source_success, "openligadb")
        return len(catalog)

    def bounds(self, settings, now):
        if settings.mode == "absolute":
            start, end = parse_time(settings.date_from), parse_time(settings.date_to)
            # Historical import is not allowed to resurrect expired matches without finished_at.
            return max(start, now - timedelta(days=3)), end
        return now - timedelta(days=settings.lookback_days), now + timedelta(days=settings.lookahead_days)

    async def sync_league(self, comp: dict):
        settings = await asyncio.to_thread(self.store.settings)
        now = utcnow()
        body = await self.fetch("openligadb", openliga_url(comp))
        try:
            matches = await asyncio.to_thread(parse_openliga, json.loads(body), comp, now)
        except (ValueError, SchemaError) as exc:
            await self.schema_failure("openligadb", exc)
            raise SourceFailure(str(exc)) from exc
        start, end = self.bounds(settings, now)
        pending = await asyncio.to_thread(self.store.pending_ids, comp["id"])
        selected = [m for m in matches if m.id in pending or not m.kickoff_at or start <= parse_time(m.kickoff_at) < end]
        count = await asyncio.to_thread(self.store.upsert_matches, selected, now)
        intervals = [settings.fixtures_interval_seconds]
        for match in selected:
            if match.status == "finished":
                intervals.append(settings.final_recheck_interval_seconds)
            elif match.kickoff_at and parse_time(match.kickoff_at) <= now:
                age = (now - parse_time(match.kickoff_at)).total_seconds()
                intervals.append(max(settings.results_interval_seconds, 21600 if age > 86400 else 3600 if age > 14400 else 300))
            elif match.kickoff_at:
                intervals.append(max(120, (parse_time(match.kickoff_at) - now).total_seconds()))
        next_poll = now.timestamp() + min(intervals)
        await asyncio.to_thread(self.store.mark_sync, comp["id"], True, next_poll, count)
        await asyncio.to_thread(self.store.source_success, "openligadb")
        log.info("sync source=openligadb league=%s count=%s", comp["id"], count)
        return count

    async def sync_html(self):
        settings = await asyncio.to_thread(self.store.settings)
        now = utcnow()
        start, end = self.bounds(settings, now)
        # Today first; one HTML request updates all competitions on that date.
        from zoneinfo import ZoneInfo
        today = now.astimezone(ZoneInfo("Europe/London")).date()
        first_day = start.astimezone(ZoneInfo("Europe/London")).date()
        last_day = end.astimezone(ZoneInfo("Europe/London")).date()
        days = [first_day + timedelta(days=i) for i in range(max(0, min(94, (last_day - first_day).days + 1)))]
        unresolved = await asyncio.to_thread(self.store.unresolved, now)
        extra_days = {parse_time(m["kickoff_at"]).astimezone(ZoneInfo("Europe/London")).date()
                      for m in unresolved if m["id"].startswith("sky:")}
        days = list(set(days) | extra_days)
        days.sort(key=lambda d: (abs((d - today).days), d))
        due = json.loads(await asyncio.to_thread(self.store.get_meta, "html_due", "{}"))
        day = next((d for d in days if due.get(d.isoformat(), 0) <= now.timestamp()), None)
        if day is None:
            return False
        body = await self.fetch("skysports_html", HTML_BASE + "/football-scores-fixtures/" + day.isoformat())
        try:
            competitions, matches = await asyncio.to_thread(parse_sky, body, day, now)
        except (ValueError, SchemaError) as exc:
            await self.schema_failure("skysports_html", exc)
            raise SourceFailure(str(exc)) from exc
        await asyncio.to_thread(self.store.catalog, competitions)
        pending = {m["id"] for m in unresolved}
        selected = [m for m in matches if m.id in pending or not m.kickoff_at or start <= parse_time(m.kickoff_at) < end]
        await asyncio.to_thread(self.store.upsert_matches, selected, now)
        for comp in competitions:
            await asyncio.to_thread(self.store.mark_sync, comp["id"], True, now.timestamp() + settings.fixtures_interval_seconds,
                                    sum(m.competition_id == comp["id"] for m in selected))
        interval = settings.live_interval_seconds if any(m.status in {"live", "paused"} for m in matches) else settings.results_interval_seconds if day <= today else settings.fixtures_interval_seconds
        due[day.isoformat()] = now.timestamp() + interval
        due = {d: t for d, t in due.items() if d >= (today - timedelta(days=3)).isoformat() or d in {v.isoformat() for v in extra_days}}
        await asyncio.to_thread(self.store.set_meta, "html_due", json.dumps(due))
        await asyncio.to_thread(self.store.source_success, "skysports_html")
        return True

    async def sync_official(self):
        from .official import LEAGUES, SOURCE, parse_page
        settings = await asyncio.to_thread(self.store.settings)
        now = utcnow()
        start, end = self.bounds(settings, now)
        plan = json.loads(await asyncio.to_thread(self.store.get_meta, "official_plan", "{}"))
        for division in LEAGUES:
            plan.setdefault(division, {"division": division, "due": 0})
        eligible = [(key, job) for key, job in plan.items() if job["due"] <= now.timestamp()]
        if not eligible:
            return False
        key, job = min(eligible, key=lambda pair: pair[1]["due"])
        division = job["division"]
        slug = LEAGUES[division][0]
        suffix = f"/{job['season']}-{str(int(job['season'])+1)[-2:]}/jornada-{job['week']}" if "week" in job else ""
        url = f"https://www.laliga.com/{slug}/resultados{suffix}"
        body = await self.fetch(SOURCE, url)
        try:
            comp, matches, rounds, current = await asyncio.to_thread(parse_page, body, division, job.get("season"), job.get("week"))
        except (ValueError, SchemaError) as exc:
            await self.schema_failure(SOURCE, exc)
            raise SourceFailure(str(exc)) from exc
        await asyncio.to_thread(self.store.catalog, [comp])
        pending = await asyncio.to_thread(self.store.pending_ids, comp["id"])
        selected = [m for m in matches if m.id in pending or start <= parse_time(m.kickoff_at) < end]
        for match in selected:
            match.source_url = url
        count = await asyncio.to_thread(self.store.upsert_matches, selected, now)
        interval = settings.results_interval_seconds if any(m.status == "unknown" or m.status == "scheduled" and parse_time(m.kickoff_at) <= now for m in selected) else settings.final_recheck_interval_seconds if any(m.status == "finished" for m in selected) else settings.fixtures_interval_seconds
        plan[key]["due"] = now.timestamp() + interval
        # Round dates are approximate boundaries; use a week of margin, then filter matches precisely.
        if "week" not in job:
            for r in rounds:
                if start - timedelta(days=7) <= parse_time(r["date"]) <= end + timedelta(days=7):
                    round_key = f"{division}:{comp['season']}:{r['week']}"
                    plan.setdefault(round_key, dict(division=division, season=comp["season"], week=r["week"], due=now.timestamp()+interval if r["week"] == current else 0))
            plan = {k:v for k,v in plan.items() if "week" not in v or v["division"] != division or v.get("season") == comp["season"]}
        await asyncio.to_thread(self.store.set_meta, "official_plan", json.dumps(plan))
        await asyncio.to_thread(self.store.mark_sync, comp["id"], True, now.timestamp()+interval, count)
        await asyncio.to_thread(self.store.source_success, SOURCE)
        return count

    async def sync_english(self, source):
        from .english import PL_BASE, bootstrap, parse_pl, parse_efl, efl_url
        settings = await asyncio.to_thread(self.store.settings)
        now = utcnow()
        due = float(await asyncio.to_thread(self.store.get_meta, "english_due:"+source, "0"))
        if due > now.timestamp():
            return False
        start, end = self.bounds(settings, now)
        try:
            if source == PL_SOURCE:
                cached = json.loads(await asyncio.to_thread(self.store.get_meta, "pl_teams", "{}"))
                if not cached or cached["refresh_after"] <= now.timestamp():
                    reference = bootstrap(json.loads(await self.fetch(source, PL_BASE+"/bootstrap-static/")))
                    reference["refresh_after"] = now.timestamp()+86400
                    await asyncio.to_thread(self.store.set_meta, "pl_teams", json.dumps(reference))
                    await asyncio.to_thread(self.store.source_success, source)
                    return False
                comp, matches = parse_pl(json.loads(await self.fetch(source, PL_BASE+"/fixtures/")), cached)
                continuation = False
            else:
                year = str(now.year if now.month >= 7 else now.year-1)
                cursor = json.loads(await asyncio.to_thread(self.store.get_meta, "efl_cursor", "{}"))
                unresolved = await asyncio.to_thread(self.store.unresolved, now)
                older = [parse_time(m["kickoff_at"]) for m in unresolved if m["id"].startswith("efl:")]
                query_start = min([start]+older)
                base = efl_url(year, query_start, end)
                if cursor and cursor.get("base") != base:
                    cursor = {}
                url = cursor.get("next") or base
                comp, matches, nxt = parse_efl(json.loads(await self.fetch(source,url)), year, url)
                seen = set(cursor.get("seen", []))
                if seen & {m.id for m in matches} or cursor.get("pages",0) >= 50:
                    raise SchemaError("EFL: повтор матчей между страницами или превышение 50 страниц")
                seen.update(m.id for m in matches)
                continuation = bool(nxt)
                next_cursor = json.dumps(dict(base=base,next=nxt,seen=list(seen),pages=cursor.get("pages",0)+1)) if nxt else "{}"
            await asyncio.to_thread(self.store.catalog, [comp])
            pending = await asyncio.to_thread(self.store.pending_ids, comp["id"])
            selected = [m for m in matches if m.id in pending or not m.kickoff_at or start <= parse_time(m.kickoff_at) < end]
            count = await asyncio.to_thread(self.store.upsert_matches, selected, now)
            if source == EFL_SOURCE:
                await asyncio.to_thread(self.store.set_meta, "efl_cursor", next_cursor)
            intervals = [settings.fixtures_interval_seconds]
            for m in selected:
                if m.status == "live":
                    intervals.append(settings.live_interval_seconds)
                elif m.status == "unknown" or m.status == "scheduled" and m.kickoff_at and parse_time(m.kickoff_at) <= now:
                    intervals.append(settings.results_interval_seconds)
                elif m.status == "finished":
                    intervals.append(settings.final_recheck_interval_seconds)
                elif m.kickoff_at:
                    intervals.append(max(120,(parse_time(m.kickoff_at)-now).total_seconds()))
            next_poll = now.timestamp() + (30 if continuation else min(intervals))
            await asyncio.to_thread(self.store.set_meta, "english_due:"+source, str(next_poll))
            await asyncio.to_thread(self.store.mark_sync, comp["id"], True, next_poll, count)
            await asyncio.to_thread(self.store.source_success, source)
            return count
        except (ValueError, SchemaError) as exc:
            await self.schema_failure(source, exc)
            raise SourceFailure(str(exc)) from exc

    async def sync_italian(self, source):
        from .italian import SA_SEASONS, select_season, sa_url, parse_sa, sb_url, parse_sb
        settings = await asyncio.to_thread(self.store.settings)
        now = utcnow()
        year = str(now.year if now.month >= 7 else now.year-1)
        start,end = self.bounds(settings,now)
        try:
            if source == SA_SOURCE:
                due = float(await asyncio.to_thread(self.store.get_meta,"italian_due:"+source,"0"))
                if due > now.timestamp():
                    return False
                cache = json.loads(await asyncio.to_thread(self.store.get_meta,"seriea_season","{}"))
                if not cache or cache.get("year") != year or cache["refresh_after"] <= now.timestamp():
                    season = select_season(json.loads(await self.fetch(source,SA_SEASONS)),year)
                    await asyncio.to_thread(self.store.set_meta,"seriea_season",json.dumps(dict(year=year,season=season,refresh_after=now.timestamp()+86400)))
                    await asyncio.to_thread(self.store.source_success,source)
                    return False
                comp,matches = parse_sa(json.loads(await self.fetch(source,sa_url(cache["season"]))),year,cache["season"])
            else:
                plan = json.loads(await asyncio.to_thread(self.store.get_meta,"legab_plan","{}"))
                plan = {key:job for key,job in plan.items() if job.get("year") == year}
                plan.setdefault("current",dict(year=year,due=0))
                jobs = [(key,job) for key,job in plan.items() if job["due"] <= now.timestamp()]
                if not jobs:
                    return False
                key,job = min(jobs,key=lambda pair:pair[1]["due"])
                comp,matches,current,available,first,last = parse_sb(await self.fetch(source,sb_url(year,job.get("week"))),year,job.get("week"))
            await asyncio.to_thread(self.store.catalog,[comp])
            pending = await asyncio.to_thread(self.store.pending_ids,comp["id"])
            # An unknown clock time must not import every date-only fixture of the season.
            from zoneinfo import ZoneInfo
            first_day = start.astimezone(ZoneInfo("Europe/Rome")).date().isoformat()
            last_day = end.astimezone(ZoneInfo("Europe/Rome")).date().isoformat()
            selected = [m for m in matches if m.id in pending or
                        (start <= parse_time(m.kickoff_at) < end if m.kickoff_at else
                         first_day <= m.scheduled_date <= last_day if m.scheduled_date else True)]
            count = await asyncio.to_thread(self.store.upsert_matches,selected,now)
            intervals = [settings.fixtures_interval_seconds]
            for m in selected:
                if m.status == "unknown" or m.status == "scheduled" and m.kickoff_at and parse_time(m.kickoff_at) <= now:
                    intervals.append(settings.results_interval_seconds)
                elif m.status == "finished":
                    intervals.append(settings.final_recheck_interval_seconds)
                elif m.kickoff_at:
                    intervals.append(max(120,(parse_time(m.kickoff_at)-now).total_seconds()))
            next_poll = now.timestamp()+min(intervals)
            if source == SA_SOURCE:
                await asyncio.to_thread(self.store.set_meta,"italian_due:"+source,str(next_poll))
            else:
                plan[key]["due"] = next_poll
                plan.setdefault(str(current),dict(year=year,week=current,due=next_poll))
                for neighbor,needed in ((current-1,first>start),(current+1,last<end)):
                    if needed and neighbor in available:
                        plan.setdefault(str(neighbor),dict(year=year,week=neighbor,due=0))
                await asyncio.to_thread(self.store.set_meta,"legab_plan",json.dumps(plan))
            await asyncio.to_thread(self.store.mark_sync,comp["id"],True,next_poll,count)
            await asyncio.to_thread(self.store.source_success,source)
            return count
        except (ValueError,SchemaError) as exc:
            await self.schema_failure(source,exc)
            raise SourceFailure(str(exc)) from exc

    async def sync_german(self):
        from .german import LEAGUES, page_url, parse_page
        settings = await asyncio.to_thread(self.store.settings)
        now = utcnow(); year = str(now.year if now.month >= 7 else now.year-1)
        start, end = self.bounds(settings, now)
        plan = json.loads(await asyncio.to_thread(self.store.get_meta, "german_plan", "{}"))
        plan = {k:v for k,v in plan.items() if v.get("year") == year}
        for division in LEAGUES:
            plan.setdefault(division, dict(division=division, year=year, due=0))
        jobs = [(k,v) for k,v in plan.items() if v["due"] <= now.timestamp()]
        if not jobs:
            return False
        key, job = min(jobs, key=lambda pair: pair[1]["due"])
        division = job["division"]
        body = await self.fetch(DFL_SOURCE, page_url(division, year, job.get("week")))
        try:
            comp, matches, current, first, last = await asyncio.to_thread(parse_page, body, division, year, job.get("week"))
        except (ValueError, SchemaError) as exc:
            await self.schema_failure(DFL_SOURCE, exc)
            raise SourceFailure(str(exc)) from exc
        await asyncio.to_thread(self.store.catalog, [comp])
        pending = await asyncio.to_thread(self.store.pending_ids, comp["id"])
        from zoneinfo import ZoneInfo
        first_day = start.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        last_day = end.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        selected = [m for m in matches if m.id in pending or
                    (start <= parse_time(m.kickoff_at) < end if m.kickoff_at else
                     first_day <= m.scheduled_date <= last_day)]
        count = await asyncio.to_thread(self.store.upsert_matches, selected, now)
        interval = settings.results_interval_seconds if any(m.status == "unknown" or
            m.status == "scheduled" and m.kickoff_at and parse_time(m.kickoff_at) <= now for m in selected) else settings.fixtures_interval_seconds
        if any(m.status == "finished" for m in selected):
            interval = min(interval, settings.final_recheck_interval_seconds)
        for m in selected:
            if m.kickoff_at and parse_time(m.kickoff_at) > now:
                interval = min(interval, max(120, (parse_time(m.kickoff_at)-now).total_seconds()))
        due = now.timestamp()+interval
        plan[key]["due"] = due
        plan.setdefault(f"{division}:{current}", dict(division=division, year=year, week=current, due=due))
        for neighbor, needed in ((current-1, first > start), (current+1, last < end)):
            if needed and 1 <= neighbor <= 34:
                plan.setdefault(f"{division}:{neighbor}", dict(division=division, year=year, week=neighbor, due=0))
        await asyncio.to_thread(self.store.set_meta, "german_plan", json.dumps(plan))
        await asyncio.to_thread(self.store.mark_sync, comp["id"], True, due, count)
        await asyncio.to_thread(self.store.source_success, DFL_SOURCE)
        return count

    async def sync_russian(self):
        from .russian import ACTIVE, active_season, page_url, parse_page
        from zoneinfo import ZoneInfo
        settings = await asyncio.to_thread(self.store.settings)
        now = utcnow(); year = str(now.year if now.month >= 7 else now.year-1)
        start,end = self.bounds(settings,now)
        try:
            cache = json.loads(await asyncio.to_thread(self.store.get_meta,"fnl_season","{}"))
            if not cache or cache.get("year") != year or cache["refresh_after"] <= now.timestamp():
                season = active_season(json.loads(await self.fetch(FNL_SOURCE,ACTIVE)),year)
                await asyncio.to_thread(self.store.set_meta,"fnl_season",json.dumps(dict(year=year,season=season,refresh_after=now.timestamp()+86400)))
                await asyncio.to_thread(self.store.source_success,FNL_SOURCE)
                return False
            plan = json.loads(await asyncio.to_thread(self.store.get_meta,"fnl_plan","{}"))
            first = start.astimezone(ZoneInfo("Europe/Moscow")); last = end.astimezone(ZoneInfo("Europe/Moscow"))
            wanted = set(); cursor = first.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
            while cursor <= last:
                wanted.add((cursor.year,cursor.month))
                cursor = cursor.replace(year=cursor.year+1,month=1) if cursor.month==12 else cursor.replace(month=cursor.month+1)
            comp_id = f"fnl:first-league:{year}"
            unresolved = await asyncio.to_thread(self.store.unresolved,now)
            for m in unresolved:
                if m["competition_id"] == comp_id and m["kickoff_at"]:
                    dt = parse_time(m["kickoff_at"]).astimezone(ZoneInfo("Europe/Moscow"))
                    wanted.add((dt.year,dt.month))
            plan = {k:v for k,v in plan.items() if v.get("season") == cache["season"] and (v["year"],v["month"]) in wanted}
            for y,month in sorted(wanted):
                plan.setdefault(f"{y}-{month}",dict(year=y,month=month,season=cache["season"],offset=0,seen=[],due=0))
            jobs = [(k,v) for k,v in plan.items() if v["due"] <= now.timestamp()]
            if not jobs:
                return False
            key,job = min(jobs,key=lambda pair:pair[1]["due"])
            comp,matches,nxt = parse_page(json.loads(await self.fetch(FNL_SOURCE,page_url(cache["season"],job["month"],job["year"],job["offset"]))),year,cache["season"],job["month"],job["year"],job["offset"])
            seen = set(job["seen"])
            if seen & {m.id for m in matches}:
                raise SchemaError("ФНЛ: повтор ID между страницами")
            seen.update(m.id for m in matches)
            await asyncio.to_thread(self.store.catalog,[comp])
            pending = await asyncio.to_thread(self.store.pending_ids,comp["id"])
            selected = [m for m in matches if m.id in pending or
                        (start <= parse_time(m.kickoff_at) < end if m.kickoff_at else
                         m.scheduled_date and first.date().isoformat() <= m.scheduled_date <= last.date().isoformat())]
            count = await asyncio.to_thread(self.store.upsert_matches,selected,now)
            intervals = [job.get("interval",settings.fixtures_interval_seconds)]
            for m in selected:
                if m.status in {"live","paused"}:intervals.append(settings.live_interval_seconds)
                elif m.status=="unknown" or m.status=="scheduled" and m.kickoff_at and parse_time(m.kickoff_at)<=now:intervals.append(settings.results_interval_seconds)
                elif m.status=="finished":intervals.append(settings.final_recheck_interval_seconds)
                elif m.kickoff_at:intervals.append(max(120,(parse_time(m.kickoff_at)-now).total_seconds()))
            interval = min(intervals)
            job.update(offset=nxt if nxt is not None else 0,seen=list(seen) if nxt is not None else [],
                       due=now.timestamp()+(30 if nxt is not None else interval))
            if nxt is not None:job["interval"]=interval
            else:job.pop("interval",None)
            await asyncio.to_thread(self.store.set_meta,"fnl_plan",json.dumps(plan))
            await asyncio.to_thread(self.store.mark_sync,comp["id"],True,job["due"],count)
            await asyncio.to_thread(self.store.source_success,FNL_SOURCE)
            return count
        except (ValueError,SchemaError) as exc:
            await self.schema_failure(FNL_SOURCE,exc)
            raise SourceFailure(str(exc)) from exc

    async def source_cycle(self, source):
        now = utcnow()
        settings = await asyncio.to_thread(self.store.settings)
        if not settings.in_window(now):
            return
        if source == "laliga_reference":
            await self.sync_official()
            return
        if source in {PL_SOURCE, EFL_SOURCE}:
            await self.sync_english(source)
            return
        if source == FNL_SOURCE:
            await self.sync_russian()
            return
        if source == DFL_SOURCE:
            await self.sync_german()
            return
        if source in {SA_SOURCE, SB_SOURCE}:
            await self.sync_italian(source)
            return
        if settings.official_only:
            return
        if source == "skysports_html":
            if settings.html_enabled:
                await self.sync_html()
            return
        updated = await asyncio.to_thread(self.store.get_meta, "catalog_updated_at")
        if not updated or (now - parse_time(updated)).total_seconds() > 86400:
            try:
                await self.discover()
            except (Deferred, SourceFailure):
                pass
        comp = await asyncio.to_thread(self.store.due, now.timestamp())
        if comp:
            try:
                await self.sync_league(comp)
            except SourceFailure as exc:
                state = await asyncio.to_thread(self.store.source, source)
                await asyncio.to_thread(self.store.mark_sync, comp["id"], False,
                                        max(now.timestamp() + 60, state["next_allowed"]), error=str(exc)[:500])

    async def run(self):
        jobs = {}
        last_cleanup = 0
        try:
            while not self.stopped.is_set():
                try:
                    leader = await asyncio.to_thread(self.store.lease, "collector", self.owner, 60)
                    if not leader:
                        for job in jobs.values():
                            job.cancel()
                        if jobs:
                            await asyncio.gather(*jobs.values(), return_exceptions=True)
                        jobs.clear()
                        await asyncio.sleep(2)
                        continue
                    now = utcnow()
                    await asyncio.to_thread(self.store.set_meta, "worker_heartbeat", stamp(now))
                    if now.timestamp() - last_cleanup >= 5:
                        await asyncio.to_thread(self.store.cleanup)
                        last_cleanup = now.timestamp()
                    # At most one active task per source; cooldown is still persisted atomically.
                    for source in ("laliga_reference", PL_SOURCE, EFL_SOURCE, SA_SOURCE, SB_SOURCE, DFL_SOURCE, FNL_SOURCE, "openligadb", "skysports_html"):
                        job = jobs.get(source)
                        if job and job.done():
                            try:
                                job.result()
                            except (Deferred, SourceFailure):
                                pass
                            except Exception:
                                log.exception("source cycle failed source=%s", source)
                            jobs.pop(source)
                        if source not in jobs:
                            jobs[source] = asyncio.create_task(self.source_cycle(source))
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Do not leave requests running if lease renewal / database access fails.
                    for job in jobs.values():
                        job.cancel()
                    if jobs:
                        await asyncio.gather(*jobs.values(), return_exceptions=True)
                    jobs.clear()
                    log.exception("worker cycle failed")
                    await asyncio.sleep(5)
        finally:
            for job in jobs.values():
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs.values(), return_exceptions=True)
            await self.client.aclose()
            await asyncio.to_thread(self.store.release, self.owner)
