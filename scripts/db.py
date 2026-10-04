"""Upsert helpers for writing parsed rows into the SQLite database."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# In development this defaults to the repo's own data/ folder. In production
# (Railway), set BVP_DATA_DIR to the mounted volume's path (e.g. /data) so
# the database survives restarts and redeploys instead of living on
# throwaway container storage.
DATA_DIR = Path(os.environ.get("BVP_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "bvp.db"


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def run_already_done(conn: sqlite3.Connection, run_date: str) -> bool:
    row = conn.execute(
        "SELECT status FROM ingestion_runs WHERE run_date = ?", (run_date,)
    ).fetchone()
    return row is not None and row[0] == "done"


def start_run(conn: sqlite3.Connection, run_date: str) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_runs (run_date, started_at, status)
        VALUES (?, datetime('now'), 'running')
        ON CONFLICT(run_date) DO UPDATE SET
            started_at = datetime('now'), status = 'running', error = NULL
        """,
        (run_date,),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_date: str, games_count: int) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET finished_at = datetime('now'), status = 'done', games_count = ?
        WHERE run_date = ?
        """,
        (games_count, run_date),
    )
    conn.commit()


def fail_run(conn: sqlite3.Connection, run_date: str, error: str) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET finished_at = datetime('now'), status = 'failed', error = ?
        WHERE run_date = ?
        """,
        (error, run_date),
    )
    conn.commit()


def upsert_player(conn: sqlite3.Connection, player_id: int, full_name: str,
                   role: str | None = None, team_id: int | None = None) -> None:
    conn.execute(
        """
        INSERT INTO players (id, full_name, role, team_id, updated_at)
        VALUES (?, ?, COALESCE(?, 'both'), ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            full_name = excluded.full_name,
            role = CASE
                WHEN players.role = excluded.role THEN players.role
                WHEN players.role = 'both' OR excluded.role = 'both' THEN 'both'
                ELSE excluded.role
            END,
            team_id = COALESCE(excluded.team_id, players.team_id),
            updated_at = datetime('now')
        """,
        (player_id, full_name, role, team_id),
    )


def upsert_team(conn: sqlite3.Connection, team_id: int, name: str) -> None:
    conn.execute(
        """
        INSERT INTO teams (id, name) VALUES (?, ?)
        ON CONFLICT(id) DO UPDATE SET name = excluded.name
        """,
        (team_id, name),
    )


def _lineup_json(rows) -> str | None:
    """A lineup is only worth storing once it actually has batters in it --
    an empty list (not yet announced) stays NULL rather than "[]", so the
    front end's single "not announced yet" check (falsy) covers both the
    column never having been touched and a prior pull seeing nothing yet."""
    return json.dumps(rows) if rows else None


def upsert_game(conn: sqlite3.Connection, game: dict) -> None:
    conn.execute(
        """
        INSERT INTO games (
            game_pk, game_date, game_date_time, game_type, status, venue_id,
            home_team_id, away_team_id,
            home_probable_pitcher_id, away_probable_pitcher_id,
            home_lineup, away_lineup, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(game_pk) DO UPDATE SET
            game_date = excluded.game_date,
            game_date_time = excluded.game_date_time,
            game_type = excluded.game_type,
            status = excluded.status,
            venue_id = excluded.venue_id,
            home_team_id = excluded.home_team_id,
            away_team_id = excluded.away_team_id,
            home_probable_pitcher_id = excluded.home_probable_pitcher_id,
            away_probable_pitcher_id = excluded.away_probable_pitcher_id,
            -- Once a lineup is posted it doesn't go back to unposted, but a
            -- later pull could still correct a late scratch/substitution --
            -- so only overwrite with a fresh non-empty value, never clobber
            -- an already-stored lineup with a since-stale empty one.
            home_lineup = COALESCE(excluded.home_lineup, games.home_lineup),
            away_lineup = COALESCE(excluded.away_lineup, games.away_lineup),
            updated_at = datetime('now')
        """,
        (
            game["game_pk"], game["game_date"], game.get("game_date_time"),
            game["game_type"], game["status"], game.get("venue_id"),
            game["home_team_id"], game["away_team_id"],
            game["home_probable_pitcher_id"], game["away_probable_pitcher_id"],
            _lineup_json(game.get("home_lineup")), _lineup_json(game.get("away_lineup")),
        ),
    )


def upsert_venue(conn: sqlite3.Connection, venue: dict) -> None:
    cols = ["id", "name", "city", "state", "lat", "lon", "azimuth_angle",
            "elevation", "roof_type", "capacity", "left_line", "left_center",
            "center", "right_center", "right_line"]
    placeholders = ", ".join("?" for _ in cols)
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"""
        INSERT INTO venues ({", ".join(cols)}, updated_at)
        VALUES ({placeholders}, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            {update_clause}, updated_at = datetime('now')
        """,
        [venue.get(c) for c in cols],
    )


def upsert_game_weather(conn: sqlite3.Connection, weather: dict) -> None:
    cols = ["game_pk", "wind_speed_mph", "wind_dir_deg", "wind_dir_compass",
            "temp_f", "sky", "forecast_time"]
    placeholders = ", ".join("?" for _ in cols)
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "game_pk")
    conn.execute(
        f"""
        INSERT INTO game_weather ({", ".join(cols)}, fetched_at)
        VALUES ({placeholders}, datetime('now'))
        ON CONFLICT(game_pk) DO UPDATE SET
            {update_clause}, fetched_at = datetime('now')
        """,
        [weather.get(c) for c in cols],
    )


_CAREER_COLS = [
    "games_played", "at_bats", "plate_appearances", "hits", "doubles", "triples",
    "home_runs", "runs", "strike_outs", "base_on_balls", "intentional_walks", "hit_by_pitch",
    "total_bases", "rbi", "stolen_bases", "caught_stealing", "left_on_base", "sac_bunts", "sac_flies",
    "ground_into_double_play", "number_of_pitches", "avg", "obp", "slg", "ops",
]


def upsert_matchup_career(conn: sqlite3.Connection, row: dict) -> None:
    cols = ["batter_id", "pitcher_id"] + _CAREER_COLS
    placeholders = ", ".join("?" for _ in cols)
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in _CAREER_COLS)
    conn.execute(
        f"""
        INSERT INTO matchup_career ({", ".join(cols)}, updated_at)
        VALUES ({placeholders}, datetime('now'))
        ON CONFLICT(batter_id, pitcher_id) DO UPDATE SET
            {update_clause}, updated_at = datetime('now')
        """,
        [row.get(c) for c in cols],
    )


_APPEARANCE_COLS = [
    "game_pk", "pitcher_id", "team_id", "game_date", "role", "outs",
    "pitches", "batters_faced", "earned_runs", "base_on_balls",
    "strike_outs", "hits",
]


def upsert_pitcher_appearance(conn: sqlite3.Connection, row: dict) -> None:
    """One pitcher's line from one game's box score. Idempotent: re-running
    ingestion for an already-stored game updates the row in place (an
    official scorer's correction after the fact is rare but does happen)
    rather than erroring or duplicating."""
    placeholders = ", ".join("?" for _ in _APPEARANCE_COLS)
    update_clause = ", ".join(
        f"{c} = excluded.{c}" for c in _APPEARANCE_COLS if c not in ("game_pk", "pitcher_id")
    )
    conn.execute(
        f"""
        INSERT INTO pitcher_appearances ({", ".join(_APPEARANCE_COLS)}, updated_at)
        VALUES ({placeholders}, datetime('now'))
        ON CONFLICT(game_pk, pitcher_id) DO UPDATE SET
            {update_clause}, updated_at = datetime('now')
        """,
        [row.get(c) for c in _APPEARANCE_COLS],
    )


def appearance_exists(conn: sqlite3.Connection, game_pk: int) -> bool:
    """Whether this game's box score has already been pulled -- a completed
    game's pitching lines don't change, so there's no reason to re-fetch
    and re-parse the box score every day a team happens to still be in the
    trailing lookback window."""
    row = conn.execute(
        "SELECT 1 FROM pitcher_appearances WHERE game_pk = ? LIMIT 1", (game_pk,)
    ).fetchone()
    return row is not None


_SEASON_COLS = ["team_id", "opponent_id"] + _CAREER_COLS


def upsert_matchup_season(conn: sqlite3.Connection, row: dict) -> None:
    cols = ["batter_id", "pitcher_id", "season"] + _SEASON_COLS
    placeholders = ", ".join("?" for _ in cols)
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in _SEASON_COLS)
    conn.execute(
        f"""
        INSERT INTO matchup_season ({", ".join(cols)}, updated_at)
        VALUES ({placeholders}, datetime('now'))
        ON CONFLICT(batter_id, pitcher_id, season) DO UPDATE SET
            {update_clause}, updated_at = datetime('now')
        """,
        [row.get(c) for c in cols],
    )
