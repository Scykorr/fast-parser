import asyncio
import html
import json
from datetime import date

import pytest
import httpx

from fast_parser.adapters import SchemaError
from fast_parser.collector import Collector, ReferenceUnavailable, SourceFailure
from scripts.verify_spanish import compare, parse_month, team_key
from test_retention import game


def test_comparison_reports_time_status_and_score_conflicts(comp, now):
    left = game(comp, now, score_home=2, score_away=1)
    right = game(comp, now, score_home=1, score_away=1)
    right.kickoff_at = right.kickoff_at.replace("10:00", "11:00")
    result = compare([left], [right])
    assert result["paired"] == 1 and result["score_mismatches"] == 1
    assert result["kickoff_mismatches"] == 1
    right.status = "scheduled"
    assert compare([left], [right])["status_mismatches"] == 1


def test_explicit_aliases_do_not_merge_reserve_team():
    assert team_key("Atlético Madrid") == team_key("Atletico Madrid")
    assert team_key("FC Villareal") == team_key("Villarreal")
    assert team_key("CA Osasuna") != team_key("Club Atletico Osasuna B")


def test_ambiguous_comparison_is_rejected(comp, now):
    with pytest.raises(ValueError, match="Ambiguous"):
        compare([game(comp, now), game(comp, now)], [])


def card(label):
    row = {"id": "test", "competition": {"id": "20", "name": {"full": "Spanish La Liga"}},
           "start": {"date": label, "time": "15:00"}, "isFixture": True,
           "teams": {"home": {"id": "a", "name": {"full": "Test Home"}},
                     "away": {"id": "b", "name": {"full": "Test Away"}}}}
    return '<div data-component-name="ui-sport-match-score" data-state="' + html.escape(json.dumps(row), quote=True) + '"></div>'


def test_month_reference_checks_date_and_does_not_invent_results(now):
    comps, matches = parse_month(card("Tuesday 6th October 2026"), 2026, 10, now)
    assert comps[0]["country"] == "Испания"
    assert matches[0].kickoff_at == "2026-10-06T14:00:00+00:00"
    assert matches[0].score_home is None and matches[0].finished_at is None
    with pytest.raises(SchemaError, match="Wrong month"):
        parse_month(card("Tuesday 6th September 2026"), 2026, 10, now)
    with pytest.raises(SchemaError, match="Wrong year"):
        parse_month(card("Tuesday 6th October 2025"), 2026, 10, now)
    with pytest.raises(SchemaError):
        parse_month("No matches", 2026, 10, now)


def test_missing_reference_preserves_source_pacing_without_global_pause(store):
    async def run():
        collector = Collector(store)
        await collector.client.aclose()
        collector.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        try:
            with pytest.raises(ReferenceUnavailable):
                await collector.fetch("skysports_html", "https://example.invalid/reference", reference=True)
            state = store.source("skysports_html")
            assert state["failures"] == 0 and state["state"] == "closed"
            assert not store.reserve_request("skysports_html", 20)
        finally:
            await collector.client.aclose()
    asyncio.run(run())


def test_missing_production_page_still_records_failure(store):
    async def run():
        collector = Collector(store)
        await collector.client.aclose()
        collector.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        try:
            with pytest.raises(SourceFailure):
                await collector.fetch("skysports_html", "https://example.invalid/production")
            assert store.source("skysports_html")["failures"] == 1
        finally:
            await collector.client.aclose()
    asyncio.run(run())


def test_timeout_has_diagnostic_reason_and_keeps_official_cooldown(store):
    async def run():
        def timeout(request):
            raise httpx.ConnectTimeout("")
        collector = Collector(store)
        await collector.client.aclose()
        collector.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        try:
            with pytest.raises(SourceFailure, match="ConnectTimeout"):
                await collector.fetch("laliga_reference", "https://example.invalid/reference", reference=True)
            state = store.source("laliga_reference")
            assert state["error"] == "ConnectTimeout" and state["failures"] == 1
            assert not store.reserve_request("laliga_reference", 30)
        finally:
            await collector.client.aclose()
    asyncio.run(run())
