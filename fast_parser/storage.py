from __future__ import annotations

import json
import base64
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .config import Settings
from .domain import Match, parse_time, stamp, utcnow


class Store:
    """Short transactions on independent connections; blocking calls run off ASGI loop."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
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
                PRAGMA user_version=1;
            """)
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
            if known and not set(settings.enabled_leagues) <= known:
                raise ValueError("Неизвестная лига: сначала обновите каталог")
            db.execute("UPDATE metadata SET value=? WHERE key='settings'", (settings.model_dump_json(),))
            db.execute("UPDATE competitions SET enabled=0")
            db.executemany("UPDATE competitions SET enabled=1,next_poll=0 WHERE shortcut=?", [(s,) for s in settings.enabled_leagues])
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
            return [dict(r) for r in db.execute("SELECT * FROM competitions" + where + " ORDER BY country,name", args)]

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
                item.observed_at = stamp(now)
                if item.status in {"finished", "cancelled"}:
                    first = (old or {}).get("first_observed_finished_at") or stamp(now)
                    item.first_observed_finished_at = first
                    anchor = parse_time(item.finished_at) or parse_time(first)
                    item.retention_anchor_method = "source" if item.finished_at else "observed"
                    item.expires_at = stamp(anchor + timedelta(hours=72))
                    if parse_time(item.expires_at) <= now:
                        db.execute("DELETE FROM matches WHERE id=?", (item.id,))
                        db.execute("INSERT OR REPLACE INTO markers VALUES(?,?)", (item.id, now.timestamp()))
                        continue
                payload = json.dumps(item.to_dict(), ensure_ascii=False)
                db.execute("""INSERT INTO matches VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    competition_id=excluded.competition_id,kickoff_at=excluded.kickoff_at,status=excluded.status,
                    expires_at=excluded.expires_at,observed_at=excluded.observed_at,payload=excluded.payload""",
                    (item.id, item.competition_id, item.source, item.external_id, item.kickoff_at, item.status, item.expires_at, item.observed_at, payload))
                count += 1
        return count

    def matches(self, cid=None, status=None, start=None, end=None, cursor=None, limit=100, now=None):
        now_s = stamp(now or utcnow())
        clauses, args = ["(expires_at IS NULL OR expires_at>?)"], [now_s]
        for column, value, op in [("competition_id", cid, "="), ("status", status, "="), ("kickoff_at", start, ">="), ("kickoff_at", end, "<")]:
            if value:
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
        data = [json.loads(r[0]) for r in rows[:limit]]
        token = base64.urlsafe_b64encode(json.dumps([data[-1]["kickoff_at"] or "9999", data[-1]["id"]]).encode()).decode() if len(rows) > limit else None
        return {"items": data, "next_cursor": token}

    def match(self, mid: str, now=None):
        with self.connect() as db:
            r = db.execute("SELECT payload FROM matches WHERE id=? AND (expires_at IS NULL OR expires_at>?)", (mid, stamp(now or utcnow()))).fetchone()
            return json.loads(r[0]) if r else None

    def cleanup(self, now=None, dry_run=False) -> int:
        now = now or utcnow()
        with self.connect() as db:
            if dry_run:
                return db.execute("SELECT COUNT(*) FROM matches WHERE expires_at<=?", (stamp(now),)).fetchone()[0]
            ids = db.execute("SELECT id FROM matches WHERE expires_at<=? LIMIT 1000", (stamp(now),)).fetchall()
            db.executemany("INSERT OR REPLACE INTO markers VALUES(?,?)", [(r[0], now.timestamp()) for r in ids])
            db.executemany("DELETE FROM matches WHERE id=?", [(r[0],) for r in ids])
            db.execute("DELETE FROM markers WHERE last_seen<?", ((now - timedelta(days=180)).timestamp(),))
            db.execute("DELETE FROM audit WHERE at<?", (stamp(now - timedelta(days=7)),))
            deleted = len(ids)
        # QA screenshots contain real matches: remove within the same retention horizon.
        qa = self.path.parent / "qa"
        if qa.is_dir():
            for artifact in qa.glob("*.png"):
                if artifact.stat().st_mtime <= now.timestamp() - 72 * 3600:
                    artifact.unlink()
        return deleted
