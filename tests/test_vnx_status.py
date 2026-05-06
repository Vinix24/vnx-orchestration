"""Unit tests for scripts/cli/vnx_status.py — W-UX-3.

Cases:
- Output contains: focus, top-3 active waves, top-3 open PRs, terminal status, last-3 decisions
- --json emits parseable JSON with stable schema (vnx_status/1.0)
- Missing strategy/ falls back gracefully (exit 0, polite message)
- Missing t0_state.json falls back gracefully (exit 0)
- No write side effects (mtime check pre/post)
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "cli"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import vnx_status as vs


# ── Sample data ────────────────────────────────────────────────────────

SAMPLE_CURRENT_STATE = """\
# VNX Project State
Last updated: 2026-05-06T00:00:00Z

**Focus**: Phase 0: Operator UX quick wins

## Roadmap Waves

### Phase 0: Operator UX quick wins
- [~] `w-ux-1`: Bootstrap strategy/ folder
- [~] `w-ux-2`: current_state.md auto-projector
- [ ] `w-ux-3`: vnx status CLI dashboard
- [ ] `w-ux-4`: GC retention policy
- [ ] `w-ux-5`: vnx init strategy

## Open Pull Requests

- PR #400: fix(t0-state): GC retention (`fix/t0-state-gc`)
- PR #396: fix(append_receipt): remove dead code (`fix/ur-001`)
- PR #395: chore: ADRs + maintenance (`chore/wave6-adrs`)
- PR #232: fix(pane-manager): cross-project leak (`fix/pane-discovery`)

## Open Items

**5 open** (1 blocking, 2 warnings)

## Recent Receipts

- [~] 2026-05-06 T1 `20260506-w-ux-2` (subprocess_completion)

## Recent Decisions

- 2026-05-05: **arch-model** -> jwt_symmetric
- 2026-05-04: **db-choice** -> postgres
- 2026-05-03: **cache-layer** -> redis
- 2026-05-02: **old-decision** -> noop
"""

SAMPLE_T0_STATE: dict = {
    "schema_version": "2.1",
    "generated_at": "2026-05-06T06:00:00Z",
    "terminals": {
        "T1": {
            "status": "idle",
            "track": "A",
            "lease_state": "idle",
            "current_dispatch": None,
            "last_update": "2026-05-01T08:58:35Z",
        },
        "T2": {
            "status": "busy",
            "track": "B",
            "lease_state": "leased",
            "current_dispatch": "20260506-w-ux-3",
            "last_update": "2026-05-06T10:00:00Z",
        },
        "T3": {
            "status": "idle",
            "track": "C",
            "lease_state": "idle",
            "current_dispatch": None,
            "last_update": "2026-05-01T09:00:00Z",
        },
    },
    "queues": {
        "pending_count": 1,
        "active_count": 1,
        "completed_last_hour": 3,
    },
}


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    strategy = tmp_path / "strategy"
    state = tmp_path / "state"
    strategy.mkdir()
    state.mkdir()
    (strategy / "current_state.md").write_text(SAMPLE_CURRENT_STATE)
    (state / "t0_state.json").write_text(json.dumps(SAMPLE_T0_STATE))
    return tmp_path


@pytest.fixture()
def no_strategy_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (state / "t0_state.json").write_text(json.dumps(SAMPLE_T0_STATE))
    return tmp_path


@pytest.fixture()
def no_t0_state_dir(tmp_path: Path) -> Path:
    strategy = tmp_path / "strategy"
    strategy.mkdir()
    (strategy / "current_state.md").write_text(SAMPLE_CURRENT_STATE)
    return tmp_path


@pytest.fixture()
def empty_dir(tmp_path: Path) -> Path:
    return tmp_path


# ── Helper ─────────────────────────────────────────────────────────────

def _run(argv=None, data_dir_=None):
    buf = StringIO()
    with patch("sys.stdout", buf):
        rc = vs.main(argv=argv, data_dir=data_dir_)
    return rc, buf.getvalue()


# ── Dashboard content ──────────────────────────────────────────────────

class TestDashboardContent:
    def test_exit_code_0(self, data_dir: Path) -> None:
        rc, _ = _run(data_dir_=data_dir)
        assert rc == 0

    def test_focus_present(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        assert "Phase 0: Operator UX quick wins" in out

    def test_active_waves_present(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        assert "w-ux-1" in out or "w-ux-2" in out

    def test_top_3_waves_at_most_3(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        wave_ids_shown = sum(1 for wid in ["w-ux-1", "w-ux-2", "w-ux-3", "w-ux-4", "w-ux-5"] if wid in out)
        assert wave_ids_shown <= 3

    def test_open_prs_present(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        assert "PR #400" in out
        assert "PR #396" in out

    def test_top_3_prs_fourth_excluded(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        assert "PR #232" not in out

    def test_terminal_status_present(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        assert "T1" in out
        assert "T2" in out
        assert "T3" in out

    def test_decisions_present(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        assert "arch-model" in out
        assert "jwt_symmetric" in out

    def test_last_3_decisions_fourth_excluded(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        assert "old-decision" not in out

    def test_all_five_sections_present(self, data_dir: Path) -> None:
        rc, out = _run(data_dir_=data_dir)
        assert rc == 0
        assert "Current Focus" in out
        assert "Active Waves" in out
        assert "Open PRs" in out
        assert "Terminal Status" in out
        assert "Recent Decisions" in out


# ── JSON output ────────────────────────────────────────────────────────

class TestJsonOutput:
    def test_json_parseable(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        assert rc == 0
        data = json.loads(out)
        assert isinstance(data, dict)

    def test_json_schema_key(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert data["schema"] == "vnx_status/1.0"

    def test_json_has_all_required_keys(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        required = {
            "schema", "focus", "active_waves", "open_prs",
            "terminals", "recent_decisions", "queues",
            "strategy_available", "t0_state_available",
        }
        assert required.issubset(data.keys())

    def test_json_focus_populated(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert "Phase 0" in data["focus"]

    def test_json_active_waves_list(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert isinstance(data["active_waves"], list)
        assert len(data["active_waves"]) <= 3

    def test_json_wave_schema(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        for w in data["active_waves"]:
            assert "badge" in w
            assert "id" in w
            assert "title" in w

    def test_json_open_prs_list(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert isinstance(data["open_prs"], list)
        assert len(data["open_prs"]) <= 3

    def test_json_terminals_populated(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert "T1" in data["terminals"]
        assert "T2" in data["terminals"]

    def test_json_decisions_list(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert isinstance(data["recent_decisions"], list)
        assert len(data["recent_decisions"]) <= 3

    def test_json_queues_populated(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert "pending_count" in data["queues"]

    def test_json_availability_flags_true(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert data["strategy_available"] is True
        assert data["t0_state_available"] is True

    def test_json_decisions_at_most_3(self, data_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=data_dir)
        data = json.loads(out)
        assert len(data["recent_decisions"]) <= 3


# ── Graceful fallback: missing strategy/ ──────────────────────────────

class TestMissingStrategyFallback:
    def test_exits_0(self, no_strategy_dir: Path) -> None:
        rc, _ = _run(data_dir_=no_strategy_dir)
        assert rc == 0

    def test_polite_message_or_warning(self, no_strategy_dir: Path) -> None:
        rc, out = _run(data_dir_=no_strategy_dir)
        lower = out.lower()
        assert "strategy" in lower or "missing" in lower or "warn" in lower

    def test_still_shows_terminal_data(self, no_strategy_dir: Path) -> None:
        rc, out = _run(data_dir_=no_strategy_dir)
        assert rc == 0
        assert "T1" in out

    def test_json_exits_0(self, no_strategy_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=no_strategy_dir)
        assert rc == 0
        data = json.loads(out)
        assert data["strategy_available"] is False

    def test_json_t0_still_available(self, no_strategy_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=no_strategy_dir)
        data = json.loads(out)
        assert data["t0_state_available"] is True


# ── Graceful fallback: missing t0_state.json ──────────────────────────

class TestMissingT0StateFallback:
    def test_exits_0(self, no_t0_state_dir: Path) -> None:
        rc, _ = _run(data_dir_=no_t0_state_dir)
        assert rc == 0

    def test_still_shows_strategic_data(self, no_t0_state_dir: Path) -> None:
        rc, out = _run(data_dir_=no_t0_state_dir)
        assert rc == 0
        assert "Phase 0" in out

    def test_json_exits_0(self, no_t0_state_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=no_t0_state_dir)
        assert rc == 0
        data = json.loads(out)

    def test_json_t0_not_available(self, no_t0_state_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=no_t0_state_dir)
        data = json.loads(out)
        assert data["t0_state_available"] is False

    def test_json_strategy_still_available(self, no_t0_state_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=no_t0_state_dir)
        data = json.loads(out)
        assert data["strategy_available"] is True


# ── Graceful fallback: both missing ───────────────────────────────────

class TestBothMissingFallback:
    def test_exits_0(self, empty_dir: Path) -> None:
        rc, _ = _run(data_dir_=empty_dir)
        assert rc == 0

    def test_not_initialised_message(self, empty_dir: Path) -> None:
        rc, out = _run(data_dir_=empty_dir)
        lower = out.lower()
        assert "not initialised" in lower or "init" in lower

    def test_json_exits_0(self, empty_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=empty_dir)
        assert rc == 0
        data = json.loads(out)

    def test_json_both_unavailable(self, empty_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=empty_dir)
        data = json.loads(out)
        assert data["strategy_available"] is False
        assert data["t0_state_available"] is False

    def test_json_error_key_present(self, empty_dir: Path) -> None:
        rc, out = _run(argv=["--json"], data_dir_=empty_dir)
        data = json.loads(out)
        assert "error" in data


# ── No write side effects ──────────────────────────────────────────────

class TestNoWriteSideEffects:
    def _mtimes(self, root: Path) -> dict:
        return {
            str(p.relative_to(root)): p.stat().st_mtime
            for p in root.rglob("*")
            if p.is_file()
        }

    def test_no_files_mutated(self, data_dir: Path) -> None:
        before = self._mtimes(data_dir)
        _run(data_dir_=data_dir)
        after = self._mtimes(data_dir)
        assert before == after

    def test_no_files_mutated_json(self, data_dir: Path) -> None:
        before = self._mtimes(data_dir)
        _run(argv=["--json"], data_dir_=data_dir)
        after = self._mtimes(data_dir)
        assert before == after

    def test_no_new_files_created(self, data_dir: Path) -> None:
        before_files = {str(p) for p in data_dir.rglob("*") if p.is_file()}
        _run(data_dir_=data_dir)
        after_files = {str(p) for p in data_dir.rglob("*") if p.is_file()}
        assert after_files == before_files


# ── Edge cases ────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_malformed_t0_state_json_falls_back(self, tmp_path: Path) -> None:
        strategy = tmp_path / "strategy"
        state = tmp_path / "state"
        strategy.mkdir()
        state.mkdir()
        (strategy / "current_state.md").write_text(SAMPLE_CURRENT_STATE)
        (state / "t0_state.json").write_text("NOT_VALID_JSON{{")
        rc, out = _run(data_dir_=tmp_path)
        assert rc == 0

    def test_empty_current_state_file(self, tmp_path: Path) -> None:
        strategy = tmp_path / "strategy"
        state = tmp_path / "state"
        strategy.mkdir()
        state.mkdir()
        (strategy / "current_state.md").write_text("")
        (state / "t0_state.json").write_text(json.dumps(SAMPLE_T0_STATE))
        rc, _ = _run(data_dir_=tmp_path)
        assert rc == 0

    def test_no_decisions_section_graceful(self, tmp_path: Path) -> None:
        strategy = tmp_path / "strategy"
        state = tmp_path / "state"
        strategy.mkdir()
        state.mkdir()
        content = "# VNX Project State\n\n**Focus**: Test\n\n## Roadmap Waves\n\n## Open Pull Requests\n"
        (strategy / "current_state.md").write_text(content)
        (state / "t0_state.json").write_text(json.dumps(SAMPLE_T0_STATE))
        rc, out = _run(data_dir_=tmp_path)
        assert rc == 0
        assert "no decisions found" in out.lower()

    def test_json_no_waves_empty_list(self, tmp_path: Path) -> None:
        strategy = tmp_path / "strategy"
        state = tmp_path / "state"
        strategy.mkdir()
        state.mkdir()
        (strategy / "current_state.md").write_text("# VNX Project State\n\n**Focus**: Empty\n")
        (state / "t0_state.json").write_text(json.dumps(SAMPLE_T0_STATE))
        rc, out = _run(argv=["--json"], data_dir_=tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert data["active_waves"] == []
