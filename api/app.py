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
import mlb_api  # noqa: E402
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

if not os.environ.get("SKIP_SCHEDULER"):
    import scheduler
    scheduler.start()


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


def _arsenal_summary(pitcher_id: int, season: int) -> dict | None:
    """For the starter card: his mix vs lefties and vs righties (most-used
    first), or None if Statcast data isn't available."""
    try:
        usage = _pitcher_usage(pitcher_id, season)
    except Exception as exc:
        print(f"[arsenal] {pitcher_id}: {exc}")
        return None
    out = {}
    for side in ("L", "R"):
        sh = pitch_mix.shares(usage.get(side) or {})
        out[side] = [{"type": pt, "name": pitch_mix.PITCH_NAMES[pt], "pct": round(v * 100)}
                     for pt, v in sorted(sh.items(), key=lambda kv: -kv[1]) if v >= 0.015]
        out[side + "_pitches"] = sum((usage.get(side) or {}).values())
    return out if (out["L_pitches"] or out["R_pitches"]) else None



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
            # Statcast pitch mix vs each side; None (card just omits it) if
            # Baseball Savant can't be reached.
            "arsenal": _arsenal_summary(pitcher_id, season),
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


def _hitters_vs(ids: tuple, sit_code: str, season: int, current_season: int) -> dict:
    """Each hitter's regular-season line vs one pitching hand in `season`."""
    ttl = PAST_SEASON_CACHE_SECONDS if season < current_season else STATS_CACHE_SECONDS
    return _cached(("hitters-vs", ids, sit_code, season), ttl,
                   lambda: parsing.parse_hitters_vs_hand(
                       mlb_api.get_hitters_vs_hand(list(ids), season, sit_code), sit_code))


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
      1. hitter vs this hand x pitcher vs this hitter's side, relative to
         league (odds ratio), small samples regressed toward league; this
         season and the two before it, weighted 5/4/3;
      2. nudged by pitch mix: how this hitter handles each pitch type
         (Statcast expected stats, same 5/4/3 seasons), weighted by how
         often this pitcher throws each one to hitters from that side --
         his last 5 outings blended with his season;
      3. any head-to-head at-bats added on top, older seasons faded;
      4. HR tonight: his expected plate appearances (by lineup spot), split
         between this pitcher (by how deep he usually goes) and the bullpen.
    Query: pitcher, opponent_team, date, optional game (game_pk, for the
    posted lineup) and role=reliever (shorter outings)."""
    pitcher_id = request.args.get("pitcher", type=int)
    opponent_team_id = request.args.get("opponent_team", type=int)
    if not pitcher_id or not opponent_team_id:
        return jsonify({"error": "pitcher and opponent_team query params (ids) are required"}), 400
    game_date = request.args.get("date")
    game_pk = request.args.get("game", type=int)
    is_reliever = request.args.get("role") == "reliever"
    season = _season_from(game_date)

    try:
        throws = _person(pitcher_id).get("throws")
        if throws not in ("L", "R"):
            return jsonify({"error": "MLB doesn't list this pitcher's throwing hand."}), 502
        sit_code = "vr" if throws == "R" else "vl"

        batters = [b for b in _team_roster(opponent_team_id)
                   if b["position_type"] != "Pitcher" and b["id"]]
        ids = tuple(sorted(b["id"] for b in batters))
        hitters = _hitters_vs(ids, sit_code, season, season)
        pitcher_split = _pitcher_vs(pitcher_id, season, season)
        league = _cached(("league", season), LEAGUE_CACHE_SECONDS,
                         lambda: matchup_estimate.league_rates(
                             parsing.parse_team_hitting_lines(mlb_api.get_league_team_hitting(season))))
        gamelog = _pitcher_gamelog(pitcher_id, season)
    except Exception as exc:
        print(f"[estimates] pitcher={pitcher_id} team={opponent_team_id}: {exc}")
        return jsonify({"error": "Couldn't load season splits from MLB right now."}), 502

    # The two seasons before this one, weighted 4 and 3 against this
    # season's 5 (matchup_estimate.SEASON_WEIGHTS). Optional: if MLB doesn't
    # answer, the projection runs on this season alone.
    past_seasons = (season - 1, season - 2)
    past_hitters, past_pitcher = [], []
    for past in past_seasons:
        try:
            past_hitters.append(_hitters_vs(ids, sit_code, past, season))
            past_pitcher.append(_pitcher_vs(pitcher_id, past, season))
        except Exception as exc:
            print(f"[estimates] {past} splits unavailable for pitcher={pitcher_id}: {exc}")
            past_hitters.append({})
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
    if game_pk:
        g = query_db("SELECT home_team_id, home_lineup, away_lineup FROM games WHERE game_pk = ?", (game_pk,))
        if g:
            raw = g[0]["home_lineup"] if g[0]["home_team_id"] == opponent_team_id else g[0]["away_lineup"]
            for p in (json.loads(raw) if raw else []):
                if p.get("id"):
                    spot_by_id[p["id"]] = p.get("order")

    rows = []
    for b in batters:
        h = hitters.get(b["id"]) or {}
        bats = h.get("bats") or next((ph[b["id"]].get("bats") for ph in past_hitters
                                      if (ph.get(b["id"]) or {}).get("bats")), None)
        side = matchup_estimate.facing_side(bats, throws)
        line = h.get("stat")
        # This season + the two before it (5/4/3), for hitter and pitcher.
        h_counts = matchup_estimate.combine_seasons(
            [matchup_estimate.hitting_counts(line)] +
            [matchup_estimate.hitting_counts((ph.get(b["id"]) or {}).get("stat")) for ph in past_hitters])
        p_counts = matchup_estimate.combine_seasons(
            [matchup_estimate.pitching_counts(pitcher_split.get(side))] +
            [matchup_estimate.pitching_counts(pp.get(side)) for pp in past_pitcher]) if side else None
        base = matchup_estimate.estimate(h_counts, p_counts, league)

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

        # Rest of the game vs the bullpen: his own (regressed) HR rate vs this
        # hand against league-average pitching.
        hr_rest = matchup_estimate.estimate(h_counts, None, league)["hr"]
        spot = spot_by_id.get(b["id"])
        pa_exp = matchup_estimate.expected_pa(league["pa_per_team_game"], spot)
        hr_tonight = matchup_estimate.hr_chance_tonight(est["hr"], hr_rest, pa_exp, share_vs_pitcher)

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
                "pa_expected": round(pa_exp, 1),
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
    return jsonify({
        "pitcher": {"id": pitcher_id, "throws": throws, "avg_bf": round(avg_bf, 1),
                    "share_of_game": round(share_vs_pitcher, 2)},
        "season": season,
        "mix_available": mix_available,
        "league": {"avg": _fmt_rate3(league["avg"]), "obp": _fmt_rate3(league["ob"]),
                   "slg": _fmt_rate3(league["slg"]), "ops": _fmt_rate3(lg_ops), "ops_num": lg_ops},
        "rows": rows,
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
