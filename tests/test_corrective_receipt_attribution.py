"""Corrective completion receipts must be attributable.

Defect: ``phantom_guard.record_phantom_if_any`` and
``pr_enforcement._record_corrective_receipt`` append a ``failed``
``subprocess_completion`` receipt with no ``provider``/``terminal``. Receipt
enrichment (``append_receipt_internals/enrichment.py``) then filled the gap
from the session resolver, which defaults ``terminal`` to ``"unknown"`` and
maps that to the literal provider ``"unknown"``. The result: the exact
rejections the learning loop must attribute — which provider fabricated a
success, which lane never opened a PR — landed as
``provider='unknown', terminal='unknown'`` next to a real model.

Fix, exercised through the REAL append path here (never a mocked
``append_receipt_payload``):
  1. the calling lane passes its real provider/terminal through to the guard;
  2. enrichment never stamps the sentinel ``"unknown"`` as a provider — an
     undeterminable provider leaves the field ABSENT, the same discipline the
     model field already follows, and a caller-supplied real provider/terminal
     is never overwritten.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import phantom_guard as pg  # noqa: E402
import pr_enforcement as pe  # noqa: E402


def _pinned_ledger(tmp_path: Path, monkeypatch) -> Path:
    """Pin the store so the real append path stays in tmp_path.

    VNX_STATE_DIR is the store enrichment reads; pointing both the ledger and
    VNX_STATE_DIR at the SAME directory makes the primary ledger the central
    one too, so the dual-write mirror is a no-op (cutover skip). Nothing here
    touches the real store.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    for key in ("VNX_CURRENT_PROVIDER", "VNX_CURRENT_TERMINAL", "VNX_CURRENT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    return state_dir / "t0_receipts.ndjson"


def _last_line(ledger: Path) -> dict:
    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, f"no receipt landed in {ledger}"
    return json.loads(lines[-1])


def _phantom_context() -> "pg.PhantomDecisionContext":
    return pg.PhantomDecisionContext(
        role="backend-developer", task_class=None, read_only=None,
        status="done", worktree_diff=None,
    )


class _CompletedRC:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


# ---------------------------------------------------------------------------
# 1. phantom guard corrective receipt names the real provider + terminal
# ---------------------------------------------------------------------------

def test_phantom_guard_corrective_receipt_carries_provider_and_terminal(monkeypatch, tmp_path):
    ledger = _pinned_ledger(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pg, "guard_at_govern",
        lambda **kw: pg.PhantomVerdict(True, "PHANTOM: test reason"),
    )
    monkeypatch.setenv("VNX_CURRENT_MODEL", "claude-sonnet-5-5")

    verdict = pg.record_phantom_if_any(
        dispatch_id="attr-phantom-1",
        context=_phantom_context(),
        receipts_file=str(ledger),
        provider="kimi",
        terminal="T-KIMI",
    )

    assert verdict.is_phantom
    stored = _last_line(ledger)
    assert stored["source"] == "phantom_guard"
    assert stored["status"] == "failed"
    assert stored["provider"] == "kimi", stored
    assert stored["terminal"] == "T-KIMI", stored
    # the model is still carried, unchanged.
    assert stored["model"] == "sonnet-5-5", stored


def test_phantom_guard_corrective_receipt_provider_resolves_from_terminal(monkeypatch, tmp_path):
    """When the lane passes only a terminal, enrichment resolves a REAL
    provider from it instead of leaving/faking the sentinel."""
    ledger = _pinned_ledger(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pg, "guard_at_govern",
        lambda **kw: pg.PhantomVerdict(True, "PHANTOM: test reason"),
    )

    pg.record_phantom_if_any(
        dispatch_id="attr-phantom-2",
        context=_phantom_context(),
        receipts_file=str(ledger),
        terminal="CODEX-T3",
    )

    stored = _last_line(ledger)
    assert stored["terminal"] == "CODEX-T3", stored
    assert stored["provider"] == "codex_cli", stored


# ---------------------------------------------------------------------------
# 2. PR-enforcement corrective receipt names the real provider + terminal
# ---------------------------------------------------------------------------

def test_pr_enforcement_corrective_receipt_carries_provider_and_terminal(monkeypatch, tmp_path):
    ledger = _pinned_ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("VNX_CURRENT_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(
        pe.subprocess, "run",
        lambda *a, **kw: _CompletedRC(128, err="fatal: could not read username"),
    )

    result = pe.enforce_pr_exists(
        dispatch_id="attr-pr-1",
        branch="dispatch/attr-pr-1",
        worktree_state="committed",
        repo_root=tmp_path,
        receipts_file=str(ledger),
        pr_title="t",
        pr_body="b",
        provider="deepseek-harness",
        terminal="T-DEEPSEEK",
    )

    assert result.ok is False
    stored = _last_line(ledger)
    assert stored["source"] == "pr_enforcement"
    assert stored["status"] == "failed"
    assert stored["autopr_rejected"] is True
    assert stored["provider"] == "deepseek-harness", stored
    assert stored["terminal"] == "T-DEEPSEEK", stored


# ---------------------------------------------------------------------------
# 3. the sentinel "unknown" is never stamped as a provider
# ---------------------------------------------------------------------------

def test_corrective_payload_without_provider_omits_the_sentinel(monkeypatch, tmp_path):
    """A corrective payload with no provider and no terminal must NOT gain the
    literal ``provider='unknown'`` from enrichment — the field stays absent,
    exactly like an undeterminable model. This is the exact base-code defect."""
    from append_receipt import append_receipt_payload

    ledger = _pinned_ledger(tmp_path, monkeypatch)
    append_receipt_payload(
        {
            "event_type": "subprocess_completion",
            "receipt_kind": "dispatch",
            "dispatch_id": "attr-sentinel-1",
            "status": "failed",
            "phantom_rejected": True,
            "source": "phantom_guard",
            "synthesized": False,
            "model": "claude-sonnet-5-5",
            "timestamp": "2026-09-28T10:00:00Z",
        },
        receipts_file=str(ledger),
        cache_window_seconds=0,
    )

    stored = _last_line(ledger)
    assert stored.get("provider") in (None, ""), (
        f"an undeterminable provider must not be stored as a fake name, got "
        f"{stored.get('provider')!r}"
    )


def test_caller_supplied_provider_and_terminal_are_never_overwritten(monkeypatch, tmp_path):
    """A caller that already carries a real provider/terminal keeps them even
    when the resolver, keyed on the terminal, would say something else
    (terminal T1 resolves to claude_code, but the payload says kimi)."""
    from append_receipt import append_receipt_payload

    ledger = _pinned_ledger(tmp_path, monkeypatch)
    append_receipt_payload(
        {
            "event_type": "subprocess_completion",
            "receipt_kind": "dispatch",
            "dispatch_id": "attr-preserve-1",
            "status": "failed",
            "autopr_rejected": True,
            "source": "pr_enforcement",
            "synthesized": False,
            "model": "kimi-k3",
            "provider": "kimi",
            "terminal": "T1",
            "timestamp": "2026-09-28T10:00:00Z",
        },
        receipts_file=str(ledger),
        cache_window_seconds=0,
    )

    stored = _last_line(ledger)
    assert stored["provider"] == "kimi", stored
    assert stored["terminal"] == "T1", stored


def test_terminal_id_alias_is_accepted_by_both_guards(monkeypatch, tmp_path):
    """The lane field is ``terminal_id`` (EnvelopeSpec/GovernSpec); the receipt
    field is ``terminal``. Both spellings must work at the guard boundary."""
    phantom_ledger = _pinned_ledger(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pg, "guard_at_govern",
        lambda **kw: pg.PhantomVerdict(True, "PHANTOM: test reason"),
    )
    pg.record_phantom_if_any(
        dispatch_id="attr-alias-phantom",
        context=_phantom_context(),
        receipts_file=str(phantom_ledger),
        terminal_id="T-KIMI",
    )
    assert _last_line(phantom_ledger)["terminal"] == "T-KIMI"

    pr_ledger = phantom_ledger
    monkeypatch.setattr(
        pe.subprocess, "run",
        lambda *a, **kw: _CompletedRC(128, err="fatal: could not read username"),
    )
    pe.enforce_pr_exists(
        dispatch_id="attr-alias-pr",
        branch="dispatch/attr-alias-pr",
        worktree_state="committed",
        repo_root=tmp_path,
        receipts_file=str(pr_ledger),
        pr_title="t",
        pr_body="b",
        provider="kimi",
        terminal_id="T-KIMI",
    )
    assert _last_line(pr_ledger)["terminal"] == "T-KIMI"



# ---------------------------------------------------------------------------
# 4. the door's env export is the fallback when a call site passes nothing
# ---------------------------------------------------------------------------

def test_phantom_guard_uses_door_env_export_when_no_parameter(monkeypatch, tmp_path):
    ledger = _pinned_ledger(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pg, "guard_at_govern",
        lambda **kw: pg.PhantomVerdict(True, "PHANTOM: test reason"),
    )
    monkeypatch.setenv("VNX_CURRENT_PROVIDER", "glm")
    monkeypatch.setenv("VNX_CURRENT_TERMINAL", "T-GLM")

    pg.record_phantom_if_any(
        dispatch_id="attr-env-phantom",
        context=_phantom_context(),
        receipts_file=str(ledger),
    )

    stored = _last_line(ledger)
    assert stored["provider"] == "glm", stored
    assert stored["terminal"] == "T-GLM", stored


def test_pr_enforcement_uses_door_env_export_when_no_parameter(monkeypatch, tmp_path):
    ledger = _pinned_ledger(tmp_path, monkeypatch)
    monkeypatch.setenv("VNX_CURRENT_PROVIDER", "glm")
    monkeypatch.setenv("VNX_CURRENT_TERMINAL", "T-GLM")
    monkeypatch.setattr(
        pe.subprocess, "run",
        lambda *a, **kw: _CompletedRC(128, err="fatal: could not read username"),
    )

    pe.enforce_pr_exists(
        dispatch_id="attr-env-pr",
        branch="dispatch/attr-env-pr",
        worktree_state="committed",
        repo_root=tmp_path,
        receipts_file=str(ledger),
        pr_title="t",
        pr_body="b",
    )

    stored = _last_line(ledger)
    assert stored["provider"] == "glm", stored
    assert stored["terminal"] == "T-GLM", stored


def test_sentinel_provider_from_env_or_parameter_is_never_stored(monkeypatch, tmp_path):
    ledger = _pinned_ledger(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pg, "guard_at_govern",
        lambda **kw: pg.PhantomVerdict(True, "PHANTOM: test reason"),
    )
    monkeypatch.setenv("VNX_CURRENT_PROVIDER", "unknown")

    pg.record_phantom_if_any(
        dispatch_id="attr-sentinel-env",
        context=_phantom_context(),
        receipts_file=str(ledger),
        provider="unknown",
    )

    assert _last_line(ledger).get("provider") in (None, "")


# ---------------------------------------------------------------------------
# 5. two projects, colliding dispatch ids: identities must not leak
# ---------------------------------------------------------------------------

def test_colliding_dispatch_id_in_second_project_keeps_its_own_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pg, "guard_at_govern",
        lambda **kw: pg.PhantomVerdict(True, "PHANTOM: test reason"),
    )
    stored = {}
    for project, provider, terminal in (("proj-a", "kimi", "T-A"), ("proj-b", "glm", "T-B")):
        project_dir = tmp_path / project
        project_dir.mkdir()
        ledger = _pinned_ledger(project_dir, monkeypatch)
        monkeypatch.setenv("VNX_PROJECT_ID", project)
        pg.record_phantom_if_any(
            dispatch_id="attr-collide-1",
            context=_phantom_context(),
            receipts_file=str(ledger),
            provider=provider,
            terminal=terminal,
        )
        stored[project] = _last_line(ledger)

    assert stored["proj-a"]["provider"] == "kimi"
    assert stored["proj-a"]["terminal"] == "T-A"
    assert stored["proj-b"]["provider"] == "glm"
    assert stored["proj-b"]["terminal"] == "T-B"
    assert stored["proj-a"]["project_id"] == "proj-a"
    assert stored["proj-b"]["project_id"] == "proj-b"
