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


if __name__ == "__main__":
    main()
