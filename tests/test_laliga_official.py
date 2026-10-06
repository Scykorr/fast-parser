import json
import pytest
from fast_parser.adapters import SchemaError
from scripts.verify_laliga_official import parse_official

def page(**changes):
    data = dict(competition="segunda-division", season="2026", gameweek={"week":8}, matches=[{
        "id":1, "status":"FullTime", "date":"2026-10-02T18:30:00+00:00",
        "home_team":{"name":"Home"}, "away_team":{"name":"Away"}, "home_score":4, "away_score":1}])
    data.update(changes)
    return '<script id="__NEXT_DATA__">'+json.dumps({"props":{"pageProps":data}})+'</script>'

def test_official_final_has_teams_score_utc_and_no_invented_end():
    m = parse_official(page(), "segunda-division", 2026, 8)[0]
    assert (m.home_team["name"], m.away_team["name"], m.score_home, m.score_away) == ("Home", "Away", 4, 1)
    assert m.finished_at is None and m.elapsed_minutes is None
    assert m.kickoff_at == "2026-10-02T18:30:00+00:00"

@pytest.mark.parametrize("changes", [{"competition":"primera-division"}, {"season":"2025"}, {"gameweek":{"week":7}}, {"matches":[]}])
def test_official_rejects_wrong_scope_and_empty_page(changes):
    with pytest.raises(SchemaError):
        parse_official(page(**changes), "segunda-division", 2026, 8)

def test_official_rejects_duplicate_and_missing_final_score():
    data = json.loads(page().split('>',1)[1].split('</script>')[0])["props"]["pageProps"]["matches"]
    with pytest.raises(SchemaError):
        parse_official(page(matches=data+data), "segunda-division", 2026, 8)
    data[0]["away_score"] = None
    with pytest.raises(SchemaError):
        parse_official(page(matches=data), "segunda-division", 2026, 8)
