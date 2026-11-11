"""Rehearse: run baseline, quarantine, verify, purge and restore on a VACUUM INTO copy of the live store and compare.

Decision 8 of the plan: the same functions as the live path, no second implementation. The live store is only read (a
sha256 before and after is in the verdict); the given manifest and artefact are only read, for the counts, digests and
rowid range the live permit carries. Everything the stages write lives in the work directory (the copy, its snapshot,
its own permit directory and its own audit directory); the one `rehearsed` line goes to the real audit directory.
A verdict that is not entirely true is exit 4; a refusal before the copy was written is a ValueError (exit 3).
"""
import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import audit
from .baseline import measure_baseline
from .contract import CORPUS_TABLES, load_quarantine, write_manifest
from .purge import _counts, _family_bytes, _session_whole, purge
from .snapshot import create_snapshot, file_sha256, table_evidence
from .store import _existing_db, _quote, quarantine, restore
from .verify import verify

COPY_NAME = "copy.db"
FLAGS = ("rows_equal", "rowids_equal", "digests_equal", "fts_match_ok", "session_equal", "purge_empty", "live_unchanged")


def _work_dir(requested, folder):
    """A new or empty directory outside the permit dir (so never inside the sealed artifact/); raise ValueError otherwise."""
    permit_dir = folder.parent
    if requested is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        work = permit_dir.parent / f"rehearse-{stamp}"
    else:
        work = Path(requested).expanduser().resolve()
    if work == permit_dir or permit_dir in work.parents:
        raise ValueError(f"work directory {work} lies inside the permit dir {permit_dir}")
    if work.exists() and (not work.is_dir() or any(work.iterdir())):
        raise ValueError(f"work directory {work} exists and is not an empty directory")
    return work


def _rowids(conn):
    found = {}
    for name in CORPUS_TABLES:
        digest, rows, low, high = hashlib.sha256(), 0, None, None
        for (rowid,) in conn.execute(f"SELECT rowid FROM {_quote(name)} ORDER BY rowid"):
            digest.update(str(rowid).encode() + b",")
            rows, low, high = rows + 1, rowid if rows == 0 else low, rowid
        found[name] = dict(rows=rows, row_min=low, row_max=high, sha256=digest.hexdigest())
    return found


def _measure(copy, session):
    """Counts, content digests, rowids, the session digest and the corpus family bytes of the copy, in one read transaction."""
    with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        return dict(counts=_counts(conn), digests=table_evidence(conn, CORPUS_TABLES), rowids=_rowids(conn),
                    session=_session_whole(conn, session), family_bytes=_family_bytes(conn),
                    orphans=conn.execute("SELECT count(*) FROM snippet_metadata m LEFT JOIN code_snippets s "
                                         "ON m.snippet_rowid = s.rowid WHERE s.rowid IS NULL").fetchone()[0])


def _pipeline(copy, work, found, stage):
    """The five stages on the copy, with the same functions the live run uses; found collects what each stage measured."""
    permit_dir, state = work / "permit", work / "state"
    permit_dir.mkdir()
    state.mkdir()
    stage[0] = "baseline"
    manifest = measure_baseline(copy, snapshot=work / "snapshot.db")
    manifest_path = permit_dir / "manifest.json"
    write_manifest(manifest_path, manifest)
    session = manifest["invariants"]["session_analytics"]
    stage[0] = "quarantine"
    document_path = quarantine(copy, permit_dir, state_dir=state)
    found["before"] = _measure(copy, session)
    stage[0] = "verify"
    verify(copy, manifest_path, document_path.parent, state_dir=state)
    stage[0] = "purge"
    purge(copy, manifest_path, document_path.parent, state_dir=state)
    found["purged"] = _measure(copy, session)
    stage[0] = "restore"
    found["proof"] = restore(copy, document_path, state_dir=state)
    found["after"] = _measure(copy, session)


def _same(*values):
    return all(value == values[0] for value in values)


def _verdict(found, document, live):
    """Every flag is true only on positive evidence: a stage that did not run leaves its flag false."""
    before, purged, after, proof = (found.get(k) for k in ("before", "purged", "after", "proof"))
    tables = {t["name"]: t for t in document["tables"]}
    ran = before is not None and purged is not None and after is not None and proof is not None
    flags = dict.fromkeys(FLAGS, False)
    if ran:
        wanted = {n: t["rows"] for n, t in tables.items()}
        flags.update(
            rows_equal=_same(before["counts"], after["counts"], wanted),
            rowids_equal=before["rowids"] == after["rowids"] and all(
                (after["rowids"][n]["row_min"], after["rowids"][n]["row_max"]) == (t["row_min"], t["row_max"])
                for n, t in tables.items()),
            digests_equal=all(_same(before["digests"][n]["digest_sha256"], after["digests"][n]["digest_sha256"],
                                    t["content_digest"]) for n, t in tables.items()),
            fts_match_ok=type(proof.get("match_probe")) is int and proof["match_probe"] > 0,
            session_equal=_same(before["session"], purged["session"], after["session"]),
            purge_empty=all(count == 0 for count in purged["counts"].values()) and len(purged["counts"]) == len(CORPUS_TABLES))
    flags["live_unchanged"] = live[0] == live[1]
    orphans = after["orphans"] if ran else None
    return dict(flags, orphans=orphans, ok=all(flags.values()) and type(orphans) is int and orphans == 0)


def _discard(copy):
    """Remove the copy and the sidecar names SQLite gives it, nothing else: explicit names, no glob (decision 22)."""
    for name in (copy.name, copy.name + "-wal", copy.name + "-shm", copy.name + "-journal"):
        copy.with_name(name).unlink(missing_ok=True)  # only the copy this run created in its own work directory


def _run(db, folder, work):
    """Copy the live store (read-only source), run the pipeline on the copy; return (found, failure, copy summary, live sha)."""
    work.mkdir(parents=True, exist_ok=True)
    live_before = file_sha256(db)
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as source:
        copy = create_snapshot(source, work / COPY_NAME)
    copy_path = Path(copy["path"])
    copy_path.chmod(0o600)  # create_snapshot seals its copy at 0400; the stages must write to this one
    found, stage, failure = {}, ["copy"], None
    try:
        _pipeline(copy_path, work, found, stage)
    except Exception as exc:  # a stage that raises is a failed rehearsal with a verdict, never a crash
        failure = dict(failed_stage=stage[0], error=f"{type(exc).__name__}: {exc}")
    return found, failure, copy_path, (live_before, file_sha256(db))


def rehearse(db_path, manifest_path, quarantine_dir, *, state_dir, work_dir=None, discard_copy=False):
    """Rehearse the purge on a copy of db_path and return the verdict; raise ValueError for a refusal before the copy was written."""
    folder, state = Path(quarantine_dir).expanduser().resolve(), Path(state_dir).expanduser().resolve()
    audit.prepare(state, folder.parent)  # B4: the audit never lands in the permit dir
    try:
        permit = audit.permit_sha256(Path(manifest_path).expanduser().resolve(), folder / "quarantine.json")
        document = load_quarantine(folder / "quarantine.json")
    except OSError as exc:
        raise ValueError(f"permit files unreadable: {exc}") from exc
    db = _existing_db(db_path)
    work = _work_dir(work_dir, folder)
    found, failure, copy_path, live = _run(db, folder, work)
    verdict = _verdict(found, document, live)
    if failure is not None:
        verdict.update(failure, ok=False)
    verdict.update(live_sha256_before=live[0], live_sha256_after=live[1], copy=str(copy_path), work_dir=str(work),
                   copy_discarded=bool(discard_copy),
                   counts={k: found[k]["counts"] for k in ("before", "purged", "after") if k in found},
                   family_bytes={k: found[k]["family_bytes"] for k in ("before", "purged", "after") if k in found})
    if discard_copy:
        _discard(copy_path)
    try:
        audit.append(state, "rehearsed", permit, "ok" if verdict["ok"] else "refused", **verdict)
    except Exception as exc:
        raise audit.AfterCommitError(f"the rehearsal ran (copy {copy_path}, ok={verdict['ok']}) "
                                     f"but its rehearsed audit line was not written: {exc}") from exc
    return verdict
