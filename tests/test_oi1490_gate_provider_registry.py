"""Regression tests for OI-1490: a gate with no PATH binary is not a missing binary.

``gate_runner.GateRunner.run`` resolved a gate's provider with
``GATE_BINARIES.get(gate)`` and, finding nothing, booked
``provider_not_installed`` against a binary name it had invented from the
gate's own name. Measured in the live audit trail
(``state/gate_execution_audit.ndjson``):

    2026-08-26 (x6)  glm_gate   binary='glm_gate'   found=False
    2026-08-28       kimi_gate  binary='kimi_gate'  found=False

That label is wrong three times over:

  1. The binary is not missing — no binary called ``kimi_gate`` has ever
     existed. The real runner, ``scripts/kimi_gate.py``, was on disk the whole
     time, and ``kimi`` itself is on PATH.
  2. It hides a routing bug behind an environment complaint, so the reader
     goes looking for an install that would not have helped.
  3. ``provider_not_installed`` is in
     ``gate_obligation_runner._TEMPORARY_NOT_EXECUTABLE_REASONS`` — "installable
     tomorrow" — so the obligation burns bounded retries waiting for a change
     that cannot happen, then escalates for the wrong reason.

Why glm gates nonetheless produced real verdicts on the same day: they do not
run through this path at all. ``glm_gate``/``kimi_gate`` are launched directly
as scripts (``python3 scripts/glm_gate.py --pr N``, the command
``pr_readiness.GATE_COST`` documents). ``GateRunner`` is only reached via
``gate_obligation_runner`` -> ``review_gate_manager.request_and_execute``, and
on THAT path both gates were equally dead. The bug was never kimi-specific.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_recorder as _rec
import gate_runner
from gate_runner import GateRunner


@pytest.fixture
def gate_dirs(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "review_gates" / "requests").mkdir(parents=True)
    (state / "review_gates" / "results").mkdir(parents=True)
    reports = tmp_path / "unified_reports"
    reports.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    return {"state": state, "reports": reports}


def _payload(gate: str, pr_number: int = 1) -> dict:
    return {
        "gate": gate,
        "status": "requested",
        "branch": "feature/test",
        "pr_number": pr_number,
        "requested_at": "2026-08-28T19:00:00Z",
        "report_path": "",
        "prompt": "Review this diff",
        "dispatch_id": f"{gate}-pr{pr_number}-1788000000",
    }


def _run(gate_dirs, gate: str, pr_number: int = 1) -> dict:
    runner = GateRunner(state_dir=gate_dirs["state"], reports_dir=gate_dirs["reports"])
    return runner.run(gate=gate, request_payload=_payload(gate, pr_number), pr_number=pr_number)


# ---------------------------------------------------------------------------
# 1. The measured defect, per gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate,runner_file", [
    ("kimi_gate", "scripts/kimi_gate.py"),
    ("glm_gate", "scripts/glm_gate.py"),
])
def test_a_script_runner_gate_is_not_a_missing_binary(gate_dirs, gate, runner_file):
    """RED on main: every one of these booked provider_not_installed."""
    result = _run(gate_dirs, gate)

    assert result["reason"] != "provider_not_installed", (
        f"{gate} has no PATH binary and never had one; reporting a missing "
        f"binary sends the reader after an install that cannot help"
    )
    assert result["reason"] == "gate_not_subprocess_routable"
    assert runner_file in result["reason_detail"], (
        "the detail must name the runner that DOES exist, so the reader can act"
    )
    assert "--pr 1" in result["reason_detail"], (
        "and the invocation that actually works, with the PR filled in"
    )


def test_the_invented_binary_name_is_gone_from_the_audit_trail(gate_dirs):
    """The exact shape measured in gate_execution_audit.ndjson."""
    _run(gate_dirs, "kimi_gate")

    audit = (gate_dirs["state"] / "gate_execution_audit.ndjson").read_text()
    record = json.loads(audit.strip().splitlines()[-1])
    check = record["provider_check"]

    assert check["binary_name"] != "kimi_gate", (
        "this is the invented name: shutil.which('kimi_gate') answers a "
        "question nobody asked, and its False was read as a missing provider"
    )
    assert check["provider_kind"] == "script_runner"
    assert check["binary_name"] == "scripts/kimi_gate.py"
    assert check["binary_found"] is True, (
        "the runner is on disk — that is the whole point: nothing was missing"
    )


def test_an_unregistered_gate_says_so_instead_of_inventing_a_binary(gate_dirs):
    result = _run(gate_dirs, "verzonnen_gate")

    assert result["reason"] == "gate_not_registered"
    assert "not found in PATH" not in result["reason_detail"], (
        "an unregistered gate is a routing bug, not an environment complaint"
    )
    assert "GATE_PROVIDERS" in result["reason_detail"]


# ---------------------------------------------------------------------------
# 2. What must NOT change
# ---------------------------------------------------------------------------


def test_a_real_path_binary_gate_still_reports_a_missing_binary(gate_dirs, monkeypatch):
    """provider_not_installed stays correct for the gates it describes."""
    monkeypatch.setattr(gate_runner.shutil, "which", lambda _b: None)

    result = _run(gate_dirs, "codex_gate")

    assert result["reason"] == "provider_not_installed"
    assert "codex binary not found in PATH" in result["reason_detail"]


def test_a_present_path_binary_gate_is_not_refused_here(gate_dirs, monkeypatch):
    """With the binary on PATH the runner must get past this check entirely."""
    monkeypatch.setattr(gate_runner.shutil, "which", lambda _b: "/usr/bin/codex")
    calls = []
    monkeypatch.setattr(
        GateRunner, "_run_subprocess_path",
        lambda self, **kw: calls.append(kw["gate"]) or {"status": "completed"},
    )

    _run(gate_dirs, "codex_gate")

    assert calls == ["codex_gate"], "a resolvable PATH gate must reach execution"


# ---------------------------------------------------------------------------
# 3. The consequence the wrong label had downstream
# ---------------------------------------------------------------------------


def test_the_new_reasons_are_permanent_not_a_bounded_wait():
    """A routing bug must not sit in the retry-until-the-environment-changes
    bucket. `provider_not_installed` belongs there and stays there."""
    import gate_obligation_runner as gor

    temporary = gor._TEMPORARY_NOT_EXECUTABLE_REASONS
    assert "provider_not_installed" in temporary, "unchanged for real binaries"
    assert "gate_not_subprocess_routable" not in temporary, (
        "no amount of waiting turns a script runner into a PATH binary"
    )
    assert "gate_not_registered" not in temporary


# ---------------------------------------------------------------------------
# 4. The drift that let this happen
# ---------------------------------------------------------------------------


def test_the_two_binary_mappings_cannot_drift_apart():
    """gate_runner's copy carried 3 gates, gate_recorder's carried 5, and
    neither carried kimi_gate or glm_gate. One registry now feeds both."""
    assert gate_runner.GATE_BINARIES == _rec._GATE_BINARIES


def test_every_path_binary_gate_can_actually_be_driven_by_this_runner():
    """A gate registered as a PATH binary is one this runner builds an argv
    for. Registering kimi_gate as `kimi` would satisfy the PATH check and then
    run a bare `kimi` with a review prompt — passing the gate and producing no
    contract_hash, no report_path, no verdict. Worse than the loud refusal it
    replaced, which is why kimi_gate is a script runner here and not a binary.
    """
    for gate in _rec._GATE_BINARIES:
        kind, _name = _rec.GATE_PROVIDERS[gate]
        assert kind == _rec.GATE_PROVIDER_PATH_BINARY
    assert "kimi_gate" not in _rec._GATE_BINARIES
    assert "glm_gate" not in _rec._GATE_BINARIES


def test_a_registered_but_unshipped_runner_says_missing_not_unroutable(gate_dirs):
    """Found by these tests, not assumed: scripts/deepseek_gate.py is not on
    disk. "Registered but not shipped" and "shipped but not drivable here" are
    different answers and the reader acts differently on each, so they get
    different reason codes. gate_request_handler already books the first as
    `gate_runner_missing`; this reuses that name instead of minting a second
    one for the same fact.
    """
    assert not (VNX_ROOT / "scripts" / "deepseek_gate.py").exists(), (
        "if deepseek_gate.py has since shipped, this test documents the "
        "transition — move it to the routable case above"
    )

    result = _run(gate_dirs, "deepseek_gate")

    assert result["reason"] == "gate_runner_missing"
    assert "has not shipped" in result["reason_detail"]
    assert result["reason"] != "provider_not_installed"


def test_shipped_script_runners_are_on_disk():
    """The two gates that DO have runners must keep having them — a rename
    that leaves the registry pointing at nothing would otherwise turn every
    one of their runs into a silent `gate_runner_missing`."""
    for gate in ("kimi_gate", "glm_gate"):
        _kind, name = _rec.GATE_PROVIDERS[gate]
        assert (VNX_ROOT / name).exists(), f"{gate} names a runner that is not on disk: {name}"


# ---------------------------------------------------------------------------
# 5. One writer per record shape
# ---------------------------------------------------------------------------

# Every module that may append a gate_skip_rationale record. The invariant is
# that exactly ONE of them builds the record; the rest delegate.
_AUDIT_WRITER_MODULES = (
    "scripts/lib/gate_recorder.py",
    "scripts/lib/gate_report_generator.py",
    "scripts/lib/gate_request_handler.py",
    "scripts/gate_runner.py",
    "scripts/lib/gate_executor.py",
    "scripts/lib/gate_artifacts.py",
)


def test_only_one_place_builds_a_provider_check_block():
    """Two writers of one event_type in one file is how shapes drift.

    gate_report_generator._write_skip_rationale used to build its own record
    for the SAME gate_execution_audit.ndjson, with its own copy of the
    gate->env-flag map (already missing wiring_gate) and its own raw
    shutil.which on a caller-supplied name. Once gate_recorder learned
    provider_kind, that file would have carried two shapes — and the records
    still doing the invented-binary lookup would be exactly the ones a reader
    filtering on provider_kind never sees.

    Third time this shape appeared in one day: OI-1472 (writers bypassing the
    result-slot guard), OI-1486 (writers sharing a fixed tmp name), this one.
    Each time a second writer nobody counted, because the first one was right.
    """
    # A BUILDER is a line that opens a dict literal for the key —
    # `"provider_check": {`. Matching the bare word instead would flag every
    # docstring that names it, including this file's own. Found by getting it
    # wrong first: the initial filter skipped any line starting with a quote,
    # which is exactly what the real builder line starts with, so it reported
    # zero builders on a tree that has one.
    builder_re = re.compile(r'^"provider_check"\s*:\s*\{')
    builders = []
    for rel in _AUDIT_WRITER_MODULES:
        path = VNX_ROOT / rel
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if builder_re.match(line.strip()):
                builders.append(f"{rel}:{lineno}: {line.strip()[:90]}")

    assert len(builders) == 1, (
        "exactly one place may construct a provider_check block — the rest must "
        "delegate to gate_recorder.write_skip_rationale (OI-1490). Found:\n  "
        + "\n  ".join(builders)
    )
    assert builders[0].startswith("scripts/lib/gate_recorder.py"), (
        f"the one builder must be gate_recorder, found: {builders[0]}"
    )


def test_the_delegating_writer_produces_the_registry_shape(tmp_path, monkeypatch):
    """Drives the REAL mixin method, not a reimplementation of it."""
    from gate_report_generator import GateReportGeneratorMixin

    class _Manager(GateReportGeneratorMixin):
        def __init__(self, state_dir):
            self.state_dir = state_dir

    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    _Manager(tmp_path)._write_skip_rationale(
        gate="glm_gate", pr_id="42",
        reason="gate_runner_missing", reason_detail="not shipped",
    )

    record = json.loads(
        (tmp_path / "gate_execution_audit.ndjson").read_text().strip().splitlines()[-1]
    )
    check = record["provider_check"]

    assert check["provider_kind"] == "script_runner", (
        "the delegating writer must produce the SAME shape as the direct one, "
        "or one file carries two shapes of one event_type"
    )
    assert check["binary_name"] == "scripts/glm_gate.py"
    assert check["binary_found"] is True


def test_the_gate_env_flag_map_exists_once():
    """The copy in gate_report_generator had already drifted: four gates where
    gate_recorder had five, missing wiring_gate. A second copy of a lookup
    table is a second answer waiting to happen."""
    generator_src = (VNX_ROOT / "scripts" / "lib" / "gate_report_generator.py").read_text()

    assert "VNX_WIRING_GATE_REQUIRED" not in generator_src
    assert "VNX_GEMINI_REVIEW_ENABLED" not in generator_src, (
        "gate->env-flag mapping belongs in gate_recorder._GATE_ENV_FLAGS only"
    )
    assert "wiring_gate" in _rec._GATE_ENV_FLAGS


# ---------------------------------------------------------------------------
# 6. The SECOND path — request time (kimi advisory on a8141931)
# ---------------------------------------------------------------------------

# GateRunner is the run-time path. gate_request_handler._mark_gate_unavailable
# -> GateResultParserMixin._classify_unavailable is the REQUEST-time one, and
# it ran the same raw shutil.which on a caller-supplied name. Fixing one path
# and leaving the other is a promise the code does not keep — the same thing
# write_result_guarded's docstring was corrected for earlier the same day.


def _classify(gate: str):
    from gate_result_parser import GateResultParserMixin

    class _P(GateResultParserMixin):
        pass

    return _P()._classify_unavailable(gate)


def test_request_time_kimi_is_not_a_missing_binary():
    """The caller passed binary_name="kimi_gate.py" — not a binary, never was,
    so the lookup could only fail and the gate could only be booked
    provider_not_installed."""
    reason, detail = _classify("kimi_gate")

    assert reason != "provider_not_installed"
    assert reason == "gate_runner_missing"
    assert "scripts/kimi_gate.py" in detail
    assert "not a PATH lookup" in detail


def test_request_time_unregistered_gate_is_a_routing_bug():
    reason, detail = _classify("verzonnen_gate")

    assert reason == "gate_not_registered"
    assert "not found in PATH" not in detail


def test_request_time_path_binary_gate_keeps_its_behaviour(monkeypatch):
    import gate_result_parser

    monkeypatch.setattr(gate_result_parser.shutil, "which", lambda _b: None)
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")

    reason, detail = _classify("codex_gate")

    assert reason == "provider_not_installed"
    assert detail == "codex binary not found in PATH", (
        "the name must come from the registry, not from a caller"
    )


_REGISTRY_ONLY_METHODS = ("_mark_gate_unavailable", "_classify_unavailable")


def test_neither_entry_point_accepts_a_provider_name():
    """The whole class of bug: a name handed in by the caller that nobody ever
    shipped. Both entry points read the registry, so there is no parameter
    left to hand one through."""
    import inspect

    from gate_request_handler import GateRequestHandlerMixin
    from gate_result_parser import GateResultParserMixin

    for cls, method in (
        (GateRequestHandlerMixin, "_mark_gate_unavailable"),
        (GateResultParserMixin, "_classify_unavailable"),
    ):
        params = inspect.signature(getattr(cls, method)).parameters
        assert "binary_name" not in params, (
            f"{cls.__name__}.{method} still accepts a caller-supplied provider name"
        )


def test_no_caller_anywhere_passes_a_provider_name():
    """Every call site in the tree, not just the file the parameter lived in.

    The first version of this guard asserted the string was absent from
    gate_request_handler.py. It went green while a caller in
    tests/test_gate_request_handler_w3f.py still passed
    ``binary_name="gemini"`` — a TypeError on a keyword-only signature, caught
    by the kimi gate rather than by the guard written to catch exactly it. The
    source was covered and the callers were not.

    That is the fifth appearance of this shape in one day (OI-1472, OI-1486,
    the duplicate provider_check block, the half-migrated
    _classify_unavailable, and now the guard itself), so this one walks the
    AST of every module instead of one file's text: a keyword argument is a
    keyword argument no matter how the call is wrapped, and a mention in prose
    or a dict key is not a call at all.
    """
    offenders = []
    for root in ("scripts", "tests"):
        for path in sorted((VNX_ROOT / root).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name not in _REGISTRY_ONLY_METHODS:
                    continue
                for kw in node.keywords:
                    if kw.arg == "binary_name":
                        rel = path.relative_to(VNX_ROOT)
                        offenders.append(f"{rel}:{node.lineno}: {name}(... binary_name=...)")

    assert offenders == [], (
        "these call sites still hand a provider name to a method that reads the "
        "registry (OI-1490); drop the argument:\n  " + "\n  ".join(offenders)
    )
