import copy
import html
import json
from datetime import date, timedelta

import pytest

from fast_parser.adapters import SchemaError, catalog_openliga, parse_openliga, parse_sky


def test_real_schema_names_score_and_no_invented_end(openliga_row, comp, now):
    m = parse_openliga([openliga_row], comp, now)[0]
    assert m.home_team["name"] == "Team A"
    assert (m.score_home, m.score_away) == (2, 1)
    assert m.finished_at is None
    assert m.elapsed_minutes is None


def test_unfinished_past_match_is_not_falsely_live(openliga_row, comp, now):
    openliga_row["matchIsFinished"] = False
    openliga_row["matchResults"] = []
    m = parse_openliga([openliga_row], comp, now)[0]
    assert m.status == "unknown"
    assert m.score_home is None


@pytest.mark.parametrize("bad", [-1, "2", True])
def test_invalid_score_quarantined(openliga_row, comp, now, bad):
    openliga_row["matchResults"][0]["pointsTeam1"] = bad
    with pytest.raises(SchemaError):
        parse_openliga([openliga_row], comp, now)


def test_wrong_league_rejected(openliga_row, comp, now):
    openliga_row["leagueShortcut"] = "hockey"
    with pytest.raises(SchemaError):
        parse_openliga([openliga_row], comp, now)


def test_catalog_only_current_football(now):
    rows = [{"leagueSeason": "2026", "leagueShortcut": "bl1", "leagueName": "Bundesliga", "sport": {"sportId": 1}},
            {"leagueSeason": "2026", "leagueShortcut": "del", "leagueName": "Hockey", "sport": {"sportId": 2}},
            {"leagueSeason": "2007", "leagueShortcut": "old", "leagueName": "Old", "sport": {"sportId": 1}}]
    assert len(catalog_openliga(rows, now)) == 1


def sky_card(status="37'", **flags):
    row = {"id": "10", "competition": {"id": "20", "name": {"full": "UEFA Nations League"}},
           "start": {"date": "Tuesday 6th October", "time": "15:00"},
           "teams": {"home": {"id": "a", "name": {"full": "<A & B>"}, "score": {"current": 0}},
                     "away": {"id": "b", "name": {"full": "Team B"}, "score": {"current": 1}}},
           "status": status, "statusQualifier": None, **flags}
    return '<div data-component-name="ui-sport-match-score" data-state="' + html.escape(json.dumps(row), quote=True) + '"></div>'


@pytest.mark.parametrize("raw,minute,added", [("37'", 37, None), ("45+2'", 45, 2), ("68'", 68, None)])
def test_sky_explicit_minute_without_invented_half(now, raw, minute, added):
    comps, matches = parse_sky(sky_card(raw, isInPlay=True), date(2026, 10, 6), now)
    m = matches[0]
    assert m.status == "live"
    assert m.period is None
    assert m.elapsed_minutes == minute and m.added_minutes == added
    assert m.kickoff_at == "2026-10-06T14:00:00+00:00"
    assert comps[0]["country"] == "Международные"


def test_sky_ht_final_postponed_and_null(now):
    assert parse_sky(sky_card("HT", isInPlay=True), date(2026, 10, 6), now)[1][0].period == "half_time"
    finished = parse_sky(sky_card("FT", isResult=True), date(2026, 10, 6), now)[1][0]
    assert finished.status == "finished" and finished.finished_at is None
    postponed = parse_sky(sky_card("Postponed", isPostponed=True, isResult=True), date(2026, 10, 6), now)[1][0]
    assert postponed.status == "postponed" and postponed.score_home is None


def test_sky_wrong_date_and_block_page(now):
    with pytest.raises(SchemaError):
        parse_sky(sky_card(isInPlay=True), date(2026, 10, 7), now)
    with pytest.raises(SchemaError):
        parse_sky("Just a moment... Enable JavaScript and cookies", date(2026, 10, 6), now)


def test_sky_changed_html_is_not_empty_success(now):
    with pytest.raises(SchemaError):
        parse_sky("<html><body>Login</body></html>", date(2026, 10, 6), now)
