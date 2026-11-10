"""Write the corpus into a sealed artefact, prove its restore path before the seal, and restore it."""
import hashlib
import os
import re
import shlex
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

from atomic_io import atomic_write_text

from . import audit
from .contract import CORPUS_TABLES, SCHEMA, SEAL, TRACK, artifact_table, load_quarantine, write_quarantine
from .digest import table_digest
from .snapshot import file_sha256

PROBE_EDGE, PROBE_MODULUS, PROBE_WORDS = 20, 9973, 8
_TOKEN = re.compile(r"[^\W_]")
LEESMIJ = """# Quarantaine van het intelligence-corpus

Dit is een verplaatsing, geen verwijdering. `artifact/` (verzegeld: map 0555, bestanden 0444) bevat `{artifact}` met {rows} uit `{db}`, elk met de eigen rowid, en `quarantine.json` met de inhoudsdigest per tabel. `manifest.json` is de nulmeting van `baseline`. Verwijder hier niets: na de purge is dit het enige terugzetpad.

Terugzetten, vanuit de vnx-orchestration-checkout (één transactie; digest en MATCH-probe vóór de commit, bij elke afwijking terug en exit 3; een niet-lege doeltabel weigert zonder `--allow-existing`):

    python3 scripts/vnx_intelligence_corpus_quarantine.py restore --db {db_arg} --quarantine {q_arg} --state-dir {state_arg}
"""


def _existing_db(db_path):
    db = Path(db_path).expanduser().resolve()
    if not db.is_file():
        raise OSError("database is not a readable regular file")
    if db.stat().st_size == 0:
        raise ValueError("refusing 0-byte decoy database")
    return db


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def source_ddl_sha256(conn):
    """sha256 over the whitespace-normalised sqlite_master.sql of both corpus tables."""
    digest = hashlib.sha256()
    for name in CORPUS_TABLES:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if row is None or not isinstance(row[0], str):
            raise ValueError(f"database has no {name} table")
        digest.update(name.encode() + b"\n" + " ".join(row[0].split()).encode() + b"\n")
    return digest.hexdigest()


def _storage_classes(conn, table, columns):
    kinds = ", ".join(f"typeof({_quote(c)})" for c in columns)
    return conn.execute(f"SELECT rowid, {kinds} FROM {_quote(table)} ORDER BY rowid")


def _copy(source, artifact, name, columns):
    names = ", ".join(map(_quote, columns))
    artifact.execute(f"CREATE TABLE {_quote(artifact_table(name))} ({names})")  # no declared type: decision 19
    rows = source.execute(f"SELECT rowid, {names} FROM {_quote(name)} ORDER BY rowid")
    marks = ", ".join("?" * (len(columns) + 1))
    artifact.executemany(f"INSERT INTO {_quote(artifact_table(name))} (rowid, {names}) VALUES ({marks})", rows)


def _prove_artifact(source, path, tables):
    """Raise ValueError unless the artefact is intact and equal to the source, row by row."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as artifact:
        checks = artifact.execute("PRAGMA integrity_check").fetchall()
        if checks != [("ok",)]:
            raise ValueError(f"artifact integrity_check failed: {checks}")
        for table in tables:
            name, columns = table["name"], table["columns"]
            found = table_digest(artifact, artifact_table(name), columns, "rowid")
            if found["digest_sha256"] != table["content_digest"] or found["rows"] != table["rows"]:
                raise ValueError(f"artifact differs from source: {name} digest")
            pairs = zip_longest(_storage_classes(source, name, columns),
                                _storage_classes(artifact, artifact_table(name), columns))
            for left, right in pairs:
                if left != right:
                    raise ValueError(f"artifact differs from source: {name} storage class at {(left or right)[0]}")


def _probe_query(values):
    for value in values:  # None (NULL or blob) offers no probe text; a row without any is a miss
        words = [w for w in (value or "").split() if _TOKEN.search(w)][:PROBE_WORDS]
        if words:
            return " ".join('"' + w.replace('"', '""') + '"' for w in words)
    return None


def _probe_fts(conn, columns):
    """MATCH a fixed sample of art's corpus rowids against the FTS5 index of main.code_snippets."""
    table = _quote(artifact_table("code_snippets"))
    sample = sorted({r[0] for sql in (f"SELECT rowid FROM art.{table} ORDER BY rowid LIMIT {PROBE_EDGE}",
                                      f"SELECT rowid FROM art.{table} ORDER BY rowid DESC LIMIT {PROBE_EDGE}",
                                      f"SELECT rowid FROM art.{table} WHERE rowid % {PROBE_MODULUS} = 0")
                     for r in conn.execute(sql)})
    texts = ", ".join(f"CASE WHEN typeof({_quote(c)}) IN ('text','integer','real') "
                      f"THEN CAST({_quote(c)} AS TEXT) END" for c in columns)
    missed = []
    for rowid in sample:
        query = _probe_query(conn.execute(f"SELECT {texts} FROM art.{table} WHERE rowid=?", (rowid,)).fetchone())
        if query is None or conn.execute("SELECT count(*) FROM main.code_snippets "
                                         "WHERE code_snippets MATCH ? AND rowid=?", (query, rowid)).fetchone() != (1,):
            missed.append(rowid)
    if not sample or missed:
        raise ValueError(f"restore proof failed: MATCH probe tried {len(sample)} rowids, missed {missed[:20]}")
    return dict(match_probe=len(sample), ok=True)


def match_probe(artifact_path, fts_ddl, columns, workdir):
    """Re-index the artefact rows into a throwaway FTS5 and MATCH a fixed rowid sample."""
    if "using fts5" not in " ".join(fts_ddl.lower().split()):
        raise ValueError("code_snippets is not an FTS5 table")
    names = ", ".join(map(_quote, columns))
    table = _quote(artifact_table("code_snippets"))
    with tempfile.TemporaryDirectory(prefix=".quarantine-probe-", dir=workdir) as scratch, \
            closing(sqlite3.connect(Path(scratch, "probe.db"), uri=True)) as conn:
        conn.execute(fts_ddl)
        conn.execute("ATTACH DATABASE ? AS art", (artifact_path.as_uri() + "?mode=ro",))
        conn.execute(f"INSERT INTO main.code_snippets (rowid, {names}) SELECT rowid, {names} FROM art.{table}")
        return _probe_fts(conn, columns)


def _write_artifact(db, target):
    """Copy and measure both corpus tables in one read transaction on a read-only source."""
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(target)) as artifact:
        source.execute("BEGIN")
        ddl_sha256, tables = source_ddl_sha256(source), []
        for name in CORPUS_TABLES:
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({_quote(name)})")]
            found = table_digest(source, name, columns, "rowid")
            tables.append(dict(name=name, rows=found["rows"], row_min=found["row_min"], row_max=found["row_max"],
                               columns=columns, content_digest=found["digest_sha256"]))
            _copy(source, artifact, name, columns)
        artifact.commit()
        _prove_artifact(source, target, tables)
        fts_ddl = source.execute("SELECT sql FROM sqlite_master WHERE name='code_snippets'").fetchone()[0]
    return ddl_sha256, tables, fts_ddl


def _fsync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _paperwork(db, out, document_path, document, state_dir):
    """After the seal: LEESMIJ.md (0444) in the permit dir, then the quarantined audit line."""
    try:
        rows = {f"{t['name']}_rows": t["rows"] for t in document["tables"]}
        readme = out / "LEESMIJ.md"
        atomic_write_text(readme, LEESMIJ.format(
            artifact=document["artifact"], db=db,
            rows=" en ".join(f"{t['rows']} rijen `{t['name']}`" for t in document["tables"]),
            db_arg=shlex.quote(str(db)), q_arg=shlex.quote(str(document_path)), state_arg=shlex.quote(str(state_dir))))
        readme.chmod(0o444)
        permit = audit.permit_sha256(out / "manifest.json", document_path)
        audit.append(state_dir, "quarantined", permit, "ok", artifact_sha256=document["artifact_sha256"], **rows)
    except Exception as exc:
        raise audit.AfterCommitError(f"artifact sealed and valid in {document_path.parent}, "
                                     f"but its LEESMIJ.md or quarantined audit line was not written: {exc}") from exc


def quarantine(db_path, out_dir, *, state_dir):
    """Write artifact/ under out_dir, prove it, seal it; raise ValueError on any refusal."""
    db = _existing_db(db_path)
    out = Path(out_dir).expanduser().resolve()
    state_dir = Path(state_dir).expanduser().resolve()
    audit.prepare(state_dir, out)
    folder = out / "artifact"
    try:
        folder.mkdir()
    except FileExistsError as exc:
        raise ValueError(f"refusing to overwrite existing {folder}") from exc
    now = datetime.now(timezone.utc)
    target = folder / f"quality_intelligence-corpus-{now:%Y%m%d}.db"
    document_path = folder / "quarantine.json"
    try:
        ddl_sha256, tables, fts_ddl = _write_artifact(db, target)
        proof = match_probe(target, fts_ddl, tables[0]["columns"], out.parent)
        _fsync(target)
        document = dict(schema=SCHEMA, track=TRACK, generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        artifact=target.name, artifact_bytes=target.stat().st_size,
                        artifact_sha256=file_sha256(target), source_ddl_sha256=ddl_sha256,
                        tables=tables, restore_proof=proof, seal=dict(SEAL))
        write_quarantine(document_path, document)
        for path in (target, document_path):
            path.chmod(0o444)
        _fsync(folder)
        folder.chmod(0o555)
    except BaseException:
        for path in (target, target.with_name(target.name + "-journal"), document_path):
            path.unlink(missing_ok=True)  # only the files this invocation created in its own new directory
        folder.rmdir()
        raise
    _paperwork(db, out, document_path, document, state_dir)  # outside the cleanup: the seal is never undone
    return document_path


def _refuse_occupied(conn, tables, allow_existing):
    """Without allow_existing any target row refuses; with it, any rowid the artefact also carries refuses."""
    for table in tables:
        name, art = _quote(table["name"]), _quote(artifact_table(table["name"]))
        columns = [row[1] for row in conn.execute(f"PRAGMA main.table_info({name})")]
        if columns != table["columns"]:
            raise ValueError(f"target {table['name']} columns {columns} differ from the artefact; nothing restored")
        if not allow_existing:
            if conn.execute(f"SELECT EXISTS (SELECT 1 FROM main.{name})").fetchone() != (0,):
                raise ValueError(f"target {table['name']} is not empty; nothing restored (--allow-existing merges)")
            continue
        clash = [r[0] for r in conn.execute(f"SELECT rowid FROM art.{art} WHERE rowid IN "
                                            f"(SELECT rowid FROM main.{name}) ORDER BY rowid")]
        if clash:
            raise ValueError(f"rowid collision in {table['name']}: {len(clash)} rowids {clash[:20]}; nothing restored")


def _insert_and_prove(conn, tables):
    """Insert every artefact row with its own rowid, then prove digest and index inside the transaction."""
    for table in tables:
        name, art = _quote(table["name"]), _quote(artifact_table(table["name"]))
        names = ", ".join(map(_quote, table["columns"]))
        conn.execute(f"INSERT INTO main.{name} (rowid, {names}) SELECT rowid, {names} FROM art.{art} ORDER BY rowid")
    for table in tables:  # the restored rowids: the range without the rows that were already there
        found = table_digest(conn, table["name"], table["columns"], "rowid",
                             where=f"t.rowid IN (SELECT rowid FROM art.{_quote(artifact_table(table['name']))})")
        if (found["digest_sha256"], found["rows"]) != (table["content_digest"], table["rows"]):
            raise ValueError(f"restore proof failed: {table['name']} digest differs from quarantine.json; rolled back")
    return _probe_fts(conn, tables[0]["columns"])


def restore(db_path, quarantine_path, *, state_dir, allow_existing=False):
    """Put the artefact rows back in one transaction that proves itself before COMMIT; refusals change nothing."""
    document_path = Path(quarantine_path).expanduser().resolve()
    document = load_quarantine(document_path)
    artifact = document_path.parent / document["artifact"]
    if artifact.stat().st_size != document["artifact_bytes"] or file_sha256(artifact) != document["artifact_sha256"]:
        raise ValueError(f"artifact does not match quarantine.json: {artifact}")
    manifest = audit.prepare(state_dir, document_path.parent.parent)
    permit = audit.permit_sha256(manifest, document_path)
    db = _existing_db(db_path)
    with closing(sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, isolation_level=None)) as conn:
        conn.execute("ATTACH DATABASE ? AS art", (artifact.as_uri() + "?mode=ro",))
        conn.execute("BEGIN IMMEDIATE")
        try:
            _refuse_occupied(conn, document["tables"], allow_existing)
            proof = _insert_and_prove(conn, document["tables"])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    rows = {t["name"]: t["rows"] for t in document["tables"]}
    try:
        audit.append(state_dir, "restored", permit, "ok", artifact_sha256=document["artifact_sha256"],
                     match_probe=proof["match_probe"], **{f"{n}_rows": k for n, k in rows.items()})
    except Exception as exc:
        raise audit.AfterCommitError(f"rows restored and committed, but the audit line was not written: {exc}") from exc
    return dict(rows=rows, match_probe=proof["match_probe"], db=str(db))
