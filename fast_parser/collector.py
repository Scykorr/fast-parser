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

log = logging.getLogger("fast_parser.collector")


class Deferred(Exception):
    pass


class SourceFailure(Exception):
    pass


class Collector:
    def __init__(self, store: Store):
        self.store = store
        self.owner = str(uuid.uuid4())
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=False, headers={"User-Agent": "FastParser/0.7 (local football results collector)"})
        self.stopped = asyncio.Event()

    async def fetch(self, source: str, url: str) -> str:
        spacing = 12.5 if source == "openligadb" else 20
        if not await asyncio.to_thread(self.store.reserve_request, source, spacing):
            raise Deferred()
        state = await asyncio.to_thread(self.store.source, source)
        try:
            async with self.client.stream("GET", url) as response:
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
            await asyncio.to_thread(self.store.source_error, source, str(exc), delay)
            raise SourceFailure(str(exc)) from exc

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
        selected = [m for m in matches if not m.kickoff_at or start <= parse_time(m.kickoff_at) < end]
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
        selected = [m for m in matches if not m.kickoff_at or start <= parse_time(m.kickoff_at) < end]
        await asyncio.to_thread(self.store.upsert_matches, selected, now)
        for comp in competitions:
            await asyncio.to_thread(self.store.mark_sync, comp["id"], True, now.timestamp() + settings.fixtures_interval_seconds,
                                    sum(m.competition_id == comp["id"] for m in selected))
        interval = settings.live_interval_seconds if any(m.status in {"live", "paused"} for m in matches) else settings.results_interval_seconds if day <= today else settings.fixtures_interval_seconds
        due[day.isoformat()] = now.timestamp() + interval
        due = {d: t for d, t in due.items() if d >= (today - timedelta(days=3)).isoformat()}
        await asyncio.to_thread(self.store.set_meta, "html_due", json.dumps(due))
        await asyncio.to_thread(self.store.source_success, "skysports_html")
        return True

    async def run(self):
        last_cleanup = 0
        try:
            while not self.stopped.is_set():
                try:
                    leader = await asyncio.to_thread(self.store.lease, "collector", self.owner, 60)
                    if not leader:
                        await asyncio.sleep(2)
                        continue
                    now = utcnow()
                    await asyncio.to_thread(self.store.set_meta, "worker_heartbeat", stamp(now))
                    if now.timestamp() - last_cleanup >= 5:
                        deleted = await asyncio.to_thread(self.store.cleanup)
                        if deleted:
                            log.info("cleanup deleted=%s", deleted)
                        last_cleanup = now.timestamp()
                    settings = await asyncio.to_thread(self.store.settings)
                    if settings.in_window(now):
                        updated = await asyncio.to_thread(self.store.get_meta, "catalog_updated_at")
                        if not updated or (now - parse_time(updated)).total_seconds() > 86400:
                            try:
                                await self.discover()
                            except (Deferred, SourceFailure):
                                pass
                        comp = await asyncio.to_thread(self.store.due, now.timestamp())
                        if comp and comp["id"].startswith("ol:"):
                            try:
                                await self.sync_league(comp)
                            except Deferred:
                                pass
                            except SourceFailure as exc:
                                state = await asyncio.to_thread(self.store.source, "openligadb")
                                await asyncio.to_thread(self.store.mark_sync, comp["id"], False, max(now.timestamp() + 60, state["next_allowed"]), error=str(exc)[:500])
                        if settings.html_enabled:
                            try:
                                await self.sync_html()
                            except (Deferred, SourceFailure):
                                pass
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("worker cycle failed")
                    await asyncio.sleep(5)
        finally:
            await self.client.aclose()
            await asyncio.to_thread(self.store.release, self.owner)
