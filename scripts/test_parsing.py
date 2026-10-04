"""
Quick smoke test: parse the real sample response (Aaron Judge vs Blake
Snell, captured live from statsapi.mlb.com) and write it into a scratch
database, then check the numbers round-trip correctly.

Run: python3 scripts/test_parsing.py
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import parsing

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "data" / "sample_vsplayer_judge_snell.json"


def main() -> None:
    raw = json.loads(FIXTURE.read_text())
    career, seasons = parsing.parse_vs_player(raw)

    assert career is not None, "expected a career row for Judge vs Snell"
    assert career["batter_id"] == 592450
    assert career["pitcher_id"] == 605483
    assert career["games_played"] == 11
    assert career["hits"] == 2
    assert career["avg"] == ".111"
    print(f"career row OK: {career}")

    assert len(seasons) == 6, f"expected 6 season rows, got {len(seasons)}"
    seasons_by_year = {s["season"]: s for s in seasons}
    assert seasons_by_year["2024"]["hits"] == 1
    assert seasons_by_year["2024"]["opponent_id"] == 137  # Giants
    assert seasons_by_year["2016"]["team_id"] == 147  # Yankees
    print(f"season rows OK: {len(seasons)} seasons, 2016-2024")

    # Round-trip through an in-memory database.
    conn = sqlite3.connect(":memory:")
    conn.executescript((ROOT / "data" / "schema.sql").read_text())

    db.upsert_player(conn, career["batter_id"], "Aaron Judge", role="batter")
    db.upsert_player(conn, career["pitcher_id"], "Blake Snell", role="pitcher")
    db.upsert_matchup_career(conn, career)
    for s in seasons:
        db.upsert_matchup_season(conn, s)
    conn.commit()

    got = conn.execute(
        "SELECT games_played, hits, avg FROM matchup_career WHERE batter_id=? AND pitcher_id=?",
        (career["batter_id"], career["pitcher_id"]),
    ).fetchone()
    assert got == (11, 2, ".111"), got
    print(f"db round-trip OK: {got}")

    season_count = conn.execute("SELECT COUNT(*) FROM matchup_season").fetchone()[0]
    assert season_count == 6, season_count

    # Upsert again with the same data to confirm no duplicate-key errors
    # and that ON CONFLICT updates in place rather than erroring.
    db.upsert_matchup_career(conn, career)
    for s in seasons:
        db.upsert_matchup_season(conn, s)
    conn.commit()
    season_count_after = conn.execute("SELECT COUNT(*) FROM matchup_season").fetchone()[0]
    assert season_count_after == 6, "re-running ingestion should not create duplicates"
    print("idempotent re-upsert OK (no duplicate rows)")

    conn.close()
    print("\nAll parsing + db tests passed.")


def test_innings_pitched_to_outs() -> None:
    assert parsing._innings_pitched_to_outs("6.0") == 18
    assert parsing._innings_pitched_to_outs("5.1") == 16
    assert parsing._innings_pitched_to_outs("5.2") == 17
    assert parsing._innings_pitched_to_outs("0.0") == 0
    assert parsing._innings_pitched_to_outs(None) is None
    assert parsing._innings_pitched_to_outs("") is None
    print("test_innings_pitched_to_outs OK")


# Hand-built, following the MLB Stats API's documented box-score shape
# (same stat field names -- baseOnBalls, strikeOuts, numberOfPitches, etc.
# -- already verified elsewhere in this codebase's vsPlayer responses).
# Not a captured live response like the fixture above -- worth a one-time
# spot-check against a real completed game's box score before fully
# trusting this in production.
_FAKE_BOXSCORE = {
    "teams": {
        "away": {
            "team": {"id": 147},  # Yankees
            "pitchers": [605483, 123456],  # starter listed first
            "players": {
                "ID605483": {
                    "person": {"id": 605483, "fullName": "Blake Snell"},
                    "stats": {"pitching": {
                        "gamesStarted": 1, "inningsPitched": "6.0",
                        "numberOfPitches": 94, "battersFaced": 23,
                        "earnedRuns": 2, "baseOnBalls": 1,
                        "strikeOuts": 7, "hits": 4,
                    }},
                },
                "ID123456": {
                    "person": {"id": 123456, "fullName": "Middle Reliever"},
                    "stats": {"pitching": {
                        "gamesStarted": 0, "inningsPitched": "1.1",
                        "numberOfPitches": 19, "battersFaced": 5,
                        "earnedRuns": 0, "baseOnBalls": 0,
                        "strikeOuts": 2, "hits": 1,
                    }},
                },
                "ID592450": {  # a position player -- no pitching block
                    "person": {"id": 592450, "fullName": "Aaron Judge"},
                    "stats": {"batting": {"hits": 1}},
                },
            },
        },
        "home": {
            "team": {"id": 137},
            "pitchers": [999001],
            "players": {
                "ID999001": {
                    "person": {"id": 999001, "fullName": "Home Starter"},
                    "stats": {"pitching": {
                        "gamesStarted": 1, "inningsPitched": "5.2",
                        "numberOfPitches": 101, "battersFaced": 26,
                        "earnedRuns": 3, "baseOnBalls": 3,
                        "strikeOuts": 5, "hits": 6,
                    }},
                },
            },
        },
    },
}


def test_parse_boxscore() -> None:
    rows = parsing.parse_boxscore(_FAKE_BOXSCORE, game_pk=777001, game_date="2026-09-30")
    assert len(rows) == 3, f"expected 3 pitching lines (batter skipped), got {len(rows)}"

    by_id = {r["pitcher_id"]: r for r in rows}
    snell = by_id[605483]
    assert snell["role"] == "starter"
    assert snell["outs"] == 18
    assert snell["team_id"] == 147
    assert snell["game_pk"] == 777001
    assert snell["game_date"] == "2026-09-30"

    reliever = by_id[123456]
    assert reliever["role"] == "reliever"
    assert reliever["outs"] == 4  # 1.1 IP -> 3 + 1

    home_starter = by_id[999001]
    assert home_starter["role"] == "starter"
    assert home_starter["outs"] == 17  # 5.2 IP -> 15 + 2
    assert home_starter["team_id"] == 137

    assert 592450 not in by_id, "position player with no pitching block should be skipped"
    print(f"test_parse_boxscore OK: {len(rows)} pitching lines")


if __name__ == "__main__":
    main()
    test_innings_pitched_to_outs()
    test_parse_boxscore()
    print("\nAll boxscore parsing tests passed.")
