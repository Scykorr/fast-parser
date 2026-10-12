from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp without timezone")
    return dt.astimezone(UTC)


@dataclass
class Match:
    id: str
    competition_id: str
    source: str
    external_id: str
    home_team: dict
    away_team: dict
    kickoff_at: str | None
    status: str
    score_home: int | None = None
    score_away: int | None = None
    regular_home: int | None = None
    regular_away: int | None = None
    extra_home: int | None = None
    extra_away: int | None = None
    penalties_home: int | None = None
    penalties_away: int | None = None
    period: str | None = None
    elapsed_minutes: int | None = None
    added_minutes: int | None = None
    finished_at: str | None = None
    observed_at: str | None = None
    first_observed_finished_at: str | None = None
    expires_at: str | None = None
    retention_anchor_method: str | None = None
    source_updated_at: str | None = None
    raw_status: str | None = None
    verification: str = "single_source"
    result_verified: bool = False
    independent_confirmations: list = field(default_factory=list)
    source_url: str | None = None
    public_match_url: str | None = None
    scheduled_date: str | None = None
    kickoff_precision: str = "unknown"
    score_scope: str | None = None
    archive_until: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


SCORE_SCOPES = {"regulation", "including_extra", "including_penalties", "unknown"}
ORGANIZER_ZONES = {
    "pl:": "Europe/London", "efl:": "Europe/London", "sky:": "Europe/London",
    "ll:": "Europe/Madrid", "sa:": "Europe/Rome", "sb:": "Europe/Rome",
    "dfl:": "Europe/Berlin", "fnl:": "Europe/Moscow", "rpl:": "Europe/Moscow",
}
HASH_KEYS = (
    "id", "competition_id", "status", "kickoff_at", "scheduled_date", "kickoff_precision",
    "home_team", "away_team", "score_home", "score_away", "regular_home", "regular_away",
    "extra_home", "extra_away", "penalties_home", "penalties_away", "score_scope",
    "period", "elapsed_minutes", "added_minutes", "raw_status", "finished_at",
    "source_url", "public_match_url", "result_verified", "independent_confirmations",
)


def team_id(team) -> str | None:
    if not isinstance(team, dict):
        return None
    value = team.get("id")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and value.strip():
        team["id"] = value.strip()
        return team["id"]
    return None


def confirmation_sources(items) -> set[str]:
    found = set()
    for item in items or []:
        if isinstance(item, str) and item.strip():
            found.add(item.strip())
        elif isinstance(item, dict) and str(item.get("source") or "").strip():
            found.add(str(item["source"]).strip())
    return found


def organizer_day(match_id: str, kickoff_at: str | None) -> str | None:
    kickoff = parse_time(kickoff_at)
    zone = next((name for prefix, name in ORGANIZER_ZONES.items() if match_id.startswith(prefix)), None)
    if kickoff is None or zone is None:
        return None
    return kickoff.astimezone(ZoneInfo(zone)).date().isoformat()


def content_hash(item: dict) -> str:
    body = {key: item.get(key) for key in HASH_KEYS}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def prepare_match(item: Match) -> bool:
    """Normalize one card. Return False when it has no stable team ids and must not be stored."""
    if not team_id(item.home_team) or not team_id(item.away_team) or item.home_team["id"] == item.away_team["id"]:
        return False
    if item.kickoff_at:
        item.kickoff_precision = "instant"
    elif item.scheduled_date:
        item.kickoff_at = None
        item.kickoff_precision = "date_only"
    else:
        item.kickoff_precision = "unknown"
    if not item.scheduled_date:
        item.scheduled_date = organizer_day(item.id, item.kickoff_at)
    if item.raw_status == "":
        item.raw_status = None
    if not isinstance(item.independent_confirmations, list):
        item.independent_confirmations = []
    item.result_verified = len(confirmation_sources(item.independent_confirmations)) >= 2
    if item.status == "finished" and item.score_scope not in SCORE_SCOPES:
        item.score_scope = "unknown"
    if item.score_scope != "regulation":
        return True
    if item.regular_home is None and item.score_home is not None and item.score_away is not None:
        item.regular_home, item.regular_away = item.score_home, item.score_away
    return True


def present_match(item: dict) -> dict:
    """Fill the public card so old payloads expose the same keys."""
    defaults = {
        "scheduled_date": None, "source_url": None, "public_match_url": None,
        "regular_home": None, "regular_away": None, "extra_home": None, "extra_away": None,
        "penalties_home": None, "penalties_away": None, "score_scope": None,
        "kickoff_precision": None, "independent_confirmations": [], "result_verified": False,
        "raw_status": None, "archive_until": None, "finished_at": None,
    }
    for key, value in defaults.items():
        item.setdefault(key, value)
    if item.get("raw_status") == "":
        item["raw_status"] = None
    if not item.get("scheduled_date"):
        item["scheduled_date"] = organizer_day(item.get("id") or "", item.get("kickoff_at"))
    if item.get("kickoff_at"):
        item["kickoff_precision"] = "instant"
    elif item.get("scheduled_date"):
        item["kickoff_precision"] = "date_only"
    else:
        item["kickoff_precision"] = "unknown"
    if not isinstance(item.get("independent_confirmations"), list):
        item["independent_confirmations"] = []
    item["result_verified"] = len(confirmation_sources(item["independent_confirmations"])) >= 2
    if item.get("status") == "finished" and item.get("score_scope") not in SCORE_SCOPES:
        item["score_scope"] = "unknown"
    return item
