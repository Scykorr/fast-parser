from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .collector import Collector
from .config import ROOT, Settings
from .domain import parse_time, stamp, utcnow
from .storage import Store
from .verification import CoverageVerification
from .english import OFFICIAL_SOURCES, OFFICIAL_PREFIXES, PL_SOURCE, EFL_SOURCE, catalog as english_catalog
from .sources import DFL_SOURCE, SA_SOURCE, SB_SOURCE, source_for_comp
from .italian import catalog as italian_catalog
from .german import catalog as german_catalog


def create_app(db_path: Path | None = None, start_worker=True) -> FastAPI:
    store = Store(db_path or Path(os.environ.get("FAST_PARSER_DB", str(ROOT / "data" / "football.sqlite3"))))
    csrf = secrets.token_urlsafe(32)
    if start_worker:
        from .official import LEAGUES
        now = utcnow()
        year = str(now.year if now.month >= 7 else now.year-1)
        store.catalog([dict(id=f"ll:{division}:{year}", shortcut=division, season=year,
                            name=name, country="Испания", category="league")
                       for division, (_, name) in LEAGUES.items()])
        store.catalog(english_catalog(year))
        store.catalog(italian_catalog(year))
        store.catalog(german_catalog(year))

    @asynccontextmanager
    async def lifespan(app):
        collector = Collector(store) if start_worker else None
        task = asyncio.create_task(collector.run()) if collector else None
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Fast Parser", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.csrf = csrf
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    templates = Jinja2Templates(directory=ROOT / "fast_parser" / "templates")
    app.mount("/static", StaticFiles(directory=ROOT / "fast_parser" / "static"), name="static")

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if origin != expected or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
                return JSONResponse({"detail": "Неверный origin или CSRF token; обновите страницу"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith("/api") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={"csrf": csrf, "version": __version__})

    @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    def api_docs(request: Request):
        schema = app.openapi()
        endpoints = [{"path": path, "method": method.upper(), "summary": value.get("summary", "")}
                     for path, operations in schema["paths"].items() for method, value in operations.items()]
        return templates.TemplateResponse(request=request, name="api_docs.html", context={"version": __version__, "endpoints": endpoints})

    @app.get("/api/v1/countries")
    def countries():
        rows = store.competitions()
        grouped = {}
        for r in rows:
            grouped[r["country"]] = grouped.get(r["country"], 0) + 1
        return {"items": [{"id": name, "name": name, "competition_count": count} for name, count in sorted(grouped.items())]}

    @app.get("/api/v1/competitions")
    def competitions(country: str | None = None):
        rows = store.competitions(country)
        official_only = store.settings().official_only
        for row in rows:
            official = row["id"].startswith(OFFICIAL_PREFIXES)
            source = source_for_comp(row["id"])
            row.update(source=source,
                       official_source=official, source_role="primary" if official else "legacy_unofficial",
                       collection_allowed=official or not official_only,
                       minute_supported=row["id"].startswith(("sky:","pl:")), period_supported=False,
                       redundancy="none", live_score_supported=row["id"].startswith(("sky:","pl:")))
        return {"items": rows}

    @app.put("/api/v1/competitions/{cid}/verification")
    def save_verification(cid: str, verification: CoverageVerification):
        try:
            return store.save_verification(cid, verification)
        except KeyError as exc:
            raise HTTPException(404, "Лига не найдена") from exc

    @app.get("/api/v1/matches")
    def matches(competition_id: str | None = None, status: str | None = None, date_from: str | None = None,
                date_to: str | None = None, cursor: str | None = None, limit: int = Query(default=100, ge=1, le=200)):
        try:
            start = stamp(parse_time(date_from)) if date_from else None
            end = stamp(parse_time(date_to)) if date_to else None
            if start and end and start >= end:
                raise ValueError("Начало должно быть раньше конца")
            if status and status not in {"scheduled", "live", "paused", "finished", "postponed", "cancelled", "abandoned", "unknown"}:
                raise ValueError("Неизвестный статус")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            return store.matches(competition_id, status, start, end, cursor, limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/changes")
    def changes(after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200)):
        try:
            return store.changes(after, limit)
        except ValueError as exc:
            raise HTTPException(410, str(exc)) from exc

    @app.get("/api/v1/matches/{mid}")
    def match(mid: str):
        result = store.match(mid)
        if result is None:
            raise HTTPException(404, "Матч не найден или срок хранения истёк")
        return result

    @app.get("/api/v1/settings")
    def settings():
        return store.settings().model_dump()

    @app.put("/api/v1/settings")
    def save_settings(settings: Settings):
        try:
            store.save_settings(settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"message": "Сохранено. Worker применит настройки в следующем цикле (после текущего запроса)."}

    @app.post("/api/v1/sync/{cid}", status_code=202)
    def sync(cid: str):
        try:
            if store.settings().official_only and not cid.startswith(OFFICIAL_PREFIXES):
                raise ValueError("Официальный источник этой лиги пока не подключен; неофициальный сбор отключен")
            if cid.startswith(OFFICIAL_PREFIXES):
                if not any(c["id"] == cid for c in store.competitions()):
                    raise ValueError("Лига не найдена")
                if cid.startswith("ll:"):
                    store.set_meta("official_plan", "{}")
                elif cid.startswith(("pl:","efl:")):
                    source = PL_SOURCE if cid.startswith("pl:") else EFL_SOURCE
                    store.set_meta("english_due:"+source, "0")
                    if source == EFL_SOURCE:
                        store.set_meta("efl_cursor", "{}")
                elif cid.startswith("sa:"):
                    store.set_meta("italian_due:"+SA_SOURCE,"0")
                else:
                    store.set_meta("german_plan" if cid.startswith("dfl:") else "legab_plan","{}")
                return {"message": "Официальный сбор поставлен в очередь; лимит 30 секунд сохраняется."}
            if cid.startswith("sky:"):
                if not store.settings().html_enabled:
                    raise ValueError("HTML-источник выключен в настройках")
                store.set_meta("html_due", "{}")
            else:
                store.request_sync(cid)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"message": "Проверка поставлена в очередь; лимиты источника сохраняются."}

    @app.post("/api/v1/sources/{source}/reset")
    def reset(source: str):
        if source not in {"openligadb", "skysports_html"} | OFFICIAL_SOURCES:
            raise HTTPException(404)
        # Manual recheck is an explicit operator action; never done by automatic retries.
        state = store.source(source)
        if state["state"] != "blocked":
            raise HTTPException(409, "Источник не заблокирован; текущий cooldown сохраняется")
        store.reset_source(source)
        return {"message": "Источник разблокирован для одной новой проверки"}

    @app.get("/api/v1/diagnostics")
    def diagnostics():
        comps = store.competitions()
        now = utcnow()
        enabled = sum(bool(c["enabled"]) for c in comps if c["id"].startswith("ol:"))
        return {"version": __version__, "server_time": stamp(now), "worker_heartbeat": store.get_meta("worker_heartbeat") or None,
                "catalog_updated_at": store.get_meta("catalog_updated_at") or None,
                "source_policy": "official_only" if store.settings().official_only else "official_with_legacy",
                "sources": [store.source(s) for s in ("laliga_reference", PL_SOURCE, EFL_SOURCE, SA_SOURCE, SB_SOURCE, DFL_SOURCE, "openligadb", "skysports_html")],
                "coverage": {"catalog": len(comps), "verified": sum(c["state"] == "verified" for c in comps), "enabled_openliga": enabled,
                             "verification": {status: sum(c["coverage_verification"]["status"] == status for c in comps)
                                              for status in ("verified", "partial", "unverified")}},
                "plan": {"openliga_max_requests_per_minute": 4.8, "html_max_requests_per_minute": 3,
                         "minimum_full_sweep_seconds": round(enabled * 12.5), "world_coverage_guaranteed": False},
                "unresolved_over_24h": store.unresolved(now),
                "storage": "SQLite WAL, local single collector", "retention_hours": 72}

    @app.get("/health/live")
    def live():
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready")
    def ready():
        store.settings()
        return {"status": "ok", "database": "ready"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        comps = store.competitions()
        return "\n".join([f"fast_parser_competitions {len(comps)}", f"fast_parser_verified_competitions {sum(c['state']=='verified' for c in comps)}", ""])

    return app


app = create_app(start_worker=os.environ.get("FAST_PARSER_WORKER", "1") != "0")
