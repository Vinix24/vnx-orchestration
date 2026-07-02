"""Regression: the dashboard /state/ static mapper must not allow path traversal.

Guards against the absolute-anchor bypass where GET /state//etc/passwd (or the
%2F-encoded form) made translate_path() reset to an absolute path outside the
state directory, yielding unauthenticated arbitrary file read.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(_DASHBOARD))

import serve_dashboard  # noqa: E402


class _Handler(serve_dashboard.DashboardHandler):
    def __init__(self):  # bypass SimpleHTTPRequestHandler socket setup
        pass


def _translate(path: str) -> str:
    return _Handler().translate_path(path)


def _inside_state_dirs(p: str) -> bool:
    resolved = Path(p).resolve()
    for base in (serve_dashboard.CANONICAL_STATE_DIR, serve_dashboard.LEGACY_STATE_DIR):
        base_resolved = base.resolve()
        if resolved == base_resolved or base_resolved in resolved.parents:
            return True
    return False


@pytest.mark.parametrize(
    "evil",
    [
        "/state//etc/passwd",
        "/state/%2Fetc%2Fpasswd",
        "/state/%2F%2Fetc%2Fpasswd",
        "/state/%2F..%2F..%2Fetc%2Fpasswd",
        "/state/../../etc/passwd",
        "/state/../../../../../../etc/passwd",
    ],
)
def test_state_traversal_blocked(evil: str) -> None:
    result = _translate(evil)
    # Never resolve to the real target...
    assert Path(result).resolve() != Path("/etc/passwd")
    # ...and always stay confined to a state dir.
    assert _inside_state_dirs(result), f"{evil!r} escaped to {result!r}"


def test_legit_state_path_still_served() -> None:
    # Behaviour preservation: a normal state file maps under the state dir.
    result = _translate("/state/dashboard_status.json")
    assert result.endswith("dashboard_status.json")
    assert _inside_state_dirs(result)
