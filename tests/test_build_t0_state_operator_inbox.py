"""operator-vragen-tot-antwoord Q5 (ADR-076 besluit 10): the operator_inbox block, the SessionStart lines and the role rules.

Everything runs on tmp_path. The hook reads the inbox summary and writes no ``presented`` line."""
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
    assert block["freshness"] == "current" and block["as_of"] and block["status"] == "ok"
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


def test_block_comes_from_the_summary_without_running_a_reader(env, monkeypatch):
    state, dispatches = env
    _open(state, "a", "2026-11-10T00:00:00Z")
    monkeypatch.setattr(inbox, "show", lambda *a, **kw: pytest.fail("the index must not run show()"))
    monkeypatch.setattr(inbox, "refresh", lambda *a, **kw: pytest.fail("the index must not run a reader"))
    doc = bts.build_t0_state(state, dispatches)
    assert doc["operator_inbox"]["status"] == "ok" and doc["operator_inbox"]["open_total"] == 1
    assert doc["operator_inbox"]["as_of"] == oq.read_inbox_summary(PID, state_dir=state)["summary"]["generated_at"]


def test_stale_summary_is_never_ok_and_never_zero(env):
    state, _ = env
    _open(state, "a", "2026-11-10T00:00:00Z")
    with open(state / oq.LEDGER_FILENAME, "a") as raw:
        raw.write("\n")  # a writer outside the module
    block = bts._build_operator_inbox(PID, state)
    assert block["status"] == "unknown" and block["freshness"] == "stale" and block["open_total"] is None
    assert block["points"] == [] and "ledger changed" in block["reason"] and block["as_of"]


def test_missing_summary_is_never_ok_and_never_zero(env):
    block = bts._build_operator_inbox(PID, env[0])
    assert block["status"] == "unknown" and block["freshness"] == "missing" and block["open_total"] is None
    assert block["as_of"] is None and "vnx inbox" in block["reason"]


def test_unknown_ledger_in_the_summary_is_unknown_not_a_count(env):
    state, _ = env
    _open(state, "a", "2026-11-10T00:00:00Z")
    with open(state / oq.LEDGER_FILENAME, "a") as raw:
        raw.write("{broken\n")
    oq.rebuild_state(PID, state_dir=state)
    block = bts._build_operator_inbox(PID, state)
    assert block["status"] == "unknown" and block["open_total"] is None and "operator_queue.ndjson" in block["reason"]


def test_failing_summary_read_is_an_error_and_the_rest_of_the_index_is_built(env, monkeypatch):
    state, dispatches = env

    def boom(*a, **kw):
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(oq, "read_inbox_summary", boom)
    doc = bts.build_t0_state(state, dispatches)
    assert doc["operator_inbox"] == {"status": "error", "reason": "RuntimeError: summary exploded"}
    assert bts._build_t0_index(doc)["schema"] == "t0_index/1.6"


# ── the hook ────────────────────────────────────────────────────────────────

def _hook_ctx(tmp_path, monkeypatch, prepare):
    for var in ("VNX_DATA_DIR", "VNX_STATE_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_HOME",
                "VNX_CANONICAL_ROOT", "VNX_PROJECT_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "vnx-data-home"))
    t0 = tmp_path / "project" / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    (tmp_path / "project" / ".vnx").mkdir()
    state = Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    prepare(state)
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    r = subprocess.run(["bash", str(HOOK)], capture_output=True, text=True, cwd=str(t0), env=dict(os.environ), timeout=30)
    assert r.returncode == 0, r.stderr
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before  # the hook wrote nothing, no `presented` line
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def _open_two(state):
    _open(state, "a", "2026-11-10T00:00:00Z", block="vnx promote X")
    _open(state, "b", "2026-11-11T00:00:00Z")


def test_hook_prints_the_points_from_the_summary(tmp_path, monkeypatch):
    ctx = _hook_ctx(tmp_path, monkeypatch, _open_two)
    summary = json.loads(next(Path(tmp_path).rglob(oq.SUMMARY_FILENAME)).read_text(encoding="utf-8"))
    assert f"operator inbox: 2 open (+0 more), as of {summary['generated_at']}" in ctx
    assert "presented 0x] vraag a" in ctx and "vnx promote X" in ctx and "presented 0x] vraag b" in ctx


def test_hook_ignores_a_stale_index_block_and_reads_the_summary(tmp_path, monkeypatch):
    def prepare(state):
        _open_two(state)
        (state / "t0_index.json").write_text(json.dumps({"operator_inbox": {
            "status": "ok", "open_total": 99, "more": 0, "points": []}}), encoding="utf-8")
    ctx = _hook_ctx(tmp_path, monkeypatch, prepare)
    assert "operator inbox: 2 open" in ctx and "99 open" not in ctx


def _stale(state):
    _open_two(state)
    with open(state / oq.LEDGER_FILENAME, "ab") as handle:
        handle.write(b"\n")  # the ledger now differs from ledger_bytes in the summary


def _missing(state):
    _open_two(state)
    (state / oq.SUMMARY_FILENAME).unlink()


def _unknown(state):
    _open_two(state)
    with open(state / oq.LEDGER_FILENAME, "ab") as handle:
        handle.write(b"not json\n")
    oq.rebuild_state(PID, state_dir=state)


@pytest.mark.parametrize("prepare, word", [(_stale, "stale"), (_missing, "missing"), (_unknown, "unknown")])
def test_hook_says_unreadable_never_no_open_questions_and_no_current_count(tmp_path, monkeypatch, prepare, word):
    ctx = _hook_ctx(tmp_path, monkeypatch, prepare)
    line = next(ln for ln in ctx.splitlines() if ln.startswith("operator inbox: "))
    assert "could not be read" in line and word in line and "NOT the same as no open questions" in line
    assert " 2 open" not in line and "as of" not in line


def test_hook_prints_nothing_extra_for_zero_open(tmp_path, monkeypatch):
    def prepare(state):
        pid = _open(state, "a", "2026-11-10T00:00:00Z")
        oq.close_point(PID, pid, uitkomst="beantwoord", bewijs="src", now="2026-11-15T00:00:00Z", state_dir=state)
    assert "operator inbox" not in _hook_ctx(tmp_path, monkeypatch, prepare)


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
