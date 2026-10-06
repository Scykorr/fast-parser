from datetime import UTC, datetime

import pytest

from fast_parser.storage import Store


@pytest.fixture
def now():
    return datetime(2026, 10, 6, 12, tzinfo=UTC)


@pytest.fixture
def comp():
    return {"id": "ol:bl1:2026", "shortcut": "bl1", "season": "2026", "name": "Bundesliga", "country": "Германия", "category": "Не указана"}


@pytest.fixture
def store(tmp_path, comp):
    db = Store(tmp_path / "test.sqlite3")
    db.catalog([comp])
    return db


@pytest.fixture
def openliga_row():
    # Synthetic content matching the verified public API contract; no retained real match payload.
    return {"matchID": 10, "leagueShortcut": "bl1", "leagueSeason": 2026,
            "matchDateTimeUTC": "2026-10-06T10:00:00Z", "matchIsFinished": True,
            "team1": {"teamId": 1, "teamName": "Team A"}, "team2": {"teamId": 2, "teamName": "Team B"},
            "matchResults": [{"resultName": "Endergebnis", "pointsTeam1": 2, "pointsTeam2": 1}]}
