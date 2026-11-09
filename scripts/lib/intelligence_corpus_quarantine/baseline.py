"""Read-only table measurements in one SQLite read transaction."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .contract import SCHEMA, TRACK, validate_manifest


def measure_baseline(db_path):
    """Measure a real store; I/O/SQL errors propagate instead of becoming zero rows."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise OSError("database is not a readable regular file")
    if path.stat().st_size == 0:
        raise ValueError("refusing 0-byte decoy database")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        if "code_snippets" not in names:
            raise ValueError("database has no code_snippets table")
        db = {"path": str(path), "bytes": path.stat().st_size}
        for pragma in ("page_size", "page_count", "freelist_count", "user_version", "journal_mode"):
            db[pragma] = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        checks = conn.execute("PRAGMA integrity_check").fetchall()
        if checks != [("ok",)]:
            raise ValueError(f"integrity_check failed: {checks}")
        db["integrity_check"] = "ok"
        tables = []
        for name in names:
            identifier = '"' + name.replace('"', '""') + '"'
            rows = conn.execute(f"SELECT count(*) FROM {identifier}").fetchone()[0]
            # Virtual tables own no pages; their shadow tables are measured separately.
            size = conn.execute("SELECT coalesce(sum(pgsize), 0) FROM dbstat WHERE name=?", (name,)).fetchone()[0]
            tables.append(dict(name=name, rows=rows, dbstat_bytes=size))
    return validate_manifest(dict(schema=SCHEMA, track=TRACK,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), db=db, tables=tables))
