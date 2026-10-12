from datetime import timedelta

from fastapi.testclient import TestClient

from fast_parser.app import create_app
from fast_parser.domain import content_hash, stamp
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
    assert archived[0]["content_hash"] == content_hash(archived[0])
    assert store.match("ol:1", later) is None and store.in_archive("ol:1", later)


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


def test_overdue_scheduled_is_published_as_unknown(store, comp, now):
    old = game(comp, now - timedelta(days=3), status="scheduled")
    old.id, old.external_id = "ol:old", "old"
    store.upsert_matches([old], now - timedelta(days=3))
    assert store.match("ol:old", now)["status"] == "unknown"
    fresh = game(comp, now - timedelta(minutes=1), status="scheduled")
    fresh.id, fresh.external_id = "ol:fresh", "fresh"
    store.upsert_matches([fresh], now)
    assert store.match("ol:fresh", now)["status"] == "scheduled"
    future = game(comp, now + timedelta(days=2), status="scheduled")
    future.id, future.external_id = "ol:future", "future"
    store.upsert_matches([future], now - timedelta(days=3))
    assert store.match("ol:future", now)["status"] == "scheduled"


def test_slip_ids_cross_leagues_and_archive_is_not_a_plain_404(tmp_path, comp, now):
    app = create_app(tmp_path / "slip.sqlite3", start_worker=False)
    store = app.state.store
    other = {"id": "ol:la1:2026", "shortcut": "la1", "season": "2026", "name": "Primera", "country": "Испания", "category": "league"}
    store.catalog([comp, other])
    settings = store.settings()
    settings.official_only = False
    store.save_settings(settings)
    first = game(comp, now, status="scheduled")
    second = game(comp, now, status="scheduled")
    second.id, second.external_id, second.competition_id = "ol:2", "2", other["id"]
    finished = game(comp, now, score_home=1, score_away=0, score_scope="regulation", regular_home=1, regular_away=0)
    finished.id, finished.external_id = "ol:done", "done"
    store.upsert_matches([first, second, finished], now)
    with TestClient(app) as client:
        page = client.get("/api/v1/matches", params=[("id", "ol:1"), ("id", "ol:2")]).json()
        assert {item["id"] for item in page["items"]} == {"ol:1", "ol:2"}
        assert all(item["content_hash"] for item in page["items"])
        events = client.get("/api/v1/changes", params=[("id", "ol:1"), ("id", "ol:2")]).json()["items"]
        assert {event["match_id"] for event in events} == {"ol:1", "ol:2"}
        assert events[0]["content_hash"] == events[0]["match"]["content_hash"]
        only = client.get("/api/v1/changes", params={"competition_id": other["id"]}).json()["items"]
        assert {event["match_id"] for event in only} == {"ol:2"}
        missing = client.get("/api/v1/matches/missing")
        assert missing.status_code == 404 and missing.json()["archived"] is False
        gone = client.get("/api/v1/matches/ol:done")
        assert gone.status_code == 404 and gone.json()["archived"] is True and gone.json()["id"] == "ol:done"
        archived = client.get("/api/v1/results", params={"id": "ol:done"}).json()["items"]
        assert archived[0]["id"] == "ol:done" and archived[0]["content_hash"]
        assert client.get("/api/v1/matches", params=[("id", f"x{i}") for i in range(101)]).status_code == 422


def test_date_only_precision_is_not_midnight(store, comp, now):
    match = game(comp, now, status="scheduled")
    match.kickoff_at = None
    match.scheduled_date = "2026-10-09"
    store.upsert_matches([match], now)
    saved = store.match(match.id, now)
    assert saved["kickoff_at"] is None and saved["kickoff_precision"] == "date_only"
    assert saved["scheduled_date"] == "2026-10-09"
