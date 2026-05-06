#!/usr/bin/env python3
"""close_resolved_threshold_ois.py — One-shot cleanup of stale threshold OIs.

Many file-size and function-size threshold OIs in the open-items store reference
file paths inside *deleted or superseded worktrees* (e.g. `vnx-night-w3-refactor-b/`).
The underlying issue was resolved in a different PR (e.g. W1A/W1B/W1C refactor sprint
in main on 2026-05-01), but the OIs were never auto-closed because the rescan logic
keys on the original (now-stale) path string.

This script:
  1. Loads open blocker/warn-severity OIs whose title says "exceeds threshold".
  2. For each, extracts the file basename and (if applicable) the symbol name.
  3. Re-checks the canonical file at the main worktree path:
       - File-size OIs: close if file is now under the threshold OR file no longer exists.
       - Function-size OIs: close if the function no longer exceeds the threshold OR
         the function no longer exists (renamed/extracted/moved).
  4. Closes resolved OIs with an audit-trail reason.
  5. Prints a summary.

Idempotent — running twice is safe (already-closed OIs are skipped).

Usage:
    python3 scripts/maintenance/close_resolved_threshold_ois.py [--dry-run] [--apply]

Requires --apply to actually close OIs; default is dry-run safety.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OPEN_ITEMS_FILE = REPO_ROOT / ".vnx-data" / "state" / "open_items.json"
OPEN_ITEMS_CLI = REPO_ROOT / "scripts" / "open_items_manager.py"

FILE_BLOCKER_THRESHOLDS = {".py": 500, ".sh": 500, ".ts": 400, ".tsx": 400, ".js": 400}
FUNC_BLOCKER_THRESHOLD_PY = 70
FUNC_BLOCKER_THRESHOLD_SH = 50

TITLE_FILE_RX = re.compile(r"File exceeds (?:blocking|warning) threshold:\s*(\d+) lines")
TITLE_FUNC_RX = re.compile(r"Function exceeds (?:blocking|warning) threshold:\s*(\d+) lines")
DETAILS_FILE_RX = re.compile(r"file=([^,\s]+)")
DETAILS_SYMBOL_RX = re.compile(r"symbol=([\w_]+)")


def load_items() -> list[dict]:
    data = json.loads(OPEN_ITEMS_FILE.read_text(encoding="utf-8"))
    return data.get("items", data) if isinstance(data, dict) else data


def canonical_path(orig_path: str) -> Path:
    """Map any worktree-prefixed path to the main-repo equivalent.

    If the input is already repo-relative (e.g. ``scripts/foo.py``) it is
    returned unchanged. Only when the input contains a ``/Development/...``
    absolute prefix do we strip both the dev-root prefix and the worktree
    directory name to recover the repo-relative path.
    """
    if "/Development/" not in orig_path:
        return REPO_ROOT / orig_path
    rel = orig_path.split("/Development/", 1)[1]
    rel = rel.split("/", 1)[1] if "/" in rel else rel
    return REPO_ROOT / rel


def py_function_lines(file_path: Path, symbol: str) -> int | None:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            return (node.end_lineno or 0) - node.lineno + 1
    return None


def sh_function_lines(file_path: Path, symbol: str) -> int | None:
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    pattern = re.compile(rf"^{re.escape(symbol)}\s*\(\)\s*\{{", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    start = text.count("\n", 0, m.start())
    depth = 0
    for i, line in enumerate(text.split("\n")[start:], start=start):
        depth += line.count("{") - line.count("}")
        if depth == 0 and i > start:
            return i - start + 1
    return None


def classify(item: dict) -> tuple[str, str] | None:
    """Return ('resolved'|'still-violating'|'unknown', reason) for a threshold OI."""
    title = item.get("title", "")
    details = item.get("details", "")
    file_match = DETAILS_FILE_RX.search(details)
    if not file_match:
        return None
    orig_path = file_match.group(1)
    canon = canonical_path(orig_path)

    if not canon.exists():
        return ("resolved", f"file no longer exists at canonical path: {canon}")

    title_func = TITLE_FUNC_RX.search(title)
    title_file = TITLE_FILE_RX.search(title)
    sym_match = DETAILS_SYMBOL_RX.search(details)

    if title_func and sym_match:
        symbol = sym_match.group(1)
        if canon.suffix == ".py":
            length = py_function_lines(canon, symbol)
            threshold = FUNC_BLOCKER_THRESHOLD_PY
        elif canon.suffix == ".sh":
            length = sh_function_lines(canon, symbol)
            threshold = FUNC_BLOCKER_THRESHOLD_SH
        else:
            return ("unknown", f"unsupported file type for function check: {canon.suffix}")
        if length is None:
            return ("resolved", f"function {symbol!r} not found in {canon.name} (renamed/extracted/moved)")
        if length <= threshold:
            return ("resolved", f"function {symbol!r} now {length} lines (was {title_func.group(1)}, threshold {threshold})")
        return ("still-violating", f"function {symbol!r} still {length} lines (threshold {threshold})")

    if title_file:
        try:
            length = sum(1 for _ in canon.open(encoding="utf-8"))
        except OSError:
            return ("resolved", f"file unreadable: {canon}")
        threshold = FILE_BLOCKER_THRESHOLDS.get(canon.suffix, 500)
        if length <= threshold:
            return ("resolved", f"file {canon.name} now {length} lines (was {title_file.group(1)}, threshold {threshold})")
        return ("still-violating", f"file {canon.name} still {length} lines (threshold {threshold})")

    return ("unknown", "could not parse threshold from title")


def close_oi(oi_id: str, reason: str) -> bool:
    proc = subprocess.run(
        ["python3", str(OPEN_ITEMS_CLI), "close", oi_id, "--reason", reason],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return proc.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually close OIs (default is dry-run)")
    args = parser.parse_args()

    items = load_items()
    threshold_ois = [
        it for it in items
        if it.get("status") == "open"
        and ("exceeds" in it.get("title", "").lower())
    ]

    print(f"Inspecting {len(threshold_ois)} open 'exceeds threshold' OIs...\n")

    by_action: dict[str, list[tuple[str, str]]] = {"resolved": [], "still-violating": [], "unknown": []}
    for it in threshold_ois:
        result = classify(it)
        if result is None:
            by_action["unknown"].append((it["id"], "no file= in details"))
            continue
        action, reason = result
        by_action[action].append((it["id"], reason))

    for action, entries in by_action.items():
        print(f"=== {action.upper()} ({len(entries)}) ===")
        for oi_id, reason in entries[:50]:
            print(f"  {oi_id}: {reason[:120]}")
        if len(entries) > 50:
            print(f"  ... and {len(entries) - 50} more")
        print()

    if args.apply:
        print("Applying closures...")
        ok = 0
        for oi_id, reason in by_action["resolved"]:
            if close_oi(oi_id, f"Threshold cleanup: {reason}"):
                ok += 1
        print(f"\nClosed {ok}/{len(by_action['resolved'])} resolved OIs.")
        return 0
    else:
        print(f"DRY RUN — would close {len(by_action['resolved'])} OIs. Re-run with --apply to actually close.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
