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


# --- Symlink escapes ---------------------------------------------------------
# translate_path() returns a path that SimpleHTTPRequestHandler opens. When every candidate was
# rejected it used to return the canonical candidate anyway ("a path that does not exist"), which
# handed a symlink pointing outside the state dir straight to the handler. The fix must map that
# case to a path the handler answers 404 for, never to the rejected candidate.

def _state_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> "tuple[Path, Path]":
    """Point the module's state dirs at fresh temp dirs for one test."""
    canonical = tmp_path / "canonical_state"
    legacy = tmp_path / "legacy_state"
    canonical.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(serve_dashboard, "CANONICAL_STATE_DIR", canonical)
    monkeypatch.setattr(serve_dashboard, "LEGACY_STATE_DIR", legacy)
    return canonical, legacy


def _outside_sentinel(tmp_path: Path) -> Path:
    """A file outside both state dirs that a leaked path would expose."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "secret.txt"
    sentinel.write_text("outside the state dir\n")
    return sentinel


def _serves_sentinel(translated: str, sentinel: Path) -> bool:
    """True when the translated path (what the handler opens) yields the sentinel's content."""
    try:
        return Path(translated).read_bytes() == sentinel.read_bytes()
    except OSError:
        return False


def test_state_symlink_escape_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An escaping symlink in the canonical dir is refused, with the legacy dir empty."""
    canonical, _legacy = _state_dirs(tmp_path, monkeypatch)
    sentinel = _outside_sentinel(tmp_path)
    (canonical / "leak.txt").symlink_to(sentinel)

    result = _translate("/state/leak.txt")

    assert not _serves_sentinel(result, sentinel), f"leaked {sentinel} via {result!r}"
    assert _inside_state_dirs(result), f"escaped to {result!r}"
    assert not Path(result).exists(), f"rejected candidate returned anyway: {result!r}"


def test_state_symlink_escape_blocked_in_both_state_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two escaping symlinks (canonical and legacy) are both refused."""
    canonical, legacy = _state_dirs(tmp_path, monkeypatch)
    sentinel = _outside_sentinel(tmp_path)
    (canonical / "leak.txt").symlink_to(sentinel)
    (legacy / "leak.txt").symlink_to(sentinel)

    result = _translate("/state/leak.txt")

    assert not _serves_sentinel(result, sentinel), f"leaked {sentinel} via {result!r}"
    assert _inside_state_dirs(result)
    assert not Path(result).exists(), f"rejected candidate returned anyway: {result!r}"


def test_legacy_file_still_served_when_canonical_symlink_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused canonical symlink must not hide a real file of the same name in legacy."""
    canonical, legacy = _state_dirs(tmp_path, monkeypatch)
    sentinel = _outside_sentinel(tmp_path)
    (canonical / "status.json").symlink_to(sentinel)
    legacy_file = legacy / "status.json"
    legacy_file.write_text('{"source": "legacy"}\n')

    result = _translate("/state/status.json")

    assert Path(result).resolve() == legacy_file.resolve()
    assert not _serves_sentinel(result, sentinel)
