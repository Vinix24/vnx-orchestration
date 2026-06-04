"""CLI entry point — score every document in a SQLite database.

    python3 cli.py --db documents.db --project-id default

Applies the idempotent document_scores migration, runs score_all, prints the
``{"scored": N, "skipped": M}`` result as JSON on stdout, and exits 0 on success
or 1 on any uncaught error.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import scorer

MIGRATION = Path(__file__).resolve().parent / "migrations" / "001_add_document_scores.sql"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply the idempotent migration so document_scores is guaranteed present."""
    conn.executescript(MIGRATION.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score all documents in a SQLite database."
    )
    parser.add_argument("--db", required=True, help="Path to the documents SQLite database.")
    parser.add_argument(
        "--project-id", default="default", help="Project scope for the scores."
    )
    args = parser.parse_args(argv)

    with sqlite3.connect(args.db, timeout=30) as conn:
        _ensure_schema(conn)
        result = scorer.score_all(conn, project_id=args.project_id)

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - top-level guard: any failure is exit 1
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
