"""
Daily ingestion job.

For a given date (default: today), this:
  1. Pulls the schedule + probable starting pitchers for every game.
  2. For each game, pulls both teams' active rosters.
  3. For each probable pitcher, pulls BVP stats against every position
     player on the opposing roster (pitchers don't bat in most games, so
     we skip opposing pitchers as "batters").
  4. Upserts everything into the local SQLite database.

This keeps the ongoing API usage small and predictable: roughly
(games today) x (2 lineups x ~13 position players) calls, once a day,
rather than paying per-lookup every time someone opens the site.

Run: python3 scripts/ingest_daily.py [YYYY-MM-DD]
"""
from __future__ import annotations

import sys
import time
import traceback
from datetime import date
from pathlib import Path

# Make this importable both as a standalone script (python3 scripts/ingest_daily.py,
# run from anywhere) and as a module from the web app's background scheduler,
# regardless of the caller's current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import ingest_appearances
import mlb_api
import parsing
import venues as venues_mod

REQUEST_PAUSE_SECONDS = 0.3  # be a polite citizen of a free public API


def ingest_date(target_date: str, force: bool = False) -> int:
    """Ingest one date's games. Returns the number of games processed.

    Safe to call repeatedly: if this date was already fully ingested, it
    skips the work (pass force=True to re-run anyway).
    """
    conn = db.connect()
    try:
        if not force and db.run_already_done(conn, target_date):
            print(f"{target_date}: already ingested, skipping (pass force=True to redo)")
            return 0

        db.start_run(conn, target_date)
        schedule_raw = mlb_api.get_schedule(target_date)
        games = parsing.parse_schedule(schedule_raw)
        print(f"{target_date}: {len(games)} game(s) found")

        for game in games:
            db.upsert_team(conn, game["home_team_id"], game["home_team_name"])
            db.upsert_team(conn, game["away_team_id"], game["away_team_name"])

            # Players referenced by a row must exist before the row that
            # references them is inserted (foreign keys are enforced) --
            # so upsert the probable pitchers *before* upsert_game, since
            # games.home/away_probable_pitcher_id points at players.id.
            home_pitcher = game["home_probable_pitcher_id"]
            away_pitcher = game["away_probable_pitcher_id"]
            if home_pitcher:
                db.upsert_player(conn, home_pitcher,
                                  game["home_probable_pitcher_name"], role="pitcher")
            if away_pitcher:
                db.upsert_player(conn, away_pitcher,
                                  game["away_probable_pitcher_name"], role="pitcher")

            # Same reasoning for the venue: games.venue_id points at
            # venues.id, so the venue row has to exist first. This is a
            # no-op after the first time any game references this park.
            venues_mod.ensure_venue(conn, game.get("venue_id"))

            db.upsert_game(conn, game)
            conn.commit()

            venues_mod.refresh_weather(conn, game)

            # Trailing-window box scores for both teams -- feeds bullpen
            # fatigue and starter recent-form. Idempotent (skips games
            # already backfilled), so safe to call for every game today
            # even though most days this is a no-op after the first run.
            for team_id in (game["home_team_id"], game["away_team_id"]):
                n = ingest_appearances.backfill_team_appearances(conn, team_id, target_date)
                if n:
                    print(f"    appearances: team {team_id}, {n} new game(s) backfilled")

            matchup_pairs = []
            if home_pitcher:
                matchup_pairs += _pairs_for_pitcher(
                    conn, home_pitcher, game["away_team_id"])
            if away_pitcher:
                matchup_pairs += _pairs_for_pitcher(
                    conn, away_pitcher, game["home_team_id"])

            print(f"  game {game['game_pk']}: "
                  f"{game['away_team_name']} @ {game['home_team_name']} "
                  f"-> {len(matchup_pairs)} batter/pitcher pairs to refresh")

            for batter_id, pitcher_id in matchup_pairs:
                _ingest_matchup(conn, batter_id, pitcher_id)

        db.finish_run(conn, target_date, len(games))
        print("Done.")
        return len(games)
    except Exception as exc:
        db.fail_run(conn, target_date, str(exc))
        raise
    finally:
        conn.close()


def refresh_probable_pitchers(target_date: str) -> int:
    """Lightweight check that runs every hour REGARDLESS of whether
    target_date's full ingestion has already happened for the day.

    The full ingest_date() above is guarded by ingestion_runs so it only
    does its (expensive) work once per date -- which means a probable
    pitcher MLB announces later in the day (very common: teams often lock
    in a starter, especially a bullpen-game "opener", just hours before
    first pitch) never got picked up until the next calendar day. This
    re-pulls just the schedule (one cheap API call) and fills in any
    probable pitcher that's newly known since the last check, upserting
    the game row the same way ingest_date does.

    To stay cheap enough to run hourly all day, the expensive per-pitcher
    work (pulling BVP stats against the opposing roster, backfilling
    recent appearances) only runs for a pitcher that's newly discovered
    this pass -- not the whole day's slate, which ingest_date already
    covers once. Returns how many newly-announced pitchers were found.
    """
    conn = db.connect()
    newly_found = 0
    try:
        schedule_raw = mlb_api.get_schedule(target_date)
        games = parsing.parse_schedule(schedule_raw)

        for game in games:
            status = (game.get("status") or "").strip().lower()

            existing = conn.execute(
                "SELECT home_probable_pitcher_id, away_probable_pitcher_id "
                "FROM games WHERE game_pk = ?",
                (game["game_pk"],),
            ).fetchone()
            had_home = existing[0] if existing else None
            had_away = existing[1] if existing else None

            db.upsert_team(conn, game["home_team_id"], game["home_team_name"])
            db.upsert_team(conn, game["away_team_id"], game["away_team_name"])

            # Bullpen fatigue/recent-form backfill every hour for both of
            # today's teams, regardless of whether a pitcher was just
            # announced -- this used to only fire the first time a team's
            # pitcher got set for the day, which meant a team whose
            # starter was announced hours before anyone noticed a backfill
            # problem would never get retried for the rest of the day.
            # backfill_team_appearances is cheap and idempotent (it skips
            # any game already stored), so doing this every hour for every
            # team playing today is a self-healing retry, not wasted work.
            ingest_appearances.backfill_team_appearances(conn, game["home_team_id"], target_date)
            ingest_appearances.backfill_team_appearances(conn, game["away_team_id"], target_date)

            if status in ingest_appearances._COMPLETED_STATUSES:
                continue  # nothing left to announce for a finished game

            home_pitcher = game["home_probable_pitcher_id"]
            away_pitcher = game["away_probable_pitcher_id"]
            if home_pitcher:
                db.upsert_player(conn, home_pitcher,
                                  game["home_probable_pitcher_name"], role="pitcher")
            if away_pitcher:
                db.upsert_player(conn, away_pitcher,
                                  game["away_probable_pitcher_name"], role="pitcher")

            venues_mod.ensure_venue(conn, game.get("venue_id"))
            db.upsert_game(conn, game)
            conn.commit()

            new_pairs = []
            if home_pitcher is not None and home_pitcher != had_home:
                print(f"    newly announced: {game['home_probable_pitcher_name']} "
                      f"(game {game['game_pk']})")
                newly_found += 1
                new_pairs += _pairs_for_pitcher(conn, home_pitcher, game["away_team_id"])
            if away_pitcher is not None and away_pitcher != had_away:
                print(f"    newly announced: {game['away_probable_pitcher_name']} "
                      f"(game {game['game_pk']})")
                newly_found += 1
                new_pairs += _pairs_for_pitcher(conn, away_pitcher, game["home_team_id"])

            for batter_id, pitcher_id in new_pairs:
                _ingest_matchup(conn, batter_id, pitcher_id)
    except Exception:
        # Same philosophy as the scheduler loop around ingest_date: a bad
        # hour (API hiccup, unexpected shape) shouldn't crash the process
        # or block the next check.
        print(f"[refresh_probable_pitchers] {target_date} failed:")
        traceback.print_exc()
    finally:
        conn.close()
    return newly_found


def _pairs_for_pitcher(conn, pitcher_id: int, opposing_team_id: int) -> list[tuple[int, int]]:
    roster_raw = mlb_api.get_team_roster(opposing_team_id)
    roster = parsing.parse_roster(roster_raw)
    pairs = []
    for player in roster:
        if player["position_type"] == "Pitcher":
            continue  # skip pitchers batting (rare, mostly NL/no-DH edge cases)
        db.upsert_player(conn, player["id"], player["full_name"],
                          role="batter", team_id=opposing_team_id)
        pairs.append((player["id"], pitcher_id))
    conn.commit()
    return pairs


def _ingest_matchup(conn, batter_id: int, pitcher_id: int) -> None:
    try:
        raw = mlb_api.get_vs_player(batter_id, pitcher_id)
    except Exception as exc:  # network hiccups shouldn't kill the whole run
        print(f"    skip batter={batter_id} pitcher={pitcher_id}: {exc}")
        return

    career, seasons = parsing.parse_vs_player(raw)
    if career is None:
        return  # no history between this pair yet
    db.upsert_matchup_career(conn, career)
    for s in seasons:
        # A season row can reference teams from years past -- not just
        # today's two teams -- since career history spans whatever teams
        # the batter/pitcher were on at the time. Those teams must exist
        # before the row referencing them does (same foreign-key reason
        # as the game/pitcher ordering above).
        if s.get("team_id") is not None:
            db.upsert_team(conn, s["team_id"], s.get("team_name") or "Unknown")
        if s.get("opponent_id") is not None:
            db.upsert_team(conn, s["opponent_id"], s.get("opponent_name") or "Unknown")
        db.upsert_matchup_season(conn, s)
    conn.commit()
    time.sleep(REQUEST_PAUSE_SECONDS)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv[1:]
    target = args[0] if args else date.today().isoformat()
    ingest_date(target, force=force)
