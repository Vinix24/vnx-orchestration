"""Characterisation of `_govern_impl` (golf schoon R32).

Written green on the code before the extract-method refactor; it must stay
green after. Real temporary dirs and the real emit/receipt writers (wrapped to
record their order); only git (an external process) is patched out of the
synthesis path.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "scripts" / "lib", _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dispatch_govern
from dispatch_govern import GovernRaw, GovernSpec, _govern_impl
from report_body_contract import CONTRACT_INVALID_STATUS

DID = "r32-char-001"

_VALID = (
    f"# Dispatch {DID}\n\n## Summary\n\nImplemented the feature with full test coverage "
    "and every check green on the head commit.\n\n## Changes\n\n- scripts/lib/foo.py: new\n\n"
    "## Verification\n\npytest tests/test_foo.py -q: 3 passed.\n\n## Open Items\n\nNone.\n"
)


# ---------------------------------------------------------------------------
# _govern_impl
# ---------------------------------------------------------------------------

@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_CONTRACT_VALIDATE", raising=False)
    data = tmp_path / "data"
    state = tmp_path / "state"
    data.mkdir()
    state.mkdir()
    return data, state


def _spec(data, state, **kw):
    return GovernSpec(dispatch_id=DID, terminal_id="T1", instruction="Do it.",
                      data_dir=data, state_dir=state, model="sonnet", **kw)


def _central(data):
    return data / "unified_reports" / f"{DID}.md"


def _write_central(data, text):
    p = _central(data)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _run(spec, raw=None, lane="tmux_interactive"):
    """Run _govern_impl, record emit/receipt order, keep git out of synthesis."""
    import governance_emit
    order = []
    real_emit = governance_emit.emit_unified_report
    real_receipt = dispatch_govern.ensure_receipt

    def emit(**kw):
        order.append(("emit", kw["frontmatter"]["contract_status"]))
        return real_emit(**kw)

    def receipt(*a, **kw):
        order.append(("receipt", kw["status"], kw["report_path"] is not None,
                      kw["report_relocated_from"]))
        return real_receipt(*a, **kw)

    with patch.object(governance_emit, "emit_unified_report", side_effect=emit), \
         patch.object(dispatch_govern, "ensure_receipt", side_effect=receipt), \
         patch.object(dispatch_govern, "_git_summary", return_value="feat: x. Synthesized."), \
         patch.object(dispatch_govern, "_git_changes", return_value="a.py | 1 +"):
        out = _govern_impl(spec, raw or GovernRaw(receipt={"status": "done"}, duration_seconds=2.0), lane)
    return out, order


def _fm(path):
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return text


def test_authored_valid_report(dirs):
    data, state = dirs
    _write_central(data, _VALID)
    out, order = _run(_spec(data, state))
    assert out.contract_status == "authored"
    assert out.permission_enforcement == "soft"
    assert out.error is None
    assert out.body_result is not None and out.body_result.valid
    assert out.report_path == _central(data)
    text = _fm(out.report_path)
    assert "contract_status: authored" in text
    assert "## Summary" in text
    assert order == [("emit", "authored"), ("receipt", "done", True, None)]


def test_authored_report_declaring_failure_gets_failed_status(dirs):
    data, state = dirs
    _write_central(data, _VALID.replace("## Summary", "**Status**: failed\n\n## Summary"))
    out, order = _run(_spec(data, state))
    assert out.contract_status == "authored"
    assert order == [("emit", "authored"), ("receipt", "failed", True, None)]


def test_authored_existing_frontmatter_is_merged(dirs):
    data, state = dirs
    _write_central(data, "---\nworker_note: keep-me\ncontract_status: bogus\n---\n" + _VALID)
    out, _ = _run(_spec(data, state))
    text = _fm(out.report_path)
    assert "worker_note: keep-me" in text
    assert "contract_status: authored" in text
    assert "contract_status: bogus" not in text
    assert text.count("# Dispatch r32-char-001") == 1


def test_invalid_worker_report_synthesizes_shadow(dirs, caplog):
    data, state = dirs
    _write_central(data, "## Summary\n\nshort\n")
    with caplog.at_level(logging.INFO, logger="dispatch_govern"):
        out, order = _run(_spec(data, state))
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("govern: worker report exists but invalid (missing=") and
               m.endswith(f" for dispatch={DID} — synthesizing") for m in msgs)
    assert any(m.startswith("govern: synthesized body failed validation for dispatch=")
               and m.endswith("— flagging violated (shadow)") for m in msgs)
    assert out.contract_status == "synthesized"
    assert out.permission_enforcement == "soft"
    assert order == [("emit", "synthesized"), ("receipt", CONTRACT_INVALID_STATUS, True, None)]


def test_enforce_synthesized_stays_synthesized_strict(dirs, monkeypatch):
    data, state = dirs
    monkeypatch.setenv("VNX_CONTRACT_VALIDATE", " Enforce ")
    out, order = _run(_spec(data, state), raw=GovernRaw(receipt=None, duration_seconds=1.0))
    assert out.contract_status == "synthesized"
    assert out.permission_enforcement == "strict"
    assert order == [("emit", "synthesized"), ("receipt", CONTRACT_INVALID_STATUS, True, None)]


def test_enforce_authored_violated_returns_early(dirs, monkeypatch):
    data, state = dirs
    monkeypatch.setenv("VNX_CONTRACT_VALIDATE", "enforce")
    _write_central(data, _VALID)
    import report_body_contract as rbc
    real = rbc.validate_body
    calls = []

    def flaky(body, pr_id=None):
        calls.append(body)
        res = real(body, pr_id=pr_id)
        if len(calls) == 2:
            res.valid = False
        return res

    with patch.object(rbc, "validate_body", side_effect=flaky):
        out, order = _run(_spec(data, state))
    assert out.contract_status == "violated"
    assert out.report_path is None
    assert out.permission_enforcement == "strict"
    assert out.error.startswith("contract_violated: missing=")
    assert order == []


def test_shadow_authored_violated_continues(dirs):
    data, state = dirs
    _write_central(data, _VALID)
    import report_body_contract as rbc
    real = rbc.validate_body
    calls = []

    def flaky(body, pr_id=None):
        calls.append(body)
        res = real(body, pr_id=pr_id)
        if len(calls) == 2:
            res.valid = False
        return res

    with patch.object(rbc, "validate_body", side_effect=flaky):
        out, order = _run(_spec(data, state))
    assert out.contract_status == "violated"
    assert order == [("emit", "violated"), ("receipt", CONTRACT_INVALID_STATUS, True, None)]


def test_plan_reviewer_with_fence_is_authored(dirs, caplog):
    data, state = dirs
    _write_central(data, "Review text.\n\n```vnx-plan-verdict\nVERIFIED\n```\n")
    with caplog.at_level(logging.INFO, logger="dispatch_govern"):
        out, order = _run(_spec(data, state, role="plan-reviewer"))
    assert out.contract_status == "authored"
    assert out.body_result is None
    assert "```vnx-plan-verdict" in out.report_path.read_text(encoding="utf-8")
    assert any("plan-reviewer report accepted as authored" in r.getMessage() for r in caplog.records)
    assert order[0] == ("emit", "authored")


def test_plan_reviewer_without_fence_synthesizes(dirs, caplog):
    data, state = dirs
    _write_central(data, "Review text without a fence.\n")
    with caplog.at_level(logging.INFO, logger="dispatch_govern"):
        out, _ = _run(_spec(data, state, role="plan-reviewer"))
    assert out.contract_status == "synthesized"
    assert any("has no verdict fence" in r.getMessage() for r in caplog.records)


def test_panelist_nonempty_authored_and_empty_synthesized(dirs, caplog):
    data, state = dirs
    _write_central(data, "Free-form panel analysis.\n")
    out, _ = _run(_spec(data, state, role="deliberation-panelist"))
    assert out.contract_status == "authored" and out.body_result is None
    _write_central(data, "   \n")
    with caplog.at_level(logging.INFO, logger="dispatch_govern"):
        out2, _ = _run(_spec(data, state, role="deliberation-panelist"))
    assert out2.contract_status == "synthesized"
    assert any(r.getMessage() == f"govern: deliberation-panelist report exists but is empty "
               f"for dispatch={DID} — synthesizing" for r in caplog.records)


def test_stray_report_relocated_to_central(dirs, tmp_path):
    data, state = dirs
    repo = tmp_path / "repo"
    stray = repo / ".vnx-data" / "unified_reports" / f"{DID}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(_VALID, encoding="utf-8")
    out, order = _run(_spec(data, state, repo_root=repo))
    assert out.contract_status == "authored"
    assert not stray.exists()
    assert out.report_path == _central(data)
    assert order[-1] == ("receipt", "done", True, str(stray))


def test_invalid_stray_report_is_left_in_place(dirs, tmp_path):
    data, state = dirs
    repo = tmp_path / "repo"
    stray = repo / ".vnx-data" / "unified_reports" / f"{DID}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("junk\n", encoding="utf-8")
    out, order = _run(_spec(data, state, repo_root=repo))
    assert out.contract_status == "synthesized"
    assert stray.read_text(encoding="utf-8") == "junk\n"
    assert order[-1][3] is None


def test_emit_failure_still_writes_receipt(dirs, caplog):
    data, state = dirs
    import governance_emit
    order = []
    real_receipt = dispatch_govern.ensure_receipt

    def receipt(*a, **kw):
        order.append((kw["status"], kw["report_path"]))
        return real_receipt(*a, **kw)

    with patch.object(governance_emit, "emit_unified_report", side_effect=RuntimeError("disk full")), \
         patch.object(dispatch_govern, "ensure_receipt", side_effect=receipt), \
         patch.object(dispatch_govern, "_git_summary", return_value="feat: x."), \
         patch.object(dispatch_govern, "_git_changes", return_value=""), \
         caplog.at_level(logging.WARNING, logger="dispatch_govern"):
        out = _govern_impl(_spec(data, state), GovernRaw(receipt={"status": "done"}), "tmux_interactive")
    assert out.report_path is None
    assert out.error == "disk full"
    assert out.contract_status == "synthesized"
    assert order == [(CONTRACT_INVALID_STATUS, None)]
    assert any(r.getMessage() == f"govern: emit_unified_report failed for {DID}: disk full"
               for r in caplog.records)


def test_emit_receives_lane_status_response_text(dirs):
    data, state = dirs
    import governance_emit
    seen = {}
    with patch.object(governance_emit, "emit_unified_report",
                      side_effect=lambda **kw: seen.update(kw) or Path("/x.md")), \
         patch.object(dispatch_govern, "ensure_receipt"), \
         patch.object(dispatch_govern, "_git_summary", return_value="feat: x."), \
         patch.object(dispatch_govern, "_git_changes", return_value=""):
        _govern_impl(_spec(data, state), GovernRaw(receipt=None), "laneX")
    assert seen["response_text"] == "Lane: laneX. Status: timeout."
    assert seen["provider"] == "claude" and seen["model"] == "sonnet"
    assert seen["findings"] is None and seen["overwrite"] is True
