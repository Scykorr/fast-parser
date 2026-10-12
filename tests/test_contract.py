from datetime import timedelta

from fastapi.testclient import TestClient

from fast_parser.app import create_app
from fast_parser.domain import Match, stamp
from test_retention import game


def sky(comp, now):
    match = game(comp, now, status="scheduled")
    match.id, match.external_id, match.source = "sky:1", "1", "skysports_html"
    match.competition_id = "sky:1"
    return match


def test_results_keep_the_same_id_after_the_ui_card_expires(store, comp, now):
    store.upsert_matches([game(comp, now, score_home=2, score_away=1, score_scope="regulation",
                                regular_home=2, regular_away=1)], now)
    later = now + timedelta(hours=73)
    assert store.match("ol:1", later) is None
    assert store.cleanup(later) == 1
    archived = store.results(now=later)["items"]
    assert archived[0]["id"] == "ol:1"
    assert archived[0]["regular_home"] == 2 and archived[0]["score_scope"] == "regulation"
    assert "scheduled_date" in archived[0] and archived[0]["source_url"] is None


def test_card_without_team_ids_is_not_stored(store, comp, now):
    match = game(comp, now, status="scheduled")
    match.home_team = {"name": "Home"}
    match.away_team = {"name": "Away"}
    assert store.upsert_matches([match], now) == 0
    assert store.match(match.id, now) is None


def test_single_source_cannot_mark_result_verified(store, comp, now):
    match = game(comp, now, score_scope="regulation", regular_home=1, regular_away=0)
    match.result_verified = True
    match.independent_confirmations = [{"source": "openligadb"}]
    store.upsert_matches([match], now)
    saved = store.match(match.id, now)
    assert saved["result_verified"] is False and saved["independent_confirmations"] == [{"source": "openligadb"}]
    match.independent_confirmations = [{"source": "openligadb"}, {"source": "other"}]
    match.result_verified = True
    store.upsert_matches([match], now)
    assert store.match(match.id, now)["result_verified"] is True


def test_stale_live_is_published_as_unknown(store, comp, now):
    store.upsert_matches([game(comp, now, status="live")], now)
    assert store.match("ol:1", now)["status"] == "live"
    assert store.match("ol:1", now + timedelta(minutes=5))["status"] == "unknown"


def test_official_only_hides_legacy_cards_and_old_cursor_is_gone(tmp_path, comp, now):
    app = create_app(tmp_path / "api.sqlite3", start_worker=False)
    store = app.state.store
    store.catalog([comp, {"id": "sky:1", "shortcut": "sky:1", "season": "2026",
                          "name": "Sky", "country": "Англия", "category": "league"}])
    store.upsert_matches([game(comp, now, status="scheduled"), sky(comp, now)], now)
    with TestClient(app) as client:
        assert client.get("/api/v1/matches").json()["items"] == []
        page = client.get("/api/v1/changes", params={"competition_id": comp["id"]}).json()
        assert page["items"][0]["competition_id"] == comp["id"]
        assert page["items"][0]["content_hash"]
        assert client.get("/api/v1/changes", params={"competition_id": "sky:1"}).json()["items"]
        store.set_meta("changes_floor", "5")
        assert client.get("/api/v1/changes").status_code == 410
        store.set_meta("worker_heartbeat", stamp(now))
        stale = store.readiness(now + timedelta(days=6))
        assert stale["status"] == "stale" and stale["data_age_seconds"] >= 6 * 86400
        assert stale["collector_fresh"] is False


def test_date_only_precision_is_not_midnight(store, comp, now):
    match = game(comp, now, status="scheduled")
    match.kickoff_at = None
    match.scheduled_date = "2026-10-09"
    store.upsert_matches([match], now)
    saved = store.match(match.id, now)
    assert saved["kickoff_at"] is None and saved["kickoff_precision"] == "date_only"
    assert saved["scheduled_date"] == "2026-10-09"
