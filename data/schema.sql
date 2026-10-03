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
    nws_forecast_hourly_url TEXT,   -- cached from the NWS /points lookup, so we
                                    -- only need one request per refresh after that
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Wind/temperature snapshot for one game, pulled from the National
-- Weather Service (free, no key) using the venue's coordinates. Replaced
-- each ingestion run, so it reflects whatever forecast was current as of
-- that run, not a live-updating value.
CREATE TABLE IF NOT EXISTS game_weather (
    game_pk         INTEGER PRIMARY KEY REFERENCES games(game_pk),
    wind_speed_mph  INTEGER,
    wind_dir_deg    REAL,     -- meteorological "from" bearing, true compass
    wind_dir_compass TEXT,    -- e.g. "ENE", as NWS reports it
    temp_f          INTEGER,
    sky             TEXT,
    forecast_time   TEXT,     -- the forecast period's own start time, from NWS
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
