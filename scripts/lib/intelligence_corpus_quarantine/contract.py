"""One Q1 manifest schema shared by writing, loading and validation."""
import json
from datetime import datetime
from pathlib import Path

from atomic_io import atomic_write_text

TRACK = "intelligence-corpus-quarantine"
SCHEMA = 1
DB_FIELDS = dict(path=str, bytes=int, page_size=int, page_count=int, freelist_count=int,
                 user_version=int, journal_mode=str, integrity_check=str)
MANIFEST_FIELDS = dict(schema=int, track=str, generated_at=str, db=dict, tables=list)
TABLE_FIELDS = dict(name=str, rows=int, dbstat_bytes=int)


def _fields(value, fields):
    if type(value) is not dict or value.keys() != fields.keys():
        raise ValueError(f"expected manifest fields: {', '.join(fields)}")
    for name, kind in fields.items():
        item = value[name]
        if type(item) is not kind or (kind is str and not item) or (kind is int and name != "user_version" and item < 0):
            raise ValueError(f"invalid manifest field: {name}")


def validate_manifest(manifest):
    """Return the manifest; raise ValueError on missing, malformed or unknown evidence."""
    _fields(manifest, MANIFEST_FIELDS)
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
    return manifest


def load_manifest(path):
    """Load UTF-8 JSON and validate by default, with no parse-only bypass."""
    return validate_manifest(json.loads(Path(path).read_text(encoding="utf-8")))


def write_manifest(path, manifest):
    """Validate, fsync and atomically replace using the shared atomic writer."""
    validate_manifest(manifest)
    text = json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n"
    atomic_write_text(Path(path), text)
