"""
Starter overview card math, checked against REAL MLB responses captured
live on 2026-10-05 (Yankees @ Rays, ALDS): Cam Schlittler and Freddy
Peralta. Trimmed to the fields the parser reads, otherwise exactly as MLB
returned them -- including Peralta's three "season" splits (he was traded
mid-season: combined total + one per team), and both pitchers' in-progress
10-05 start, which the card must leave out when previewing that game.

Expected numbers were worked out by hand from the same rows, and the
season/career lines match MLB's own published ERA/WHIP for each.

Run: python3 scripts/test_pitcher_overview.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bullpen
import parsing


def _start(date, game_type, outs, er, h, bb, k):
    return {"date": date, "gameType": game_type, "game": {"gamePk": 0},
            "stat": {"gamesStarted": 1, "outs": outs, "earnedRuns": er,
                     "hits": h, "baseOnBalls": bb, "strikeOuts": k}}


PERALTA_TOTALS = {"stats": [
    {"type": {"displayName": "season"}, "splits": [
        {"stat": {"gamesStarted": 32, "outs": 507, "earnedRuns": 83, "hits": 161,
                  "baseOnBalls": 63, "strikeOuts": 156, "era": "4.42", "whip": "1.33"}},
        {"stat": {"gamesStarted": 22, "outs": 341, "earnedRuns": 63, "hits": 120,
                  "baseOnBalls": 48, "strikeOuts": 113, "era": "4.99"}},
        {"stat": {"gamesStarted": 10, "outs": 166, "earnedRuns": 20, "hits": 41,
                  "baseOnBalls": 15, "strikeOuts": 43, "era": "3.25"}},
    ]},
    {"type": {"displayName": "career"}, "splits": [
        {"stat": {"gamesStarted": 194, "outs": 3300, "earnedRuns": 454, "hits": 855,
                  "baseOnBalls": 423, "strikeOuts": 1309, "era": "3.71", "whip": "1.16"}}]},
]}
PERALTA_LOG = {"stats": [{"type": {"displayName": "gameLog"}, "splits": [
    _start("2026-08-26", "R", 18, 0, 2, 0, 4),
    _start("2026-09-01", "R", 18, 1, 3, 0, 7),
    _start("2026-09-08", "R", 18, 1, 5, 1, 2),
    _start("2026-09-13", "R", 15, 0, 0, 3, 4),
    _start("2026-09-19", "R", 17, 1, 4, 2, 6),
    _start("2026-09-25", "R", 16, 0, 4, 3, 4),
    _start("2026-10-05", "D", 14, 1, 3, 1, 3),  # the game being previewed, in progress
]}]}

SCHLITTLER_TOTALS = {"stats": [
    {"type": {"displayName": "season"}, "splits": [
        {"stat": {"gamesStarted": 33, "outs": 581, "earnedRuns": 42, "hits": 129,
                  "baseOnBalls": 49, "strikeOuts": 239, "era": "1.95", "whip": "0.92"}}]},
    {"type": {"displayName": "career"}, "splits": [
        {"stat": {"gamesStarted": 47, "outs": 800, "earnedRuns": 66, "hits": 187,
                  "baseOnBalls": 80, "strikeOuts": 323, "era": "2.23", "whip": "1.00"}}]},
]}
SCHLITTLER_LOG = {"stats": [{"type": {"displayName": "gameLog"}, "splits": [
    _start("2026-09-02", "R", 24, 1, 2, 0, 9),
    _start("2026-09-08", "R", 21, 1, 3, 0, 10),
    _start("2026-09-13", "R", 18, 0, 1, 2, 8),
    _start("2026-09-19", "R", 18, 1, 3, 3, 6),
    _start("2026-09-24", "R", 9, 1, 3, 2, 5),
    _start("2026-09-29", "F", 19, 0, 2, 1, 10),  # Wild Card start
    _start("2026-10-05", "D", 13, 2, 6, 1, 2),   # the game being previewed
]}]}


def overview(totals, log, before_date="2026-10-05"):
    return bullpen.pitcher_overview(parsing.parse_pitching_totals(totals),
                                    parsing.parse_pitching_gamelog(log),
                                    n=5, before_date=before_date)


def test_traded_pitcher_uses_combined_season_line():
    o = overview(PERALTA_TOTALS, PERALTA_LOG)
    s = o["season"]
    # Must be the 32-start combined line, matching MLB's own 4.42 / 1.33.
    assert s["games_started"] == 32, s
    assert (s["era"], s["whip"], s["ip_display"]) == ("4.42", "1.33", "169.0"), s
    assert s["k9"] == "8.3", s
    assert o["career"]["era"] == "3.71", o["career"]
    print(f"test_traded_pitcher_uses_combined_season_line OK: {s['era']} ERA / {s['whip']} WHIP over {s['ip_display']} IP")


def test_last_five_excludes_the_previewed_game():
    o = overview(PERALTA_TOTALS, PERALTA_LOG)
    last = o["last_starts"]
    # 09-01 .. 09-25: 84 outs (28.0 IP), 3 ER, 16 H, 9 BB, 23 K
    assert (last["from_date"], last["to_date"]) == ("2026-09-01", "2026-09-25"), last
    assert (last["era"], last["whip"], last["k9"]) == ("0.96", "0.89", "7.4"), last
    assert last["ip_per_start"] == "5.2" and last["postseason_starts"] == 0, last  # 84/5 = 16.8 -> 17 outs
    print(f"test_last_five_excludes_the_previewed_game OK: {last['era']} ERA, {last['ip_per_start']} IP/start")


def test_last_five_spans_regular_season_and_postseason():
    o = overview(SCHLITTLER_TOTALS, SCHLITTLER_LOG)
    assert (o["season"]["era"], o["season"]["whip"]) == ("1.95", "0.92"), o["season"]
    assert o["career"]["era"] == "2.23", o["career"]
    last = o["last_starts"]
    # 09-08 .. 09-29 (incl. the Wild Card start): 85 outs (28.1 IP), 3 ER, 12 H, 8 BB, 39 K
    assert (last["from_date"], last["to_date"]) == ("2026-09-08", "2026-09-29"), last
    assert last["postseason_starts"] == 1, last
    assert (last["era"], last["whip"], last["k9"], last["ip_per_start"]) == ("0.95", "0.71", "12.4", "5.2"), last
    print(f"test_last_five_spans_regular_season_and_postseason OK: {last['era']} ERA, incl. 1 postseason start")


def test_no_starts_yet():
    o = overview({"stats": []}, {"stats": []})
    assert o == {"season": None, "last_starts": None, "career": None}, o
    print("test_no_starts_yet OK")


if __name__ == "__main__":
    test_traded_pitcher_uses_combined_season_line()
    test_last_five_excludes_the_previewed_game()
    test_last_five_spans_regular_season_and_postseason()
    test_no_starts_yet()
    print("\nAll pitcher overview tests passed.")
