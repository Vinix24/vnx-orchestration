"""Characterisation tests for build_runtime_snapshot's raw-model guard.

The guard (dispatch-agent-lane-coercion / OI-LANECOERCE, defence in depth) re-runs
the constraint check against ``spec.model`` when a worker model pin has already
replaced it with ``effective_model``, and folds every NEW blocking violation into
``constraint_verdicts`` behind a ``[raw-model guard] `` message prefix.

These tests run the real ``build_runtime_snapshot`` and patch only the constraint
check the module imports at call time (``providers.constraint_enforcer.check_constraints``)
— the same seam tests/test_dispatch_enforcement.py uses. The fake tells the two
calls apart by ``check_registry``: the primary check passes ``True``, the guard
passes ``False``.

They were written green on the pre-extraction code and stay green after the guard
moves into its own helper, proving the move is behaviour-neutral: the returned
verdict tuple is byte-identical, in the same order, for every spec.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SCRIPTS_LIB = _REPO_ROOT / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from dispatch_cli import (  # noqa: E402
    WORKER_CLAUDE_OVERRIDE_ENV,
    WORKER_CLAUDE_OVERRIDE_REASON_ENV,
    build_runtime_snapshot,
    load_spec,
)
from dispatch_plan import ConstraintVerdict  # noqa: E402
from dispatch_spec import Reject, validate  # noqa: E402


_TRACK_UNLINKED = ConstraintVerdict(
    code="track_unlinked",
    severity="warn",
    message="dispatch has no track_id (VNX_REQUIRE_DISPATCH_TRACK is OFF; advisory-only)",
)


def _make_bundle_spec(
    tmp_path: Path,
    *,
    target_slot: str = "T0",
    model: str | None = None,
) -> tuple[Path, Path]:
    """A promoted bundle whose spec lives inside it, so the staging-binding check
    passes and only the guard under test can add constraint verdicts.

    ``T0`` carries the floor pin ``opus-5-5`` (its own SSOT entry), so a spec that
    requests ``sonnet`` produces ``effective_model='opus-5-5'`` and
    ``raw_model='sonnet'`` — exactly the mismatch the guard exists for.
    """
    data_dir = tmp_path / "vnx-data"
    staging_id = "20261024-staging-raw-model-guard"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)
    inst = bundle_dir / "instruction.md"
    inst.write_text("# Raw-model guard probe\n\nRoute one task.\n", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20261024-raw-model-guard",
        "staging_id": staging_id,
        "instruction_file": str(inst),
        "role": "backend-developer",
        "target_slot": target_slot,
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "model": model,
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("VNX_REQUIRE_DISPATCH_TRACK", raising=False)
    monkeypatch.delenv(WORKER_CLAUDE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(WORKER_CLAUDE_OVERRIDE_REASON_ENV, raising=False)


class _FakeConstraintCheck:
    """Return ``primary`` for the pre-guard check and ``raw`` for the guard.

    ``raw`` may be an exception instance: the guard must treat a raised check as
    fail-closed (one ``registry-unavailable`` blocking verdict, never a crash).
    """

    def __init__(self, primary=(), raw=()):
        self.primary = list(primary)
        self.raw = raw
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["check_registry"]:
            return list(self.primary)
        if isinstance(self.raw, BaseException):
            raise self.raw
        return list(self.raw)

    @property
    def guard_calls(self) -> list[dict]:
        return [c for c in self.calls if c["check_registry"] is False]


def _snapshot(tmp_path: Path, fake: _FakeConstraintCheck, *, model: str | None):
    data_dir, spec_file = _make_bundle_spec(tmp_path, model=model)
    spec = load_spec(spec_file)
    vspec = validate(spec, project_id="vnx-dev", repo_root=_REPO_ROOT)
    assert not isinstance(vspec, Reject), vspec
    with patch("providers.constraint_enforcer.check_constraints", fake):
        return build_runtime_snapshot(vspec, data_dir=data_dir, spec_file=spec_file)


def test_raw_model_equal_to_effective_model_skips_guard(tmp_path):
    """A requested model that the floor pin already resolves to is NOT re-checked.

    T0 pins opus-5-5 (floor); asking for opus-5-5 makes raw == effective, so the
    guard must never run and the snapshot keeps only the primary verdicts.
    """
    fake = _FakeConstraintCheck(primary=(), raw=RuntimeError("must not be called"))
    snapshot = _snapshot(tmp_path, fake, model="opus-5-5")

    assert fake.guard_calls == [], "raw == effective must not trigger the raw-model guard"
    assert snapshot.constraint_verdicts == (_TRACK_UNLINKED,)


def test_raw_model_differs_and_clean_adds_no_verdict(tmp_path):
    """A differing raw model with no blocking violation adds nothing, but the guard
    call itself must happen with the RAW model and check_registry=False."""
    fake = _FakeConstraintCheck(primary=(), raw=())
    snapshot = _snapshot(tmp_path, fake, model="sonnet")

    assert len(fake.guard_calls) == 1
    guard = fake.guard_calls[0]
    assert guard["model"] == "sonnet"
    assert guard["provider"] == "claude"
    assert guard["terminal_id"] == "T0"
    assert guard["role"] == "backend-developer"
    assert guard["via"] == "headless"
    assert guard["check_registry"] is False
    assert guard["env"] is not None
    assert snapshot.constraint_verdicts == (_TRACK_UNLINKED,)


def test_raw_model_differs_with_blocking_violation_appends_guard_verdict(tmp_path):
    """One blocking raw violation is appended LAST, with the [raw-model guard]
    prefix and the verdict's own fields preserved."""
    raw = [ConstraintVerdict(code="kimi-via-cli-only", severity="blocking", message="raw model not allowed")]
    fake = _FakeConstraintCheck(primary=(), raw=raw)
    snapshot = _snapshot(tmp_path, fake, model="sonnet")

    assert snapshot.constraint_verdicts == (
        ConstraintVerdict(
            code="kimi-via-cli-only",
            severity="blocking",
            message="[raw-model guard] raw model not allowed",
            override_applied=False,
        ),
        _TRACK_UNLINKED,
    )


def test_raw_model_blocking_violation_with_existing_code_is_not_added(tmp_path):
    """A raw blocking violation whose code the primary check already reported is
    not duplicated; the primary verdict stays first and unchanged."""
    primary = [ConstraintVerdict(code="duplicate-code", severity="blocking", message="primary says no")]
    raw = [ConstraintVerdict(code="duplicate-code", severity="blocking", message="raw says no too")]
    fake = _FakeConstraintCheck(primary=primary, raw=raw)
    snapshot = _snapshot(tmp_path, fake, model="sonnet")

    assert snapshot.constraint_verdicts == (
        ConstraintVerdict(code="duplicate-code", severity="blocking", message="primary says no"),
        _TRACK_UNLINKED,
    )


def test_raw_model_warn_violation_is_not_added(tmp_path):
    """Only BLOCKING raw violations are folded in; warn-only pin noise is dropped
    (it is already reported once by the primary check)."""
    raw = [ConstraintVerdict(code="pin-noise", severity="warn", message="advisory only")]
    fake = _FakeConstraintCheck(primary=(), raw=raw)
    snapshot = _snapshot(tmp_path, fake, model="sonnet")

    assert snapshot.constraint_verdicts == (_TRACK_UNLINKED,)


def test_raw_model_check_raising_yields_one_registry_unavailable(tmp_path):
    """A raised raw-model check is fail-closed: exactly one registry-unavailable
    blocking verdict, and the build still returns its snapshot."""
    fake = _FakeConstraintCheck(primary=(), raw=RuntimeError("registry down"))
    snapshot = _snapshot(tmp_path, fake, model="sonnet")

    assert snapshot.constraint_verdicts == (
        ConstraintVerdict(
            code="registry-unavailable",
            severity="blocking",
            message="Constraint registry unavailable (raw-model guard) — fail-closed: registry down",
        ),
        _TRACK_UNLINKED,
    )
