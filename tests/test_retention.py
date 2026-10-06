from datetime import timedelta

from fast_parser.domain import Match, stamp


def game(comp, now, **fields):
    return Match(id="ol:1", competition_id=comp["id"], source="openligadb", external_id="1",
                 home_team={"id": "1", "name": "A"}, away_team={"id": "2", "name": "B"},
                 kickoff_at=stamp(now - timedelta(hours=2)), status=fields.pop("status", "finished"), **fields)


def test_72_hour_boundary_and_cleanup(store, comp, now):
    store.upsert_matches([game(comp, now, finished_at=stamp(now))], now)
    assert store.match("ol:1", now + timedelta(hours=72) - timedelta(microseconds=1)) is not None
    assert store.match("ol:1", now + timedelta(hours=72)) is None
    assert store.cleanup(now + timedelta(hours=72), dry_run=True) == 1
    assert store.cleanup(now + timedelta(hours=72)) == 1
    assert store.match("ol:1", now + timedelta(hours=72)) is None


def test_first_observed_anchor_not_extended_by_score(store, comp, now):
    store.upsert_matches([game(comp, now, score_home=1)], now)
    store.upsert_matches([game(comp, now, score_home=2)], now + timedelta(hours=10))
    m = store.match("ol:1", now)
    assert m["score_home"] == 2
    assert m["first_observed_finished_at"] == stamp(now)
    assert m["expires_at"] == stamp(now + timedelta(hours=72))
    assert m["retention_anchor_method"] == "observed"


def test_late_exact_end_removes_match(store, comp, now):
    store.upsert_matches([game(comp, now)], now)
    store.upsert_matches([game(comp, now, finished_at=stamp(now - timedelta(hours=80)))], now + timedelta(hours=1))
    assert store.match("ol:1", now + timedelta(hours=1)) is None


def test_deleted_match_does_not_return_after_restart(store, comp, now):
    store.upsert_matches([game(comp, now)], now)
    later = now + timedelta(hours=73)
    store.cleanup(later)
    from fast_parser.storage import Store
    restarted = Store(store.path)
    assert restarted.upsert_matches([game(comp, now)], later) == 0
    assert restarted.match("ol:1", later) is None


def test_moved_future_match_can_revive(store, comp, now):
    store.upsert_matches([game(comp, now)], now)
    later = now + timedelta(hours=73)
    store.cleanup(later)
    m = game(comp, now, status="scheduled")
    m.kickoff_at = stamp(later + timedelta(days=1))
    store.upsert_matches([m], later)
    assert store.match(m.id, later)["expires_at"] is None


def test_cancelled_has_separate_anchor_and_unknown_is_retained(store, comp, now):
    store.upsert_matches([game(comp, now, status="cancelled")], now)
    assert store.match("ol:1", now)["expires_at"]
    store.upsert_matches([game(comp, now, status="unknown")], now + timedelta(hours=1))
    assert store.match("ol:1", now + timedelta(days=4))["expires_at"] is None


def test_old_source_response_cannot_overwrite_new(store, comp, now):
    store.upsert_matches([game(comp, now, score_home=4, source_updated_at=stamp(now))], now)
    store.upsert_matches([game(comp, now, score_home=0, source_updated_at=stamp(now - timedelta(minutes=1)))], now)
    assert store.match("ol:1", now)["score_home"] == 4


def test_pagination_order_and_no_duplicates(store, comp, now):
    matches = []
    for i in range(8):
        m = game(comp, now, status="scheduled")
        m.id, m.external_id = f"ol:{i}", str(i)
        m.kickoff_at = stamp(now + timedelta(hours=8 - i))
        matches.append(m)
    store.upsert_matches(matches, now)
    ids, cursor = [], None
    while True:
        page = store.matches(cursor=cursor, limit=3, now=now)
        ids.extend(m["id"] for m in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert ids == [f"ol:{i}" for i in reversed(range(8))]
