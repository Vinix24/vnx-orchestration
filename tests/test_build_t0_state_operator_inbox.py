"""operator-vragen-tot-antwoord Q5 (ADR-076 besluit 10): the operator_inbox block, the SessionStart lines and the role rules.

Everything runs on tmp_path. The hook only reads t0_index.json and writes no ``presented`` line."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_t0_state as bts  # noqa: E402
import inbox_cli  # noqa: E402
import operator_queue as oq  # noqa: E402
import operator_queue_inbox as inbox  # noqa: E402
import pr_queue_state  # noqa: E402
import vnx_paths  # noqa: E402

PID = "proj"
HOOK = _ROOT / "hooks" / "sessionstart.sh"
ROLE = _ROOT / ".claude" / "terminals" / "T0" / "role-orchestrator.md"
RULE_ASK = "Before you ask Vincent anything, record it with `vnx inbox ask`; a question that lives only in the chat counts as not asked."
RULE_END = "End every status update to Vincent with `vnx inbox --present --channel status`, which shows the open points and writes the `presented` lines."


def _quiet_reader():
    def reader(project_id, **kw):
        return {"bron": 3, "dekking": "gelezen", "reden": None, "unknown": []}
    return types.SimpleNamespace(READERS=(("fake", reader),))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    monkeypatch.setattr(inbox, "_MODULES", (_quiet_reader(),))
    monkeypatch.setattr(pr_queue_state, "_get_open_prs", lambda project_root: ([], None))
    monkeypatch.setattr(pr_queue_state, "_get_merged_today", lambda project_root: ([], None, False))
    state = tmp_path / "root" / "state"
    state.mkdir(parents=True)
    dispatches = tmp_path / "dispatches"
    for sub in ("pending", "active", "conflicts"):
        (dispatches / sub).mkdir(parents=True)
    return state, dispatches


def _open(state, ref, asked, block=""):
    return oq.open_point(PID, bron=3, ref=ref, vraag=f"vraag {ref}", sluit_bij="x", opened_by="t", kopieerblok=block,
                         asked_at=asked, now=asked, state_dir=state)["point_id"]


def _ledger_bytes(state):
    path = state / oq.LEDGER_FILENAME
    return path.read_bytes() if path.exists() else b""


def test_block_holds_only_open_points_in_besluit_10_order(env):
    state, _ = env
    ids = {r: _open(state, r, f"2026-11-{10 + i:02d}T00:00:00Z", block=f"BLOCK-{r}") for i, r in enumerate("abcdefg")}
    oq.close_point(PID, ids["a"], uitkomst="beantwoord", bewijs="src", now="2026-11-15T00:00:00Z", state_dir=state)
    block = bts._build_operator_inbox(PID, state)
    assert block["status"] == "ok" and block["open_total"] == 6 and block["more"] == 1
    assert len(block["points"]) == 5 and ids["a"] not in [p["point_id"] for p in block["points"]]
    assert [p["point_id"] for p in block["points"]] == [ids[r] for r in "bcdef"]  # oldest first, nothing presented yet
    assert set(block["points"][0]) == {"point_id", "vraag", "age", "presented", "kopieerblok"}
    assert block["points"][0]["kopieerblok"] == "BLOCK-b"


def test_building_twice_writes_no_presented_line(env):
    state, dispatches = env
    _open(state, "a", "2026-11-10T00:00:00Z")
    before = _ledger_bytes(state)
    first = bts.build_t0_state(state, dispatches)
    bts._build_t0_index(first)
    bts.build_t0_state(state, dispatches)
    after = _ledger_bytes(state)
    assert after == before and b"presented" not in after
    assert first["operator_inbox"]["open_total"] == 1


def test_raising_show_gives_error_and_rest_of_index_is_built(env, monkeypatch):
    state, dispatches = env

    def boom(*a, **kw):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(inbox, "show", boom)
    doc = bts.build_t0_state(state, dispatches)
    index = bts._build_t0_index(doc)
    assert doc["operator_inbox"] == {"status": "error", "reason": "RuntimeError: ledger exploded"}
    assert index["operator_inbox"]["status"] == "error"
    assert index["schema"] == "t0_index/1.6" and index["queue"] and "health" in index


def test_show_that_runs_over_the_budget_is_an_error(env, monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(bts, "_INBOX_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(inbox, "show", lambda *a, **kw: release.wait(5))
    block = bts._build_operator_inbox(PID, env[0])
    release.set()
    assert block["status"] == "error" and block["reason"].startswith("timeout")


@pytest.mark.parametrize("shown", [{}, {"status": "ok"}, {"status": "ok", "points": [{"point_id": "x"}], "reason": None,
                                                         "coverage": [], "open_total": 1, "more": 0}])
def test_malformed_show_result_is_an_error(env, monkeypatch, shown):
    monkeypatch.setattr(inbox, "show", lambda *a, **kw: shown)
    assert bts._build_operator_inbox(PID, env[0])["status"] == "error"


def test_unknown_status_passes_through_with_its_reason(env, monkeypatch):
    monkeypatch.setattr(inbox, "show", lambda *a, **kw: {"status": "unknown", "reason": "ledger unreadable",
                                                          "coverage": [], "open_total": None, "points": [], "more": 0})
    block = bts._build_operator_inbox(PID, env[0])
    assert block["status"] == "unknown" and block["open_total"] is None


# ── the hook ────────────────────────────────────────────────────────────────

def _hook_ctx(tmp_path, monkeypatch, index):
    for var in ("VNX_DATA_DIR", "VNX_STATE_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_PROJECT_ID", "VNX_HOME",
                "VNX_CANONICAL_ROOT", "VNX_PROJECT_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "vnx-data-home"))
    t0 = tmp_path / "project" / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    (tmp_path / "project" / ".vnx").mkdir()
    state = Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "t0_index.json").write_text(json.dumps(index), encoding="utf-8")
    before = sorted(p.name for p in state.iterdir())
    r = subprocess.run(["bash", str(HOOK)], capture_output=True, text=True, cwd=str(t0), env=dict(os.environ), timeout=30)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in state.iterdir()) == before  # the hook wrote no ledger
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_hook_prints_the_points(tmp_path, monkeypatch):
    index = {"operator_inbox": {"status": "ok", "reason": None, "coverage": [], "open_total": 7, "more": 5, "points": [
        {"point_id": "p1", "vraag": "Akkoord op promote X?", "age": "2d 3h", "presented": 0, "kopieerblok": "vnx promote X"},
        {"point_id": "p2", "vraag": "Welke kant op?", "age": "5h 0m", "presented": 2, "kopieerblok": ""}]}}
    ctx = _hook_ctx(tmp_path, monkeypatch, index)
    assert "operator inbox: 7 open (+5 more)" in ctx
    assert "[2d 3h, presented 0x] Akkoord op promote X?" in ctx and "vnx promote X" in ctx
    assert "[5h 0m, presented 2x] Welke kant op?" in ctx


@pytest.mark.parametrize("block", [
    {"status": "error", "reason": "RuntimeError: boom"},
    {"status": "unknown", "reason": "ledger unreadable", "open_total": None},
    {"status": "ok", "reason": None},  # open_total missing: not zero
    "garbage",
])
def test_hook_says_unreadable_never_no_open_questions(tmp_path, monkeypatch, block):
    ctx = _hook_ctx(tmp_path, monkeypatch, {"operator_inbox": block})
    assert "operator inbox: " in ctx and ("not readable" in ctx or "could not be read" in ctx)
    assert "no open questions" not in ctx.replace("NOT the same as no open questions", "")
    if isinstance(block, dict) and block.get("reason"):
        assert block["reason"] in ctx


def test_hook_flags_an_index_without_the_block(tmp_path, monkeypatch):
    assert "operator inbox: not readable" in _hook_ctx(tmp_path, monkeypatch, {"schema": "t0_index/1.3"})


def test_hook_prints_nothing_extra_for_zero_open(tmp_path, monkeypatch):
    block = {"status": "ok", "reason": None, "coverage": [], "open_total": 0, "more": 0, "points": []}
    assert "operator inbox" not in _hook_ctx(tmp_path, monkeypatch, {"operator_inbox": block})


# ── the role rules ──────────────────────────────────────────────────────────

def test_role_has_both_rules_and_the_end_command_parses(env, capsys):
    text = ROLE.read_text(encoding="utf-8")
    assert RULE_ASK in text and RULE_END in text
    state, _ = env
    pid = _open(state, "a", "2026-11-10T00:00:00Z")
    args = RULE_END.split("`")[1].split()[2:]  # "vnx inbox <args>"
    assert args == ["--present", "--channel", "status"]
    assert inbox_cli.main(["--project-id", PID, "--state-dir", str(state)] + args) == 0
    assert pid in capsys.readouterr().out
    assert _ledger_bytes(state).count(b'"kanaal": "status"') + _ledger_bytes(state).count(b'"kanaal":"status"') == 1
    ask = RULE_ASK.split("`")[1].split()[2:]
    assert ask == ["ask"]


# ── round 2: an unknown source is never ok or zero (ADR-076 besluit 11), no writer left behind ─────────

def _raising_reader():
    def reader(project_id, **kw):
        raise RuntimeError("source gone")
    return types.SimpleNamespace(READERS=(("boom", reader),))


@pytest.mark.parametrize("opened", [0, 2])
def test_unknown_source_makes_the_block_unknown_and_the_hook_names_it(env, tmp_path, monkeypatch, opened):
    state, dispatches = env
    monkeypatch.setattr(inbox, "_MODULES", (_quiet_reader(), _raising_reader()))
    ids = [_open(state, r, f"2026-11-1{i}T00:00:00Z") for i, r in enumerate("ab"[:opened])]
    doc = bts.build_t0_state(state, dispatches)
    block = doc["operator_inbox"]
    assert block["status"] == "unknown" and block["open_total"] is None
    assert "boom" in block["reason"] and "RuntimeError" in block["reason"]
    assert block["unknown_sources"] == [{"name": "boom", "reden": "RuntimeError"}]
    assert [p["point_id"] for p in block["points"]] == ids  # the points stay as show() gave them
    ctx = _hook_ctx(tmp_path, monkeypatch, bts._build_t0_index(doc))
    assert "  bron boom: unknown (RuntimeError)" in ctx
    assert "NOT the same as no open questions" in ctx
    for ref in "ab"[:opened]:
        assert f"vraag {ref}" in ctx


def test_every_line_of_unknown_sources_reaches_the_hook(tmp_path, monkeypatch):
    block = {"status": "unknown", "reason": "bron 1: x; bron 2: y", "open_total": None, "more": 0, "points": [],
             "unknown_sources": [{"name": "1", "reden": "x"}, {"name": "2", "reden": "y"}]}
    ctx = _hook_ctx(tmp_path, monkeypatch, {"operator_inbox": block})
    assert "  bron 1: unknown (x)" in ctx and "  bron 2: unknown (y)" in ctx


@pytest.mark.parametrize("coverage", [None, "garbage", [{"bron": 4, "dekking": "weird"}], [{"bron": 4}]])
def test_malformed_coverage_is_unknown_never_ok(env, monkeypatch, coverage):
    monkeypatch.setattr(inbox, "show", lambda *a, **kw: {"status": "ok", "reason": None, "coverage": coverage,
                                                          "open_total": 0, "points": [], "more": 0})
    block = bts._build_operator_inbox(PID, env[0])
    assert block["status"] == "unknown" and block["open_total"] is None and block["unknown_sources"]


_DRIVER = r"""
import os, sys, time, types, json
from pathlib import Path
sys.path[:0] = [sys.argv[1] + "/scripts/lib", sys.argv[1] + "/scripts"]
import build_t0_state as bts, operator_queue as oq, operator_queue_inbox as inbox
state = Path(sys.argv[2])
orig = oq._write_state
def slow(sdir, st):
    time.sleep(1.0)  # the reader sits between its ledger append and the projection when the budget runs out
    orig(sdir, st)
def reader(project_id, **kw):
    oq._write_state = slow
    oq.open_point(project_id, bron=3, ref="b", vraag="vraag b", sluit_bij="x", opened_by="t",
                  asked_at="2026-11-12T00:00:00Z", now="2026-11-12T00:00:00Z", state_dir=state)
    return {"bron": 3, "dekking": "gelezen", "reden": None, "unknown": []}
inbox._MODULES = (types.SimpleNamespace(READERS=(("writer", reader),)),)
bts._INBOX_BUDGET_SECONDS = 0.2
print(json.dumps(bts._build_operator_inbox("proj", state)))
"""


def test_timeout_mid_reader_leaves_no_torn_line_and_projection_catches_up(tmp_path):
    import time

    state = tmp_path / "state"
    _open(state, "a", "2026-11-10T00:00:00Z")
    env_ = {k: v for k, v in os.environ.items() if k not in ("VNX_CANONICAL_ROOT", "VNX_PROJECT_ID")}
    r = subprocess.run([sys.executable, "-c", _DRIVER, str(_ROOT), str(state)], capture_output=True, text=True,
                       timeout=30, env=env_)
    assert r.returncode == 0, r.stderr
    block = json.loads(r.stdout.strip().splitlines()[-1])
    assert block["status"] == "error" and block["reason"].startswith("timeout")
    deadline, agree = time.monotonic() + 10, False
    while time.monotonic() < deadline and not agree:
        lines = oq._ledger_lines(state, PID)  # strict reader: a torn line raises QueueUnreadable
        stored = json.loads((state / oq.STATE_FILENAME).read_text(encoding="utf-8"))
        agree = len(lines) == 2 and stored["points"] == oq.read_state(PID, state_dir=state)["points"]
        if not agree:
            time.sleep(0.1)
    assert agree, "operator_queue.json disagrees with the ledger: the writer was stopped between append and projection"
    assert not list(state.glob("*.tmp"))
