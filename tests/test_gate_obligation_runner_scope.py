"""tests/test_gate_obligation_runner_scope.py — OI-1378: --since / --dispatch-prefix.

The obligation runner works but has never run write=True against the live
store: 328 pending obligations at once is weeks of backlog in one shot. This
pins the scope filter that lets the runner be pointed at a slice (the last
week, or one dispatch-id prefix) instead of the whole backlog.

Filter contract:
  - ``--since YYYY-MM-DD`` filters on ``declared_at``; falls back to the
    dispatch_id's ``YYYYMMDD-`` date prefix when ``declared_at`` is missing or
    unparseable; an obligation with neither is OUT of scope under an active
    ``--since`` (scope means scope, no silent inclusion).
  - ``--dispatch-prefix`` filters on a literal dispatch_id prefix.
  - Combinable as AND.
  - With neither flag, behavior is byte-for-byte unchanged: same
    ``obligations_seen`` and same ``pending_after`` as before scoping existed.
  - The scope itself (``since``, ``dispatch_prefix``, ``obligations_in_scope``)
    is reported in the JSON summary alongside the existing ``obligations_seen``.

Tests run against throwaway dirs under tmp_path — never ~/.vnx-data/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    STATUS_PENDING,
    obligation_path,
    register_obligation,
    update_obligation,
)


@pytest.fixture(autouse=True)
def _no_gh_network(monkeypatch):
    """This file exercises scope filtering only, never PR/branch resolution.

    OI-1388 defect 2 made ``run(write=False)`` walk the same PR-resolution
    decision tree a real run does (``resolve_pr_number`` — read-only ``gh``
    calls). Without this, an obligation with no cached ``pr_number`` would
    hit real ``gh``/``git`` subprocess calls against this checkout's own
    GitHub origin for a dispatch_id that was never real, and (since neither
    the PR nor the branch exists there) resolve to ``would_retire`` instead
    of the "no PR yet, branch still alive" wait these tests intend to fix
    scope filtering against. Pin PR resolution to a stable "awaiting, branch
    exists" outcome so every test's ``pending_after`` stays about scope, not
    about network state.
    """
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: None)
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, owner_repo: True)


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "obligations").mkdir(parents=True, exist_ok=True)
    return state_dir


def _make_obligation(
    state_dir: Path,
    *,
    dispatch_id: str,
    gate: str = "codex_gate",
    declared_at: str | None = "__default__",
) -> Path:
    """Register a pending obligation, optionally overriding declared_at.

    ``declared_at="__default__"`` leaves register_obligation's own timestamp;
    any other value (including None) is stamped over it via update_obligation
    so tests can simulate a missing/legacy declared_at field.
    """
    path = register_obligation(state_dir, dispatch_id=dispatch_id, gate=gate)
    assert path is not None
    if declared_at != "__default__":
        update_obligation(path, declared_at=declared_at)
    return path


def _read(state_dir: Path, dispatch_id: str) -> dict:
    import json

    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# --since
# ---------------------------------------------------------------------------


def test_since_excludes_older_and_keeps_newer(tmp_path):
    state_dir = _state_dir(tmp_path)
    _make_obligation(
        state_dir, dispatch_id="20260810-old-oblig", declared_at="2026-08-10T09:00:00Z",
    )
    _make_obligation(
        state_dir, dispatch_id="20260820-new-oblig", declared_at="2026-08-20T09:00:00Z",
    )

    summary = runner.run(state_dir, write=False, since="2026-08-14")

    assert summary["obligations_in_scope"] == 1
    assert summary["pending_after"] == 1
    dispatch_ids = {o["dispatch_id"] for o in summary["outcomes"]}
    assert dispatch_ids == {"20260820-new-oblig"}


# ---------------------------------------------------------------------------
# --dispatch-prefix
# ---------------------------------------------------------------------------


def test_dispatch_prefix_filters_by_prefix(tmp_path):
    state_dir = _state_dir(tmp_path)
    _make_obligation(state_dir, dispatch_id="20260820-q1-alpha")
    _make_obligation(state_dir, dispatch_id="20260820-q2-beta")

    summary = runner.run(state_dir, write=False, dispatch_prefix="20260820-q1")

    assert summary["obligations_in_scope"] == 1
    assert summary["pending_after"] == 1
    dispatch_ids = {o["dispatch_id"] for o in summary["outcomes"]}
    assert dispatch_ids == {"20260820-q1-alpha"}


# ---------------------------------------------------------------------------
# Combined: AND
# ---------------------------------------------------------------------------


def test_since_and_dispatch_prefix_combine_as_and(tmp_path):
    state_dir = _state_dir(tmp_path)
    # Matches both filters.
    _make_obligation(
        state_dir, dispatch_id="20260820-q1-match", declared_at="2026-08-20T09:00:00Z",
    )
    # Right prefix, wrong date (too old) — excluded by --since.
    _make_obligation(
        state_dir, dispatch_id="20260810-q1-tooold", declared_at="2026-08-10T09:00:00Z",
    )
    # Right date, wrong prefix — excluded by --dispatch-prefix.
    _make_obligation(
        state_dir, dispatch_id="20260820-other-branch", declared_at="2026-08-20T09:00:00Z",
    )

    summary = runner.run(
        state_dir, write=False, since="2026-08-14", dispatch_prefix="20260820-q1",
    )

    assert summary["obligations_in_scope"] == 1
    assert summary["pending_after"] == 1
    dispatch_ids = {o["dispatch_id"] for o in summary["outcomes"]}
    assert dispatch_ids == {"20260820-q1-match"}


# ---------------------------------------------------------------------------
# No flags: unchanged behavior
# ---------------------------------------------------------------------------


def test_no_flags_behaves_identically_to_unscoped(tmp_path):
    state_dir = _state_dir(tmp_path)
    _make_obligation(
        state_dir, dispatch_id="20260810-old-oblig", declared_at="2026-08-10T09:00:00Z",
    )
    _make_obligation(
        state_dir, dispatch_id="20260820-new-oblig", declared_at="2026-08-20T09:00:00Z",
    )
    _make_obligation(state_dir, dispatch_id="no-date-here-oblig", declared_at=None)

    baseline = runner.run(state_dir, write=False)
    scoped_but_open = runner.run(state_dir, write=False, since=None, dispatch_prefix=None)

    assert baseline["obligations_seen"] == 3
    assert baseline["pending_after"] == 3
    assert baseline["obligations_in_scope"] == baseline["obligations_seen"]
    assert scoped_but_open["obligations_seen"] == baseline["obligations_seen"]
    assert scoped_but_open["pending_after"] == baseline["pending_after"]


# ---------------------------------------------------------------------------
# declared_at fallback / missing-both exclusion
# ---------------------------------------------------------------------------


def test_missing_declared_at_falls_back_to_dispatch_id_date_prefix(tmp_path):
    state_dir = _state_dir(tmp_path)
    _make_obligation(state_dir, dispatch_id="20260810-nodate-oblig", declared_at=None)

    included = runner.run(state_dir, write=False, since="2026-08-05")
    excluded = runner.run(state_dir, write=False, since="2026-08-15")

    assert included["obligations_in_scope"] == 1
    assert included["pending_after"] == 1
    assert excluded["obligations_in_scope"] == 0
    assert excluded["pending_after"] == 0


def test_missing_both_is_out_of_scope_only_when_since_active(tmp_path):
    state_dir = _state_dir(tmp_path)
    _make_obligation(state_dir, dispatch_id="no-date-here-oblig", declared_at=None)

    without_since = runner.run(state_dir, write=False)
    with_since = runner.run(state_dir, write=False, since="2026-01-01")

    assert without_since["obligations_in_scope"] == 1
    assert without_since["pending_after"] == 1
    assert with_since["obligations_in_scope"] == 0
    assert with_since["pending_after"] == 0


def test_unparseable_declared_at_falls_back_to_dispatch_id_date_prefix(tmp_path):
    state_dir = _state_dir(tmp_path)
    _make_obligation(
        state_dir, dispatch_id="20260812-garbage-timestamp", declared_at="not-a-date",
    )

    included = runner.run(state_dir, write=False, since="2026-08-01")
    excluded = runner.run(state_dir, write=False, since="2026-08-13")

    assert included["obligations_in_scope"] == 1
    assert excluded["obligations_in_scope"] == 0


# ---------------------------------------------------------------------------
# Scope fields land in the JSON summary
# ---------------------------------------------------------------------------


def test_scope_fields_present_in_summary(tmp_path):
    state_dir = _state_dir(tmp_path)
    _make_obligation(
        state_dir, dispatch_id="20260820-q1-scoped", declared_at="2026-08-20T09:00:00Z",
    )
    _make_obligation(
        state_dir, dispatch_id="20260801-out-of-scope", declared_at="2026-08-01T09:00:00Z",
    )

    summary = runner.run(
        state_dir, write=False, since="2026-08-14", dispatch_prefix="20260820",
    )

    assert summary["since"] == "2026-08-14"
    assert summary["dispatch_prefix"] == "20260820"
    assert summary["obligations_in_scope"] == 1
    assert summary["obligations_seen"] == 2


def test_scope_fields_present_via_cli_json(tmp_path, capsys):
    state_dir = _state_dir(tmp_path)
    _make_obligation(
        state_dir, dispatch_id="20260820-cli-scoped", declared_at="2026-08-20T09:00:00Z",
    )

    rc = runner.main(
        [
            "--state-dir", str(state_dir),
            "--no-write", "--json",
            "--since", "2026-08-14",
            "--dispatch-prefix", "20260820",
        ]
    )
    out = capsys.readouterr().out
    import json

    payload = json.loads(out)
    assert payload["since"] == "2026-08-14"
    assert payload["dispatch_prefix"] == "20260820"
    assert payload["obligations_in_scope"] == 1
    assert rc == 11  # one obligation still pending within scope


# ---------------------------------------------------------------------------
# --since validation
# ---------------------------------------------------------------------------


def test_since_rejects_malformed_date(tmp_path, capsys):
    state_dir = _state_dir(tmp_path)
    import pytest

    with pytest.raises(SystemExit):
        runner.main(["--state-dir", str(state_dir), "--no-write", "--since", "not-a-date"])
    err = capsys.readouterr().err
    assert "--since" in err


def test_registered_obligation_status_untouched_by_scope_check(tmp_path):
    """Sanity: scope filtering happens before the terminal-status skip, and a
    freshly registered obligation is still pending — the filter itself never
    mutates records."""
    state_dir = _state_dir(tmp_path)
    _make_obligation(state_dir, dispatch_id="20260820-untouched")
    runner.run(state_dir, write=False, since="2026-08-01")
    assert _read(state_dir, "20260820-untouched")["status"] == STATUS_PENDING
