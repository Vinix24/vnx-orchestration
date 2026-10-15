"""`vnx gate <pr> --only <gate>` refuses a switched-off gate before any launch (provider-schakelaar C).

These tests source the real ``scripts/commands/gate.sh`` against a fake VNX_HOME that carries the
REAL ``providers_cli.py`` and ``provider_switch.py`` (see ``tests/_gate_harness.py``), a capturing
stub ``review_gate_manager.py`` and a stub ``gh`` that logs every call. A refused gate must leave
both untouched: no branch lookup, no request, no launch. The switch file is a tmp file passed
through ``VNX_PROVIDER_SWITCH_FILE``; nothing reads the operator's ``~/.vnx``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _gate_harness import GATE_SH, install_gate_lib_modules  # after the sys.path insert above

PR = "1281"
PR_HEAD = "dispatch/20261015-ps-c-review-gates"

STUB_MANAGER = """\
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
Path(os.environ["CAPTURE"]).write_text(json.dumps(argv), encoding="utf-8")
seat = argv[argv.index("--review-stack") + 1]
print(json.dumps({"has_required_failure": False,
                  "gates": [{"gate": seat, "passed": True, "execution_status": "completed",
                             "request_status": "completed", "detail": {"gate": seat}}]}))
"""

STUB_GH = """\
#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  printf '%s' "$PR_HEAD"
  exit 0
fi
exit 1
"""

DRIVER = """\
#!/usr/bin/env bash
set -euo pipefail
log() {{ printf '%s\\n' "$*"; }}
err() {{ printf 'ERROR: %s\\n' "$*" >&2; }}
export VNX_HOME={vnx_home}
export VNX_STATE_DIR={state_dir}
export PROJECT_ROOT={project}
source {gate_sh}
cmd_gate "$@"
"""


@pytest.fixture
def harness(tmp_path):
    vnx_home = tmp_path / "vnx-home"
    install_gate_lib_modules(vnx_home / "scripts" / "lib")
    (vnx_home / "scripts" / "review_gate_manager.py").write_text(STUB_MANAGER, encoding="utf-8")
    state_dir = tmp_path / "state"
    (state_dir / "review_gates" / "results").mkdir(parents=True)
    (state_dir / "review_gates" / "requests").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(STUB_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    driver = tmp_path / "run_gate.sh"
    driver.write_text(DRIVER.format(vnx_home=vnx_home, state_dir=state_dir, project=project, gate_sh=GATE_SH),
                      encoding="utf-8")
    driver.chmod(0o755)
    return {"tmp": tmp_path, "vnx_home": vnx_home, "driver": driver, "bin_dir": bin_dir,
            "capture": tmp_path / "captured_args.json", "gh_log": tmp_path / "gh.log",
            "switch": tmp_path / "switch" / "providers.json",
            "audit": state_dir / "gate_execution_audit.ndjson"}


def audit_records(harness) -> list:
    """The GATE-9 lines in the gate audit ledger, oldest first."""
    if not harness["audit"].exists():
        return []
    return [json.loads(line) for line in harness["audit"].read_text(encoding="utf-8").splitlines() if line.strip()]


def write_switch(harness, text: str) -> None:
    harness["switch"].parent.mkdir(parents=True, exist_ok=True)
    harness["switch"].write_text(text, encoding="utf-8")


def switch_off(harness, *families: str) -> None:
    write_switch(harness, json.dumps({"version": 1, "global": {
        family: {"enabled": False, "changed_at": "2026-10-15T08:00:00Z", "changed_by": "op", "reason": "tegoed"}
        for family in families
    }}))


def run_gate(harness, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("VNX_OVERRIDE_PROVIDER_") and k != "VNX_PROJECT_ID"}
    env.update(PATH=f"{harness['bin_dir']}:{env['PATH']}", CAPTURE=str(harness["capture"]),
               GH_LOG=str(harness["gh_log"]), PR_HEAD=PR_HEAD,
               VNX_PROVIDER_SWITCH_FILE=str(harness["switch"]))
    return subprocess.run(["bash", str(harness["driver"]), *args], env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("short,gate", [("kimi", "kimi_gate"), ("kimi_gate", "kimi_gate"), ("glm", "glm_gate")])
def test_only_a_switched_off_gate_refuses_before_anything_runs(harness, short, gate):
    switch_off(harness, "kimi", "glm")

    result = run_gate(harness, PR, "--only", short)

    assert result.returncode == 4, result.stderr
    assert f"'{gate}' is switched off by the provider switch" in result.stderr
    assert f"provider_switched_off: {gate} family=" in result.stderr
    assert "consumer=vnx gate --only" in result.stderr
    assert not harness["capture"].exists(), "review_gate_manager ran for a switched-off gate"
    assert not harness["gh_log"].exists(), "the PR branch was resolved for a switched-off gate"


def test_only_an_allowed_gate_runs_as_before(harness):
    switch_off(harness, "kimi")

    result = run_gate(harness, PR, "--only", "deepseek")

    assert result.returncode == 0, result.stderr
    captured = json.loads(harness["capture"].read_text(encoding="utf-8"))
    assert captured[captured.index("--review-stack") + 1] == "deepseek_gate"


def test_only_codex_is_never_refused(harness):
    write_switch(harness, "{broken")

    result = run_gate(harness, PR, "--only", "codex")

    assert result.returncode == 0, result.stderr
    assert harness["capture"].exists()


def test_only_with_an_unreadable_switch_file_refuses_a_switchable_gate(harness):
    write_switch(harness, "{broken")

    result = run_gate(harness, PR, "--only", "kimi")

    assert result.returncode == 4
    assert "source=unreadable" in result.stderr
    assert not harness["capture"].exists()


def test_only_refuses_fail_closed_when_the_switch_cannot_be_consulted(harness):
    (harness["vnx_home"] / "scripts" / "providers_cli.py").unlink()

    result = run_gate(harness, PR, "--only", "codex")

    assert result.returncode == 1
    assert "Could not consult the provider switch for 'codex_gate'" in result.stderr
    assert not harness["capture"].exists()


# ---------------------------------------------------------------------------
# Fix round 2 (PR #2058, codex warning): the refusal lands in the gate audit ledger
# ---------------------------------------------------------------------------

def test_only_a_switched_off_gate_records_its_refusal_in_the_gate_audit_ledger(harness):
    """ADR-005: the refusal is a gate decision, so it is a GATE-9 line in
    gate_execution_audit.ndjson, written by gate_recorder.write_skip_rationale, before the
    command returns 4."""
    switch_off(harness, "kimi")

    result = run_gate(harness, PR, "--only", "kimi")

    assert result.returncode == 4, result.stderr
    [record] = audit_records(harness)
    assert record["event_type"] == "gate_skip_rationale"
    assert record["gate"] == "kimi_gate"
    assert record["pr_id"] == PR
    assert record["reason"] == "provider_switched_off"
    detail = record["reason_detail"]
    assert detail.startswith("provider_switched_off: kimi_gate family=kimi ")
    assert "source=global" in detail and "scope=global" in detail
    assert "consumer=vnx gate --only" in detail
    assert "reason='tegoed'" in detail
    # The refusal comes before the PR is looked up: no head, and gh is never called.
    assert record["commit_sha"] == ""
    assert record["provider_check"]["provider_kind"] == "harness_lane"
    assert not harness["gh_log"].exists()
    assert not harness["capture"].exists()


def test_only_an_allowed_gate_writes_no_refusal_line(harness):
    switch_off(harness, "kimi")

    result = run_gate(harness, PR, "--only", "deepseek")

    assert result.returncode == 0, result.stderr
    assert audit_records(harness) == []


def test_only_a_refusal_that_cannot_be_recorded_returns_1_and_launches_nothing(harness):
    """An unrecorded decision never reads as an ordinary switch refusal (exit 4)."""
    switch_off(harness, "kimi")
    harness["audit"].mkdir()  # the ledger path is a directory: the append fails

    result = run_gate(harness, PR, "--only", "kimi")

    assert result.returncode == 1
    assert "'kimi_gate' is switched off by the provider switch" in result.stderr
    assert "Could not record the refusal in the gate audit ledger" in result.stderr
    assert not harness["capture"].exists()
    assert not harness["gh_log"].exists()


def test_only_the_fail_closed_refusal_writes_no_switch_line(harness):
    """With the switch unconsultable there is no switch decision to record: exit 1, the same
    class as gate.sh's other environment errors, and no provider_switched_off line."""
    (harness["vnx_home"] / "scripts" / "providers_cli.py").unlink()
    write_switch(harness, "{broken")

    result = run_gate(harness, PR, "--only", "kimi")

    assert result.returncode == 1
    assert audit_records(harness) == []


def test_status_only_never_consults_the_switch(harness):
    switch_off(harness, "kimi")

    result = run_gate(harness, PR, "--only", "kimi", "--status")

    assert result.returncode == 0, result.stderr
    assert not harness["capture"].exists()


# ---------------------------------------------------------------------------
# R7 (OI-2070): an unknown gate name is unknown, not switched off
# ---------------------------------------------------------------------------

REGISTERED_GATES = ("ci_gate", "claude_github_optional", "codex_gate", "deepseek_gate",
                    "gemini_review", "glm_gate", "kimi_gate", "wiring_gate")


@pytest.mark.parametrize("name", ["kimmi", "gemini", "kimi_cli"], ids=["typo", "short-name", "provider-not-gate"])
def test_only_an_unknown_gate_is_refused_as_unknown(harness, name):
    """`kimmi` is a typo the family map does not know (the old code said "switched off");
    `gemini` and `kimi_cli` are names the family map does know, so the old code let them
    through to the gate manager as a gate nobody registered."""
    result = run_gate(harness, PR, "--only", name)

    booked = [record["reason"] for record in audit_records(harness)]
    assert (result.returncode, booked) == (2, []), result.stderr
    assert f"unknown gate '{name}'" in result.stderr
    for gate in REGISTERED_GATES:
        assert gate in result.stderr
    assert "switched off" not in result.stderr
    assert "vnx providers on" not in result.stderr
    assert not harness["capture"].exists(), "review_gate_manager ran for an unknown gate"
    assert not harness["gh_log"].exists(), "the PR branch was resolved for an unknown gate"


def test_only_an_unknown_gate_is_unknown_with_every_family_switched_off(harness):
    """With the switch refusing everything, an unknown name still reads as unknown:
    the name is checked before the switch is asked."""
    switch_off(harness, "kimi", "glm", "deepseek", "gemini")

    result = run_gate(harness, PR, "--only", "kimmi")

    booked = [record["reason"] for record in audit_records(harness)]
    assert (result.returncode, booked) == (2, []), result.stderr
    assert "unknown gate 'kimmi'" in result.stderr
    assert "provider_switched_off" not in result.stderr


def test_only_every_registered_gate_passes_the_name_check(harness):
    """No registered gate is refused as unknown; the switch still decides the switchable ones."""
    switch_off(harness, "kimi")

    for gate in REGISTERED_GATES:
        result = run_gate(harness, PR, "--only", gate)
        assert "unknown gate" not in result.stderr, (gate, result.stderr)
    assert [r["gate"] for r in audit_records(harness)] == ["kimi_gate"]


def test_only_refuses_fail_closed_when_the_gate_names_cannot_be_read(harness):
    """The name check reads gate_recorder's registry; without it nothing is requested and
    nothing reads as a switch refusal or as an unknown name. The module is replaced, not
    removed: VNX_HOME's scripts/lib comes first on PYTHONPATH, so a broken copy there
    shadows any gate_recorder an inherited PYTHONPATH would otherwise find."""
    (harness["vnx_home"] / "scripts" / "lib" / "gate_recorder.py").write_text(
        'raise ImportError("gate_recorder is unavailable in this test")\n', encoding="utf-8",
    )

    result = run_gate(harness, PR, "--only", "kimi")

    assert result.returncode == 1
    assert "Could not check the gate name 'kimi_gate'" in result.stderr
    assert "unknown gate" not in result.stderr
    assert audit_records(harness) == []
    assert not harness["capture"].exists()
