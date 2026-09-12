"""test_dispatch_stop_conditions_gate.py — golf 2A
(dispatch/20260904-deur-leest-stopcondities), plus the same-day herstelronde.

stop_conditions.py (PR #1754) measures four autonomous-chain stop-conditions
(E1 main-CI-red, E4 gh-auth-dead, provider-exhaustion, E6
repeated-gate-failure-cause) but nothing read its output — the door fired
blind. This dispatch wires the door to measure stop_conditions.run_all_checks()
LIVE before every fire (both real and dry-run, via build_runtime_snapshot).

Herstelronde (same day): PR #1757's own CI run turned 58 pre-existing tests
red with `REJECT [stop-conditions-triggered]: ... gh_auth_dead: gh is niet
geauthenticeerd ... You are not logged into any GitHub hosts` — the CI
runner's own `gh` CLI is not logged in, a fact about the test job's
subprocess environment, not about this project. check_gh_auth_dead (and
check_main_ci_red, same shape one layer down) take NO state_dir/project_root
scoping the door controls — grepping the FULL CI log confirmed all 65
occurrences were gh_auth_dead, never provider_exhausted/
repeated_gate_failure_cause (those two ARE correctly resolved against this
door's own state_dir the whole time, confirmed by both code-reading and this
CI evidence). Only provider_exhausted and repeated_gate_failure_cause — the
two checks resolved against the state_dir THIS call passes in — are now
BLOCKING-eligible (_STOP_CONDITIONS_BLOCKING_CHECK_IDS); main_ci_red and
gh_auth_dead are measured and surfaced loudly but are WARN-only. halt.json is
now written by an explicit write_halt_file() call, exactly once, only on a
real (non-dry-run) fire that hits a blocking-eligible trigger — never as a
side effect of the measurement itself (run_all_checks is always called with
write_halt=False here).

Every TRIGGERED-blocking test in this file is RED on the branch point: before
this fix, build_runtime_snapshot never called stop_conditions at all, so a
dispatch fired straight through regardless of any active stop-condition. Ran
red against the pre-fix revision (git stash of the dispatch_cli.py changes),
green after — see the dispatch report for the exact counts, both rounds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import run_dispatch  # noqa: E402
from stop_conditions import CheckStatus, StopConditionResult  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_dispatch_refire_guard.py's _make_bundle)
# ---------------------------------------------------------------------------

def _make_bundle(
    tmp_path: Path, *, staging_id: str, dispatch_id: str, provider: str = "claude", gate: str = "codex_gate",
) -> "tuple[Path, Path]":
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": gate,
        "dispatch_paths": [],
        "provider": provider,
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _result(check_id: str, status: CheckStatus, message: str = "test") -> StopConditionResult:
    return StopConditionResult(check_id=check_id, status=status, message=message)


# ---------------------------------------------------------------------------
# Unit-level: mocked stop_conditions.run_all_checks, one scenario each.
# ---------------------------------------------------------------------------

def test_triggered_stop_condition_blocks_fire(tmp_path, monkeypatch, capsys):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-triggered", dispatch_id="20260904-stopcond-triggered",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"

    fake_results = [
        _result("main_ci_red", CheckStatus.CLEAR),
        _result("gh_auth_dead", CheckStatus.CLEAR),
        _result("provider_exhausted", CheckStatus.TRIGGERED, "kimi: 3 consecutive auth_rejected"),
        _result("repeated_gate_failure_cause", CheckStatus.CLEAR),
    ]

    with patch("stop_conditions.run_all_checks", return_value=fake_results) as mock_run, \
         patch("stop_conditions.write_halt_file") as mock_write_halt, \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "an active blocking-eligible TRIGGERED stop-condition must refuse the fire"
    mock_execute.assert_not_called()
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs.get("write_halt") is False, (
        "the measurement call itself must never write — see write_halt_file below"
    )
    # a real fire hitting a blocking-eligible trigger must persist halt.json
    # exactly once, via an explicit write_halt_file() call
    mock_write_halt.assert_called_once_with(state_dir, fake_results)
    err = capsys.readouterr().err
    assert "stop-conditions-triggered" in err
    assert "provider_exhausted" in err


def test_triggered_stop_condition_with_override_reason_proceeds(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-override", dispatch_id="20260904-stopcond-override",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [_result("provider_exhausted", CheckStatus.TRIGGERED, "kimi: 3 consecutive auth_rejected")]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(
            spec_file,
            stop_conditions_override_reason="operator: known false positive, verified kimi lane recovered by hand",
        )

    assert rc == 0, "an explicit --override-stop-conditions reason must let the dispatch proceed"
    mock_execute.assert_called_once()


def test_unmeasurable_stop_condition_does_not_block(tmp_path, monkeypatch, capsys):
    """UNMEASURABLE is not a green light, but it is also not grounds to
    refuse — a caller that refused on UNMEASURABLE would close the door
    every time gh hiccups, which is fragility, not fail-closed."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-unmeasurable", dispatch_id="20260904-stopcond-unmeasurable",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [
        _result("main_ci_red", CheckStatus.UNMEASURABLE, "gh CLI niet beschikbaar"),
        _result("gh_auth_dead", CheckStatus.UNMEASURABLE, "gh CLI niet beschikbaar"),
        _result("provider_exhausted", CheckStatus.UNMEASURABLE, "receipts-bestand ontbreekt"),
        _result("repeated_gate_failure_cause", CheckStatus.UNMEASURABLE, "resultaten-map ontbreekt"),
    ]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, "all-UNMEASURABLE must not block — unmeasurable is not evidence of a real condition"
    mock_execute.assert_called_once()
    err = capsys.readouterr().err
    assert "unmeasurable" in err.lower()


def test_all_clear_does_not_block(tmp_path, monkeypatch):
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-clear", dispatch_id="20260904-stopcond-clear",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [
        _result("main_ci_red", CheckStatus.CLEAR),
        _result("gh_auth_dead", CheckStatus.CLEAR),
        _result("provider_exhausted", CheckStatus.CLEAR),
        _result("repeated_gate_failure_cause", CheckStatus.CLEAR),
    ]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# Herstelronde regression: the exact CI incident from PR #1757 — an ambient,
# unscoped probe (gh_auth_dead / main_ci_red) TRIGGERED must warn loudly but
# never block and never persist halt.json on its own.
# ---------------------------------------------------------------------------

def test_gh_auth_dead_triggered_warns_but_does_not_block(tmp_path, monkeypatch, capsys):
    """Direct regression test for PR #1757's own CI run (job 100996619247):
    a CI runner whose `gh` is not logged in must not turn every dispatch test
    into a stop-conditions refusal."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-ghauth-warn", dispatch_id="20260904-stopcond-ghauth-warn",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"

    fake_results = [
        _result("gh_auth_dead", CheckStatus.TRIGGERED, "gh is niet geauthenticeerd (E4)"),
        _result("main_ci_red", CheckStatus.CLEAR),
        _result("provider_exhausted", CheckStatus.CLEAR),
        _result("repeated_gate_failure_cause", CheckStatus.CLEAR),
    ]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("stop_conditions.write_halt_file") as mock_write_halt, \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, "gh_auth_dead is an unscoped ambient probe — WARN-only, must never block the door"
    mock_execute.assert_called_once()
    # a WARN-only trigger must never persist halt.json
    mock_write_halt.assert_not_called()
    assert not (state_dir / "halt.json").exists()
    err = capsys.readouterr().err
    assert "gh_auth_dead" in err
    assert "WARN-only" in err


def test_main_ci_red_triggered_warns_but_does_not_block(tmp_path, monkeypatch, capsys):
    """Same shape one check over: check_main_ci_red also takes no state_dir
    the door controls (only project_root, which this call never passes), so
    it is WARN-only for the same reason."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-cired-warn", dispatch_id="20260904-stopcond-cired-warn",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    fake_results = [_result("main_ci_red", CheckStatus.TRIGGERED, "VNX CI conclusion=failure on main")]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("stop_conditions.write_halt_file") as mock_write_halt, \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()
    mock_write_halt.assert_not_called()


def test_stop_conditions_checked_on_dry_run_too(tmp_path, monkeypatch):
    """_check_reachability's own precedent (OI-1248): a lane-blocking fact
    refuses on BOTH the dry-run and the real path. Stop-conditions follow the
    same rule for a blocking-eligible check — a dry-run must preview the real
    refusal, not fire blind — but must NEVER persist halt.json: a dry-run is
    a preview, zero durable side effects."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-dryrun", dispatch_id="20260904-stopcond-dryrun",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"

    fake_results = [_result("provider_exhausted", CheckStatus.TRIGGERED, "kimi: 3 consecutive auth_rejected")]

    with patch("stop_conditions.run_all_checks", return_value=fake_results), \
         patch("stop_conditions.write_halt_file") as mock_write_halt, \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 1, "dry-run must also refuse on a blocking-eligible TRIGGERED stop-condition"
    mock_execute.assert_not_called()
    # a dry-run must never persist halt.json
    mock_write_halt.assert_not_called()
    assert not (state_dir / "halt.json").exists()


def test_measurement_crash_degrades_to_unmeasurable_not_a_door_crash(tmp_path, monkeypatch):
    """A bug in the checker must never become an outage of the whole door —
    fail-open on the CHECKER's own crash, never a raised exception out of
    run_dispatch."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260904-staging-stopcond-crash", dispatch_id="20260904-stopcond-crash",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("stop_conditions.run_all_checks", side_effect=RuntimeError("boom")), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# End-to-end: REAL stop_conditions.check_provider_exhausted (no mocking of
# stop_conditions itself) against a crafted receipts ledger reproducing the
# exact live scenario named in the original dispatch brief — three
# consecutive kimi auth_rejected receipts. gh-dependent checks (main_ci_red,
# gh_auth_dead — both WARN-only anyway per the herstelronde fix) are
# neutralized via shutil.which so the test has no real network/gh-auth
# dependency; provider_exhausted and repeated_gate_failure_cause run for real,
# resolved against the state_dir this call passes in.
#
# OI-1694: the door now SCOPES a blocking-eligible TRIGGERED sub-result down
# to whether it is actually relevant to THIS dispatch's provider/gate, instead
# of refusing on the bare fact that SOMETHING, somewhere, is exhausted. Every
# test below uses a REAL receipts ledger / review_gates results dir in
# tmp_path — never a patched stop_conditions.run_all_checks — so the
# relevance logic itself is exercised, not a mock standing in for it.
# ---------------------------------------------------------------------------

def _write_kimi_exhausted_ledger(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        for i in range(3):
            rec = {
                "event_type": "task_complete",
                "provider": "kimi",
                "status": "failed",
                "failure_class": "auth_rejected",
                "dispatch_id": f"20260903-kimi-fail-{i}",
                "timestamp": f"2026-09-03T0{i}:00:00Z",
            }
            fh.write(json.dumps(rec) + "\n")


def _write_exhausted_ledger(state_dir: Path, provider: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        for i in range(3):
            rec = {
                "event_type": "task_complete",
                "provider": provider,
                "status": "failed",
                "failure_class": "auth_rejected",
                "dispatch_id": f"20260903-{provider}-fail-{i}",
                "timestamp": f"2026-09-03T0{i}:00:00Z",
            }
            fh.write(json.dumps(rec) + "\n")


def _neutralize_gh(monkeypatch) -> None:
    import stop_conditions
    monkeypatch.setattr(stop_conditions.shutil, "which", lambda _bin: None)


def _write_repeated_gate_results(results_dir: Path, gate: str, reason: str = "provider_not_installed") -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    for i, pr in enumerate([9001, 9002, 9003]):
        d = {
            "pr_number": pr,
            "gate": gate,
            "status": "unavailable",
            "reason": reason,
            "recorded_at": f"2026-09-0{i+1}T00:00:00Z",
        }
        (results_dir / f"pr-{pr}-{gate}.json").write_text(json.dumps(d), encoding="utf-8")


def test_1_unrelated_provider_exhaustion_does_not_block(tmp_path, monkeypatch, capsys):
    """Scenario 1 (rood, het defect zelf): only kimi is exhausted; a dispatch
    with provider=claude, gate=glm_gate touches neither kimi nor glm-harness
    — the door must NOT block."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260908-staging-oi1694-1", dispatch_id="20260908-oi1694-1",
        provider="claude", gate="glm_gate",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"
    _write_kimi_exhausted_ledger(state_dir)
    _neutralize_gh(monkeypatch)

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, "kimi-exhaustion must not block a claude/glm_gate dispatch that never touches kimi"
    mock_execute.assert_called_once()

    # Scenario 6: the measurement is NOT gone — it is a WARN, not a deletion.
    err = capsys.readouterr().err
    assert "kimi" in err, "the exhausted-but-out-of-scope provider must still be named in the door's output"

    import stop_conditions
    raw_results = stop_conditions.run_all_checks(state_dir=state_dir, write_halt=False)
    by_id = {r.check_id: r for r in raw_results}
    assert by_id["provider_exhausted"].status == CheckStatus.TRIGGERED
    assert "kimi" in json.dumps(by_id["provider_exhausted"].evidence)


def test_2_provider_match_still_blocks(tmp_path, monkeypatch, capsys):
    """Scenario 2: same kimi-only ledger, but the dispatch itself IS kimi —
    the brake must still bite via the provider match."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260908-staging-oi1694-2", dispatch_id="20260908-oi1694-2",
        provider="kimi", gate="glm_gate",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"
    _write_kimi_exhausted_ledger(state_dir)
    _neutralize_gh(monkeypatch)

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "a kimi dispatch must still be refused by kimi's own exhaustion"
    mock_execute.assert_not_called()
    err = capsys.readouterr().err
    assert "stop-conditions-triggered" in err


def test_3_gate_match_pulls_provider_into_scope_and_still_blocks(tmp_path, monkeypatch, capsys):
    """Scenario 3: glm-harness is exhausted; dispatch is provider=claude,
    gate=glm_gate — glm_gate's own provider (glm-harness) pulls this
    dispatch's scope in, so the brake must still bite."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260908-staging-oi1694-3", dispatch_id="20260908-oi1694-3",
        provider="claude", gate="glm_gate",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"
    _write_exhausted_ledger(state_dir, "glm-harness")
    _neutralize_gh(monkeypatch)

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "glm_gate must pull glm-harness exhaustion into scope even for a claude-provider dispatch"
    mock_execute.assert_not_called()


def test_4_unknown_ledger_vocabulary_blocks_regardless_of_provider(tmp_path, monkeypatch, capsys):
    """Scenario 4: an exhausted label absent from every alias set can never
    be positively proven unrelated — it must block no matter the spec
    provider/gate."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260908-staging-oi1694-4", dispatch_id="20260908-oi1694-4",
        provider="claude", gate="glm_gate",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"
    _write_exhausted_ledger(state_dir, "future-provider-xyz")
    _neutralize_gh(monkeypatch)

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "unrecognized ledger vocabulary must fail closed and block"
    mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 5: undeterminable scope falls back to (None, None) — i.e. broad
# blocking. Unit-tested directly against _resolve_stop_conditions_scope
# rather than through run_dispatch: by the time build_runtime_snapshot runs,
# spec.provider == Provider.AUTO can no longer occur on the real path (OI-962's
# smart router resolves AUTO to a concrete provider, falling back to CLAUDE,
# BEFORE validate()/build_runtime_snapshot — see run_dispatch's own comment
# at the router-pre-validate call), and an empty spec.gate is filled in by
# _resolve_gate_via_router before build_runtime_snapshot too. Both router
# steps are fail-open (a broken router leaves AUTO/empty-gate untouched), so
# _resolve_stop_conditions_scope's own fallback is still live defense-in-depth
# for that fail-open path — exercised here directly, at the level it actually
# guards.
# ---------------------------------------------------------------------------

from dispatch_cli import _resolve_stop_conditions_scope  # noqa: E402
from dispatch_spec import DispatchSpec, Isolation, Provider  # noqa: E402


def _spec(*, provider: Provider, gate: str) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="20260908-oi1694-scope-unit",
        staging_id="20260908-oi1694-scope-unit",
        instruction_file=Path("/tmp/instruction.md"),
        role="backend-developer",
        target_slot="T0",
        gate=gate,
        dispatch_paths=(),
        provider=provider,
        isolation=Isolation.WORKTREE,
    )


def test_5a_auto_provider_cannot_be_scoped():
    """Scenario 5 (auto half): provider=auto has not resolved to a real
    provider yet — the scope is undeterminable, so the door must fall back
    to broad blocking (None, None) exactly like before OI-1694."""
    scope = _resolve_stop_conditions_scope(_spec(provider=Provider.AUTO, gate="glm_gate"))
    assert scope == (None, None)


def test_5b_empty_gate_cannot_be_scoped():
    """Scenario 5 (empty-gate half): a non-auto provider with no gate
    assigned also cannot be scoped via GATE_LEDGER_PROVIDERS — same broad
    fallback."""
    scope = _resolve_stop_conditions_scope(_spec(provider=Provider.CLAUDE, gate=""))
    assert scope == (None, None)


def test_5c_gate_outside_gate_ledger_providers_cannot_be_scoped():
    """A legacy phase sentinel ("planning"/"implementation") is a legal
    spec.gate value (dispatch_spec's LEGACY_GATE_SENTINELS) but is not a key
    in GATE_LEDGER_PROVIDERS — undeterminable, same broad fallback."""
    scope = _resolve_stop_conditions_scope(_spec(provider=Provider.CLAUDE, gate="planning"))
    assert scope == (None, None)


def test_determinable_scope_unions_provider_and_gate_ledger_labels():
    """Sanity check on the happy path this whole dispatch is about: a
    concrete provider + a known gate resolves to a real, unioned scope."""
    providers, gates = _resolve_stop_conditions_scope(_spec(provider=Provider.CLAUDE, gate="glm_gate"))
    assert providers == {"claude", "glm-harness"}
    assert gates == {"glm_gate"}


def test_8a_repeated_gate_failure_on_a_different_gate_does_not_block(tmp_path, monkeypatch, capsys):
    """Scenario 8 (different-gate half): codex_gate has a real 3x repeat, but
    THIS dispatch is scoped to glm_gate — must not block."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260908-staging-oi1694-8a", dispatch_id="20260908-oi1694-8a",
        provider="claude", gate="glm_gate",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"
    _write_repeated_gate_results(state_dir / "review_gates" / "results", "codex_gate")
    _neutralize_gh(monkeypatch)

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, "a repeated failure on codex_gate must not block a glm_gate dispatch"
    mock_execute.assert_called_once()
    err = capsys.readouterr().err
    assert "codex_gate" in err, "the out-of-scope repeat must still be named, not silently dropped"


def test_8b_repeated_gate_failure_on_the_dispatchs_own_gate_still_blocks(tmp_path, monkeypatch, capsys):
    """Scenario 8 (own-gate half): the same 3x repeat, but the dispatch
    itself targets codex_gate — must block."""
    data_dir, spec_file = _make_bundle(
        tmp_path, staging_id="20260908-staging-oi1694-8b", dispatch_id="20260908-oi1694-8b",
        provider="claude", gate="codex_gate",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state_dir = data_dir / "state"
    _write_repeated_gate_results(state_dir / "review_gates" / "results", "codex_gate")
    _neutralize_gh(monkeypatch)

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "a repeated failure on the dispatch's own gate must still block"
    mock_execute.assert_not_called()
