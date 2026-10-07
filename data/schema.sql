-- Batter vs Pitcher (BVP) tool — SQLite schema
-- Stores: known players, today's games/probable pitchers, and the actual
-- career + season-by-season batter-vs-pitcher matchup numbers.

CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);

-- One row per MLB ballpark. Filled in lazily the first time a game
-- references it (see scripts/venues.py) -- stadiums don't move, so this
-- is write-once-per-park, not a daily refresh. azimuth_angle is the
-- compass bearing from home plate toward straightaway center field,
-- which is what lets the weather card draw wind direction relative to
-- the actual field instead of just true north.
CREATE TABLE IF NOT EXISTS venues (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL,
    city                    TEXT,
    state                   TEXT,
    lat                     REAL,
    lon                     REAL,
    azimuth_angle           REAL,   -- degrees; NULL if MLB doesn't publish one for this park
    elevation               INTEGER,
    roof_type               TEXT,   -- Open / Retractable / Dome / Indoor, as MLB reports it
    capacity                INTEGER,
    left_line               INTEGER,
    left_center             INTEGER,
    center                  INTEGER,
    right_center            INTEGER,
    right_line              INTEGER,
    hr_factor               REAL,   -- park factor, 100 = league average, from a
                                    -- hand-seeded reference (scripts/seed_park_factors.py)
                                    -- -- NOT pulled by daily ingestion, since park
                                    -- factors don't change game to game. NULL until seeded.
    hit_factor               REAL,  -- same scale/source as hr_factor, for hits overall
    nws_forecast_hourly_url TEXT,   -- unused (left over from an earlier weather
                                    -- source, National Weather Service, that
                                    -- only covered U.S. parks); harmless to keep
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Wind/temperature snapshot for one game, pulled from Open-Meteo (free, no
-- key, worldwide coverage) using the venue's coordinates. Replaced each
-- ingestion run, so it reflects whatever forecast was current as of that
-- run, not a live-updating value.
CREATE TABLE IF NOT EXISTS game_weather (
    game_pk         INTEGER PRIMARY KEY REFERENCES games(game_pk),
    wind_speed_mph  INTEGER,
    wind_dir_deg    REAL,     -- meteorological "from" bearing, true compass
    wind_dir_compass TEXT,    -- e.g. "ENE"
    temp_f          INTEGER,
    sky             TEXT,
    forecast_time   TEXT,     -- the forecast period's own start time (UTC)
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    first_name  TEXT,
    last_name   TEXT,
    role        TEXT CHECK(role IN ('batter', 'pitcher', 'both')) DEFAULT 'both',
    team_id     INTEGER REFERENCES teams(id),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per game, with the probable starters. This is what drives the
-- daily job: for each game, we know which pitcher is facing which team,
-- and can pull BVP numbers for that pitcher against the opposing lineup.
CREATE TABLE IF NOT EXISTS games (
    game_pk                 INTEGER PRIMARY KEY,
    game_date               TEXT NOT NULL,       -- officialDate, e.g. '2026-10-02'
    game_date_time           TEXT,                -- full first-pitch timestamp (UTC ISO), for picking the closest weather forecast hour
    game_type               TEXT,                -- R / F / D / L / W (reg season, postseason rounds)
    status                  TEXT,
    venue_id                INTEGER REFERENCES venues(id),
    home_team_id            INTEGER REFERENCES teams(id),
    away_team_id            INTEGER REFERENCES teams(id),
    home_probable_pitcher_id INTEGER REFERENCES players(id),
    away_probable_pitcher_id INTEGER REFERENCES players(id),
    home_lineup              TEXT,                -- JSON array of {order, id, name, position}; NULL until MLB posts it (usually 1-3 hrs before first pitch)
    away_lineup              TEXT,                -- same, away team
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Career (all-time) totals for one batter vs one pitcher.
-- Comes from the API's "vsPlayerTotal" stat group.
CREATE TABLE IF NOT EXISTS matchup_career (
    batter_id       INTEGER NOT NULL REFERENCES players(id),
    pitcher_id      INTEGER NOT NULL REFERENCES players(id),
    games_played    INTEGER,
    at_bats         INTEGER,
    plate_appearances INTEGER,
    hits            INTEGER,
    doubles         INTEGER,
    triples         INTEGER,
    home_runs       INTEGER,
    runs            INTEGER,
    strike_outs     INTEGER,
    base_on_balls   INTEGER,
    intentional_walks INTEGER,
    hit_by_pitch    INTEGER,
    total_bases     INTEGER,
    rbi             INTEGER,
    stolen_bases    INTEGER,
    caught_stealing INTEGER,
    left_on_base    INTEGER,
    sac_bunts       INTEGER,
    sac_flies       INTEGER,
    ground_into_double_play INTEGER,
    number_of_pitches INTEGER,
    avg             TEXT,
    obp             TEXT,
    slg             TEXT,
    ops             TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (batter_id, pitcher_id)
);

-- Season-by-season breakdown for one batter vs one pitcher.
-- Comes from the API's "vsPlayer" stat group (one row per season played).
CREATE TABLE IF NOT EXISTS matchup_season (
    batter_id       INTEGER NOT NULL REFERENCES players(id),
    pitcher_id      INTEGER NOT NULL REFERENCES players(id),
    season          TEXT NOT NULL,
    team_id         INTEGER REFERENCES teams(id),
    opponent_id     INTEGER REFERENCES teams(id),
    games_played    INTEGER,
    at_bats         INTEGER,
    plate_appearances INTEGER,
    hits            INTEGER,
    doubles         INTEGER,
    triples         INTEGER,
    home_runs       INTEGER,
    runs            INTEGER,
    strike_outs     INTEGER,
    base_on_balls   INTEGER,
    intentional_walks INTEGER,
    hit_by_pitch    INTEGER,
    total_bases     INTEGER,
    rbi             INTEGER,
    stolen_bases    INTEGER,
    caught_stealing INTEGER,
    left_on_base    INTEGER,
    sac_bunts       INTEGER,
    sac_flies       INTEGER,
    ground_into_double_play INTEGER,
    number_of_pitches INTEGER,
    avg             TEXT,
    obp             TEXT,
    slg             TEXT,
    ops             TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (batter_id, pitcher_id, season)
);

-- One row per pitcher per game: the actual box-score line, not a season
-- total. This is what both bullpen fatigue and starter recent-form trends
-- are built from -- a reliever's rolling 7-day workload and a starter's
-- last-3/5-starts line are just two different slices of the same table.
-- `role` comes from the box score's own "gamesStarted" flag, not a guess.
-- innings_outs stores outs recorded (18 = 6.0 IP, 17 = 5.2 IP) rather than
-- a fractional innings number, since outs add up correctly and fractional
-- innings (".1" = one out, not one tenth) do not.
CREATE TABLE IF NOT EXISTS pitcher_appearances (
    -- Deliberately NOT "REFERENCES games(game_pk)": most rows here are
    -- backfilled from a team's recent game history, which is pulled
    -- directly from the schedule-range endpoint and never written to the
    -- `games` table (that table only ever holds today's slate). A plain
    -- integer game_pk is enough to dedupe on (see the primary key below).
    game_pk         INTEGER NOT NULL,
    pitcher_id      INTEGER NOT NULL REFERENCES players(id),
    team_id         INTEGER REFERENCES teams(id),
    game_date       TEXT NOT NULL,
    role            TEXT CHECK(role IN ('starter', 'reliever')),
    outs            INTEGER,   -- outs recorded this appearance
    pitches         INTEGER,
    batters_faced   INTEGER,
    earned_runs     INTEGER,
    base_on_balls   INTEGER,
    strike_outs     INTEGER,
    hits            INTEGER,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (game_pk, pitcher_id)
);

CREATE INDEX IF NOT EXISTS idx_appearances_pitcher_date ON pitcher_appearances(pitcher_id, game_date);
CREATE INDEX IF NOT EXISTS idx_appearances_team_date ON pitcher_appearances(team_id, game_date);

CREATE INDEX IF NOT EXISTS idx_matchup_season_batter ON matchup_season(batter_id);
CREATE INDEX IF NOT EXISTS idx_matchup_season_pitcher ON matchup_season(pitcher_id);
CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date);

-- Tracks when the daily ingestion job last ran for a given date, so the
-- in-process scheduler (see api/scheduler.py) knows whether it still needs
-- to run today, even after a redeploy/restart.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_date    TEXT PRIMARY KEY,   -- the game date this run covered
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,               -- 'running' / 'done' / 'failed'
    games_count INTEGER,
    error       TEXT
);

-- ---- Daily data archive ---------------------------------------------------
-- What actually happened, game by game, saved from each final box score so
-- the site builds its own history: the live "our projection vs what
-- happened" check, games started / plate appearances by lineup spot, and
-- player game logs. Like pitcher_appearances, game_pk is a plain integer
-- (no foreign key): games are archived straight from the schedule.

-- One row per final game.
CREATE TABLE IF NOT EXISTS game_results (
    game_pk         INTEGER PRIMARY KEY,
    game_date       TEXT NOT NULL,
    game_type       TEXT,               -- R / F / D / L / W
    away_team_id    INTEGER,
    home_team_id    INTEGER,
    away_runs       INTEGER,
    home_runs       INTEGER,
    away_hits       INTEGER,
    home_hits       INTEGER,
    archived_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_game_results_date ON game_results(game_date);

-- Every hitter who batted in a final game. batting_order is the lineup
-- spot (1-9); started = 1 for the original starter in that spot, 0 for a
-- substitute (MLB's box score codes "300" = starter in the 3 hole, "301" =
-- first sub there).
CREATE TABLE IF NOT EXISTS batter_game_results (
    game_pk         INTEGER NOT NULL,
    batter_id       INTEGER NOT NULL,
    team_id         INTEGER,
    game_date       TEXT NOT NULL,
    is_home         INTEGER,
    batting_order   INTEGER,
    started         INTEGER,
    position        TEXT,
    pa              INTEGER,
    ab              INTEGER,
    h               INTEGER,
    d2              INTEGER,
    d3              INTEGER,
    hr              INTEGER,
    bb              INTEGER,
    ibb             INTEGER,
    hbp             INTEGER,
    so              INTEGER,
    tb              INTEGER,
    r               INTEGER,
    rbi             INTEGER,
    sb              INTEGER,
    sf              INTEGER,
    PRIMARY KEY (game_pk, batter_id)
);
CREATE INDEX IF NOT EXISTS idx_batter_results_batter ON batter_game_results(batter_id, game_date);

-- The site's own projection for each starting hitter vs the opposing
-- starter, saved shortly before first pitch (overwritten on each pre-game
-- pass until the game starts, so the last write is the pre-game snapshot).
CREATE TABLE IF NOT EXISTS projection_log (
    game_pk         INTEGER NOT NULL,
    game_date       TEXT NOT NULL,
    pitcher_id      INTEGER NOT NULL,
    batter_id       INTEGER NOT NULL,
    team_id         INTEGER,
    lineup_spot     INTEGER,
    is_home         INTEGER,
    est_avg         REAL,
    est_obp         REAL,
    est_slg         REAL,
    hit_chance      REAL,
    hr_chance       REAL,
    pa_expected     REAL,
    starter_share   REAL,
    model_version   TEXT,
    logged_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (game_pk, pitcher_id, batter_id)
);
CREATE INDEX IF NOT EXISTS idx_projection_log_date ON projection_log(game_date);
