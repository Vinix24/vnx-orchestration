"""The permit (all nine requirements): manifest, artefact and live store must describe exactly the same rows.

One function, two callers: `verify` (read-only, no delete anywhere near it) and Q5's purge, which passes the connection of its
own write transaction. A requirement is true on positive evidence only: a missing field, None, a wrong type, a malformed file
or an unknown value refuses (exit 3) and names its requirement number.
"""
import json
import re
import sqlite3
from contextlib import closing
from itertools import zip_longest
from pathlib import Path

from . import audit
from .contract import CORPUS_TABLES, SCHEMA, SEAL, TRACK, artifact_table, validate_manifest, validate_quarantine
from .digest import table_digest
from .snapshot import file_sha256
from .store import _quote, source_ddl_sha256

_NO_PERMIT = "0" * 64  # audit label of a refusal whose files are too broken to hash


class Refusal(ValueError):
    """A permit requirement is not met; ValueError so the CLI maps it to exit 3."""

    def __init__(self, requirement, reason):
        super().__init__(f"permit refused, requirement {requirement}: {reason}")
        self.requirement = requirement


class AuditWriteError(RuntimeError):
    """The audit line could not be written (exit 1, never a silent success)."""


def _get(doc, path, kind, req):
    """doc[path] of exactly type kind (bool is not int); missing, None or another type refuses."""
    value = doc
    for key in path.split("."):
        value = value.get(key) if type(value) is dict else None
    if type(value) is not kind:
        raise Refusal(req, f"{path} is missing, None or not a {kind.__name__}")
    return value


def _hex(doc, path, req):
    value = _get(doc, path, str, req)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise Refusal(req, f"{path} is not a sha256 hex digest")
    return value


def _load(path, label):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal(1, f"{label} is missing or not parseable JSON: {exc}") from exc
    if type(document) is not dict:
        raise Refusal(1, f"{label} is not a JSON object")
    return document


def _open_ro(path, req, what):
    try:
        uri = Path(path).as_uri() + "?mode=ro"  # ValueError: a relative path has no file URI
    except ValueError as exc:
        raise Refusal(req, f"{what} path {str(path)!r} cannot be opened as a database: {exc}") from exc
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("BEGIN")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            conn.close()
            raise Refusal(req, f"{what} integrity_check is not ok")
        return conn
    except sqlite3.Error as exc:
        raise Refusal(req, f"{what} does not open as a database: {exc}") from exc


def _columns(conn, name):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({_quote(name)})")]


def _first_difference(left, left_table, right, right_table, columns):
    """The first rowid whose values or storage classes differ between two tables, or None."""
    picked = ", ".join(f"{_quote(c)}, typeof({_quote(c)})" for c in columns)
    rows = [c.execute(f"SELECT rowid, {picked} FROM {_quote(t)} ORDER BY rowid") for c, t in ((left, left_table), (right, right_table))]
    return next(((a or b)[0] for a, b in zip_longest(*rows) if a != b), None)


def _envelope(manifest, quarantine):
    """Requirement 1: both documents parse, schema == 1 and both carry the track constant (B3)."""
    for label, document in (("manifest.json", manifest), ("quarantine.json", quarantine)):
        if _get(document, "schema", int, 1) != SCHEMA or _get(document, "track", str, 1) != TRACK:
            raise Refusal(1, f"{label} has an unknown schema or track")


def _artifact(quarantine, folder):
    """Requirement 2: relative artefact name, size and sha256 measured on the file."""
    name = _get(quarantine, "artifact", str, 2)
    if name in ("", ".", "..") or Path(name).name != name or name.startswith("/"):
        raise Refusal(2, f"artifact must be a file name relative to quarantine.json, without ..: {name!r}")
    size, sha = _get(quarantine, "artifact_bytes", int, 2), _hex(quarantine, "artifact_sha256", 2)
    path = folder / name
    try:
        if path.stat().st_size != size or file_sha256(path) != sha:
            raise Refusal(2, f"artifact {name} differs from artifact_bytes or artifact_sha256")
    except OSError as exc:
        raise Refusal(2, f"artifact {name} is unreadable: {exc}") from exc
    return path


def _seal(path, folder):
    """The seal as quarantine set it: folder 0555, artefact and quarantine.json 0444."""
    dir_mode, file_mode = int(SEAL["dir_mode"], 8), int(SEAL["file_mode"], 8)
    for target, mode in ((folder, dir_mode), (path, file_mode), (folder / "quarantine.json", file_mode)):
        if target.stat().st_mode & 0o777 != mode:
            raise Refusal(3, f"seal broken: {target.name} is not {mode:04o}")


def _measure(conn, table, columns, want, who):  # table_digest of table; Refusal 4 when missing, unreadable or other columns
    try:
        got = table_digest(conn, table, columns, "rowid") if _columns(conn, table) == columns else None
    except (sqlite3.Error, ValueError):
        got = None
    if got is None:
        raise Refusal(4, f"{who} table {table} is missing, unreadable or its columns differ from quarantine.json")
    return got, [field for field in want if got[field] != want[field]]


def _tables(live, art, quarantine):
    """Requirement 4: per corpus table the live table, its tables[] entry and the artifact's copy agree (see check_permit)."""
    entries = _get(quarantine, "tables", list, 4)  # shape first: a name is only a key once it is a corpus table string
    if any(type(e) is not dict or type(e.get("name")) is not str or e["name"] not in CORPUS_TABLES for e in entries):
        raise Refusal(4, "quarantine.json tables[] has an entry that is not a corpus table object")
    found = {e["name"]: e for e in entries}
    rows = {}
    for name in CORPUS_TABLES:
        table = found.get(name)
        if table is None:
            raise Refusal(4, f"quarantine.json has no entry for {name}")
        columns, digest = _get(table, "columns", list, 4), _hex(table, "content_digest", 4)
        bounds = [table.get(k) for k in ("rows", "row_min", "row_max")]
        if type(bounds[0]) is not int or any(type(b) not in (int, type(None)) for b in bounds[1:]):
            raise Refusal(4, f"{name} rows, row_min or row_max are missing or malformed")
        want = dict(zip(("rows", "row_min", "row_max"), bounds), digest_sha256=digest)
        _, differs = _measure(art, artifact_table(name), columns, want, "artifact")  # measured, not trusted from the metadata
        if differs:
            raise Refusal(4, f"artifact {artifact_table(name)} differs from quarantine.json in {', '.join(differs)}")
        measured, differs = _measure(live, name, columns, want, "live")
        if differs:
            raise Refusal(4, f"{name} live rows differ from the artefact, first at rowid "
                          f"{_first_difference(live, name, art, artifact_table(name), columns)} (B1)")
        rows[name] = measured["rows"]
    return rows


def _session(live, manifest):
    """Requirement 6: session_analytics is exactly as the baseline measured it (B2)."""
    inv = _get(manifest, "invariants.session_analytics", dict, 6)
    columns, digest, rows = _get(inv, "columns", list, 6), _hex(inv, "digest_sha256", 6), _get(inv, "rows", int, 6)
    bound = int if rows else type(None)
    max_id, sum_id = _get(inv, "max_id", bound, 6), _get(inv, "sum_id", bound, 6)
    now = _columns(live, "session_analytics")
    if now != columns:
        raise Refusal(6, f"session_analytics columns changed: {sorted(set(now) ^ set(columns))}")
    measured = table_digest(live, "session_analytics", columns, "id", where=None if max_id is None else f"t.id <= {max_id}")
    live_rows, live_sum = live.execute("SELECT count(*), sum(id) FROM session_analytics").fetchone()
    if (measured["digest_sha256"], live_rows, live_sum) != (digest, rows, sum_id):
        snapshot = manifest.get("snapshot")  # the baseline rows live in the snapshot; it only names the id
        try:
            with closing(sqlite3.connect(Path(snapshot["path"]).as_uri() + "?mode=ro", uri=True)) as snap:
                drift, note = _first_difference(live, "session_analytics", snap, "session_analytics", columns), ""
        except (sqlite3.Error, OSError, KeyError, TypeError, ValueError) as exc:
            drift, note = None, f" (snapshot unreadable: {exc})"
        raise Refusal(6, f"session_analytics changed since the baseline, another writer: "
                         f"id {drift} is missing, added or changed{note}")
    return live_rows


def _snapshot(manifest):
    """Requirement 8: the snapshot file is the one the manifest recorded, intact and readable."""
    snap = _get(manifest, "snapshot", dict, 8)
    raw_path, size, sha = _get(snap, "path", str, 8), _get(snap, "bytes", int, 8), _hex(snap, "sha256", 8)
    path = Path(raw_path)
    if not path.is_absolute() or "\0" in raw_path:  # the baseline writes an absolute path; anything else is a foreign manifest
        raise Refusal(8, f"snapshot.path must be an absolute path without NUL: {raw_path!r}")
    if _get(snap, "integrity_check", str, 8) != "ok" or size == 0:
        raise Refusal(8, "snapshot integrity_check is not ok or bytes is 0")
    try:
        if path.stat().st_size != size or file_sha256(path) != sha:
            raise Refusal(8, "snapshot bytes or sha256 differ from the manifest")
    except OSError as exc:
        raise Refusal(8, f"snapshot is unreadable: {exc}") from exc
    _open_ro(path, 8, "snapshot").close()
    return sha


def _check(live, db, manifest_path, folder):
    manifest, quarantine = _load(manifest_path, "manifest.json"), _load(folder / "quarantine.json", "quarantine.json")
    if Path(db).stat().st_size == 0:
        raise Refusal(9, "database is 0 bytes (decoy)")
    _envelope(manifest, quarantine)  # 1
    artifact = _artifact(quarantine, folder)  # 2
    if _get(quarantine, "seal", dict, 3) != SEAL:
        raise Refusal(3, "quarantine.json names an unknown seal")
    _seal(artifact, folder)  # seal_ok
    with closing(_open_ro(artifact, 3, "artifact")) as art:  # 3
        corpus_rows = _tables(live, art, quarantine)  # 4
    wanted_ddl = _hex(quarantine, "source_ddl_sha256", 5)  # 5
    try:
        ddl_equal = source_ddl_sha256(live) == wanted_ddl
    except ValueError as exc:
        raise Refusal(5, f"live DDL of the corpus tables is unreadable: {exc}") from exc
    if not ddl_equal:
        raise Refusal(5, "live DDL of code_snippets and snippet_metadata differs from the artefact's source")
    session_rows = _session(live, manifest)  # 6
    recorded = _get(manifest, "corpus.rows", dict, 7)
    if any(type(recorded.get(n)) is not int or recorded[n] != corpus_rows[n] for n in CORPUS_TABLES):
        raise Refusal(7, f"live corpus rows {corpus_rows} differ from manifest.corpus.rows")
    snapshot_sha = _snapshot(manifest)  # 8
    if _get(manifest, "db.bytes", int, 9) <= 0:  # 9
        raise Refusal(9, "manifest db.bytes is not positive (decoy)")
    try:
        validate_manifest(manifest)
        validate_quarantine(quarantine)
    except ValueError as exc:
        raise Refusal(1, f"document shape: {exc}") from exc
    return dict(rows_equal=True, digests_equal=True, ddl_equal=True, seal_ok=True, session_equal=True, snapshot_ok=True,
                requirements_checked=[1, 2, 3, 4, 5, 6, 7, 8, 9], rows=corpus_rows, session_rows=session_rows,
                snapshot_sha256=snapshot_sha, user_version=live.execute("PRAGMA user_version").fetchone()[0],  # recorded, never a gate (decision 5)
                artifact_sha256=quarantine["artifact_sha256"],
                permit_sha256=audit.permit_sha256(manifest_path, folder / "quarantine.json"))


def check_permit(db, manifest_path, quarantine_dir, *, conn=None):
    """Check requirements 1-9 of the permit; return the verdict dict when all hold, else raise Refusal naming the first that does not.

    conn is the caller's own connection (Q5, inside its write transaction); without it a read-only
    connection on db is opened in one read transaction. Nothing is written either way.

    Requirement 4 compares three things, not two: per corpus table the live table, tables[] in quarantine.json and the same
    table inside the artifact (opened read-only) agree on table_digest, rows, row_min, row_max and the column list. An
    artifact that lacks a table or differs refuses requirement 4 with a reason naming `artifact` and the field.
    """
    folder = Path(quarantine_dir).expanduser().resolve()
    if conn is not None:
        return _check(conn, db, manifest_path, folder)
    path = Path(db).expanduser().resolve()
    if not path.is_file():
        raise Refusal(9, "database is not a readable regular file")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as live:
        live.execute("BEGIN")
        return _check(live, path, manifest_path, folder)


def _record(state_dir, permit, status, **numbers):
    try:
        audit.append(state_dir, "verified", permit, status, **numbers)
    except Exception as exc:
        raise AuditWriteError(f"the verified/{status} audit line was not written: {exc}") from exc


def verify(db, manifest_path, quarantine_dir, *, state_dir):
    """check_permit plus exactly one audit line; a refusal re-raises, a failing audit write is AuditWriteError."""
    folder, state = Path(quarantine_dir).expanduser().resolve(), Path(state_dir).expanduser().resolve()
    permit_dir = folder.parent  # the same rule as audit.prepare: nothing of the audit may land anywhere in the permit dir
    if state == permit_dir or permit_dir in state.parents:
        raise ValueError(f"audit state dir {state} lies inside the permit dir {permit_dir} (B4)")
    try:
        permit = audit.permit_sha256(manifest_path, folder / "quarantine.json")
    except OSError:
        permit = _NO_PERMIT
    try:
        verdict = check_permit(db, manifest_path, folder)
    except Refusal as refusal:
        try:
            _record(state, permit, "refused", requirement=refusal.requirement, reason=str(refusal))
        except AuditWriteError as exc:
            raise AuditWriteError(f"{refusal}; {exc}") from refusal
        raise
    _record(state, permit, "ok", corpus_rows=verdict["rows"], session_rows=verdict["session_rows"],
            requirements_checked=verdict["requirements_checked"], user_version=verdict["user_version"])
    return verdict
