from __future__ import annotations

import json
import base64
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import LEGACY_ENABLED_LEAGUES, Settings
from .domain import ORGANIZER_ZONES, Match, content_hash, parse_time, prepare_match, present_match, stamp, utcnow
from .verification import CoverageVerification
from .english import OFFICIAL_SOURCES

RESULTS_ARCHIVE_DAYS = 180
HEARTBEAT_FRESH_SECONDS = 30


class Store:
    """Short transactions on independent connections; blocking calls run off ASGI loop."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 4:
                raise ValueError("База создана более новой версией приложения")
            if version in {1, 2, 3}:
                backup_path = path.with_suffix(f".v{version}.bak")
                with sqlite3.connect(backup_path) as backup:
                    db.backup(backup)
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS competitions(
                    id TEXT PRIMARY KEY, shortcut TEXT NOT NULL, season TEXT NOT NULL,
                    name TEXT NOT NULL, country TEXT NOT NULL, category TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'unverified',
                    last_success_at TEXT, error TEXT, next_poll REAL NOT NULL DEFAULT 0,
                    count INTEGER NOT NULL DEFAULT 0, live_supported INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS matches(
                    id TEXT PRIMARY KEY, competition_id TEXT NOT NULL REFERENCES competitions(id),
                    source TEXT NOT NULL, external_id TEXT NOT NULL, kickoff_at TEXT,
                    status TEXT NOT NULL, expires_at TEXT, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(source, external_id));
                CREATE INDEX IF NOT EXISTS match_filter ON matches(competition_id,kickoff_at,id);
                CREATE INDEX IF NOT EXISTS match_expiry ON matches(expires_at);
                CREATE INDEX IF NOT EXISTS match_status ON matches(status,kickoff_at);
                CREATE INDEX IF NOT EXISTS match_order ON matches(competition_id,COALESCE(kickoff_at,'9999'),id);
                CREATE TABLE IF NOT EXISTS markers(id TEXT PRIMARY KEY, last_seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS source_state(name TEXT PRIMARY KEY,next_allowed REAL NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'closed',error TEXT,last_success TEXT);
                CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY,owner TEXT NOT NULL,until REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL, match_id TEXT NOT NULL, kind TEXT NOT NULL,
                    competition_id TEXT, content_hash TEXT);
                CREATE INDEX IF NOT EXISTS changes_at ON changes(at);
                CREATE TABLE IF NOT EXISTS results(
                    id TEXT PRIMARY KEY, competition_id TEXT NOT NULL, source TEXT NOT NULL, kickoff_at TEXT,
                    status TEXT NOT NULL, archive_until TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS results_order ON results(competition_id, COALESCE(kickoff_at,'9999'), id);
                CREATE INDEX IF NOT EXISTS results_until ON results(archive_until);
                CREATE TABLE IF NOT EXISTS coverage_verification(
                    competition_id TEXT PRIMARY KEY REFERENCES competitions(id),
                    updated_at TEXT NOT NULL, payload TEXT NOT NULL);
                PRAGMA user_version=4;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(changes)")}
            if "competition_id" not in columns:
                db.execute("ALTER TABLE changes ADD COLUMN competition_id TEXT")
            if "content_hash" not in columns:
                db.execute("ALTER TABLE changes ADD COLUMN content_hash TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS changes_competition ON changes(competition_id, seq)")
            db.execute("""UPDATE changes SET competition_id=(
                SELECT competition_id FROM matches WHERE matches.id=changes.match_id)
                WHERE competition_id IS NULL""")
            for row in db.execute("SELECT payload FROM matches WHERE status IN ('finished','cancelled')"):
                stored = json.loads(row[0])
                if not stored.get("archive_until"):
                    anchor = parse_time(stored.get("finished_at")) or parse_time(stored.get("first_observed_finished_at")) or utcnow()
                    stored["archive_until"] = stamp(anchor + timedelta(days=RESULTS_ARCHIVE_DAYS))
                db.execute("""INSERT OR IGNORE INTO results(id,competition_id,source,kickoff_at,status,archive_until,payload)
                    VALUES(?,?,?,?,?,?,?)""",
                    (stored["id"], stored["competition_id"], stored["source"], stored.get("kickoff_at"),
                     stored["status"], stored["archive_until"], json.dumps(stored, ensure_ascii=False)))
            if not db.execute("SELECT 1 FROM metadata WHERE key='settings'").fetchone():
                db.execute("INSERT INTO metadata VALUES('settings',?)", (Settings().model_dump_json(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self) -> Settings:
        return Settings.model_validate_json(self.get_meta("settings"))

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_meta(self, key: str, value: str):
        with self.connect() as db:
            db.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def save_settings(self, settings: Settings):
        with self.connect() as db:
            known = {r[0] for r in db.execute("SELECT DISTINCT shortcut FROM competitions")}
            # Absent historical shortcuts are a stored legacy preference, not a typo.
            # A shortcut outside that set must already exist in the catalog.
            missing = set(settings.enabled_leagues) - known
            if known and not missing <= set(LEGACY_ENABLED_LEAGUES):
                raise ValueError("Неизвестная лига: сначала обновите каталог")
            db.execute("UPDATE metadata SET value=? WHERE key='settings'", (settings.model_dump_json(),))
            db.execute("UPDATE competitions SET enabled=0")
            db.executemany("""UPDATE competitions SET enabled=1,next_poll=0 WHERE shortcut=?
                AND season=(SELECT MAX(c.season) FROM competitions c WHERE c.shortcut=competitions.shortcut)""", [(s,) for s in settings.enabled_leagues])
            db.execute("INSERT INTO audit(at,kind,detail) VALUES(?,?,?)", (stamp(utcnow()), "settings", "configuration updated"))

    def catalog(self, items: list[dict]):
        enabled = self.settings().enabled_leagues
        with self.connect() as db:
            for row in items:
                if row["id"].startswith("ol:"):
                    db.execute("UPDATE competitions SET enabled=0 WHERE shortcut=? AND id<>?", (row["shortcut"], row["id"]))
                db.execute("""INSERT INTO competitions(id,shortcut,season,name,country,category,enabled)
                    VALUES(:id,:shortcut,:season,:name,:country,:category,:enabled)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,country=excluded.country,category=excluded.category""",
                    {**row, "enabled": int(row["shortcut"] in enabled)})

    def competitions(self, country: str | None = None) -> list[dict]:
        with self.connect() as db:
            where, args = (" WHERE country=?", [country]) if country else ("", [])
            rows = [dict(r) for r in db.execute("SELECT c.*,v.updated_at AS verification_updated_at,v.payload AS verification_payload FROM competitions c LEFT JOIN coverage_verification v ON v.competition_id=c.id" + where + " ORDER BY country,name", args)]
        for row in rows:
            raw = row.pop("verification_payload")
            row["coverage_verification"] = json.loads(raw) if raw else CoverageVerification().model_dump()
            row["coverage_verification"]["updated_at"] = row.pop("verification_updated_at")
            row["collection_state"] = {"verified": "available", "degraded": "degraded"}.get(row["state"], "pending")
        return rows

    def save_verification(self, cid: str, verification: CoverageVerification):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM competitions WHERE id=?", (cid,)).fetchone():
                raise KeyError(cid)
            at = stamp(utcnow())
            db.execute("INSERT INTO coverage_verification VALUES(?,?,?) ON CONFLICT(competition_id) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
                       (cid, at, verification.model_dump_json()))
            db.execute("INSERT INTO audit(at,kind,detail) VALUES(?,?,?)", (at, "coverage_verification", cid))
        return {**verification.model_dump(), "updated_at": at}

    def mark_sync(self, cid: str, success: bool, next_poll: float, count=0, error=None):
        with self.connect() as db:
            if success:
                db.execute("UPDATE competitions SET state='verified',last_success_at=?,error=NULL,next_poll=?,count=? WHERE id=?",
                           (stamp(utcnow()), next_poll, count, cid))
            else:
                db.execute("UPDATE competitions SET state='degraded',error=?,next_poll=? WHERE id=?", (error, next_poll, cid))

    def due(self, now: float):
        with self.connect() as db:
            row = db.execute("SELECT * FROM competitions WHERE enabled=1 AND id LIKE 'ol:%' AND next_poll<=? ORDER BY next_poll,id LIMIT 1", (now,)).fetchone()
            return dict(row) if row else None

    def request_sync(self, cid: str):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM competitions WHERE id=? AND enabled=1", (cid,)).fetchone():
                raise ValueError("Выберите включённую лигу")
            # Preserve an already pending job; source cooldown always takes precedence.
            db.execute("UPDATE competitions SET next_poll=MIN(next_poll,?) WHERE id=?", (utcnow().timestamp(), cid))

    def lease(self, name: str, owner: str, seconds=45) -> bool:
        now = utcnow().timestamp()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute("SELECT owner,until FROM leases WHERE name=?", (name,)).fetchone()
            if r and r[0] != owner and r[1] > now:
                return False
            db.execute("INSERT INTO leases VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,until=excluded.until", (name, owner, now + seconds))
            return True

    def release(self, owner: str):
        with self.connect() as db:
            db.execute("DELETE FROM leases WHERE owner=?", (owner,))

    def source(self, name: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM source_state WHERE name=?", (name,)).fetchone()
            if row:
                return dict(row)
            db.execute("INSERT OR IGNORE INTO source_state(name) VALUES(?)", (name,))
            return dict(db.execute("SELECT * FROM source_state WHERE name=?", (name,)).fetchone())

    def reserve_request(self, name: str, spacing: float) -> bool:
        now = utcnow().timestamp()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO source_state(name) VALUES(?)", (name,))
            r = db.execute("SELECT next_allowed,state FROM source_state WHERE name=?", (name,)).fetchone()
            if r[1] == "blocked" or r[0] > now:
                return False
            db.execute("UPDATE source_state SET next_allowed=? WHERE name=?", (now + spacing, name))
            return True

    def source_success(self, name: str):
        with self.connect() as db:
            db.execute("UPDATE source_state SET failures=0,state='closed',error=NULL,last_success=? WHERE name=?", (stamp(utcnow()), name))

    def source_error(self, name: str, error: str, delay: float, blocked=False):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO source_state(name) VALUES(?)", (name,))
            db.execute("UPDATE source_state SET failures=failures+1,state=?,error=?,next_allowed=MAX(next_allowed,?) WHERE name=?",
                       ("blocked" if blocked else "open", error[:500], utcnow().timestamp() + delay, name))

    def reset_source(self, name: str):
        with self.connect() as db:
            db.execute("UPDATE source_state SET failures=0,state='closed',next_allowed=0,error=NULL WHERE name=?", (name,))

    def upsert_matches(self, matches: list[Match], now=None) -> int:
        now = now or utcnow()
        count = 0
        with self.connect() as db:
            for item in matches:
                if not prepare_match(item):
                    db.execute("INSERT INTO audit(at,kind,detail) VALUES(?,?,?)",
                               (stamp(now), "rejected_missing_team_id", item.id))
                    continue
                previous = db.execute("SELECT payload FROM matches WHERE id=?", (item.id,)).fetchone()
                old = json.loads(previous[0]) if previous else None
                marker = db.execute("SELECT 1 FROM markers WHERE id=?", (item.id,)).fetchone()
                # A moved future fixture can legitimately revive an external ID.
                if marker and not (item.status == "scheduled" and parse_time(item.kickoff_at) and parse_time(item.kickoff_at) > now):
                    db.execute("UPDATE markers SET last_seen=? WHERE id=?", (now.timestamp(), item.id))
                    continue
                if marker:
                    db.execute("DELETE FROM markers WHERE id=?", (item.id,))
                if old and old.get("source_updated_at") and item.source_updated_at and parse_time(item.source_updated_at) < parse_time(old["source_updated_at"]):
                    continue
                if old and old["status"] in {"finished", "cancelled"} and item.status not in {"finished", "cancelled"}:
                    # Only an explicitly newer source revision may retract a final result.
                    if not (old.get("source_updated_at") and item.source_updated_at
                            and parse_time(item.source_updated_at) > parse_time(old["source_updated_at"])):
                        db.execute("INSERT INTO audit(at,kind,detail) VALUES(?,?,?)",
                                   (stamp(now), "rejected_status_regression", item.id))
                        continue
                item.observed_at = stamp(now)
                if item.status in {"finished", "cancelled"}:
                    first = (old or {}).get("first_observed_finished_at") or stamp(now)
                    item.first_observed_finished_at = first
                    anchor = parse_time(item.finished_at) or parse_time(first)
                    item.retention_anchor_method = "source" if item.finished_at else "observed"
                    item.expires_at = stamp(anchor + timedelta(hours=72))
                    if old and old.get("expires_at"):
                        item.expires_at = min(item.expires_at, old["expires_at"])
                    archive = stamp(anchor + timedelta(days=RESULTS_ARCHIVE_DAYS))
                    previous_archive = (old or {}).get("archive_until")
                    item.archive_until = min(archive, previous_archive) if previous_archive else archive
                    self._save_result(db, item)
                    if parse_time(item.expires_at) <= now:
                        if old:
                            self._change(db, now, item, "deleted")
                        db.execute("DELETE FROM matches WHERE id=?", (item.id,))
                        db.execute("INSERT OR REPLACE INTO markers VALUES(?,?)", (item.id, now.timestamp()))
                        continue
                else:
                    item.archive_until = None
                    db.execute("DELETE FROM results WHERE id=?", (item.id,))
                payload = json.dumps(item.to_dict(), ensure_ascii=False)
                db.execute("""INSERT INTO matches VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    competition_id=excluded.competition_id,kickoff_at=excluded.kickoff_at,status=excluded.status,
                    expires_at=excluded.expires_at,observed_at=excluded.observed_at,payload=excluded.payload""",
                    (item.id, item.competition_id, item.source, item.external_id, item.kickoff_at, item.status, item.expires_at, item.observed_at, payload))
                self._change(db, now, item, "upsert")
                count += 1
        return count

    def _change(self, db, now, item: Match, kind: str):
        payload = item.to_dict()
        db.execute("INSERT INTO changes(at,match_id,kind,competition_id,content_hash) VALUES(?,?,?,?,?)",
                   (stamp(now), item.id, kind, item.competition_id, content_hash(payload)))

    def _save_result(self, db, item: Match):
        db.execute("""INSERT INTO results(id,competition_id,source,kickoff_at,status,archive_until,payload)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            competition_id=excluded.competition_id,source=excluded.source,kickoff_at=excluded.kickoff_at,
            status=excluded.status,archive_until=excluded.archive_until,payload=excluded.payload""",
            (item.id, item.competition_id, item.source, item.kickoff_at, item.status,
             item.archive_until, json.dumps(item.to_dict(), ensure_ascii=False)))

    def matches(self, cid=None, status=None, start=None, end=None, cursor=None, limit=100, now=None, official=False, match_ids=None):
        now_s = stamp(now or utcnow())
        clauses, args = ["(expires_at IS NULL OR expires_at>?)"], [now_s]
        if official:
            names = sorted(OFFICIAL_SOURCES)
            clauses.append("source IN (" + ",".join("?" * len(names)) + ")")
            args.extend(names)
        if match_ids:
            clauses.append("id IN (" + ",".join("?" * len(match_ids)) + ")")
            args.extend(match_ids)
        for column, value, op in [("competition_id", cid, "="), ("status", status, "="), ("kickoff_at", start, ">="), ("kickoff_at", end, "<")]:
            if value:
                if column == "kickoff_at":
                    day_op = op if op == ">=" or value[11:19] == "00:00:00" else "<="
                    clauses.append(f"(kickoff_at{op}? OR (kickoff_at IS NULL AND json_extract(payload,'$.scheduled_date'){day_op}?))")
                    args.extend([value,value[:10]])
                else:
                    clauses.append(column + op + "?")
                    args.append(value)
        if cursor:
            try:
                key, mid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if not isinstance(key, str) or not isinstance(mid, str):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("Неверный cursor") from exc
            clauses.append("(COALESCE(kickoff_at,'9999'),id)>(?,?)")
            args.extend([key, mid])
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM matches WHERE " + " AND ".join(clauses) + " ORDER BY COALESCE(kickoff_at,'9999'),id LIMIT ?", [*args, limit + 1]).fetchall()
        settings, sources = self.settings(), {}
        data = [self.decorate(json.loads(r[0]), now=now, settings=settings, sources=sources) for r in rows[:limit]]
        token = base64.urlsafe_b64encode(json.dumps([data[-1]["kickoff_at"] or "9999", data[-1]["id"]]).encode()).decode() if len(rows) > limit else None
        return {"items": data, "next_cursor": token}

    def match(self, mid: str, now=None):
        with self.connect() as db:
            r = db.execute("SELECT payload FROM matches WHERE id=? AND (expires_at IS NULL OR expires_at>?)", (mid, stamp(now or utcnow()))).fetchone()
            return self.decorate(json.loads(r[0]), now=now) if r else None

    def in_archive(self, mid: str, now=None) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM results WHERE id=? AND archive_until>?", (mid, stamp(now or utcnow()))).fetchone()
            return row is not None

    def cleanup(self, now=None, dry_run=False) -> int:
        now = now or utcnow()
        with self.connect() as db:
            if dry_run:
                return db.execute("SELECT COUNT(*) FROM matches WHERE expires_at<=?", (stamp(now),)).fetchone()[0]
            ids = db.execute("SELECT id,payload FROM matches WHERE expires_at<=? LIMIT 1000", (stamp(now),)).fetchall()
            db.executemany("INSERT OR REPLACE INTO markers VALUES(?,?)", [(r["id"], now.timestamp()) for r in ids])
            for row in ids:
                stored = json.loads(row["payload"])
                db.execute("INSERT INTO changes(at,match_id,kind,competition_id,content_hash) VALUES(?,?,?,?,?)",
                           (stamp(now), stored["id"], "deleted", stored.get("competition_id"), content_hash(stored)))
            db.executemany("DELETE FROM matches WHERE id=?", [(r["id"],) for r in ids])
            db.execute("DELETE FROM results WHERE archive_until<=?", (stamp(now),))
            cutoff = stamp(now - timedelta(days=7))
            removed = db.execute("SELECT MAX(seq) FROM changes WHERE at<?", (cutoff,)).fetchone()[0]
            if removed:
                db.execute("INSERT INTO metadata VALUES('changes_floor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(removed),))
                db.execute("DELETE FROM changes WHERE seq<=?", (removed,))
            db.execute("DELETE FROM markers WHERE last_seen<?", ((now - timedelta(days=180)).timestamp(),))
            db.execute("DELETE FROM audit WHERE at<?", (stamp(now - timedelta(days=7)),))
            deleted = len(ids)
        for version in (1, 2, 3):
            backup = self.path.with_suffix(f".v{version}.bak")
            if backup.exists() and backup.stat().st_mtime <= now.timestamp() - 72 * 3600:
                backup.unlink()
        # QA screenshots contain real matches: remove within the same retention horizon.
        qa = self.path.parent / "qa"
        if qa.is_dir():
            for artifact in qa.glob("*.png"):
                if artifact.stat().st_mtime <= now.timestamp() - 72 * 3600:
                    artifact.unlink()
        return deleted

    def unresolved(self, now=None):
        now = now or utcnow()
        with self.connect() as db:
            rows = db.execute("SELECT id,competition_id,kickoff_at FROM matches WHERE status NOT IN ('finished','cancelled') AND kickoff_at<=? ORDER BY kickoff_at", (stamp(now - timedelta(hours=24)),)).fetchall()
            return [dict(r) for r in rows]

    def pending_ids(self, cid):
        with self.connect() as db:
            return {r[0] for r in db.execute("SELECT id FROM matches WHERE competition_id=? AND status NOT IN ('finished','cancelled')", (cid,))}

    def results(self, cid=None, status="finished", cursor=None, limit=100, now=None, official=False, match_ids=None):
        now_s = stamp(now or utcnow())
        clauses, args = ["archive_until>?"], [now_s]
        if official:
            names = sorted(OFFICIAL_SOURCES)
            clauses.append("source IN (" + ",".join("?" * len(names)) + ")")
            args.extend(names)
        if cid:
            clauses.append("competition_id=?")
            args.append(cid)
        if match_ids:
            clauses.append("id IN (" + ",".join("?" * len(match_ids)) + ")")
            args.extend(match_ids)
        if status:
            clauses.append("status=?")
            args.append(status)
        if cursor:
            try:
                key, mid = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if not isinstance(key, str) or not isinstance(mid, str):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("Неверный cursor") from exc
            clauses.append("(COALESCE(kickoff_at,'9999'),id)>(?,?)")
            args.extend([key, mid])
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM results WHERE " + " AND ".join(clauses) + " ORDER BY COALESCE(kickoff_at,'9999'),id LIMIT ?", [*args, limit + 1]).fetchall()
        settings, sources = self.settings(), {}
        data = [self.decorate(json.loads(r[0]), now=now, settings=settings, sources=sources) for r in rows[:limit]]
        token = base64.urlsafe_b64encode(json.dumps([data[-1]["kickoff_at"] or "9999", data[-1]["id"]]).encode()).decode() if len(rows) > limit else None
        return {"items": data, "next_cursor": token, "archive_days": RESULTS_ARCHIVE_DAYS}

    def readiness(self, now=None):
        now = now or utcnow()
        settings = self.settings()
        heartbeat = self.get_meta("worker_heartbeat")
        checked = parse_time(heartbeat) if heartbeat else None
        heartbeat_age = max(0, (now - checked).total_seconds()) if checked else None
        collector_fresh = heartbeat_age is not None and heartbeat_age <= HEARTBEAT_FRESH_SECONDS
        with self.connect() as db:
            latest = db.execute("SELECT MAX(observed_at) FROM matches").fetchone()[0]
        observed = parse_time(latest) if latest else None
        data_age = max(0, int((now - observed).total_seconds())) if observed else None
        limit = max(settings.fixtures_interval_seconds, settings.results_interval_seconds,
                    settings.live_interval_seconds, settings.final_recheck_interval_seconds) * 2
        data_fresh = data_age is None or data_age <= limit
        return {"status": "ok" if collector_fresh and data_fresh else "stale", "database": "ready",
                "collector_fresh": collector_fresh, "data_age_seconds": data_age}

    def decorate(self, item, now=None, settings=None, sources=None):
        now = now or utcnow()
        settings = settings or self.settings()
        sources = sources if sources is not None else {}
        item = present_match(item)
        if item["source"] not in sources:
            sources[item["source"]] = self.source(item["source"])
        state = sources[item["source"]]
        stored_status = item["status"]
        interval = (settings.live_interval_seconds if stored_status in {"live", "paused"}
                    else settings.final_recheck_interval_seconds if stored_status in {"finished", "cancelled"}
                    else settings.results_interval_seconds if parse_time(item.get("kickoff_at")) and parse_time(item["kickoff_at"]) <= now
                    else settings.fixtures_interval_seconds)
        checked = parse_time(item.get("observed_at"))
        age = max(0, (now - checked).total_seconds()) if checked else None
        stale = age is None or age > interval * 2 or state["state"] != "closed"
        if stored_status in {"live", "paused"} and stale:
            item["status"] = "unknown"
        elif stored_status == "scheduled" and stale and self._scheduled_is_overdue(item, now):
            item["status"] = "unknown"
        item.update(last_checked_at=item.get("observed_at"), freshness_age_seconds=age,
                    official_source=item["source"] in OFFICIAL_SOURCES,
                    source_role="primary" if item["source"] in OFFICIAL_SOURCES else "legacy_unofficial",
                    is_stale=stale, source_state=state["state"], freshness_threshold_seconds=interval * 2)
        item["content_hash"] = content_hash(item)
        return item

    def _scheduled_is_overdue(self, item, now) -> bool:
        kickoff = parse_time(item.get("kickoff_at"))
        if kickoff is not None:
            return kickoff <= now
        day = item.get("scheduled_date")
        if not isinstance(day, str) or len(day) != 10:
            return False
        zone = next((name for prefix, name in ORGANIZER_ZONES.items() if str(item.get("id") or "").startswith(prefix)), None)
        today = now.astimezone(ZoneInfo(zone)).date().isoformat() if zone else now.date().isoformat()
        return day < today

    def changes(self, after=0, limit=100, now=None, competition_id=None, match_ids=None):
        now = now or utcnow()
        with self.connect() as db:
            floor_row = db.execute("SELECT value FROM metadata WHERE key='changes_floor'").fetchone()
            floor = int(floor_row[0]) if floor_row else 0
            if after < floor:
                raise ValueError("Курсор изменений истёк: выполните полную синхронизацию")
            query = "SELECT * FROM changes WHERE seq>?"
            args = [after]
            if competition_id:
                query += " AND competition_id=?"
                args.append(competition_id)
            if match_ids:
                query += " AND match_id IN (" + ",".join("?" * len(match_ids)) + ")"
                args.extend(match_ids)
            rows = db.execute(query + " ORDER BY seq LIMIT ?", [*args, limit + 1]).fetchall()
            watermark = db.execute("SELECT seq FROM sqlite_sequence WHERE name='changes'").fetchone()
            ids = list({row["match_id"] for row in rows[:limit]})
            payloads = {}
            if ids:
                placeholders = ",".join("?" for _ in ids)
                payloads = {row["id"]: json.loads(row["payload"]) for row in db.execute(
                    f"SELECT id,payload FROM matches WHERE id IN ({placeholders}) AND (expires_at IS NULL OR expires_at>?)",
                    [*ids, stamp(now)])}
        items = []
        settings, sources = self.settings(), {}
        for row in rows[:limit]:
            event = dict(row)
            payload = payloads.get(event["match_id"])
            match = self.decorate(dict(payload), now, settings, sources) if payload else None
            event["kind"] = "upsert" if match else "deleted"
            event["match"] = match
            if match:
                event["content_hash"] = match["content_hash"]
            items.append(event)
        return {"items": items, "next_after": items[-1]["seq"] if items else after,
                "has_more": len(rows) > limit, "watermark": watermark[0] if watermark else 0,
                "retention_days": 7, "server_time": stamp(now)}
