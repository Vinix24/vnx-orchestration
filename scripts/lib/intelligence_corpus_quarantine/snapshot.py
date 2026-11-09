"""Exclusive VACUUM INTO snapshots and verified reuse of recorded evidence."""
import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .contract import CORPUS_TABLES, load_manifest
from .digest import table_digest


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_snapshot(conn, target):
    path = Path(target).expanduser().absolute()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"snapshot target already exists: {path}") from exc
    os.close(fd)
    try:
        conn.execute("VACUUM INTO ?", (str(path),))
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as copy:
                checks = copy.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"snapshot integrity_check failed: {exc}") from exc
        if checks != [("ok",)]:
            raise ValueError(f"snapshot integrity_check failed: {checks}")
        with path.open("rb") as source:
            os.fsync(source.fileno())
        result = dict(path=str(path.resolve()), bytes=path.stat().st_size,
                      sha256=file_sha256(path), integrity_check="ok")
        path.chmod(0o444)
        return result
    except BaseException:
        path.unlink()  # Remove only the failed copy exclusively created by this invocation.
        raise


def verify_reused_corpus(conn, target):
    """Raise ValueError on corpus drift; caller holds the live read transaction."""
    path = Path(target).expanduser().resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as copy:
        copy.execute("BEGIN")
        for name in CORPUS_TABLES:
            evidence = []
            for source in (conn, copy):
                columns = [row[1] for row in source.execute(f"PRAGMA table_info({name})")]
                evidence.append(table_digest(source, name, columns, "rowid"))
            for field, label in (("rows", "rows"), ("digest_sha256", "digest")):
                if evidence[0][field] != evidence[1][field]:
                    raise ValueError(f"live corpus differs from the reused snapshot: {name} {label}")


def reuse_snapshot(target, manifest_path):
    try:
        snapshot = load_manifest(manifest_path)["snapshot"]
        path = Path(target).expanduser().resolve()
        if path != Path(snapshot["path"]).resolve():
            raise ValueError("snapshot path differs from manifest")
        if file_sha256(path) != snapshot["sha256"] or path.stat().st_size != snapshot["bytes"]:
            raise ValueError("snapshot hash or size mismatch")
        return snapshot
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"cannot reuse snapshot: {exc}") from exc
