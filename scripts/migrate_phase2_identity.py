#!/usr/bin/env python3
"""Phase 6 P2 — Identity Layer migration scanner.

Lints the canonical Phase 6 P2 hot paths to confirm identity propagation is
wired in. P2 hardens three governance entry points:

* ``scripts/append_receipt.py`` (+ ``append_receipt_internals/payload.py``) —
  every NDJSON receipt should be attributable to the four-tuple identity.
* ``scripts/lib/dispatch_register.py`` — every dispatch_register event ditto.
* ``scripts/lib/subprocess_adapter.py`` (+ ``subprocess_dispatch_internals/
  delivery.py``) — workers spawned via Popen receive the orchestrator's
  identity through ``VNX_*_ID`` env vars.

The scanner walks each hot path file looking for either an import of
``vnx_identity`` or use of the canonical env-var names. If any hot path is
missing identity wiring, the file is reported as a missed call site and
the exit code is non-zero.

Other SQLite-touching scripts (read models, intelligence aggregators) are
deliberately out of scope — they inherit identity through env-var
propagation from their parent orchestrator/worker process. P3/P4 will
revisit centralized DB consolidation; until then, env inheritance is the
contract.

Run::

    python3 scripts/migrate_phase2_identity.py
    python3 scripts/migrate_phase2_identity.py --json
    python3 scripts/migrate_phase2_identity.py --all   # broader survey
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

IDENTITY_TOKENS = (
    "vnx_identity",
    "resolve_identity",
    "try_resolve_identity",
    "_stamp_identity",
    "_resolve_identity_for_register",
    "_build_worker_identity_env",
    "VNX_OPERATOR_ID",
    "VNX_PROJECT_ID",
    "operator_id",
    "project_id",
)

P2_HOT_PATHS = [
    "scripts/append_receipt.py",
    "scripts/lib/append_receipt_internals/payload.py",
    "scripts/lib/dispatch_register.py",
    "scripts/lib/subprocess_adapter.py",
    "scripts/lib/subprocess_dispatch_internals/delivery.py",
]

SQLITE_RE = re.compile(r"sqlite3\.(connect|Connection)\b")
SUBPROCESS_TOKENS = ("subprocess.Popen", "subprocess.run", "subprocess.call")


@dataclass
class Finding:
    path: str
    reason: str

    def display(self) -> str:
        return f"{self.path}: {self.reason}"


def _has_any(text: str, tokens: Iterable[str]) -> bool:
    return any(token in text for token in tokens)


def _scan_hot_path(rel_path: str) -> List[Finding]:
    full = REPO_ROOT / rel_path
    if not full.is_file():
        return [Finding(rel_path, "P2 hot path missing — file not found")]
    text = full.read_text(encoding="utf-8")
    if not _has_any(text, IDENTITY_TOKENS):
        return [Finding(rel_path, "P2 hot path lacks identity wiring (import vnx_identity or stamp env vars)")]
    return []


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if any(part.startswith(".") for part in path.relative_to(REPO_ROOT).parts):
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _broader_survey() -> List[Finding]:
    """Optional --all sweep: report every SQLite/subprocess script lacking identity awareness."""
    findings: List[Finding] = []
    for path in _iter_python_files(SCRIPTS_DIR):
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        touches_sqlite = bool(SQLITE_RE.search(text))
        spawns_subprocess = _has_any(text, SUBPROCESS_TOKENS)
        if not touches_sqlite and not spawns_subprocess:
            continue
        if _has_any(text, IDENTITY_TOKENS):
            continue
        rel = str(path.relative_to(REPO_ROOT))
        kind = "sqlite" if touches_sqlite else "subprocess"
        findings.append(Finding(rel, f"out-of-scope ({kind}) — env-var inheritance only; revisit in P3/P4"))
    return findings


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 6 P2 identity migration scanner")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON only")
    parser.add_argument("--all", action="store_true", help="Also list out-of-scope files for situational awareness")
    args = parser.parse_args(argv)

    findings: List[Finding] = []
    for rel in P2_HOT_PATHS:
        findings.extend(_scan_hot_path(rel))

    extra: List[Finding] = []
    if args.all:
        extra = _broader_survey()

    if args.json:
        print(
            json.dumps(
                {
                    "missed_p2_hot_paths": [asdict(f) for f in findings],
                    "out_of_scope_survey": [asdict(f) for f in extra],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        if not findings:
            print(f"[ok] migrate_phase2_identity: 0 missed call sites across {len(P2_HOT_PATHS)} P2 hot paths")
        else:
            print(f"[!] migrate_phase2_identity: {len(findings)} missed P2 call site(s)")
            for finding in findings:
                print(f"  - {finding.display()}")
        if args.all and extra:
            print(f"\n[~] out-of-scope survey: {len(extra)} non-P2 files lack identity wiring (P3/P4 territory)")
            for finding in extra:
                print(f"  - {finding.display()}")

    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
