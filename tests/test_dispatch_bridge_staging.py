"""tests/test_dispatch_bridge_staging.py — PR-12 bridge staging non-forgeability.

`stage_spec_bundle` is the FIRST writer of a door spec-bundle — the trust boundary
codex RED'd twice on PR-4. These tests pin the non-forgeable invariants the bridge
shipped WITHOUT tests (PR-1 of the PR-12 plan): traversal rejection via `_ID_RE`,
symlink-escape refusal on both the pending root and the bundle dir, the
instruction_sha256 bind over the written bytes, and ADR-007 project_id carriage.

All tests pass an explicit `data_dir=tmp_path` so the door's live `_resolve_data_dir`
is never touched — fully isolated.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_bridge  # noqa: E402

_GOOD_ID = "20260101-120000-feat"


def _stage(tmp_path, **over):
    base = dict(
        instruction_text="do the thing", dispatch_id=_GOOD_ID, role="dev",
        target_slot="T1", project_id="p1", provider="claude", data_dir=tmp_path,
    )
    base.update(over)
    return dispatch_bridge.stage_spec_bundle(**base)


# --- traversal: staging_id is _ID_RE-validated BEFORE any path join ---

@pytest.mark.parametrize("bad", ["../evil", "a/b", "foo/../bar", "..", "/abs", "a\x00b", ""])
def test_stage_rejects_unsafe_dispatch_id(tmp_path, bad):
    with pytest.raises(ValueError):
        _stage(tmp_path, dispatch_id=bad)


# --- happy path: a real promoted bundle under pending/<id>/ ---

def test_stage_writes_bundle_under_pending(tmp_path):
    spec = _stage(tmp_path)
    assert spec.name == "dispatch-spec.json"
    bundle = spec.parent
    assert bundle.name == _GOOD_ID
    assert bundle.parent == (tmp_path / "dispatches" / "pending")
    assert (bundle / "instruction.md").read_text(encoding="utf-8") == "do the thing"


def test_stage_spec_carries_project_id_and_staging_id(tmp_path):
    # ADR-007: the bundle carries project_id; staging_id is derived from dispatch_id.
    payload = json.loads(_stage(tmp_path).read_text(encoding="utf-8"))
    assert payload["project_id"] == "p1"
    assert payload["dispatch_id"] == _GOOD_ID
    assert payload["staging_id"] == _GOOD_ID
    assert payload["provider"] == "claude"


def test_stage_binds_instruction_sha_over_written_bytes(tmp_path):
    text = "exact bytes — TOCTOU-bound"
    payload = json.loads(_stage(tmp_path, instruction_text=text).read_text(encoding="utf-8"))
    assert payload["instruction_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_stage_normalizes_legacy_provider_alias(tmp_path):
    # legacy tmux-mode string normalizes to the closed Provider enum value
    payload = json.loads(_stage(tmp_path, provider="codex_cli").read_text(encoding="utf-8"))
    assert payload["provider"] == "codex"


# --- gate validation (OI-845): gate is a closed set, same pattern as provider ---

def test_stage_rejects_unknown_gate_name(tmp_path):
    # "codex" is not a real gate ("codex_gate" is) — this reproduced the incident:
    # a dispatch staged with gate="codex" silently never ran any gate.
    with pytest.raises(ValueError, match="codex"):
        _stage(tmp_path, gate="codex")


def test_stage_rejects_unknown_gate_lists_valid_names(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        _stage(tmp_path, gate="not-a-real-gate")
    message = str(excinfo.value)
    for valid in ("gemini_review", "codex_gate", "claude_github_optional", "ci_gate", "wiring_gate"):
        assert valid in message


@pytest.mark.parametrize(
    "valid_gate",
    ["gemini_review", "codex_gate", "claude_github_optional", "ci_gate", "wiring_gate"],
)
def test_stage_accepts_every_canonical_gate_name(tmp_path, valid_gate):
    payload = json.loads(_stage(tmp_path, gate=valid_gate).read_text(encoding="utf-8"))
    assert payload["gate"] == valid_gate


def test_stage_accepts_empty_gate_as_unset(tmp_path):
    payload = json.loads(_stage(tmp_path, gate="").read_text(encoding="utf-8"))
    assert payload["gate"] == ""


def test_stage_normalizes_planning_sentinel_to_empty_gate(tmp_path):
    # "planning" is the literal string dispatch_create.sh:365-367 stamps as its
    # no-gate default ("V8: No gate specified, defaulting to 'planning'") — that
    # value flows dispatch_lifecycle.sh -> runtime_core_cli.py -> dispatch_broker.py
    # (persisted into bundle.json) -> pool_worker_runner.py -> the door. No gate
    # runner has ever matched "planning"; it must stage as an empty (unset) gate,
    # not raise, or every dispatch created without an explicit --gate would REJECT
    # at the door.
    payload = json.loads(_stage(tmp_path, gate="planning").read_text(encoding="utf-8"))
    assert payload["gate"] == ""


def test_stage_normalizes_planning_sentinel_case_and_whitespace_insensitive(tmp_path):
    payload = json.loads(_stage(tmp_path, gate="  Planning  ").read_text(encoding="utf-8"))
    assert payload["gate"] == ""


@pytest.mark.parametrize("sentinel", sorted(dispatch_bridge._LEGACY_PHASE_SENTINELS))
def test_stage_normalizes_every_legacy_phase_sentinel_to_empty_gate(tmp_path, sentinel):
    # Every member of _LEGACY_PHASE_SENTINELS is a lifecycle PHASE name leaking into
    # the gate field (see dispatch_bridge._LEGACY_PHASE_SENTINELS docstring for the
    # producer of each), not a real review gate. "implementation" is
    # pr_queue_manager.py's `pr.get('gate', 'implementation')` default (lines
    # 870/997/1638) — same class of defect as "planning", different word.
    payload = json.loads(_stage(tmp_path, gate=sentinel).read_text(encoding="utf-8"))
    assert payload["gate"] == ""


@pytest.mark.parametrize("sentinel", sorted(dispatch_bridge._LEGACY_PHASE_SENTINELS))
def test_stage_normalizes_every_legacy_phase_sentinel_case_and_whitespace_insensitive(tmp_path, sentinel):
    payload = json.loads(_stage(tmp_path, gate=f"  {sentinel.upper()}  ").read_text(encoding="utf-8"))
    assert payload["gate"] == ""


# --- symlink escape: refused at WRITE time, not just read (defense-in-depth) ---

def test_stage_refuses_symlinked_pending_root_escape(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (data / "dispatches").mkdir()
    # pre-plant pending/ as a symlink escaping the data root
    (data / "dispatches" / "pending").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink escape|outside data root"):
        _stage(data, data_dir=data, dispatch_id="20260101-120000-sym")


def test_stage_refuses_symlinked_bundle_dir_escape(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    pending = data / "dispatches" / "pending"; pending.mkdir(parents=True)
    did = "20260101-120000-bdir"
    # pre-plant the id dir as a symlink escaping pending
    (pending / did).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes pending root"):
        _stage(data, data_dir=data, dispatch_id=did)


# --- bridge_dispatch surfaces a staging error as a clean exit 1, never a side door ---

def test_bridge_dispatch_rejects_unsafe_id_as_exit_1(tmp_path):
    rc = dispatch_bridge.bridge_dispatch(
        instruction_text="x", dispatch_id="../evil", role="dev",
        target_slot="T1", project_id="p1", data_dir=tmp_path, dry_run=True,
    )
    assert rc == 1  # clean reject — never falls back to a side-door delivery


# --- claude_headless lane_safety gate (OI-223): fail-closed by default, yaml-driven ---

def test_bridge_headless_blocked_by_default_before_staging(tmp_path, monkeypatch, capsys):
    """allow_headless=True without the override env var: REJECT before stage_spec_bundle
    ever runs — no bundle is written, no side-door path is reachable."""
    monkeypatch.delenv("VNX_OVERRIDE_CLAUDE_HEADLESS", raising=False)
    reached_staging = []
    monkeypatch.setattr(
        dispatch_bridge, "stage_spec_bundle",
        lambda **kw: reached_staging.append(kw),
    )
    rc = dispatch_bridge.bridge_dispatch(
        instruction_text="x", dispatch_id=_GOOD_ID, role="dev",
        target_slot="T1", project_id="p1", data_dir=tmp_path,
        allow_headless=True, headless_reason="benchmark", dry_run=True,
    )
    assert rc == 1
    assert reached_staging == []
    err = capsys.readouterr().err
    assert "headless-blocked" in err
    assert "VNX_OVERRIDE_CLAUDE_HEADLESS" in err


def test_bridge_headless_wrong_override_value_still_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_OVERRIDE_CLAUDE_HEADLESS", "true")  # only "1" opts in
    reached_staging = []
    monkeypatch.setattr(
        dispatch_bridge, "stage_spec_bundle",
        lambda **kw: reached_staging.append(kw),
    )
    rc = dispatch_bridge.bridge_dispatch(
        instruction_text="x", dispatch_id=_GOOD_ID, role="dev",
        target_slot="T1", project_id="p1", data_dir=tmp_path,
        allow_headless=True, headless_reason="benchmark", dry_run=True,
    )
    assert rc == 1
    assert reached_staging == []


def test_bridge_headless_override_reaches_staging(tmp_path, monkeypatch):
    """allow_headless=True + VNX_OVERRIDE_CLAUDE_HEADLESS=1: the gate is lifted and
    execution proceeds into stage_spec_bundle (a real bundle gets written).

    OI-1072: the bundle directory is cleaned up after run_dispatch returns,
    so the bundle must NOT exist after bridge_dispatch()."""
    monkeypatch.setenv("VNX_OVERRIDE_CLAUDE_HEADLESS", "1")
    import dispatch_cli

    captured_spec = {}

    def _mock_run_dispatch(spec_file, dry_run=False):
        # Capture and verify the bundle was correctly formed BEFORE cleanup
        bundle = spec_file.parent
        payload = json.loads((bundle / "dispatch-spec.json").read_text(encoding="utf-8"))
        captured_spec["allow_headless"] = payload["allow_headless"]
        captured_spec["bundle"] = bundle
        return 0

    monkeypatch.setattr(dispatch_cli, "run_dispatch", _mock_run_dispatch)

    rc = dispatch_bridge.bridge_dispatch(
        instruction_text="x", dispatch_id=_GOOD_ID, role="dev",
        target_slot="T1", project_id="p1", data_dir=tmp_path,
        allow_headless=True, headless_reason="benchmark", dry_run=True,
    )

    assert rc == 0
    assert captured_spec.get("allow_headless") is True
    # OI-1072: bundle must be cleaned up after dispatch
    assert not captured_spec["bundle"].exists()


def test_bridge_non_headless_dispatch_never_consults_the_gate(tmp_path, monkeypatch):
    """allow_headless absent (default False): the claude tmux subscription lane is
    unaffected — staging proceeds without the headless gate firing at all.

    OI-1072: the bundle directory is cleaned up after run_dispatch returns,
    so the bundle must NOT exist after bridge_dispatch()."""
    monkeypatch.delenv("VNX_OVERRIDE_CLAUDE_HEADLESS", raising=False)
    import dispatch_cli

    captured_spec = {}

    def _mock_run_dispatch(spec_file, dry_run=False):
        # Capture and verify the bundle was correctly formed BEFORE cleanup
        bundle = spec_file.parent
        payload = json.loads((bundle / "dispatch-spec.json").read_text(encoding="utf-8"))
        captured_spec["allow_headless"] = payload["allow_headless"]
        captured_spec["bundle"] = bundle
        return 0

    monkeypatch.setattr(dispatch_cli, "run_dispatch", _mock_run_dispatch)

    rc = dispatch_bridge.bridge_dispatch(
        instruction_text="x", dispatch_id=_GOOD_ID, role="dev",
        target_slot="T1", project_id="p1", data_dir=tmp_path, dry_run=True,
    )

    assert rc == 0
    assert captured_spec.get("allow_headless") is False
    # OI-1072: bundle must be cleaned up after dispatch
    assert not captured_spec["bundle"].exists()
