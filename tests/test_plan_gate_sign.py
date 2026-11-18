"""`vnx plan-gate sign`: the operator signs an existing plan-gate pass (OI-2849, ADR-056)."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(_ROOT / "scripts" / "lib"), str(_ROOT / "scripts")]

import actor_class  # noqa: E402
import ndjson_hash_chain  # noqa: E402
import plan_gate_enforcement  # noqa: E402
import plan_gate_evidence  # noqa: E402
import planning_cli  # noqa: E402
import tracks as tracks_lib  # noqa: E402

Grant = plan_gate_enforcement.PromotionGrant
TRACK, PROJECT, IDENTITY, LEDGER = "trk-sign", "vnx-dev", "operator@example.invalid", ".vnx-attest/plan-gates.ndjson"
pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed")


@pytest.fixture
def e(tmp_path, monkeypatch):
    """A throw-away key, an allowed_signers file and an operator shell, all on tmp_path."""
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True)
    (tmp_path / ".vnx-attest").mkdir()  # the signer file is the only source (no env override)
    (tmp_path / ".vnx-attest" / "allowed_signers").write_text(f'{IDENTITY} namespaces="vnx-attestation" {(tmp_path / "k.pub").read_text()}')
    for name in (*actor_class.DISPATCH_MARKERS, *actor_class.TERMINAL_MARKERS, actor_class.ROLE_MARKER, "VNX_PROCESS_OWNER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: True)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
    return tmp_path


def _pass(root, ts="2026-11-14T16:13:26Z"):
    assert plan_gate_evidence.emit_plan_gate_pass(repo_root=root, track_id=TRACK, project_id=PROJECT, resolver="run",
                                                  timestamp=ts, seats=2, scope="heavy", gov_trace="w=3; profile=standard (project)")
    return (root / LEDGER).read_bytes()


def _sign(root, identity=IDENTITY):
    return planning_cli.main(["plan-gate", "sign", TRACK, "--key", str(root / "k"), "--identity", identity, "--project-id", PROJECT])


def _grant(root):
    return plan_gate_enforcement.verify_promotion_grant(TRACK, PROJECT, root, root / ".vnx-attest" / "allowed_signers")


def test_operator_signs_chain_stays_valid_and_newer_unsigned_pass_wins(e, monkeypatch):
    _pass(e)
    assert _grant(e) is Grant.PRESENT_UNSIGNED
    assert _sign(e) == 0
    assert _grant(e) is Grant.VERIFIED
    entries = list(ndjson_hash_chain.walk_chain((e / LEDGER)))
    assert len(entries) == 2 == len((e / LEDGER).read_text().splitlines())
    (_, first, first_hash), (_, signed, _) = entries
    assert signed["prev_hash"] == first_hash == signed["signs_record_hash"]
    assert signed["signed_by_operator_at"] and signed["signature"]
    assert all(signed[f] == first[f] for f in ("track_id", "project_id", "resolver", "resolved_at", "seats", "scope", "gov_trace", "gate_profile"))
    monkeypatch.setenv(actor_class.ROLE_MARKER, "orchestrator")
    monkeypatch.setattr(tracks_lib, "track_autonomous_promotion_eligible", lambda *a, **k: True)
    refusal, extra = planning_cli._promotion_refusal(e, PROJECT, {"output_kind": "pr"}, TRACK)
    assert refusal is None and extra["grant"]["state"] == "verified"
    _pass(e, ts="2026-11-15T08:00:00Z")  # a newer unsigned plan-gate run wins as the latest record
    assert _grant(e) is Grant.PRESENT_UNSIGNED


_REFUSALS = {"t0": (2, "only the operator"), "no_pass": (3, "no plan_gate_pass"),
             "identity": (4, "is not in"), "no_signers_file": (4, "no allowed_signers")}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_refusal_writes_nothing_and_grant_stays_unsigned(e, monkeypatch, capsys, case):
    code, message = _REFUSALS[case]
    before = None if case == "no_pass" else _pass(e)
    if case == "t0":
        monkeypatch.setenv(actor_class.ROLE_MARKER, "orchestrator")
    if case == "no_signers_file":
        (e / ".vnx-attest" / "allowed_signers").unlink()
    assert _sign(e, identity="stranger@example.invalid" if case == "identity" else IDENTITY) == code
    assert message in capsys.readouterr().err
    assert ((e / LEDGER).read_bytes() if before else (e / LEDGER).exists()) == (before or False)
    assert _grant(e) is (Grant.ABSENT if before is None else Grant.PRESENT_UNSIGNED)



def test_pass_landing_between_read_and_append_is_refused(e, monkeypatch, capsys):
    _pass(e)
    read = plan_gate_evidence.verify_plan_gate_pass

    def read_then_b_lands(*a):  # the sign step reads A, then pass B lands (once)
        monkeypatch.setattr(plan_gate_evidence, "verify_plan_gate_pass", read)
        return read(*a), _pass(e, ts="2026-11-15T08:00:00Z")
    monkeypatch.setattr(plan_gate_evidence, "verify_plan_gate_pass", lambda *a: read_then_b_lands(*a)[0])
    assert _sign(e) == 4 and "latest plan-gate pass changed" in capsys.readouterr().err
    assert len((e / LEDGER).read_bytes().splitlines()) == 2 and _grant(e) is Grant.PRESENT_UNSIGNED


def test_horizon_sign_reads_the_ledger_of_the_selected_project_dir(e, monkeypatch, tmp_path_factory):
    from vnx_cli.commands import horizon
    proj, cwd = tmp_path_factory.mktemp("proj"), tmp_path_factory.mktemp("cwd")
    _pass(proj)
    shutil.copy(e / ".vnx-attest" / "allowed_signers", proj / ".vnx-attest" / "allowed_signers")
    monkeypatch.delenv("VNX_PROJECT_ROOT")
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(cwd / "data"))
    monkeypatch.chdir(cwd)
    args = argparse.Namespace(project_dir=str(proj), project_id=PROJECT, json=False, track_id=TRACK, key=str(e / "k"), identity=IDENTITY)
    assert horizon._cmd_plan_gate_sign(args) == 0 and not (cwd / LEDGER).exists()
    assert plan_gate_enforcement.verify_promotion_grant(TRACK, PROJECT, proj, proj / ".vnx-attest" / "allowed_signers") is Grant.VERIFIED
