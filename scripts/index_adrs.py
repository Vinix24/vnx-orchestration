#!/usr/bin/env python3
"""Index all ADR markdown files into the adrs table + FTS5.

ADR-005: NDJSON event written BEFORE SQLite commit; raises on event-write failure.
ADR-007: composite PK (adr_id, project_id); project_id stamped explicitly.
Idempotent: skips ADRs whose source_hash matches the stored value.

Usage:
    python3 scripts/index_adrs.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

_DEFAULT_PROJECT_ID = "vnx-dev"


def _parse_adr(file_path: Path, project_id: str = _DEFAULT_PROJECT_ID) -> dict:
    stem = file_path.stem  # e.g. ADR-007-multitenant-project-id-stamping
    parts = stem.split("-")
    adr_id = f"{parts[0]}-{parts[1]}"  # ADR-007

    content = file_path.read_text(encoding="utf-8")
    source_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    lines = content.splitlines()

    # Title: first # heading
    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    # Status: from **Status:** line or front-matter 'Status: X'
    status = "Proposed"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("**Status:**"):
            status = stripped.split("**Status:**", 1)[1].strip().strip("*")
            break
        m = re.match(r"^Status:\s*(.+)$", stripped, re.IGNORECASE)
        if m:
            status = m.group(1).strip()
            break

    # Parse markdown sections by '## ' headers
    sections: dict[str, str] = {}
    current_section: Optional[str] = None
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current_section is not None:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = line[3:].strip()
            current_lines = []
        elif current_section is not None:
            current_lines.append(line)
    if current_section is not None:
        sections[current_section] = "\n".join(current_lines).strip()

    decision_text = sections.get("Decision") or sections.get("decision") or ""
    decision_summary = decision_text

    # Extract binding_rules: bullet lines from Decision section
    binding_rules = _extract_bullets(decision_text)

    # applies_to_tables / applies_to_skills: parse if present
    applies_to_tables = _extract_bullets(sections.get("Applies to tables", ""))
    applies_to_skills = _extract_bullets(sections.get("Applies to skills", ""))

    # triggers: regex patterns from 'When to apply' section if present
    triggers = _extract_bullets(sections.get("When to apply", ""))

    return {
        "adr_id": adr_id,
        "project_id": project_id,
        "status": status,
        "title": title,
        "decision_summary": decision_summary,
        "binding_rules": json.dumps(binding_rules),
        "applies_to_tables": json.dumps(applies_to_tables),
        "applies_to_skills": json.dumps(applies_to_skills),
        "triggers": json.dumps(triggers),
        "file_path": str(file_path),
        "source_hash": source_hash,
    }


def _extract_bullets(text: str) -> list[str]:
    bullets = []
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s+(.+)$", line)
        if m:
            bullets.append(m.group(1).strip())
    return bullets


def _write_ndjson_event(events_file: Path, adr: dict) -> None:
    record_id = hashlib.sha256(
        f"{adr['adr_id']}:{adr['source_hash']}".encode()
    ).hexdigest()
    event = {
        "record_id": record_id,
        "event_type": "adr_indexed",
        "adr_id": adr["adr_id"],
        "project_id": adr["project_id"],
        "source_hash": adr["source_hash"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }
    events_file.parent.mkdir(parents=True, exist_ok=True)
    # ADR-005: raise on failure, never swallow
    with open(events_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _upsert_adr(conn: sqlite3.Connection, adr: dict) -> bool:
    existing = conn.execute(
        "SELECT source_hash FROM adrs WHERE adr_id = ? AND project_id = ?",
        (adr["adr_id"], adr["project_id"]),
    ).fetchone()

    if existing and existing[0] == adr["source_hash"]:
        return False  # idempotent skip

    conn.execute(
        """
        INSERT INTO adrs
            (adr_id, project_id, status, title, decision_summary,
             binding_rules, applies_to_tables, applies_to_skills,
             triggers, file_path, source_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(adr_id, project_id) DO UPDATE SET
            status           = excluded.status,
            title            = excluded.title,
            decision_summary = excluded.decision_summary,
            binding_rules    = excluded.binding_rules,
            applies_to_tables = excluded.applies_to_tables,
            applies_to_skills = excluded.applies_to_skills,
            triggers         = excluded.triggers,
            file_path        = excluded.file_path,
            indexed_at       = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            source_hash      = excluded.source_hash
        """,
        (
            adr["adr_id"],
            adr["project_id"],
            adr["status"],
            adr["title"],
            adr["decision_summary"],
            adr["binding_rules"],
            adr["applies_to_tables"],
            adr["applies_to_skills"],
            adr["triggers"],
            adr["file_path"],
            adr["source_hash"],
        ),
    )
    return True


def index_adrs(
    db_path: Path,
    adr_dir: Path,
    events_file: Path,
    project_id: str = _DEFAULT_PROJECT_ID,
) -> dict:
    """Index all ADR-*.md files from adr_dir into db_path.

    Returns a summary dict with 'indexed', 'skipped', 'total' counts.
    Raises on NDJSON event-write failure (ADR-005).
    """
    adr_files = sorted(adr_dir.glob("ADR-*.md"))
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # manual transaction control

    indexed = 0
    skipped = 0

    for md_file in adr_files:
        adr = _parse_adr(md_file, project_id=project_id)

        # ADR-005: NDJSON event BEFORE SQLite commit; raise on failure
        _write_ndjson_event(events_file, adr)

        conn.execute("BEGIN")
        try:
            changed = _upsert_adr(conn, adr)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if changed:
            indexed += 1
        else:
            skipped += 1

    conn.close()
    return {"indexed": indexed, "skipped": skipped, "total": len(adr_files)}


def main() -> None:
    paths = ensure_env()
    vnx_home = Path(paths["VNX_HOME"])
    state_dir = Path(paths["VNX_STATE_DIR"])

    db_path = state_dir / "quality_intelligence.db"
    adr_dir = vnx_home / "docs" / "governance" / "decisions"
    events_file = Path(paths["VNX_DATA_DIR"]) / "events" / "adr_index.ndjson"

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path} — run quality_db_init.py first")
    if not adr_dir.exists():
        raise SystemExit(f"ADR directory not found: {adr_dir}")

    result = index_adrs(db_path, adr_dir, events_file)
    print(
        f"ADR indexing complete: {result['indexed']} indexed, "
        f"{result['skipped']} skipped, {result['total']} total"
    )


if __name__ == "__main__":
    main()
