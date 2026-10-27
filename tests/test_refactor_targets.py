"""Refactor targets stay under their raw-line budget, measured through
scripts/lib/refactor_census.py on the working tree, never hard-coded.
close_track_if_done was 349 raw lines on origin/main before
dlv-92a8405367ec moved its close-time revalidation out; the budget is 200.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "scripts" / "lib") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import refactor_census  # noqa: E402

RAW_LIMIT = 200
TARGETS = {
    "close_track_if_done": "scripts/lib/track_reconciler_closure.py",
    "run_reconcile": "scripts/lib/objective_reconcile.py",
}


def _raw_length(path: str, qualname: str) -> int:
    """The census's raw length; its own AST span when below census reporting."""
    census = refactor_census.census(str(_ROOT), paths="scripts/lib")
    for item in census["long_functions"]["items"]:
        if (item["path"], item["qualname"]) == (path, qualname):
            return item["raw_lines"]
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"), filename=path)
    nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == qualname
    ]
    assert len(nodes) == 1, f"expected exactly one {qualname} in {path}, found {len(nodes)}"
    return nodes[0].end_lineno - nodes[0].lineno + 1


@pytest.mark.parametrize("qualname", sorted(TARGETS), ids=sorted(TARGETS))
def test_function_under_200_raw_lines(qualname: str) -> None:
    path = TARGETS[qualname]
    raw = _raw_length(path, qualname)
    assert raw < RAW_LIMIT, (
        f"{path}::{qualname} is {raw} raw lines (limit {RAW_LIMIT}); "
        f"extract the next block instead of extending it"
    )


def test_close_track_if_done_after_s3() -> None:
    """S3 ratchet: gates and phase walk extracted, so close_track_if_done must
    now sit under 150 raw lines and at most 70 executable lines (was 165 raw /
    77 executable before the split)."""
    path = TARGETS["close_track_if_done"]
    raw = _raw_length(path, "close_track_if_done")
    measured = refactor_census.measure_source(
        (_ROOT / path).read_text(encoding="utf-8"), filename=path
    )
    matches = [f for f in measured if f.qualname == "close_track_if_done"]
    assert len(matches) == 1, f"expected exactly one close_track_if_done, found {len(matches)}"
    executable = matches[0].executable_lines
    assert raw < 150, (
        f"{path}::close_track_if_done is {raw} raw lines after S3 (limit 150); "
        f"executable={executable} (limit 70)"
    )
    assert executable <= 70, (
        f"{path}::close_track_if_done is {executable} executable lines after S3 "
        f"(limit 70); raw={raw} (limit 150)"
    )
