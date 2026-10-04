"""Create (or re-create) the local SQLite database from schema.sql."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as db_module  # noqa: E402  (for DB_PATH, which honors BVP_DATA_DIR)

DB_PATH = db_module.DB_PATH
SCHEMA_PATH = ROOT / "data" / "schema.sql"

# CREATE TABLE IF NOT EXISTS (in schema.sql) only creates tables that don't
# exist yet -- it does nothing to a table that's already there, even if
# schema.sql has since grown new columns for it. So an already-running
# database (like the live one on Railway) needs those columns added by
# hand. This runs on every boot, checks what's actually there with
# PRAGMA table_info, and adds anything missing -- safe to run repeatedly,
# and means a schema change here just needs a normal deploy, never a
# manual migration step.
_NEW_COLUMNS = {
    "games": [("venue_id", "INTEGER REFERENCES venues(id)"),
              ("game_date_time", "TEXT"),
              ("home_lineup", "TEXT"),
              ("away_lineup", "TEXT")],
    "matchup_career": [("runs", "INTEGER"), ("stolen_bases", "INTEGER"),
                        ("caught_stealing", "INTEGER")],
    "matchup_season": [("runs", "INTEGER"), ("stolen_bases", "INTEGER"),
                        ("caught_stealing", "INTEGER")],
    "venues": [("hr_factor", "REAL"), ("hit_factor", "REAL")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _NEW_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col_name, col_type in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
    conn.commit()


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
        _migrate(conn)
        # Best-effort: fill in park factors for any venue we've already
        # ingested that doesn't have them yet. Wrapped defensively -- a
        # problem here should never block the app from starting up.
        try:
            import seed_park_factors
            seed_park_factors.seed(conn)
        except Exception as exc:
            print(f"    park factor seeding skipped: {exc}")
    finally:
        conn.close()
    print(f"Database ready at {db_path}")


if __name__ == "__main__":
    init_db()
