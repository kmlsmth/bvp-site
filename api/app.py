"""
Minimal backend API for the batter-vs-pitcher tool.

Built on Flask (already available, no extra install needed) + the plain
sqlite3 database the ingestion job fills in. Three endpoints:

  GET /api/games?date=YYYY-MM-DD
      Today's (or any date's) games with probable starting pitchers.

  GET /api/matchup?batter=<id>&pitcher=<id>
      Career totals + season-by-season history for one batter vs one
      pitcher.

  GET /api/players?q=<search text>
      Name search, so a front end can let someone type "Judge" and get
      back matching players with their ids.

In production, this process also runs a background thread (see
scheduler.py) that keeps the database topped up daily -- there is no
separate cron service, since Railway can't share one disk between two
services. Set SKIP_SCHEDULER=1 to disable that thread (handy for local
dev when you'd rather run scripts/ingest_daily.py by hand).

Run locally:  python3 api/app.py   (serves on http://localhost:8000)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, make_response, request, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
# Make both scripts/ and this file's own directory importable regardless of
# whether this module is run directly (python3 api/app.py) or imported as
# api.app (production, via wsgi.py) -- in the latter case Python does not
# automatically add api/ itself to sys.path the way it does for a script.
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bullpen as bullpen_module  # noqa: E402
import db as db_module  # noqa: E402  (shares BVP_DATA_DIR / DB_PATH logic)
import init_db as init_db_module  # noqa: E402
import matchup_estimate  # noqa: E402
import model_constants  # noqa: E402
import mlb_api  # noqa: E402
import pages  # noqa: E402
import parsing  # noqa: E402
import pitch_mix  # noqa: E402
import savant_api  # noqa: E402
import stats as stats_module  # noqa: E402

BULLPEN_WINDOW_DAYS = 7  # trailing window for fatigue + recent appearances

DB_PATH = db_module.DB_PATH
WEB_DIR = ROOT / "web"

# Idempotent: CREATE TABLE IF NOT EXISTS, so this is safe to run on every
# boot. Makes sure the database (and its parent dir, e.g. a fresh Railway
# volume) exists before anything tries to query it.
init_db_module.init_db(DB_PATH)

app = Flask(__name__)

def _start_scheduler() -> None:
    import scheduler
    import archive as archive_module
    scheduler.start(
        on_tick=lambda today: snapshot_pregame_projections(today),
        on_hourly=lambda today: archive_module.archive_recent(today, days_back=1),
    )


@app.get("/")
def index():
    """The whole front end is one self-contained page (web/index.html) --
    it does its own routing client-side (see the hash-based router in that
    file), so this is the only page route the server needs.

    Explicit no-cache: send_from_directory's default headers leave the
    browser free to reuse an old cached copy of this page without even
    asking the server (no Cache-Control, just Last-Modified, is enough for
    most browsers to serve straight from disk for a while). That's exactly
    how a deploy can "not show up" after a reload -- the browser never
    re-requested it. no-cache (not no-store) still lets the browser keep a
    local copy, it just has to revalidate with the server first every
    time, so an unchanged page is still a cheap 304 and a changed one is
    never missed."""
    response = make_response(send_from_directory(WEB_DIR, "index.html"))
    response.headers["Cache-Control"] = "no-cache"
    return response


def query_db(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _window(as_of_date: str) -> tuple[str, str]:
    """The trailing BULLPEN_WINDOW_DAYS before as_of_date (both inclusive,
    never including as_of_date itself -- matches ingest_appearances.py,
    which only ever backfills games strictly before the ingestion date)."""
    as_of = date.fromisoformat(as_of_date)
    start = (as_of - timedelta(days=BULLPEN_WINDOW_DAYS)).isoformat()
    end = (as_of - timedelta(days=1)).isoformat()
    return start, end


def _league_avg_weekly_reliever_pitches(as_of_date: str) -> float:
    """The 30-team average weekly bullpen pitch count, so one team's
    workload is judged against its real peers rather than a guessed fixed
    number. Early in a season (or in this site's early days, before much
    history has been ingested) this naturally has fewer teams to average
    over -- compute_bullpen_fatigue() falls back to treating "no data yet"
    as average rather than crashing or returning a misleading score."""
    start, end = _window(as_of_date)
    rows = query_db(
        """
        SELECT team_id, SUM(pitches) AS total
        FROM pitcher_appearances
        WHERE role = 'reliever' AND game_date BETWEEN ? AND ?
        GROUP BY team_id
        """,
        (start, end),
    )
    totals = [r["total"] for r in rows if r["total"]]
    return sum(totals) / len(totals) if totals else 0.0


def _team_bullpen_rows(team_id: int, as_of_date: str) -> list[dict]:
    start, end = _window(as_of_date)
    return query_db(
        """
        SELECT pa.pitcher_id, p.full_name, pa.game_date, pa.pitches, pa.outs
        FROM pitcher_appearances pa
        JOIN players p ON p.id = pa.pitcher_id
        WHERE pa.role = 'reliever' AND pa.team_id = ? AND pa.game_date BETWEEN ? AND ?
        ORDER BY pa.game_date DESC
        """,
        (team_id, start, end),
    )


def _team_fatigue(team_id: int, as_of_date: str, league_avg: float) -> dict:
    rows = _team_bullpen_rows(team_id, as_of_date)
    return bullpen_module.compute_bullpen_fatigue(rows, as_of_date, league_avg)


# --- Live MLB lookups made while serving a page ------------------------
# The starter overview card and the bullpen dropdown read a few things
# straight from MLB when a page asks for them (rather than in the nightly
# job), cached in memory so a busy game page costs MLB a handful of calls
# an hour, not one per visitor. One gunicorn worker (see Procfile), so one
# shared cache; a restart just starts it empty again.
STATS_CACHE_SECONDS = 60 * 60
ROSTER_CACHE_SECONDS = 60 * 60
BVP_FETCH_CACHE_SECONDS = 6 * 60 * 60

_cache: dict = {}
_cache_lock = threading.Lock()
_bvp_fetch_lock = threading.Lock()


def _cached(key, ttl_seconds: int, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl_seconds:
            return hit[1]
    value = fn()  # outside the lock: network calls shouldn't block other lookups
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _team_roster(team_id: int) -> list[dict]:
    return _cached(("roster", team_id), ROSTER_CACHE_SECONDS,
                   lambda: parsing.parse_roster(mlb_api.get_team_roster(team_id)))


def _person(person_id: int) -> dict:
    return _cached(("person", person_id), STATS_CACHE_SECONDS,
                   lambda: parsing.parse_person_throws(mlb_api.get_person(person_id)))


def _season_from(request_date: str | None) -> int:
    try:
        return date.fromisoformat(request_date).year if request_date else date.today().year
    except ValueError:
        return date.today().year


SAVANT_CACHE_SECONDS = 12 * 60 * 60  # Statcast leaderboards update overnight


def _pitcher_gamelog(pitcher_id: int, season: int) -> list[dict]:
    """Regular season + postseason game-by-game lines (cached)."""
    return _cached(("gamelog", pitcher_id, season), STATS_CACHE_SECONDS,
                   lambda: parsing.parse_pitching_gamelog(
                       mlb_api.get_pitching_stats(pitcher_id, season, "gameLog",
                                                  mlb_api.REGULAR_AND_POSTSEASON)))


PAST_SEASON_CACHE_SECONDS = 7 * 24 * 60 * 60  # finished seasons don't change


def _savant_board(kind: str, season: int, current_season: int | None = None) -> dict:
    """Savant pitch-arsenal leaderboard, every batter or every pitcher.
    Past seasons (season < current_season) are kept for a week."""
    ttl = PAST_SEASON_CACHE_SECONDS if current_season and season < current_season else SAVANT_CACHE_SECONDS
    return _cached(("savant-board", kind, season), ttl,
                   lambda: pitch_mix.parse_arsenal_csv(savant_api.get_arsenal_leaderboard(kind, season)))


def _pitcher_usage_by_date(pitcher_id: int, season: int) -> dict:
    """{game_date: {"L": {type: count}, "R": {...}}} -- his pitch mix by
    batter side, outing by outing (regular season + postseason)."""
    return _cached(("savant-usage", pitcher_id, season), SAVANT_CACHE_SECONDS,
                   lambda: pitch_mix.usage_by_date(savant_api.get_pitcher_pitches(pitcher_id, season)))


def _pitcher_usage(pitcher_id: int, season: int) -> dict:
    """{"L": {type: count}, "R": {...}} -- his season pitch mix by batter side."""
    return pitch_mix.total_usage(_pitcher_usage_by_date(pitcher_id, season))



def _warm_statcast_loop() -> None:
    """Background: keep today's starters' Statcast downloads cached, so the
    first visitor to a game page doesn't wait on Baseball Savant (the
    pitch-by-pitch file is ~2 MB and takes a few seconds). Every 30 min;
    already-cached entries cost nothing."""
    import ingest_daily
    while True:
        try:
            today = ingest_daily.baseball_today()
            season = int(today[:4])
            _savant_board("batter", season)
            _savant_board("pitcher", season)
            for past in (season - 1, season - 2):
                _savant_board("batter", past, season)
            for g in query_db("SELECT home_probable_pitcher_id AS h, away_probable_pitcher_id AS a "
                              "FROM games WHERE game_date = ?", (today,)):
                for pid in (g["h"], g["a"]):
                    if pid:
                        _pitcher_usage(pid, season)
                        _pitcher_gamelog(pid, season)
        except Exception as exc:
            print(f"[statcast warm] {exc}")
        time.sleep(30 * 60)


if not os.environ.get("SKIP_SCHEDULER"):
    threading.Thread(target=_warm_statcast_loop, name="statcast-warm", daemon=True).start()

@app.get("/api/games")
def games():
    game_date = request.args.get("date")
    if not game_date:
        return jsonify({"error": "date query param is required, e.g. ?date=2026-10-02"}), 400

    rows = query_db(
        """
        SELECT g.game_pk, g.game_date, g.game_date_time, g.game_type, g.status,
               g.home_team_id, ht.name AS home_team,
               g.away_team_id, at.name AS away_team,
               hp.full_name AS home_probable_pitcher, g.home_probable_pitcher_id,
               ap.full_name AS away_probable_pitcher, g.away_probable_pitcher_id,
               g.home_lineup, g.away_lineup,
               v.id AS venue_id, v.name AS venue_name, v.azimuth_angle AS venue_azimuth_angle,
               v.roof_type AS venue_roof_type, v.hr_factor AS venue_hr_factor,
               v.hit_factor AS venue_hit_factor,
               gw.wind_speed_mph, gw.wind_dir_deg, gw.wind_dir_compass,
               gw.temp_f, gw.sky, gw.forecast_time, gw.fetched_at AS weather_fetched_at
        FROM games g
        LEFT JOIN teams ht ON ht.id = g.home_team_id
        LEFT JOIN teams at ON at.id = g.away_team_id
        LEFT JOIN players hp ON hp.id = g.home_probable_pitcher_id
        LEFT JOIN players ap ON ap.id = g.away_probable_pitcher_id
        LEFT JOIN venues v ON v.id = g.venue_id
        LEFT JOIN game_weather gw ON gw.game_pk = g.game_pk
        WHERE g.game_date = ?
        ORDER BY g.game_pk
        """,
        (game_date,),
    )

    # home_lineup/away_lineup are stored as JSON text (see db.upsert_game);
    # decode to real arrays here so the front end gets actual lineup data,
    # not a string to parse itself. NULL (not announced yet) becomes [].
    for r in rows:
        r["home_lineup"] = json.loads(r["home_lineup"]) if r["home_lineup"] else []
        r["away_lineup"] = json.loads(r["away_lineup"]) if r["away_lineup"] else []

    # Bullpen fatigue, one grade per team -- computed once per unique team
    # on the slate (not once per game row) and cached for this request,
    # since the league-average baseline and each team's score are the same
    # regardless of which game they're attached to.
    team_ids = {r["home_team_id"] for r in rows} | {r["away_team_id"] for r in rows}
    team_ids.discard(None)
    if team_ids:
        league_avg = _league_avg_weekly_reliever_pitches(game_date)
        fatigue_by_team = {tid: _team_fatigue(tid, game_date, league_avg) for tid in team_ids}
        for r in rows:
            r["home_bullpen_fatigue"] = fatigue_by_team.get(r["home_team_id"])
            r["away_bullpen_fatigue"] = fatigue_by_team.get(r["away_team_id"])

    return jsonify(rows)


@app.get("/api/matchup")
def matchup():
    batter_id = request.args.get("batter", type=int)
    pitcher_id = request.args.get("pitcher", type=int)
    if not batter_id or not pitcher_id:
        return jsonify({"error": "batter and pitcher query params (player ids) are required"}), 400

    career = query_db(
        """
        SELECT mc.*, b.full_name AS batter_name, p.full_name AS pitcher_name
        FROM matchup_career mc
        JOIN players b ON b.id = mc.batter_id
        JOIN players p ON p.id = mc.pitcher_id
        WHERE mc.batter_id = ? AND mc.pitcher_id = ?
        """,
        (batter_id, pitcher_id),
    )
    seasons = query_db(
        """
        SELECT ms.*, ot.name AS opponent_name
        FROM matchup_season ms
        LEFT JOIN teams ot ON ot.id = ms.opponent_id
        WHERE ms.batter_id = ? AND ms.pitcher_id = ?
        ORDER BY ms.season
        """,
        (batter_id, pitcher_id),
    )

    if not career:
        return jsonify({"career": None, "seasons": [], "message": "No recorded history between these two players."})

    return jsonify({"career": career[0], "seasons": seasons})


@app.get("/api/lineup")
def lineup():
    """Every batter on one team's roster against one probable pitcher, in
    a single call -- what the front end's full stat-line table needs.

    Fetching this one batter/pitcher pair at a time (like /api/matchup
    above) would mean ~13 separate calls per lineup table; this does it
    in one, which is what makes the pitcher-switcher dropdown feel
    instant instead of spinning for a few seconds on every switch.
    """
    pitcher_id = request.args.get("pitcher", type=int)
    opponent_team_id = request.args.get("opponent_team", type=int)
    if not pitcher_id or not opponent_team_id:
        return jsonify({"error": "pitcher and opponent_team query params (ids) are required"}), 400

    # Relievers: the nightly job only pulls matchup history for each
    # game's two probable starters (pulling every bullpen arm against every
    # hitter nightly would be ~10x the MLB calls). So the first time anyone
    # picks a reliever, pull his history against this lineup right now.
    fetch_warning = None
    if request.args.get("fetch") == "1":
        try:
            _fetch_bvp_on_demand(pitcher_id, opponent_team_id,
                                 request.args.get("pitcher_team", type=int))
        except Exception as exc:  # show whatever's already stored rather than failing
            print(f"[lineup fetch] pitcher={pitcher_id} team={opponent_team_id}: {exc}")
            fetch_warning = "Couldn't reach MLB just now -- showing whatever history is already stored."

    pitcher_rows = query_db("SELECT full_name FROM players WHERE id = ?", (pitcher_id,))
    team_rows = query_db("SELECT name FROM teams WHERE id = ?", (opponent_team_id,))
    pitcher_name = pitcher_rows[0]["full_name"] if pitcher_rows else None
    team_name = team_rows[0]["name"] if team_rows else None

    # Last 10 starts is comfortably more than the 5 pitcher_recent_form()
    # actually uses -- pulled this way (not LIMIT 5) so a pitcher who
    # skipped a turn in the rotation still gets 5 *starts*, not 5 rows
    # that happen to include a gap.
    starter_rows = query_db(
        "SELECT game_date, outs, pitches, batters_faced, earned_runs, "
        "base_on_balls, strike_outs, hits "
        "FROM pitcher_appearances WHERE pitcher_id = ? AND role = 'starter' "
        "ORDER BY game_date DESC LIMIT 10",
        (pitcher_id,),
    )
    recent_form = bullpen_module.pitcher_recent_form(starter_rows, n=5)

    roster = query_db(
        """
        SELECT id, full_name FROM players
        WHERE team_id = ? AND role IN ('batter', 'both')
        ORDER BY full_name
        """,
        (opponent_team_id,),
    )

    careers = {
        row["batter_id"]: row
        for row in query_db("SELECT * FROM matchup_career WHERE pitcher_id = ?", (pitcher_id,))
    }
    season_rows = query_db(
        """
        SELECT ms.*, ot.name AS opponent_name
        FROM matchup_season ms
        LEFT JOIN teams ot ON ot.id = ms.opponent_id
        WHERE ms.pitcher_id = ?
        ORDER BY ms.season
        """,
        (pitcher_id,),
    )
    seasons_by_batter: dict[int, list[dict]] = {}
    for s in season_rows:
        seasons_by_batter.setdefault(s["batter_id"], []).append(s)

    with_history = []
    no_history = []
    for batter in roster:
        career = careers.get(batter["id"])
        if career is None:
            no_history.append({"id": batter["id"], "name": batter["full_name"]})
            continue
        seasons = [
            {
                "season": s["season"],
                "opponent_name": s["opponent_name"],
                **stats_module.compute_batting_stats(s),
            }
            for s in seasons_by_batter.get(batter["id"], [])
        ]
        with_history.append({
            "id": batter["id"],
            "name": batter["full_name"],
            "stats": stats_module.compute_batting_stats(career),
            "seasons": seasons,
        })

    with_history.sort(key=lambda b: b["stats"]["pa"] or 0, reverse=True)

    team_totals_raw = stats_module.sum_raw_counts(
        [careers[b["id"]] for b in roster if b["id"] in careers]
    )
    team_totals = stats_module.compute_batting_stats(team_totals_raw)

    return jsonify({
        "pitcher": {"id": pitcher_id, "name": pitcher_name, "recent_form": recent_form},
        "opponent_team": {"id": opponent_team_id, "name": team_name},
        "rows": with_history,
        "no_history": no_history,
        "team_totals": team_totals,
        "fetch_warning": fetch_warning,
    })


def _fetch_bvp_on_demand(pitcher_id: int, opponent_team_id: int,
                         pitcher_team_id: int | None) -> None:
    """Pull one pitcher's history against every position player on one
    team's active roster, and store it exactly the way the nightly job
    does (same parsing, same tables). Done once per pitcher/team per
    BVP_FETCH_CACHE_SECONDS; MLB calls run a few at a time in parallel so
    the first view of a reliever takes a couple of seconds, not ~10."""
    key = ("bvp-fetched", pitcher_id, opponent_team_id)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < BVP_FETCH_CACHE_SECONDS:
            return

    with _bvp_fetch_lock:  # two visitors picking the same reliever at once fetch it once
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < BVP_FETCH_CACHE_SECONDS:
                return

        batters = [p for p in _team_roster(opponent_team_id)
                   if p["position_type"] != "Pitcher" and p["id"]]
        pitcher_name = _person(pitcher_id).get("full_name") or "Unknown"

        def pull(batter_id):
            try:
                return mlb_api.get_vs_player(batter_id, pitcher_id)
            except Exception as exc:
                print(f"    skip batter={batter_id} pitcher={pitcher_id}: {exc}")
                return None

        with ThreadPoolExecutor(max_workers=5) as pool:
            raws = list(pool.map(pull, [b["id"] for b in batters]))

        conn = db_module.connect(DB_PATH)
        try:
            # Same write order as ingest_daily.py: players and teams a row
            # points at must exist before the row itself (foreign keys).
            db_module.upsert_player(conn, pitcher_id, pitcher_name,
                                    role="pitcher", team_id=pitcher_team_id)
            for b in batters:
                db_module.upsert_player(conn, b["id"], b["full_name"],
                                        role="batter", team_id=opponent_team_id)
            for raw in raws:
                if raw is None:
                    continue
                career, seasons = parsing.parse_vs_player(raw)
                if career is None:
                    continue  # never faced each other
                db_module.upsert_matchup_career(conn, career)
                for s in seasons:
                    if s.get("team_id") is not None:
                        db_module.upsert_team(conn, s["team_id"], s.get("team_name") or "Unknown")
                    if s.get("opponent_id") is not None:
                        db_module.upsert_team(conn, s["opponent_id"], s.get("opponent_name") or "Unknown")
                    db_module.upsert_matchup_season(conn, s)
            conn.commit()
        finally:
            conn.close()

        # Only remember it as done if every call worked -- otherwise the
        # next visitor retries the whole thing.
        if all(r is not None for r in raws):
            with _cache_lock:
                _cache[key] = (time.time(), True)


@app.get("/api/pitcher")
def pitcher_overview_route():
    """The starter overview card: throwing hand, this regular season's
    ERA/WHIP/K9/innings, last 5 starts (regular season + postseason)
    ERA/WHIP/K9 + innings per start, and career ERA. Straight from MLB's
    own season/career/game-log stats, cached for an hour."""
    pitcher_id = request.args.get("id", type=int)
    if not pitcher_id:
        return jsonify({"error": "id query param (pitcher id) is required"}), 400
    game_date = request.args.get("date")
    season = _season_from(game_date)

    def build():
        person = parsing.parse_person_throws(mlb_api.get_person(pitcher_id))
        totals = parsing.parse_pitching_totals(
            mlb_api.get_pitching_stats(pitcher_id, season, "season,career",
                                       mlb_api.REGULAR_SEASON))
        gamelog = _pitcher_gamelog(pitcher_id, season)
        return {
            "id": pitcher_id,
            "name": person["full_name"],
            "throws": person["throws"],
            "season_year": season,
            **bullpen_module.pitcher_overview(totals, gamelog, n=5, before_date=game_date),
        }

    try:
        return jsonify(_cached(("pitcher", pitcher_id, game_date), STATS_CACHE_SECONDS, build))
    except Exception as exc:
        print(f"[pitcher overview] {pitcher_id}: {exc}")
        return jsonify({"error": "Couldn't load this pitcher's stats from MLB right now."}), 502


@app.get("/api/staff")
def staff_route():
    """Every pitcher on a team's active roster, busiest first (appearances,
    then pitches, over the same trailing week Bullpen Watch uses) -- the
    pitcher dropdown's bullpen list. Throwing hand isn't included (it'd be
    one MLB call per pitcher); the matchup table doesn't depend on it."""
    team_id = request.args.get("team", type=int)
    game_date = request.args.get("date")
    if not team_id or not game_date:
        return jsonify({"error": "team and date query params are required"}), 400

    try:
        roster = [p for p in _team_roster(team_id) if p["position_type"] == "Pitcher" and p["id"]]
    except Exception as exc:
        print(f"[staff] team={team_id}: {exc}")
        return jsonify({"error": "Couldn't load this team's roster from MLB right now."}), 502

    start, end = _window(game_date)
    usage = {
        r["pitcher_id"]: r
        for r in query_db(
            """
            SELECT pitcher_id, COUNT(*) AS appearances, SUM(pitches) AS pitches,
                   MAX(game_date) AS last_appearance
            FROM pitcher_appearances
            WHERE team_id = ? AND game_date BETWEEN ? AND ?
            GROUP BY pitcher_id
            """,
            (team_id, start, end),
        )
    }
    pitchers = []
    for p in roster:
        u = usage.get(p["id"]) or {}
        pitchers.append({
            "id": p["id"],
            "name": p["full_name"],
            "appearances": u.get("appearances") or 0,
            "pitches": u.get("pitches") or 0,
            "last_appearance": u.get("last_appearance"),
        })
    pitchers.sort(key=lambda x: (-x["appearances"], -x["pitches"], x["name"] or ""))
    return jsonify({"team_id": team_id, "window_days": BULLPEN_WINDOW_DAYS, "pitchers": pitchers})


@app.get("/api/bullpen")
def bullpen_route():
    """One team's bullpen fatigue grade plus a per-reliever breakdown over
    the trailing window -- what the game page's "Bullpen Watch" card needs.
    Kept as its own call (rather than folded into /api/lineup) since the
    homepage's schedule badges need the fatigue grade without the rest of
    this payload, via /api/games -- this endpoint is only for the drill-in.
    """
    team_id = request.args.get("team", type=int)
    game_date = request.args.get("date")
    if not team_id or not game_date:
        return jsonify({"error": "team and date query params are required, e.g. ?team=147&date=2026-10-02"}), 400

    rows = _team_bullpen_rows(team_id, game_date)
    league_avg = _league_avg_weekly_reliever_pitches(game_date)
    fatigue = bullpen_module.compute_bullpen_fatigue(rows, game_date, league_avg)

    by_pitcher: dict[int, dict] = {}
    for r in rows:
        pid = r["pitcher_id"]
        entry = by_pitcher.setdefault(pid, {
            "id": pid, "name": r["full_name"], "appearances": 0,
            "pitches": 0, "last_appearance": None,
        })
        entry["appearances"] += 1
        entry["pitches"] += r["pitches"] or 0
        if entry["last_appearance"] is None or r["game_date"] > entry["last_appearance"]:
            entry["last_appearance"] = r["game_date"]
    relievers = sorted(by_pitcher.values(), key=lambda x: x["pitches"], reverse=True)

    team_rows = query_db("SELECT name FROM teams WHERE id = ?", (team_id,))
    team_name = team_rows[0]["name"] if team_rows else None

    return jsonify({
        "team": {"id": team_id, "name": team_name},
        "fatigue": fatigue,
        "relievers": relievers,
        "window_days": BULLPEN_WINDOW_DAYS,
    })



LEAGUE_CACHE_SECONDS = 6 * 60 * 60


def _fmt_rate3(x: float | None) -> str | None:
    """.312-style (OBP)."""
    return None if x is None else f"{x:.3f}".lstrip("0")


def _fmt_pct(x: float | None, digits: int = 1) -> str | None:
    return None if x is None else f"{x * 100:.{digits}f}%"


def _int_or_zero(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _hitters_vs(ids: tuple, season: int, current_season: int) -> dict:
    """Each hitter's regular-season lines vs left- AND right-handed
    pitching in `season`, one MLB call: {"vl": {id: ...}, "vr": {id: ...}}."""
    ttl = PAST_SEASON_CACHE_SECONDS if season < current_season else STATS_CACHE_SECONDS

    def load():
        raw = mlb_api.get_hitters_vs_hand(list(ids), season, "vl,vr")
        return {code: parsing.parse_hitters_vs_hand(raw, code) for code in ("vl", "vr")}
    return _cached(("hitters-vs-both", ids, season), ttl, load)


def _pitcher_vs(pitcher_id: int, season: int, current_season: int) -> dict:
    """A pitcher's regular-season lines vs left- and right-handed hitters."""
    ttl = PAST_SEASON_CACHE_SECONDS if season < current_season else STATS_CACHE_SECONDS
    return _cached(("pitcher-vs", pitcher_id, season), ttl,
                   lambda: parsing.parse_pitcher_vs_hand(mlb_api.get_pitcher_vs_hand(pitcher_id, season)))


@app.get("/api/estimates")
def estimates_route():
    """Every position player on the opposing roster, estimated against THIS
    pitcher: AVG / OBP / SLG / OPS, and his chance of at least one home run
    tonight -- next to his real 2026 regular-season line vs this pitcher's
    hand. Covers every hitter, including the many with no head-to-head
    history at all.

    Layers (see matchup_estimate.py and pitch_mix.py):
      1. talent from ALL plate appearances (hitter vs both hands, pitcher vs
         both sides; this season and the two before it, weighted 5/4/3),
         regressed toward league, then adjusted to this matchup's platoon
         edge; hitter x pitcher relative to league (odds ratio);
      2. nudged by pitch mix: how this hitter handles each pitch type
         (Statcast expected stats, same 5/4/3 seasons), weighted by how
         often this pitcher throws each one to hitters from that side --
         his last 5 outings blended with his season;
      3. any head-to-head at-bats added on top, older seasons faded;
      4. 1+ hit / 1+ HR: averaged over the real spread of plate appearances
         starters get in his lineup spot (home/away), split between this
         pitcher (by how deep he usually goes) and the bullpen.
    Layers 1 and 4 were backtested on every 2026 game (scripts/backtest.py).
    Query: pitcher, opponent_team, date, optional game (game_pk, for the
    posted lineup) and role=reliever (shorter outings)."""
    pitcher_id = request.args.get("pitcher", type=int)
    opponent_team_id = request.args.get("opponent_team", type=int)
    if not pitcher_id or not opponent_team_id:
        return jsonify({"error": "pitcher and opponent_team query params (ids) are required"}), 400
    game_date = request.args.get("date")
    game_pk = request.args.get("game", type=int)
    is_reliever = request.args.get("role") == "reliever"
    try:
        return jsonify(compute_estimates(pitcher_id, opponent_team_id, game_date, game_pk, is_reliever))
    except EstimatesError as exc:
        return jsonify({"error": str(exc)}), 502


class EstimatesError(Exception):
    """MLB couldn't supply what the estimate needs (message is user-facing)."""


MODEL_VERSION = "v3"


def compute_estimates(pitcher_id: int, opponent_team_id: int, game_date: str | None,
                      game_pk: int | None = None, is_reliever: bool = False) -> dict:
    """Everything /api/estimates returns, as a dict (also used by the
    pre-game snapshot that saves projections to the archive)."""
    season = _season_from(game_date)

    try:
        throws = _person(pitcher_id).get("throws")
        if throws not in ("L", "R"):
            raise EstimatesError("MLB doesn't list this pitcher's throwing hand.")
        sit_code = "vr" if throws == "R" else "vl"

        batters = [b for b in _team_roster(opponent_team_id)
                   if b["position_type"] != "Pitcher" and b["id"]]
        ids = tuple(sorted(b["id"] for b in batters))
        hitters_both = _hitters_vs(ids, season, season)
        hitters = hitters_both[sit_code]
        pitcher_split = _pitcher_vs(pitcher_id, season, season)
        league = _cached(("league", season), LEAGUE_CACHE_SECONDS,
                         lambda: matchup_estimate.league_rates(
                             parsing.parse_team_hitting_lines(mlb_api.get_league_team_hitting(season))))
        gamelog = _pitcher_gamelog(pitcher_id, season)
    except EstimatesError:
        raise
    except Exception as exc:
        print(f"[estimates] pitcher={pitcher_id} team={opponent_team_id}: {exc}")
        raise EstimatesError("Couldn't load season splits from MLB right now.")

    # The two seasons before this one, weighted 4 and 3 against this
    # season's 5 (matchup_estimate.SEASON_WEIGHTS). Optional: if MLB doesn't
    # answer, the projection runs on this season alone.
    past_seasons = (season - 1, season - 2)
    past_hitters, past_pitcher = [], []
    for past in past_seasons:
        try:
            past_hitters.append(_hitters_vs(ids, past, season))
            past_pitcher.append(_pitcher_vs(pitcher_id, past, season))
        except Exception as exc:
            print(f"[estimates] {past} splits unavailable for pitcher={pitcher_id}: {exc}")
            past_hitters.append({"vl": {}, "vr": {}})
            past_pitcher.append({})

    # Pitch-type layer: optional -- if Baseball Savant can't be reached the
    # estimates still work, just without the pitch-mix nudge.
    try:
        batter_board = _savant_board("batter", season)
        pitcher_board = _savant_board("pitcher", season).get(pitcher_id) or {}
        usage_by_date = _pitcher_usage_by_date(pitcher_id, season)
        usage = pitch_mix.total_usage(usage_by_date, game_date)
        mix_available = bool(usage.get("L") or usage.get("R"))
    except Exception as exc:
        print(f"[estimates] Statcast unavailable for pitcher={pitcher_id}: {exc}")
        batter_board, pitcher_board, usage_by_date, usage, mix_available = {}, {}, {}, {}, False
    past_boards = []
    if mix_available:
        for past in past_seasons:
            try:
                past_boards.append(_savant_board("batter", past, season))
            except Exception as exc:
                print(f"[estimates] Statcast {past} hitter board unavailable: {exc}")
                past_boards.append({})

    # How much of the game he'll pitch: average batters faced over recent
    # outings (last 5 starts, or last 10 relief appearances) before today,
    # as a share of a team's plate appearances in a game.
    outings = sorted((g for g in gamelog
                      if g.get("game_date") and (not game_date or g["game_date"] < game_date)
                      and bool(g.get("started")) != is_reliever),
                     key=lambda g: g["game_date"], reverse=True)[:10 if is_reliever else 5]
    bf = [g["batters_faced"] for g in outings if g.get("batters_faced")]
    avg_bf = sum(bf) / len(bf) if bf else (4.5 if is_reliever else 22.0)
    share_vs_pitcher = min(1.0, avg_bf / league["pa_per_team_game"])

    # Stored head-to-head totals vs this pitcher, by hitter: career (shown,
    # and decides who gets a projection) and season by season (what goes
    # into the projection, older seasons faded).
    h2h_by_id = {r["batter_id"]: r for r in query_db(
        "SELECT * FROM matchup_career WHERE pitcher_id = ?", (pitcher_id,))}
    h2h_seasons_by_id: dict = {}
    for r in query_db("SELECT * FROM matchup_season WHERE pitcher_id = ?", (pitcher_id,)):
        h2h_seasons_by_id.setdefault(r["batter_id"], []).append(r)

    spot_by_id = {}
    batting_home = None   # is the team we're projecting the home team? (None = unknown)
    if game_pk:
        g = query_db("SELECT home_team_id, home_lineup, away_lineup FROM games WHERE game_pk = ?", (game_pk,))
        if g:
            batting_home = g[0]["home_team_id"] == opponent_team_id
            raw = g[0]["home_lineup"] if g[0]["home_team_id"] == opponent_team_id else g[0]["away_lineup"]
            for p in (json.loads(raw) if raw else []):
                if p.get("id"):
                    spot_by_id[p["id"]] = p.get("order")

    rows = []
    for b in batters:
        h = hitters.get(b["id"]) or {}
        bats = h.get("bats") or next((ph[code][b["id"]].get("bats") for ph in past_hitters for code in ("vl", "vr")
                                      if (ph[code].get(b["id"]) or {}).get("bats")), None)
        side = matchup_estimate.facing_side(bats, throws)
        line = h.get("stat")
        # Talent from ALL his plate appearances (both hands) and all the
        # pitcher's batters faced, this season + the two before (5/4/3),
        # then adjusted to this matchup's platoon edge (see
        # matchup_estimate.talent_rates -- backtested on every 2026 game).
        seasons = [hitters_both] + past_hitters
        pitchers = [pitcher_split] + past_pitcher

        def hit_line(d, code):
            return matchup_estimate.hitting_counts(((d.get(code) or {}).get(b["id"]) or {}).get("stat"))
        h_all = [matchup_estimate.add_counts(hit_line(d, "vl"), hit_line(d, "vr")) for d in seasons]
        h_hand = [hit_line(d, sit_code) for d in seasons]
        p_all = [matchup_estimate.add_counts(matchup_estimate.pitching_counts(d.get("L")),
                                             matchup_estimate.pitching_counts(d.get("R"))) for d in pitchers]
        p_side = [matchup_estimate.pitching_counts(d.get(side)) if side else None for d in pitchers]
        ones = {r: 1.0 for r in matchup_estimate.RATES + matchup_estimate.AB_RATES}
        platoon = (model_constants.PLATOON_FACTOR["same" if side == throws else "opp"] if side else ones)
        hitter_platoon = ones if bats == "S" else platoon   # switch hitters always have the platoon edge
        b_rates = matchup_estimate.talent_rates(h_all, h_hand, league, hitter_platoon,
                                                matchup_estimate.HITTER_STABILIZE)
        p_rates = matchup_estimate.talent_rates(p_all, p_side if side else p_all, league, platoon,
                                                matchup_estimate.PITCHER_STABILIZE)
        base = matchup_estimate.combine_rates(b_rates, p_rates, league)

        # His recent pitch mix (last 5 outings) blended with his season mix.
        side_usage = pitch_mix.blended_shares(usage_by_date, side, game_date) if side and usage_by_date else {}
        hitter_rows = pitch_mix.combine_hitter_rows(
            [(batter_board.get(b["id"]) or {}, matchup_estimate.SEASON_WEIGHTS[0])] +
            [(pb.get(b["id"]) or {}, w) for pb, w in zip(past_boards, matchup_estimate.SEASON_WEIGHTS[1:])])
        mix = pitch_mix.mix_matchup(hitter_rows, side_usage)
        est = matchup_estimate.apply_mix(base, mix)
        # Any head-to-head at-bats he does have count as real evidence on
        # top, older seasons counting for less.
        h2h = matchup_estimate.h2h_counts(h2h_by_id.get(b["id"]))
        h2h_weighted = (matchup_estimate.h2h_faded(h2h_seasons_by_id.get(b["id"]), season)
                        if b["id"] in h2h_seasons_by_id else h2h)
        est = matchup_estimate.apply_h2h(est, h2h_weighted)

        # Rest of the game vs the bullpen: him against a league-average
        # pitcher of this matchup type.
        vs_pen = matchup_estimate.combine_rates(
            b_rates, {r: league[r] * hitter_platoon[r] for r in hitter_platoon}, league)
        spot = spot_by_id.get(b["id"])
        # The real spread of plate appearances starters get in this lineup
        # spot, home or away (model_constants.STARTER_PA_GAMES).
        dist = matchup_estimate.pa_distribution(spot, batting_home)
        pa_exp = sum(n * w for n, w in dist)
        hr_tonight = matchup_estimate.chance_over_pa_distribution(est["hr"], vs_pen["hr"], dist, share_vs_pitcher)
        hit_tonight = matchup_estimate.chance_over_pa_distribution(
            matchup_estimate.hits_per_pa(est, league), matchup_estimate.hits_per_pa(vs_pen, league),
            dist, share_vs_pitcher)

        breakdown = []
        for pt, sh in sorted(side_usage.items(), key=lambda kv: -kv[1]):
            if sh < 0.015:
                continue
            hr_row, pr_row = hitter_rows.get(pt) or {}, pitcher_board.get(pt) or {}
            breakdown.append({
                "type": pt, "name": pitch_mix.PITCH_NAMES[pt], "usage": round(sh * 100),
                "hitter_xwoba": _fmt_rate3(hr_row.get("xwoba")), "hitter_pa": round(hr_row.get("pa", 0)),
                "pitcher_xwoba": _fmt_rate3(pr_row.get("xwoba")),
            })

        ops = est["ob"] + est["slg"]
        pa = _int_or_zero((line or {}).get("plateAppearances"))
        rows.append({
            "id": b["id"],
            "name": h.get("name") or b["full_name"],
            "bats": bats,
            "lineup_spot": spot,
            # Career head-to-head vs this pitcher (the front end projects
            # hitters with fewer than 5 AB and shows this next to the name).
            "h2h": {"ab": h2h["ab"], "pa": h2h["pa"], "h": h2h["avg"], "hr": h2h["hr"]} if h2h
                   else {"ab": 0, "pa": 0, "h": 0, "hr": 0},
            "vs_hand": {
                "pa": pa,
                "avg": (line or {}).get("avg"), "obp": (line or {}).get("obp"),
                "slg": (line or {}).get("slg"), "ops": (line or {}).get("ops"),
                "hr": (line or {}).get("homeRuns"),
            },
            "est": {
                "avg": _fmt_rate3(est["avg"]), "obp": _fmt_rate3(est["ob"]),
                "slg": _fmt_rate3(est["slg"]), "ops": _fmt_rate3(ops), "ops_num": ops,
                "hr_tonight": _fmt_pct(hr_tonight, 0), "hr_tonight_num": hr_tonight,
                "hit_tonight": _fmt_pct(hit_tonight, 0), "hit_tonight_num": hit_tonight,
                "pa_expected": round(pa_exp, 1),
                "avg_num": est["avg"], "obp_num": est["ob"], "slg_num": est["slg"],
            },
            "mix": None if mix is None else {
                "xwoba_vs_mix": _fmt_rate3(mix["mix"]["xwoba"]),
                "xwoba_usual": _fmt_rate3(mix["baseline"]["xwoba"]),
                "diff_num": mix["mix"]["xwoba"] - mix["baseline"]["xwoba"],
                "breakdown": breakdown,
            },
        })
    rows.sort(key=lambda r: r["est"]["ops_num"], reverse=True)

    lg_ops = league["ob"] + league["slg"]
    return {
        "pitcher": {"id": pitcher_id, "throws": throws, "avg_bf": round(avg_bf, 1),
                    "share_of_game": round(share_vs_pitcher, 2)},
        "season": season,
        "model_version": MODEL_VERSION,
        "batting_home": batting_home,
        "mix_available": mix_available,
        "league": {"avg": _fmt_rate3(league["avg"]), "obp": _fmt_rate3(league["ob"]),
                   "slg": _fmt_rate3(league["slg"]), "ops": _fmt_rate3(lg_ops), "ops_num": lg_ops},
        "rows": rows,
    }


# ---- Player / team pages and the game summary --------------------------

PAGE_CACHE_SECONDS = 60 * 60


def _league_ranks(season: int) -> dict:
    return _cached(("league-ranks", season), LEAGUE_CACHE_SECONDS,
                   lambda: pages.league_ranks(mlb_api.get_league_team_splits(season)))


def _team_lines(team_id: int, season: int) -> dict:
    return _cached(("team-lines", team_id, season), PAGE_CACHE_SECONDS,
                   lambda: pages.team_lines(mlb_api.get_team_hitting(team_id, season)))


def _games_for_team(team_id: int, game_date: str) -> list[dict]:
    return query_db(
        """
        SELECT g.game_pk, g.game_date, g.game_date_time, g.home_team_id, g.away_team_id,
               ht.name AS home_team, at.name AS away_team,
               g.home_probable_pitcher_id, hp.full_name AS home_probable_pitcher,
               g.away_probable_pitcher_id, ap.full_name AS away_probable_pitcher
        FROM games g
        LEFT JOIN teams ht ON ht.id = g.home_team_id
        LEFT JOIN teams at ON at.id = g.away_team_id
        LEFT JOIN players hp ON hp.id = g.home_probable_pitcher_id
        LEFT JOIN players ap ON ap.id = g.away_probable_pitcher_id
        WHERE g.game_date = ? AND (g.home_team_id = ? OR g.away_team_id = ?)
        ORDER BY g.game_date_time
        """, (game_date, team_id, team_id))


def _team_game_today(team_id: int, game_date: str | None) -> dict | None:
    """His team's game on game_date (first game of a doubleheader), from the
    opposing side's point of view: opponent and the opposing probable starter."""
    if not game_date or not team_id:
        return None
    rows = _games_for_team(team_id, game_date)
    if not rows:
        return None
    g = rows[0]
    home = g["home_team_id"] == team_id
    return {"game_pk": g["game_pk"], "date": g["game_date"], "time": g["game_date_time"], "is_home": home,
            "opponent": g["away_team"] if home else g["home_team"],
            "opponent_id": g["away_team_id"] if home else g["home_team_id"],
            "own_starter": {"id": g["home_probable_pitcher_id"] if home else g["away_probable_pitcher_id"],
                            "name": g["home_probable_pitcher"] if home else g["away_probable_pitcher"]},
            "opp_starter": {"id": g["away_probable_pitcher_id"] if home else g["home_probable_pitcher_id"],
                            "name": g["away_probable_pitcher"] if home else g["home_probable_pitcher"]}}


def _h2h_line(batter_id: int, pitcher_id: int) -> dict | None:
    rows = query_db("SELECT * FROM matchup_career WHERE batter_id = ? AND pitcher_id = ?", (batter_id, pitcher_id))
    if not rows:
        return None
    r = rows[0]
    c = {"pa": r.get("plate_appearances") or 0, "ab": r.get("at_bats") or 0, "h": r.get("hits") or 0,
         "d2": r.get("doubles") or 0, "d3": r.get("triples") or 0, "hr": r.get("home_runs") or 0,
         "bb": r.get("base_on_balls") or 0, "hbp": r.get("hit_by_pitch") or 0, "so": r.get("strike_outs") or 0,
         "sf": r.get("sac_flies") or 0, "rbi": r.get("rbi") or 0, "sb": 0, "g": None}
    c["tb"] = r.get("total_bases") if r.get("total_bases") is not None else (
        c["h"] + c["d2"] + 2 * c["d3"] + 3 * c["hr"])
    return pages.hit_line(c)


@app.get("/api/player")
def player_route():
    """A player page. Hitters: last 3 seasons, this season vs LHP / RHP,
    last 7 / 15 / 30 games, recent game log, and today's matchup (with his
    history vs today's opposing starter). Pitchers: last 3 seasons, this
    season vs LHH / RHH, last 5 starts (or relief outings), today's start.
    Query: id, date."""
    pid = request.args.get("id", type=int)
    if not pid:
        return jsonify({"error": "id query param is required"}), 400
    game_date = request.args.get("date")
    season = _season_from(game_date)

    def build():
        raw = mlb_api.get_person_page(pid)
        p = (raw.get("people") or [{}])[0]
        pos = p.get("primaryPosition") or {}
        team = p.get("currentTeam") or {}
        is_pitcher = pos.get("type") == "Pitcher"
        out = {"id": pid, "name": p.get("fullName"), "number": p.get("primaryNumber"), "age": p.get("currentAge"),
               "position": pos.get("abbreviation"), "bats": (p.get("batSide") or {}).get("code"),
               "throws": (p.get("pitchHand") or {}).get("code"),
               "team": {"id": team.get("id"), "name": team.get("name")},
               "kind": "pitcher" if is_pitcher else "hitter", "season": season}
        if is_pitcher:
            out["seasons"] = pages.by_season(mlb_api.get_year_by_year(pid, "pitching"), "pitching")
            out["vs_hand"] = pages.vs_hand(mlb_api.get_pitcher_vs_hand(pid, season), "pitching")
            log = sorted((g for g in _pitcher_gamelog(pid, season) if g.get("game_date")),
                         key=lambda g: g["game_date"], reverse=True)
            starter = sum(1 for g in log if g.get("started")) >= max(1, len(log) // 2)
            recent = [g for g in log if bool(g.get("started")) == starter][:5 if starter else 10]
            out["role"] = "starter" if starter else "reliever"
            out["recent"] = [{"date": g["game_date"], "opponent": g.get("opponent"), "is_home": g.get("is_home"),
                              "postseason": g.get("game_type") not in (None, "R"),
                              "ip": f"{(g.get('outs') or 0) // 3}.{(g.get('outs') or 0) % 3}",
                              "h": g.get("hits"), "er": g.get("earned_runs"), "bb": g.get("base_on_balls"),
                              "so": g.get("strike_outs"), "pitches": g.get("pitches")} for g in recent]
        else:
            out["seasons"] = pages.by_season(mlb_api.get_year_by_year(pid, "hitting"), "hitting")
            hv = mlb_api.get_hitters_vs_hand([pid], season, "vl,vr")
            out["vs_hand"] = pages.vs_hand((hv.get("people") or [{}])[0], "hitting")
            games = pages.hitting_games(mlb_api.get_hitting_gamelog(pid, season))
            out["recent"] = pages.recent_hitting(games)
            out["game_log"] = pages.game_log_rows(games, 10)
        today = _team_game_today(team.get("id"), game_date)
        if today and not is_pitcher and today["opp_starter"]["id"]:
            today["h2h"] = _h2h_line(pid, today["opp_starter"]["id"])
            today["opp_starter"]["throws"] = _person(today["opp_starter"]["id"]).get("throws")
        out["today"] = today
        return out

    try:
        return jsonify(_cached(("player-page", pid, game_date), PAGE_CACHE_SECONDS, build))
    except Exception as exc:
        print(f"[player page] {pid}: {exc}")
        return jsonify({"error": "Couldn't load this player from MLB right now."}), 502


@app.get("/api/team")
def team_route():
    """A team page: hitting this season and vs LHP / RHP with MLB rank (by
    OPS), today's game, home park factors, and the active roster.
    Bullpen workload comes from /api/bullpen. Query: id, date."""
    tid = request.args.get("id", type=int)
    if not tid:
        return jsonify({"error": "id query param is required"}), 400
    game_date = request.args.get("date")
    season = _season_from(game_date)

    def build():
        lines = _team_lines(tid, season)
        ranks = _league_ranks(season)
        roster = _team_roster(tid)
        name_rows = query_db("SELECT name FROM teams WHERE id = ?", (tid,))
        park = query_db(
            """
            SELECT v.name, v.hr_factor, v.hit_factor, v.roof_type
            FROM games g JOIN venues v ON v.id = g.venue_id
            WHERE g.home_team_id = ? AND g.game_type = 'R'
            ORDER BY g.game_date DESC LIMIT 1
            """, (tid,)) or query_db(
            "SELECT v.name, v.hr_factor, v.hit_factor, v.roof_type FROM games g JOIN venues v ON v.id = g.venue_id "
            "WHERE g.home_team_id = ? ORDER BY g.game_date DESC LIMIT 1", (tid,))
        return {
            "id": tid, "name": name_rows[0]["name"] if name_rows else None, "season": season,
            "hitting": {"season": lines["season"],
                        "L": lines["L"], "L_rank": ranks["L"].get(tid),
                        "R": lines["R"], "R_rank": ranks["R"].get(tid),
                        "teams": ranks["teams"]["L"], "league": ranks["league"]},
            "park": park[0] if park else None,
            "today": _team_game_today(tid, game_date),
            "hitters": [{"id": p["id"], "name": p["full_name"], "position": p.get("position_abbr")}
                        for p in roster if p["position_type"] != "Pitcher"],
            "pitchers": [{"id": p["id"], "name": p["full_name"]} for p in roster if p["position_type"] == "Pitcher"],
        }

    try:
        return jsonify(_cached(("team-page", tid, game_date), PAGE_CACHE_SECONDS, build))
    except Exception as exc:
        print(f"[team page] {tid}: {exc}")
        return jsonify({"error": "Couldn't load this team from MLB right now."}), 502


@app.get("/api/summary")
def summary_route():
    """Game summary: each lineup's season hitting vs the opposing starter's
    throwing hand, with its MLB rank (by OPS) and the league line for
    comparison. (Starters, bullpens, park and weather come from the data the
    game page already loads.) Query: game, date."""
    game_pk = request.args.get("game", type=int)
    game_date = request.args.get("date")
    if not game_pk:
        return jsonify({"error": "game query param is required"}), 400
    season = _season_from(game_date)
    g = query_db("SELECT home_team_id, away_team_id, home_probable_pitcher_id, away_probable_pitcher_id "
                 "FROM games WHERE game_pk = ?", (game_pk,))
    if not g:
        return jsonify({"error": "unknown game"}), 404
    g = g[0]
    try:
        ranks = _league_ranks(season)
        out = {"season": season, "teams": ranks["teams"]["L"], "sides": {}}
        for side, team_id, opp_pitcher in (("away", g["away_team_id"], g["home_probable_pitcher_id"]),
                                           ("home", g["home_team_id"], g["away_probable_pitcher_id"])):
            throws = _person(opp_pitcher).get("throws") if opp_pitcher else None
            lines = _team_lines(team_id, season)
            out["sides"][side] = {
                "team_id": team_id, "vs": throws,
                "line": lines.get(throws) if throws else None,
                "rank": ranks[throws].get(team_id) if throws else None,
                "league": ranks["league"].get(throws) if throws else None,
                "season_line": lines["season"],
            }
        return jsonify(out)
    except Exception as exc:
        print(f"[summary] game {game_pk}: {exc}")
        return jsonify({"error": "Couldn't load team splits from MLB right now."}), 502



def snapshot_pregame_projections(game_date: str, now_utc=None, horizon_hours: float = 3.0) -> int:
    """Daily archive: save the site's projection for every posted starting
    hitter vs the opposing probable starter, for games starting within
    `horizon_hours`. Runs on every pre-game scheduler pass, overwriting until
    first pitch, so the stored row is the last pre-game projection. Returns
    rows written."""
    from datetime import datetime, timezone
    now_utc = now_utc or datetime.now(timezone.utc)
    games = query_db("SELECT game_pk, game_date, game_date_time, home_team_id, away_team_id, "
                     "home_probable_pitcher_id, away_probable_pitcher_id, home_lineup, away_lineup "
                     "FROM games WHERE game_date = ?", (game_date,))
    written = 0
    conn = db_module.connect(DB_PATH)
    try:
        for g in games:
            try:
                start = datetime.fromisoformat((g["game_date_time"] or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            mins = (start - now_utc).total_seconds() / 60
            if mins <= 0 or mins > horizon_hours * 60:
                continue   # already started (keep the last pre-game write) or too early
            for bat_team, lineup_raw, pitcher_id in (
                    (g["home_team_id"], g["home_lineup"], g["away_probable_pitcher_id"]),
                    (g["away_team_id"], g["away_lineup"], g["home_probable_pitcher_id"])):
                if not lineup_raw or not pitcher_id:
                    continue
                try:
                    est = compute_estimates(pitcher_id, bat_team, game_date, g["game_pk"])
                except EstimatesError as exc:
                    print(f"[snapshot] game {g['game_pk']} pitcher {pitcher_id}: {exc}")
                    continue
                for r in est["rows"]:
                    if not r.get("lineup_spot"):
                        continue   # bench: only starters are logged
                    e = r["est"]
                    db_module.upsert_projection(conn, {
                        "game_pk": g["game_pk"], "game_date": game_date, "pitcher_id": pitcher_id,
                        "batter_id": r["id"], "team_id": bat_team, "lineup_spot": r["lineup_spot"],
                        "is_home": 1 if est.get("batting_home") else 0,
                        "est_avg": e["avg_num"], "est_obp": e["obp_num"], "est_slg": e["slg_num"],
                        "hit_chance": e["hit_tonight_num"], "hr_chance": e["hr_tonight_num"],
                        "pa_expected": e["pa_expected"], "starter_share": est["pitcher"]["share_of_game"],
                        "model_version": est["model_version"],
                    })
                    written += 1
                conn.commit()
    finally:
        conn.close()
    if written:
        print(f"[snapshot] {game_date}: {written} projection rows saved")
    return written


@app.get("/api/archive")
def archive_route():
    """Daily data archive: what's been saved, and (once games are final) how
    the saved pre-game projections compare with what happened -- by
    predicted 1+ hit range. Query: optional since (YYYY-MM-DD)."""
    since = request.args.get("since") or "0000-00-00"
    counts = {
        "games": query_db("SELECT COUNT(*) AS n, MIN(game_date) AS first, MAX(game_date) AS last "
                          "FROM game_results WHERE game_date >= ?", (since,))[0],
        "batter_lines": query_db("SELECT COUNT(*) AS n FROM batter_game_results WHERE game_date >= ?", (since,))[0]["n"],
        "projections": query_db("SELECT COUNT(*) AS n FROM projection_log WHERE game_date >= ?", (since,))[0]["n"],
    }
    rows = query_db(
        """
        SELECT p.hit_chance, p.hr_chance, b.h, b.hr, b.pa
        FROM projection_log p
        JOIN batter_game_results b ON b.game_pk = p.game_pk AND b.batter_id = p.batter_id AND b.started = 1
        WHERE p.game_date >= ?
        """, (since,))
    buckets = []
    for lo, hi in ((0, .5), (.5, .55), (.55, .6), (.6, .65), (.65, .7), (.7, 1.01)):
        b = [r for r in rows if r["hit_chance"] is not None and lo <= r["hit_chance"] < hi]
        if b:
            buckets.append({"range": f"{lo:.0%}-{min(hi, 1):.0%}", "n": len(b),
                            "predicted": round(sum(r["hit_chance"] for r in b) / len(b), 3),
                            "actual": round(sum(1 for r in b if (r["h"] or 0) > 0) / len(b), 3)})
    hr_rows = [r for r in rows if r["hr_chance"] is not None]
    return jsonify({
        "counts": counts,
        "graded": len(rows),
        "hit_calibration": buckets,
        "hr": None if not hr_rows else {
            "predicted": round(sum(r["hr_chance"] for r in hr_rows) / len(hr_rows), 3),
            "actual": round(sum(1 for r in hr_rows if (r["hr"] or 0) > 0) / len(hr_rows), 3)},
    })


@app.get("/api/players")
def players():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"error": "q (search text) must be at least 2 characters"}), 400

    rows = query_db(
        "SELECT id, full_name, role, team_id FROM players WHERE full_name LIKE ? ORDER BY full_name LIMIT 25",
        (f"%{q}%",),
    )
    return jsonify(rows)


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


# Started last, once every function the scheduler hooks call is defined.
if not os.environ.get("SKIP_SCHEDULER"):
    _start_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

