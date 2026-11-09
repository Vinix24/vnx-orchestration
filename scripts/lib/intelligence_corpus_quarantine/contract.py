"""One manifest schema shared by writing, loading and validation."""
import json
import re
from datetime import datetime
from pathlib import Path

from atomic_io import atomic_write_text

TRACK = "intelligence-corpus-quarantine"
SCHEMA = 1
DB_FIELDS = dict(path=str, bytes=int, page_size=int, page_count=int, freelist_count=int,
                 user_version=int, journal_mode=str, integrity_check=str)
MANIFEST_FIELDS = dict(schema=int, track=str, generated_at=str, db=dict, tables=list)
TABLE_FIELDS = dict(name=str, rows=int, dbstat_bytes=int)
CORPUS_TABLES = ["code_snippets", "snippet_metadata"]
Q2_FIELDS = dict(snapshot=dict, corpus=dict, invariants=dict)


def _fields(value, fields):
    if type(value) is not dict or value.keys() != fields.keys():
        raise ValueError(f"expected manifest fields: {', '.join(fields)}")
    for name, kind in fields.items():
        item = value[name]
        if type(item) is not kind or (kind is str and not item) or (kind is int and name != "user_version" and item < 0):
            raise ValueError(f"invalid manifest field: {name}")


def validate_manifest(manifest):
    """Return the manifest; raise ValueError on missing, malformed or unknown evidence."""
    extended = type(manifest) is dict and bool(manifest.keys() & Q2_FIELDS.keys())
    _fields(manifest, MANIFEST_FIELDS | Q2_FIELDS if extended else MANIFEST_FIELDS)
    if manifest["schema"] != SCHEMA or manifest["track"] != TRACK:
        raise ValueError("unsupported manifest schema or track")
    stamp = datetime.strptime(manifest["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
    if stamp.strftime("%Y-%m-%dT%H:%M:%SZ") != manifest["generated_at"]:
        raise ValueError("invalid generated_at timestamp")
    db = manifest["db"]
    _fields(db, DB_FIELDS)
    if not Path(db["path"]).is_absolute() or any(db[k] == 0 for k in ("bytes", "page_size", "page_count")):
        raise ValueError("invalid database path or size")
    if db["integrity_check"] != "ok" or db["journal_mode"] not in {"delete", "truncate", "persist", "memory", "wal", "off"}:
        raise ValueError("invalid database integrity_check or journal_mode")
    if db["freelist_count"] > db["page_count"]:
        raise ValueError("invalid database freelist_count")
    names = []
    for table in manifest["tables"]:
        _fields(table, TABLE_FIELDS)
        names.append(table["name"])
    if "code_snippets" not in names or len(names) != len(set(names)):
        raise ValueError("missing code_snippets or duplicate tables")
    if extended:
        _validate_snapshot(manifest["snapshot"])
        validate_corpus(manifest["corpus"])
        _fields(manifest["invariants"], dict(session_analytics=dict))
        validate_session(manifest["invariants"]["session_analytics"])
        for name in CORPUS_TABLES:
            if not any(t == dict(name=name, rows=manifest["corpus"]["rows"][name],
                                dbstat_bytes=manifest["corpus"]["dbstat_bytes"][name]) for t in manifest["tables"]):
                raise ValueError("corpus disagrees with tables")
    return manifest


def _sha256(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid sha256")


def _validate_snapshot(snapshot):
    _fields(snapshot, dict(path=str, bytes=int, sha256=str, integrity_check=str))
    _sha256(snapshot["sha256"])
    if not Path(snapshot["path"]).is_absolute() or snapshot["bytes"] == 0 or snapshot["integrity_check"] != "ok":
        raise ValueError("invalid snapshot path, bytes or integrity_check")


def validate_session(session):
    bound_type = int if type(session) is dict and session.get("rows") else type(None)
    _fields(session, dict(rows=int, max_id=bound_type, sum_id=bound_type, columns=list, digest_sha256=str))
    columns = session["columns"]
    if not columns or any(type(c) is not str or not c for c in columns) or "id" not in columns or len(set(columns)) != len(columns):
        raise ValueError("invalid session columns")
    _sha256(session["digest_sha256"])
    return session


def validate_corpus(corpus):
    _fields(corpus, dict(tables=list, rows=dict, dbstat_bytes=dict, rowid_range=dict,
        distinct_file_path=int, file_path_roots=dict, languages=dict, created_at=dict, join=dict))
    if corpus["tables"] != CORPUS_TABLES:
        raise ValueError("invalid corpus tables")
    for field in ("rows", "dbstat_bytes", "rowid_range"):
        _fields(corpus[field], dict.fromkeys(CORPUS_TABLES, dict if field == "rowid_range" else int))
    for table in CORPUS_TABLES:
        bounds = corpus["rowid_range"][table]
        _fields(bounds, dict.fromkeys(("min", "max"), int if corpus["rows"][table] else type(None)))
        if corpus["rows"][table] and bounds["min"] > bounds["max"]:
            raise ValueError("invalid rowid range")
    for field in ("file_path_roots", "languages"):
        histogram = corpus[field]
        if any(type(k) is not str or not k for k in histogram):
            raise ValueError(f"invalid {field} key")
        _fields(histogram, dict.fromkeys(histogram, int))
        if sum(histogram.values()) != corpus["rows"]["code_snippets"]:
            raise ValueError(f"incomplete {field}")
    _fields(corpus["created_at"], dict.fromkeys(("min", "max"), str if corpus["rows"]["snippet_metadata"] else type(None)))
    if corpus["rows"]["snippet_metadata"]:
        for value in corpus["created_at"].values():
            datetime.fromisoformat(value)
    _fields(corpus["join"], dict(orphans=int, duplicate_snippet_rowid=int))
    return corpus


def load_manifest(path):
    """Load UTF-8 JSON and validate by default, with no parse-only bypass."""
    return validate_manifest(json.loads(Path(path).read_text(encoding="utf-8")))


def write_manifest(path, manifest):
    """Validate, fsync and atomically replace using the shared atomic writer."""
    validate_manifest(manifest)
    text = json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n"
    atomic_write_text(Path(path), text)
