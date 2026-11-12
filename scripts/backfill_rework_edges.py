#!/usr/bin/env python3
"""One-time fill of the declared rework edges of one project into ``rework_edges``.

Reads the dispatch spec bundles (``dispatch-spec.json``) and the receipts
(``t0_receipts.ndjson``) of one project and writes the edges they declare:

  * ``round``  : the spec/receipt names a predecessor (``parent_dispatch``, or ``work_ref``
                 ``dispatch/<X>``) and no OI or PR origin.
  * ``oi_fix`` : the spec/receipt also carries ``origin_oi``.
  * ``pr_fix`` : the spec/receipt also carries ``origin_pr``.

An entry with both ``origin_oi`` and ``origin_pr``, an unusable id, or a self-reference is
counted as skipped, never guessed. All declared edges are class ``rework`` (ADR-071 decision 4).
``blame`` edges are not written here.

Usage:
    python3 scripts/backfill_rework_edges.py --project-id vnx-dev --data-dir <dir>            # dry-run (default)
    python3 scripts/backfill_rework_edges.py --project-id vnx-dev --data-dir <dir> --apply

Only adds rows to ``rework_edges``; idempotent (fill-once key). No LLM, no Anthropic SDK.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Iterator, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from rework_attribution import EDGE_KINDS, record_edge

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REF_PREFIXES = ("refs/heads/", "origin/")


def _predecessor(entry: dict, own_id: str) -> Optional[str]:
    """The declared origin dispatch: explicit parent_dispatch, else the work_ref branch."""
    parent = entry.get("parent_dispatch")
    if isinstance(parent, str) and parent.strip():
        return parent.strip()
    ref = entry.get("work_ref")
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    for prefix in _REF_PREFIXES:
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
    if ref.startswith("dispatch/") and ref[len("dispatch/"):] != own_id:
        return ref[len("dispatch/"):]
    return None


def _kind_and_evidence(entry: dict) -> "tuple[Optional[str], dict]":
    oi, pr = entry.get("origin_oi"), entry.get("origin_pr")
    has_oi = isinstance(oi, (str, int)) and not isinstance(oi, bool) and str(oi).strip() != ""
    has_pr = isinstance(pr, (str, int)) and not isinstance(pr, bool) and str(pr).strip() != ""
    if has_oi and has_pr:
        return None, {}
    if has_oi:
        return "oi_fix", {"origin_oi": str(oi).strip()}
    if has_pr:
        return "pr_fix", {"origin_pr": str(pr).strip()}
    return "round", {}


def _iter_specs(data_dir: Path, project_id: str) -> Iterator[dict]:
    for path in sorted((data_dir / "dispatches").glob("**/dispatch-spec.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            yield {"_malformed": True}
            continue
        if not isinstance(raw, dict):
            yield {"_malformed": True}
        elif raw.get("project_id") == project_id:
            yield raw


def _iter_receipts(state_dir: Path, project_id: str) -> Iterator[dict]:
    path = state_dir / "t0_receipts.ndjson"
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                yield {"_malformed": True}
                continue
            if not isinstance(raw, dict):
                yield {"_malformed": True}
            elif raw.get("project_id") == project_id:
                yield raw


def collect_edges(data_dir: Path, project_id: str) -> "tuple[list[dict], Counter]":
    """Declared edges (spec first, so a spec wins over its receipt) plus skip counts."""
    edges: list[dict] = []
    skipped: Counter = Counter()
    for source, entries in (
        ("spec", _iter_specs(data_dir, project_id)),
        ("receipt", _iter_receipts(data_dir / "state", project_id)),
    ):
        for entry in entries:
            if entry.get("_malformed"):
                skipped[f"{source}_malformed"] += 1
                continue
            own = entry.get("dispatch_id")
            if not isinstance(own, str) or not _ID_RE.match(own):
                if _predecessor(entry, "") is not None:
                    skipped[f"{source}_bad_dispatch_id"] += 1
                continue
            origin = _predecessor(entry, own)
            if origin is None:
                continue
            if not _ID_RE.match(origin) or origin == own:
                skipped[f"{source}_bad_origin"] += 1
                continue
            kind, evidence = _kind_and_evidence(entry)
            if kind is None:
                skipped[f"{source}_ambiguous_origin"] += 1
                continue
            edges.append({"rework": own, "origin": origin, "kind": kind, "source": source, "evidence": evidence})
    return edges, skipped


def run(data_dir: Path, qi_path: Path, project_id: str, *, apply: bool) -> dict:
    edges, skipped = collect_edges(data_dir, project_id)
    counts = {kind: 0 for kind in EDGE_KINDS}
    written = {kind: 0 for kind in EDGE_KINDS}
    for edge in edges:
        counts[edge["kind"]] += 1
    if apply:
        conn = sqlite3.connect(str(qi_path))
        try:
            for edge in edges:
                if record_edge(
                    conn, project_id, edge["rework"], edge["origin"], edge["kind"], "rework",
                    source=edge["source"], evidence=edge["evidence"],
                ):
                    written[edge["kind"]] += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"project_id": project_id, "apply": apply, "declared": counts, "written": written, "skipped": dict(skipped)}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--data-dir", type=Path, required=True, help="the project's data dir (holds dispatches/ and state/)")
    parser.add_argument("--db", type=Path, default=None, help="quality_intelligence.db (default: <data-dir>/state/quality_intelligence.db)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the counts, write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="write the edges")
    args = parser.parse_args(argv)

    qi_path = args.db or args.data_dir / "state" / "quality_intelligence.db"
    if args.apply and not qi_path.is_file():
        print(f"backfill_rework_edges: no database at {qi_path}", file=sys.stderr)
        return 1
    try:
        result = run(args.data_dir, qi_path, args.project_id, apply=args.apply)
    except sqlite3.Error as exc:
        print(f"backfill_rework_edges: write failed, nothing committed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
