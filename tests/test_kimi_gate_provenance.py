"""kimi_gate.py write-path tests (OI-1093).

kimi_gate.py used to json.dump its result record directly. It now routes
through gate_recorder.record_terminal_result, the shared write path that
refuses a terminal result without producer identity. These tests prove the
refactor did not change kimi_gate's own output (it always sets provider/
model/dispatch_id, so it never trips the new guard) and that the file it
writes satisfies gate_status.has_producer_identity.

_make_default_dispatcher is patched on the kimi_gate module namespace, not
on plan_gate_panel where it is defined — `from X import Y` binds the name at
import time, so patching the source module would not affect kimi_gate's
already-bound reference.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import kimi_gate
from gate_status import has_producer_identity


_FAKE_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)


def test_main_writes_result_with_producer_identity(tmp_path, monkeypatch):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text("diff --git a/x b/x\n+ok\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: lambda *a2, **k2: _FAKE_REPORT)

    rc = kimi_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])

    assert rc == 0
    out = data_dir / "state" / "review_gates" / "results" / "pr-0-kimi_gate.json"
    assert out.exists()
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "pass"
    assert has_producer_identity(record) is True
    assert not out.with_suffix(".json.tmp").exists()


def test_main_offline_run_is_stamped_test_run(tmp_path, monkeypatch):
    """--diff-file runs are test_run:true regardless of the recorder refactor —
    closure_verifier must still refuse them as evidence for a real PR."""
    diff_file = tmp_path / "x.diff"
    diff_file.write_text("diff --git a/x b/x\n+ok\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: lambda *a2, **k2: _FAKE_REPORT)

    kimi_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])

    out = data_dir / "state" / "review_gates" / "results" / "pr-0-kimi_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["test_run"] is True
