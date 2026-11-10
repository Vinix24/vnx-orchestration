"""Read-only table measurements in one SQLite read transaction."""
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .contract import CORPUS_TABLES, SCHEMA, TRACK, validate_corpus, validate_manifest, validate_session
from .digest import table_digest


def measure_baseline(db_path, *, corpus=False):
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
        manifest = dict(schema=SCHEMA, track=TRACK,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), db=db, tables=tables)
        if corpus:
            manifest.update(corpus=_corpus(conn, tables), invariants=dict(session_analytics=_session(conn)))
    return validate_manifest(manifest)


def _corpus(conn, tables):
    result = dict(tables=CORPUS_TABLES)
    for field in ("rows", "dbstat_bytes"):
        result[field] = {t["name"]: t[field] for t in tables if t["name"] in CORPUS_TABLES}
    result["rowid_range"] = {name: dict(zip(("min", "max"), conn.execute(
        f"SELECT min(rowid),max(rowid) FROM {name}").fetchone())) for name in CORPUS_TABLES}
    roots = Counter()
    for path, count in conn.execute("SELECT file_path,count(*) FROM code_snippets GROUP BY file_path"):
        if not isinstance(path, str) or not path:
            raise ValueError("invalid corpus file_path")
        roots["/".join(path.split("/")[:5 + path.startswith("/")])] += count
    result.update(file_path_roots=dict(roots), languages=dict(conn.execute(
        "SELECT language,count(*) FROM code_snippets GROUP BY language")))
    result["distinct_file_path"] = conn.execute("SELECT count(DISTINCT file_path) FROM code_snippets").fetchone()[0]
    result["created_at"] = dict(zip(("min", "max"), conn.execute(
        "SELECT min(created_at),max(created_at) FROM snippet_metadata").fetchone()))
    result["join"] = dict(orphans=conn.execute("SELECT count(*) FROM snippet_metadata m "
        "LEFT JOIN code_snippets s ON m.snippet_rowid=s.rowid WHERE s.rowid IS NULL").fetchone()[0],
        duplicate_snippet_rowid=conn.execute("SELECT count(*) FROM (SELECT snippet_rowid FROM snippet_metadata "
        "GROUP BY snippet_rowid HAVING count(*) > 1)").fetchone()[0])
    return validate_corpus(result)


def _session(conn):
    columns = [r[1] for r in conn.execute("PRAGMA table_info(session_analytics)")]
    rows, max_id, sum_id = conn.execute("SELECT count(*),max(id),sum(id) FROM session_analytics").fetchone()
    digest = table_digest(conn, "session_analytics", columns, "id")
    return validate_session(dict(rows=rows, max_id=max_id, sum_id=sum_id,
                                 columns=columns, digest_sha256=digest["digest_sha256"]))
