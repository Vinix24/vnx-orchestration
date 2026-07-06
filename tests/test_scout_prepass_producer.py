#!/usr/bin/env python3
"""Tests for the scout pre-pass PRODUCER (build-step 5b).

Dispatch-ID: 20260626-scout-prepass-producer

Covers maybe_run_scout: opt-in flag, scope/task_class gating, key-auth provider
invocation (mocked), anti-hallucination ref-snapping, atomic sidecar write, and
the never-raises fail-open contract. The producer never uses a subscription lane.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import scout_prepass  # noqa: E402
import classifier_providers  # noqa: E402

_REFS = ["scripts/lib/foo.py:10-20", "scripts/lib/bar.py:50-60"]
_INSTRUCTION = "Refactor foo() to stream rows and update its single caller in bar."


class _FakeResult:
    def __init__(self, parsed=None, error=None, model="deepseek-v4-flash"):
        self.parsed_json = parsed
        self.error = error
        self.extra = {"model": model}
        self.cost_usd = 0.0
        self.latency_ms = 0


class _FakeProvider:
    def __init__(self, *, parsed=None, available=True, error=None):
        self._parsed, self._available, self._error = parsed, available, error

    def is_available(self):
        return self._available

    def classify(self, prompt, _max_tokens=1500):
        return _FakeResult(parsed=self._parsed, error=self._error)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
    monkeypatch.setattr(scout_prepass, "_candidate_refs", lambda paths, instr: list(_REFS))


def _patch_provider(monkeypatch, provider):
    monkeypatch.setattr(classifier_providers, "get_provider", lambda name=None: provider)


def _run(state_dir, **over):
    kw = dict(
        dispatch_id="D-1",
        instruction_text=_INSTRUCTION,
        dispatch_paths=["scripts/lib/foo.py", "scripts/lib/bar.py"],
        state_dir=state_dir,
        task_class="coding_interactive",
    )
    kw.update(over)
    return scout_prepass.maybe_run_scout(**kw)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_SCOUT_PREPASS", raising=False)
    assert _run(tmp_path) is None


def test_gate_pathless_short_instruction(tmp_path, enabled):
    # Paths are optional now (discovery fills them from the instruction), but a
    # PATHLESS dispatch needs a richer instruction than a path-bearing one; the
    # 65-char default is below the pathless floor, so it still gates out.
    assert _run(tmp_path, dispatch_paths=[], instruction_text=_INSTRUCTION) is None


def test_gate_pathless_rich_instruction_proceeds(tmp_path, enabled, monkeypatch):
    # A pathless dispatch with a rich enough instruction is NOT gated on path count:
    # discovery (mocked via the `enabled` _candidate_refs stub) + the provider run.
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    rich = _INSTRUCTION + " Add regression tests and update the docs accordingly."
    assert _run(tmp_path, dispatch_paths=[], instruction_text=rich) is not None


# ---------------------------------------------------------------------------
# Scouted flag + idempotency + pending sweep
# ---------------------------------------------------------------------------

def test_is_scouted_flag(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    assert scout_prepass.is_scouted(tmp_path, "D-1") is False
    assert _run(tmp_path) is not None
    assert scout_prepass.is_scouted(tmp_path, "D-1") is True
    # sha-bound: the flag holds only for the instruction it was scouted against.
    sha = scout_prepass._instruction_sha256(_INSTRUCTION)
    assert scout_prepass.is_scouted(tmp_path, "D-1", sha) is True
    assert scout_prepass.is_scouted(tmp_path, "D-1", "deadbeef") is False


def test_maybe_run_scout_skips_when_already_scouted(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    assert _run(tmp_path) is not None

    def _boom(*a, **k):
        raise AssertionError("model re-invoked for an already-scouted dispatch")

    monkeypatch.setattr(scout_prepass, "_invoke_scout_model", _boom)
    assert _run(tmp_path) is not None  # returns the existing sidecar, no re-scout


def test_scout_pending_sweep(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    pending = data_dir / "dispatches" / "pending"
    long_instr = _INSTRUCTION + " Add regression tests and update the docs accordingly."
    for did in ("D-a", "D-b"):
        bundle = pending / did
        bundle.mkdir(parents=True)
        (bundle / "instruction.md").write_text(long_instr)
        (bundle / "dispatch-spec.json").write_text(
            json.dumps({"dispatch_paths": [], "role": "backend"})
        )
    summary = scout_prepass.scout_pending_sweep(data_dir, state_dir)
    assert summary["produced"] == 2
    assert scout_prepass.is_scouted(state_dir, "D-a") is True
    # Idempotent: a second sweep re-scouts nothing.
    again = scout_prepass.scout_pending_sweep(data_dir, state_dir)
    assert again["produced"] == 0
    assert again["already"] == 2


def test_scout_emits_dispatch_linked_receipt(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    assert _run(tmp_path) is not None
    receipts = scout_prepass.scout_receipts_path(tmp_path)
    assert receipts.exists()
    rows = [json.loads(ln) for ln in receipts.read_text().splitlines() if ln.strip()]
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "scout_prepass"
    assert r["dispatch_id"] == "D-1"  # linkage back to the dispatch
    assert "cost_usd" in r and "duration_seconds" in r
    assert r["sidecar_path"].endswith("D-1.json")  # linkage to the scout data
    assert r["anchors"]["include"] == 1


def test_scout_receipt_not_re_emitted_when_already_scouted(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    _run(tmp_path)
    _run(tmp_path)  # idempotent skip — must not append a second receipt
    rows = scout_prepass.scout_receipts_path(tmp_path).read_text().splitlines()
    assert len([r for r in rows if r.strip()]) == 1


def test_gate_short_instruction(tmp_path, enabled):
    assert _run(tmp_path, instruction_text="fix it") is None


def test_gate_skips_docs_task_class(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    assert _run(tmp_path, task_class="docs_synthesis") is None


def test_gate_skips_headless_lane(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    assert _run(tmp_path, lane="claude_headless") is None


# ---------------------------------------------------------------------------
# Provider invocation + fail-open
# ---------------------------------------------------------------------------

def test_provider_unavailable_no_sidecar(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(available=False))
    assert _run(tmp_path) is None


def test_provider_error_no_sidecar(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(error="boom", parsed=None))
    assert _run(tmp_path) is None


def test_provider_unparseable_no_sidecar(tmp_path, enabled, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider(parsed=None))
    assert _run(tmp_path) is None


def test_no_candidates_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
    monkeypatch.setattr(scout_prepass, "_candidate_refs", lambda paths, instr: [])
    _patch_provider(monkeypatch, _FakeProvider(parsed={"include": [{"ref": _REFS[0]}]}))
    assert _run(tmp_path) is None


# ---------------------------------------------------------------------------
# Happy path + anti-hallucination
# ---------------------------------------------------------------------------

def test_happy_path_writes_sidecar(tmp_path, enabled, monkeypatch):
    parsed = {
        "include": [{"ref": _REFS[0], "why": "core change site"}],
        "maybe": [{"ref": _REFS[1], "why": "caller"}],
        "exclude": [],
        "tests": ["tests/test_foo.py"],
        "docs": ["docs/foo.md"],
        "plan_sketch": "Stream rows in foo, update bar's call.",
    }
    _patch_provider(monkeypatch, _FakeProvider(parsed=parsed))

    path = _run(tmp_path)
    assert path is not None and path.is_file()

    sidecar = scout_prepass.read_scout_sidecar(tmp_path, "D-1")
    assert sidecar is not None
    assert sidecar["dispatch_id"] == "D-1"
    assert sidecar["provider"] == "deepseek"
    assert sidecar["model"] == "deepseek-v4-flash"
    assert [i["ref"] for i in sidecar["include"]] == [_REFS[0]]
    assert sidecar["tests"] == ["tests/test_foo.py"]
    rendered = scout_prepass.format_scout_sketch(sidecar)
    assert _REFS[0] in rendered


def test_hallucinated_refs_snapped_out(tmp_path, enabled, monkeypatch):
    # Model invents a ref not in the candidate list → snapped out; nothing useful → no sidecar.
    parsed = {
        "include": [{"ref": "scripts/lib/evil.py:1-9", "why": "made up"}],
        "maybe": [{"ref": "another/invented.py:1-2"}],
        "plan_sketch": "",
    }
    _patch_provider(monkeypatch, _FakeProvider(parsed=parsed))
    assert _run(tmp_path) is None


def test_hallucinated_refs_dropped_but_plan_kept(tmp_path, enabled, monkeypatch):
    parsed = {
        "include": [{"ref": "scripts/lib/evil.py:1-9"}],   # dropped
        "maybe": [{"ref": _REFS[1], "why": "real"}],         # kept
        "plan_sketch": "Do the thing.",
    }
    _patch_provider(monkeypatch, _FakeProvider(parsed=parsed))
    path = _run(tmp_path)
    assert path is not None
    sidecar = scout_prepass.read_scout_sidecar(tmp_path, "D-1")
    assert sidecar["include"] == []
    assert [i["ref"] for i in sidecar["maybe"]] == [_REFS[1]]


# ---------------------------------------------------------------------------
# Provider safety + atomic write
# ---------------------------------------------------------------------------

def test_subscription_provider_refused(monkeypatch):
    # 'haiku' rides the Claude subscription → default-denied back to deepseek.
    monkeypatch.setenv("VNX_SCOUT_PROVIDER", "haiku")
    assert scout_prepass._scout_provider_name() == "deepseek"


def test_unknown_provider_defaults_to_deepseek(monkeypatch):
    monkeypatch.setenv("VNX_SCOUT_PROVIDER", "totally-made-up")
    assert scout_prepass._scout_provider_name() == "deepseek"


@pytest.mark.parametrize("name", ["deepseek", "ollama", "gemini", "codex"])
def test_allowed_keyauth_providers_passthrough(monkeypatch, name):
    monkeypatch.setenv("VNX_SCOUT_PROVIDER", name)
    assert scout_prepass._scout_provider_name() == name


def test_write_sidecar_atomic_and_sanitized(tmp_path):
    sc = {"schema_version": 1, "dispatch_id": "D-2", "include": [{"ref": "a.py:1-2"}]}
    p = scout_prepass.write_scout_sidecar(tmp_path, "D-2", sc)
    assert p.is_file()
    assert scout_prepass.read_scout_sidecar(tmp_path, "D-2")["include"][0]["ref"] == "a.py:1-2"
    with pytest.raises(ValueError):
        scout_prepass.write_scout_sidecar(tmp_path, "../escape", sc)


def test_maybe_run_scout_never_raises(tmp_path, enabled, monkeypatch):
    def _boom(name=None):
        raise RuntimeError("provider blew up")
    monkeypatch.setattr(classifier_providers, "get_provider", _boom)
    # Must swallow the error and fail open.
    assert _run(tmp_path) is None
