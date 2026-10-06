from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    timezone: str = "Europe/Moscow"
    mode: str = "rolling"
    lookback_days: int = Field(default=3, ge=0, le=3)
    lookahead_days: int = Field(default=7, ge=1, le=90)
    date_from: str | None = None
    date_to: str | None = None
    fixtures_interval_seconds: int = Field(default=21600, ge=900, le=604800)
    results_interval_seconds: int = Field(default=300, ge=120, le=86400)
    live_interval_seconds: int = Field(default=120, ge=120, le=86400)
    final_recheck_interval_seconds: int = Field(default=21600, ge=900, le=86400)
    active_windows: list[str] = Field(default_factory=list, max_length=10)
    enabled_leagues: list[str] = Field(default_factory=lambda: ["bl1", "bl2", "bl3", "pl", "la1", "ucl", "dfb"])
    html_enabled: bool = True

    @model_validator(mode="after")
    def validate_values(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Неизвестный часовой пояс IANA") from exc
        if self.mode not in {"rolling", "absolute"}:
            raise ValueError("mode: rolling или absolute")
        if len(self.enabled_leagues) > 200 or len(set(self.enabled_leagues)) != len(self.enabled_leagues):
            raise ValueError("Не более 200 уникальных лиг")
        if self.mode == "absolute":
            if not self.date_from or not self.date_to:
                raise ValueError("Нужны обе даты абсолютного диапазона")
            start = datetime.fromisoformat(self.date_from)
            end = datetime.fromisoformat(self.date_to)
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("Даты должны иметь часовой пояс; начало раньше конца")
            if (end - start).days > 90:
                raise ValueError("Абсолютный диапазон не должен превышать 90 дней")
        for window in self.active_windows:
            try:
                a, b = window.split("-")
                if time.fromisoformat(a) == time.fromisoformat(b):
                    raise ValueError()
            except ValueError as exc:
                raise ValueError("Окно: HH:MM-HH:MM, начало и конец различны") from exc
        return self

    def in_window(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.timezone)).time().replace(tzinfo=None)
        for window in self.active_windows:
            a, b = map(time.fromisoformat, window.split("-"))
            if (a < b and a <= local < b) or (a > b and (local >= a or local < b)):
                return True
        return not self.active_windows
