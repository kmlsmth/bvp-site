"""Create (or re-create) the local SQLite database from schema.sql."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as db_module  # noqa: E402  (for DB_PATH, which honors BVP_DATA_DIR)

DB_PATH = db_module.DB_PATH
SCHEMA_PATH = ROOT / "data" / "schema.sql"


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()
    print(f"Database ready at {db_path}")


if __name__ == "__main__":
    init_db()
