from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime


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
    raw_status: str = ""
    verification: str = "single_source"
    result_verified: bool = False
    source_url: str | None = None
    scheduled_date: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
