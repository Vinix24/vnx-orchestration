"""Characterisation of `deliver_via_subprocess` and `verify_pr` (golf schoon R43).

Written green on the unrefactored code; pins exact messages, exit codes, the
env handed to the spawn and the order of side effects so the extract-method
refactor can be shown byte-for-byte equal. No real claude is ever spawned.
"""
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import subprocess_dispatch  # noqa: E402
from subprocess_dispatch_internals import delivery  # noqa: E402
from attest_override import ATTEST_DIR, OVERRIDE_TRAIL_FILE, build_override_manifest  # noqa: E402
from attest_override import count_overrides_in_window, write_override_record  # noqa: E402
from attest_record import verify_attest_record, write_attest_record  # noqa: E402
from attestation import sign_manifest  # noqa: E402
from content_key import compute_diff_hash  # noqa: E402
from git_maintenance_guard import disable_auto_maintenance  # noqa: E402
from ndjson_hash_chain import append_chained_entry  # noqa: E402
from verify_pr import verify_pr  # noqa: E402

TS = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    d = tmp_path_factory.mktemp("key")
    kp = d / "k"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(kp), "-N", ""], check=True, capture_output=True)
    signers = d / "allowed_signers"
    signers.write_text(f"r43@local {kp.with_suffix('.pub').read_text().strip()}\n")
    return {"key_path": kp, "identity": "r43@local", "signers": signers}


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, name, content):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    disable_auto_maintenance(tmp_path)
    _git(tmp_path, "config", "user.email", "t@vnx.local")
    _git(tmp_path, "config", "user.name", "T")
    _commit(tmp_path, "README.md", "base\n")
    return tmp_path


def _vp(repo, base, key, **kw):
    return verify_pr(repo_root=repo, base_ref=base, head_ref="HEAD", allowed_signers_override=key["signers"], **kw)


def _override(repo, base, key, reason="incident"):
    ck = compute_diff_hash(repo_root=repo, base_ref=base)
    write_override_record(content_key=ck, reason=reason, dispatch_id="D-r43", signer_identity=key["identity"],
                          timestamp=TS, key_path=key["key_path"], repo_root=repo, allowed_signers=key["signers"])
    return ck


def _extra_trail_entry(repo, key, ck="other-key"):
    m = build_override_manifest(content_key=ck, reason="older", dispatch_id="D-old",
                                signer_identity=key["identity"], timestamp=TS)
    append_chained_entry(repo / ATTEST_DIR / OVERRIDE_TRAIL_FILE, sign_manifest(m, key["key_path"]))


class TestVerifyPr:
    def test_exempt_message_and_verbose_line(self, repo, key, capsys):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "docs/a.txt", "x\n")
        assert _vp(repo, base, key, verbose=True) == (
            0, "exempt: docs/tests/md only — un-attributed lane (named residual, accepted v1)")
        assert capsys.readouterr().err == (
            f"[verify-pr] base={base} head=HEAD merge_base={base[:12]} changed=1 classification=exempt\n")

    def test_merge_base_failure(self, repo, key):
        code, msg = _vp(repo, "no-such-ref", key)
        assert code == 1 and msg.startswith("FAIL: merge-base resolution failed: git merge-base 'no-such-ref' 'HEAD' failed: ")

    def test_config_error_without_base_signers(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "scripts/x.py", "x = 1\n")
        assert verify_pr(repo_root=repo, base_ref=base) == (
            2, f"CONFIG ERROR: .vnx-attest/allowed_signers not found at base branch {base!r}. "
               "Add .vnx-attest/allowed_signers at the base branch (see docs/governance/KEY_PROVISIONING.md).")

    def test_no_attest_fail_reason(self, repo, key):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "scripts/x.py", "x = 1\n")
        _, reason = verify_attest_record(allowed_signers=key["signers"], repo_root=repo, base_ref=base, head_ref="HEAD")
        assert _vp(repo, base, key) == (1, f"FAIL: {reason}")

    def test_pass_with_advisory_evidence_line(self, repo, key, capsys, monkeypatch):
        monkeypatch.delenv("VNX_EVIDENCE_BOUND_GATE", raising=False)
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "scripts/x.py", "x = 1\n")
        write_attest_record(dispatch_id="D-r43", deliverable_id="R43", track_id="t", plan_gate_ref="g",
                            signer_identity=key["identity"], timestamp=TS, key_path=key["key_path"],
                            repo_root=repo, base_ref=base)
        assert _vp(repo, base, key) == (0, "PASS: attestation valid")
        assert capsys.readouterr().err.startswith("[evidence-bound advisory] ")

    def test_override_recorded(self, repo, key):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "scripts/x.py", "x = 1\n")
        ck = _override(repo, base, key, reason="prod down")
        assert _vp(repo, base, key) == (
            0, f"override: RECORDED deviation — signed by 'r43@local' for {ck[:12]}… | reason: 'prod down'")

    def test_override_without_trail_row(self, repo, key):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "scripts/x.py", "x = 1\n")
        _override(repo, base, key)
        (repo / ATTEST_DIR / OVERRIDE_TRAIL_FILE).unlink()
        assert _vp(repo, base, key) == (
            1, "override REJECTED: override record is not recorded in the append-only trail (audit-ledger bypass)")

    def test_override_budget_exhausted(self, repo, key, monkeypatch):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "scripts/x.py", "x = 1\n")
        _override(repo, base, key)
        _extra_trail_entry(repo, key)
        monkeypatch.setattr("attest_override.DEFAULT_OVERRIDE_BUDGET", 1)
        assert _vp(repo, base, key) == (1, "override REJECTED: override budget exhausted (2/1)")

    def test_override_trail_reset_against_base(self, repo, key):
        _extra_trail_entry(repo, key)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "base trail")
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "scripts/x.py", "x = 1\n")
        (repo / ATTEST_DIR / OVERRIDE_TRAIL_FILE).unlink()
        _override(repo, base, key)
        assert _vp(repo, base, key) == (
            1, "override REJECTED: override trail does not append-only extend the base branch (budget-reset attempt)")

    def test_override_broken_chain_value_error(self, repo, key):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "scripts/x.py", "x = 1\n")
        _override(repo, base, key)
        _extra_trail_entry(repo, key)
        trail = repo / ATTEST_DIR / OVERRIDE_TRAIL_FILE
        lines = trail.read_text().splitlines()
        second = json.loads(lines[1])
        second["prev_hash"] = "0" * 64
        trail.write_text(lines[0] + "\n" + json.dumps(second) + "\n")
        with pytest.raises(ValueError) as exc:
            count_overrides_in_window(trail, allowed_signers=key["signers"])
        assert _vp(repo, base, key) == (1, f"override REJECTED: {exc.value}")


# ---------------------------------------------------------------------------
# deliver_via_subprocess
# ---------------------------------------------------------------------------

class _Tracker:
    def __init__(self, rotate_after):
        self.n, self.rotate_after = 0, rotate_after

    def update(self, event):
        self.n += 1

    @property
    def should_rotate(self):
        return self.rotate_after is not None and self.n >= self.rotate_after


def _run_delivery(tmp_path, *, returncode=0, timed_out=False, rotate_after=None, adapter=True):
    calls = []
    store = SimpleNamespace(clear=lambda t, archive_dispatch_id: calls.append(("clear", t, archive_dispatch_id)))
    fake_adapter = SimpleNamespace(event_store=store, trigger_report_pipeline=lambda t, d, cwd: calls.append(("trigger", t, d, cwd)))
    seen = {}

    def fake_spawn(**kw):
        seen.update(kw)
        calls.append(("spawn",))
        ev = SimpleNamespace(type="tool_use", data={"name": "Write", "input": {"file_path": "scripts/a.py"}})
        for _ in range(3):
            if kw["on_event"](ev) is False:
                calls.append(("stopped",))
                break
        return SimpleNamespace(token_usage={"in": 1}, completion_text="done", session_id="sess-1", events_written=3,
                               returncode=returncode, timed_out=timed_out, spawn_refused="refused-x",
                               _adapter=fake_adapter if adapter else None)

    class _Thread:
        def join(self, timeout):
            calls.append(("join", timeout))

    hb = threading.Event()
    p = "subprocess_dispatch_internals.delivery."
    with patch(p + "_prepare_dispatch", return_value=("INSTR", "handover.md", tmp_path, tmp_path / "m.json")), \
         patch(p + "_load_resume_session", return_value="resume-1"), \
         patch(p + "_build_worker_identity_env", return_value={"VNX_ID": "x"}), \
         patch(p + "_start_heartbeat", return_value=(hb, _Thread(), "ss", "st")), \
         patch(p + "_stop_silence_heartbeat", side_effect=lambda a, b: calls.append(("silence", a, b))), \
         patch(p + "_save_resume_session", side_effect=lambda *a: calls.append(("save",) + a)), \
         patch(p + "_mark_handover_processed", side_effect=lambda h: calls.append(("handover", h))), \
         patch("subprocess_dispatch._promote_manifest", side_effect=lambda d, **k: calls.append(("promote", d, k)) or tmp_path / "done.json"), \
         patch("subprocess_dispatch._write_rotation_handover", side_effect=lambda *a: calls.append(("rotate",))), \
         patch("subprocess_dispatch.HeadlessContextTracker", side_effect=lambda **k: _Tracker(rotate_after)), \
         patch("subprocess_dispatch.SubprocessAdapter", return_value=fake_adapter), \
         patch("vnx_paths.resolve_worker_state_dir", return_value=tmp_path / "ws"), \
         patch("provider_spawns.claude_spawn.spawn_claude", side_effect=fake_spawn):
        result = subprocess_dispatch.deliver_via_subprocess(
            "T1", "do it", "opus", "d-r43", role="backend-developer", dispatch_paths=["scripts/a.py:read_write"],
            total_deadline=50.0, chunk_timeout=20.0)
    return result, calls, seen, hb


def test_delivery_success_env_and_order(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_TOTAL_DEADLINE", raising=False)
    monkeypatch.delenv("VNX_CHUNK_TIMEOUT", raising=False)
    result, calls, seen, hb = _run_delivery(tmp_path)
    assert result.success is True and result.session_id == "sess-1" and result.event_count == 3
    assert result.manifest_path == tmp_path / "done.json" and result.touched_files == frozenset({"scripts/a.py"})
    assert seen["extra_env"] == {"VNX_ID": "x", "VNX_WORKER_STATE_DIR": str(tmp_path / "ws"),
                                 "VNX_WORKER_ROLE": "backend-developer",
                                 "VNX_DISPATCH_PATHS": json.dumps(["scripts/a.py:read_write"])}
    assert (seen["prompt"], seen["resume_session"], seen["cwd"]) == ("INSTR", "resume-1", tmp_path)
    assert calls == [("spawn",), ("promote", "d-r43", {"stage": "completed"}), ("save", "T1", "sess-1", "d-r43"),
                     ("handover", "handover.md"), ("join", 5), ("silence", "ss", "st"),
                     ("clear", "T1", "d-r43"), ("trigger", "T1", "d-r43", str(tmp_path))]
    assert hb.is_set()
    assert delivery._dispatch_token_usage.pop("d-r43") == {"in": 1}
    assert delivery._dispatch_completion_text.pop("d-r43") == "done"


@pytest.mark.parametrize("returncode,timed_out,log", [
    (3, False, "deliver_via_subprocess: subprocess exited 3 for T1 — fail-closed"),
    (0, True, "deliver_via_subprocess: timeout-terminated dispatch d-r43 for T1 — fail-closed"),
])
def test_delivery_failure_paths(tmp_path, caplog, returncode, timed_out, log):
    with caplog.at_level("WARNING", logger=delivery.logger.name):
        result, calls, _, _ = _run_delivery(tmp_path, returncode=returncode, timed_out=timed_out, adapter=False)
    assert (result.success, result.manifest_path, result.spawn_refused) == (False, tmp_path / "m.json", "refused-x")
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [log]
    assert [c[0] for c in calls] == ["spawn", "join", "silence", "clear", "trigger"]


def test_delivery_rotation(tmp_path):
    result, calls, _, _ = _run_delivery(tmp_path, rotate_after=2)
    assert (result.success, result.manifest_path) == (False, tmp_path / "done.json")
    assert calls[:4] == [("spawn",), ("rotate",), ("stopped",), ("promote", "d-r43", {})]
    assert [c[0] for c in calls[4:]] == ["join", "silence", "clear", "trigger"]
