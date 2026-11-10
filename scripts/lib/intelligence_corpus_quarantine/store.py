"""Write the corpus into a sealed artefact and prove its restore path before the seal."""
import hashlib
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

from .contract import CORPUS_TABLES, SCHEMA, SEAL, TRACK, artifact_table, write_quarantine
from .digest import table_digest
from .snapshot import file_sha256

PROBE_EDGE, PROBE_MODULUS, PROBE_WORDS = 20, 9973, 8
_TOKEN = re.compile(r"[^\W_]")


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


def quarantine(db_path, out_dir):
    """Write artifact/ under out_dir, prove it, seal it; raise ValueError on any refusal."""
    db = Path(db_path).expanduser().resolve()
    if not db.is_file():
        raise OSError("database is not a readable regular file")
    if db.stat().st_size == 0:
        raise ValueError("refusing 0-byte decoy database")
    out = Path(out_dir).expanduser().resolve()
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
        return document_path
    except BaseException:
        for path in (target, target.with_name(target.name + "-journal"), document_path):
            path.unlink(missing_ok=True)  # only the files this invocation created in its own new directory
        folder.rmdir()
        raise
