"""Purge: empty the live corpus in one transaction that re-proves the permit and session_analytics before it commits.

Order (plan "Sessie-invariant (B2)"): intent line, BEGIN IMMEDIATE, check_permit on this connection, digest A over
session_analytics, DELETE both corpus tables, digest B, A == B or ROLLBACK (exit 3), COMMIT, outcome line. Then, outside
the transaction: wal_checkpoint(TRUNCATE), VACUUM, the after-measurement, the `vacuumed` line (a failure there is exit 4).
Rows only, never tables; this module removes no file, artefact or snapshot.
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from . import audit
from .contract import CORPUS_TABLES, load_manifest
from .digest import table_digest
from .store import _existing_db, _quote
from .verify import check_permit

REQUIREMENTS = [1, 2, 3, 4, 5, 6, 7, 8, 9]
DELETE_SQL = tuple(f"DELETE FROM {_quote(name)}" for name in CORPUS_TABLES)
_FAMILY = ("SELECT name FROM sqlite_master WHERE tbl_name IN (?, ?) OR name LIKE 'code\\_snippets\\_%' ESCAPE '\\' "
           "OR name LIKE 'snippet\\_metadata\\_%' ESCAPE '\\'")  # the tables, their indexes and the FTS5 shadow tables


def _line(state, stage, permit, status, error, message, **numbers):
    try:
        audit.append(state, stage, permit, status, **numbers)
    except Exception as exc:
        raise error(f"{message}: {exc}") from exc


def _counts(conn):
    return {name: conn.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0] for name in CORPUS_TABLES}


def _family(conn):
    return {row[0] for row in conn.execute(_FAMILY, CORPUS_TABLES)}


def _family_bytes(conn):
    names = sorted(_family(conn))
    marks = ", ".join("?" * len(names))
    return conn.execute(f"SELECT coalesce(sum(pgsize), 0) FROM dbstat WHERE name IN ({marks})", names).fetchone()[0]


def _session_digest(conn, session):
    where = None if session["max_id"] is None else f"t.id <= {session['max_id']}"
    return table_digest(conn, "session_analytics", session["columns"], "id", where=where)["digest_sha256"]


def _session_whole(conn, session):
    """Digest over every session_analytics row (no id bound), with count and sum(id): inside the purge's own lock any change is its own doing."""
    found = table_digest(conn, "session_analytics", session["columns"], "id")
    return found["digest_sha256"], found["rows"], conn.execute("SELECT coalesce(sum(id), 0) FROM session_analytics").fetchone()[0]


def _session_change(before, after):
    kind = ("rows added", "rows removed")[after[1] < before[1]] if after[1] != before[1] else "rows changed"
    return f"session_analytics changed inside the delete transaction (digest B != digest A): {kind}, count {before[1]} -> {after[1]}, sum(id) {before[2]} -> {after[2]}"


def _deleted_ok(state, permit):
    """True only on a `deleted` line with status ok for this permit; an unreadable or malformed audit file refuses."""
    try:
        text = (Path(state) / audit.AUDIT_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f"audit file unreadable, resume refused: {exc}") from exc
    rows = text.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    found = False
    for number, raw in enumerate(rows, 1):
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"audit line {number} is not JSON, resume refused") from exc
        if type(record) is not dict:
            raise ValueError(f"audit line {number} is not a JSON object, resume refused")
        found = found or (record.get("stage"), record.get("status"), record.get("permit_sha256")) == ("deleted", "ok", permit)
    return found


def _prove_and_delete(conn, db, manifest_path, folder, session, permit, box):
    verdict = check_permit(db, manifest_path, folder, conn=conn)
    checked = verdict.get("requirements_checked") if type(verdict) is dict else None
    if type(checked) is not list or any(type(n) is not int for n in checked) or checked != REQUIREMENTS:
        raise ValueError(f"permit verdict covers requirements {checked!r}, not 1-9")
    if verdict.get("permit_sha256") != permit:
        raise ValueError("permit files changed while the purge ran")
    family_bytes = _family_bytes(conn)
    whole_a = _session_whole(conn, session)
    box[0] = "rolled_back"  # from the first DELETE on, a failure is a rollback and not a refusal
    for sql in DELETE_SQL:
        conn.execute(sql)
    whole_b = _session_whole(conn, session)
    if whole_b != whole_a:
        raise ValueError(_session_change(whole_a, whole_b))
    if any(_counts(conn).values()):
        raise ValueError("corpus tables are not empty after the DELETE")
    return dict(session_in_transaction_equal=True, baseline_max_id=session["max_id"],
                session_rows_before=whole_a[1], session_rows_after=whole_b[1], corpus_family_bytes_before=family_bytes)


def _after_measure(db, manifest, session):
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        if any(_counts(conn).values()):
            raise ValueError("after-measurement: corpus tables are not empty")
        if conn.execute("PRAGMA freelist_count").fetchone()[0] != 0:
            raise ValueError("after-measurement: freelist_count is not 0 after VACUUM")
        family = _family(conn)
        for table in manifest["tables"]:
            if table["name"] not in family:
                rows = conn.execute(f"SELECT count(*) FROM {_quote(table['name'])}").fetchone()[0]
                if rows < table["rows"]:
                    raise ValueError(f"after-measurement: table {table['name']} has {rows} rows, the baseline {table['rows']}")
        if _session_digest(conn, session) != session["digest_sha256"]:
            raise ValueError("after-measurement: session_analytics differs from the baseline")
        return dict(corpus_family_bytes_after=_family_bytes(conn))


def _after_commit(db, state, permit, manifest, folder, deleted):
    """The corpus is gone: every failure from here is exit 4 and names the artefact as the restore path."""
    session = manifest["invariants"]["session_analytics"]
    try:
        if deleted is not None:
            audit.append(state, "deleted", permit, "ok", **deleted)
        audit.append(state, "vacuumed", permit, "intent")
        with closing(sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, isolation_level=None)) as conn:
            if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise RuntimeError("wal_checkpoint(TRUNCATE) was blocked by another connection")
            conn.execute("VACUUM")
        after = _after_measure(db, manifest, session)
        audit.append(state, "vacuumed", permit, "ok", **{k: v for k, v in (deleted or {}).items() if k.startswith(("corpus_family", "baseline"))}, **after)
    except Exception as exc:
        raise audit.AfterCommitError(f"the corpus is gone and the artefact in {folder} is the restore path; "
                                     f"the step after the commit failed: {type(exc).__name__}: {exc}") from exc
    return after


def _delete(db, manifest_path, folder, manifest, permit, state, before):
    session = manifest["invariants"]["session_analytics"]
    rows = {f"{name}_rows": count for name, count in before.items()}
    _line(state, "deleted", permit, "intent", ValueError, "nothing deleted: the intent line was not written, the store stays closed", **rows)
    box = ["refused"]  # until the first DELETE nothing can have been written
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                proof = _prove_and_delete(conn, db, manifest_path, folder, session, permit, box)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    except BaseException as exc:
        try:
            audit.append(state, "deleted", permit, box[0], reason=f"{type(exc).__name__}: {exc}")
        except Exception as missed:  # the rollback holds: still the original failure, and the message names the missing line
            raise ValueError(f"nothing deleted ({exc}), but its {box[0]} outcome line is missing: {missed}") from exc
        raise
    return dict(rows_deleted=rows, **_after_commit(db, state, permit, manifest, folder, {**rows, **proof}))


def _resume(db, manifest, folder, permit, state):
    if not _deleted_ok(state, permit):
        reason = "corpus is 0/0 but no deleted/ok audit line carries this permit_sha256: someone else emptied it"
        _line(state, "deleted", permit, "refused", ValueError, f"{reason}; its refused line is also missing", reason=reason)
        raise ValueError(reason)
    return dict(resumed=True, **_after_commit(db, state, permit, manifest, folder, None))


def purge(db_path, manifest_path, quarantine_dir, *, state_dir):
    """Empty code_snippets and snippet_metadata (rows only) under a re-proven permit; resume a 0/0 store on the audit line."""
    folder, state = Path(quarantine_dir).expanduser().resolve(), Path(state_dir).expanduser().resolve()
    audit.prepare(state, folder.parent)  # B4: the audit never lands in the permit dir
    manifest_path = Path(manifest_path).expanduser().resolve()
    try:
        permit = audit.permit_sha256(manifest_path, folder / "quarantine.json")
    except OSError as exc:
        raise ValueError(f"permit files unreadable: {exc}") from exc
    manifest = load_manifest(manifest_path)
    inv = manifest.get("invariants")
    if type(inv) is not dict or type(inv.get("session_analytics")) is not dict:
        raise ValueError("manifest has no session_analytics invariant: baseline was run without --snapshot")
    db = _existing_db(db_path)
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as probe:
        before = _counts(probe)
    if not any(before.values()):
        return _resume(db, manifest, folder, permit, state)
    return _delete(db, manifest_path, folder, manifest, permit, state, before)
