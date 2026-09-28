"""Open items as a second signal source of the learning loop (D5).

Real code paths against tmp stores only. No provider or claude process is
started and nothing touches ~/.vnx-data.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402


def _oi(oi_id: str, title: str, dispatch: str, status: str = "open", **extra) -> dict:
    item = {
        "id": oi_id,
        "status": status,
        "severity": "warn",
        "title": title,
        "details": "",
        "origin_dispatch_id": dispatch,
        "pr_id": "",
        "created_at": "2026-09-28T10:00:00",
        "updated_at": "2026-09-28T10:00:00",
        "closed_reason": None,
    }
    item.update(extra)
    return item


def _recurring(prefix: str = "OI-", start: int = 100, **extra) -> list:
    """Three items of one class (differing PR numbers) from three dispatches."""
    return [
        _oi(f"{prefix}{start + n}", f"Codex re-audit pending PR #{1800 + n} (merged without quota)",
            f"dispatch-{n}", **extra)
        for n in range(3)
    ]


def _write_store(state_dir: Path, items: list) -> None:
    (state_dir / "open_items.json").write_text(
        json.dumps({"schema_version": "1.0", "items": items, "next_id": 999}), encoding="utf-8"
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_LEARNING_LOOP_PERSIST", raising=False)
    monkeypatch.delenv("VNX_LEARNING_LOOP_CUTOFF", raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    (tmp_path / "repo").mkdir()
    (state_dir / "t0_receipts.ndjson").write_text("", encoding="utf-8")  # zero receipts
    paths = {
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(tmp_path / "repo"),
        "VNX_DATA_DIR": str(state_dir.parent),
        "PROJECT_ROOT": str(tmp_path / "repo"),
    }
    with patch.object(ll, "ensure_env", return_value=paths), patch.object(
        sys.modules["learning_loop"], "ensure_env", return_value=paths
    ):
        yield state_dir


def _run_cycle():
    loop = ll.LearningLoop()
    with patch.object(
        ll, "evaluate_activation_gate",
        return_value={"action": "run", "probe_health": "ok", "detail": "test"},
    ):
        report = loop.daily_learning_cycle()
    loop.conn.close()
    return report


def _snapshot(state_dir: Path) -> dict:
    return {
        str(p.relative_to(state_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(state_dir.rglob("*"))
        if p.is_file()
    }


def _oi_proposals(report: dict) -> list:
    return [p for p in report["shadow"]["proposals"] if p.get("signal") == "open_items"]


# (a) a recurring OI class with zero receipts becomes a shadow proposal
def test_recurring_open_item_class_gives_shadow_proposal_from_zero_receipts(env):
    _write_store(env, _recurring())
    report = _run_cycle()
    proposals = _oi_proposals(report)
    assert len(proposals) == 1
    assert proposals[0]["source_oi_ids"] == ["OI-100", "OI-101", "OI-102"]
    assert proposals[0]["occurrence_count"] == 3
    assert report["statistics"]["failure_patterns"] == 0
    assert report["statistics"]["open_item_signals"] == 1
    assert report["statistics"]["open_items_read"] == 3


def test_items_from_one_dispatch_do_not_make_a_class(env):
    items = [
        _oi(f"OI-{n}", f"Codex re-audit pending PR #{n}", "one-dispatch") for n in range(5)
    ]
    _write_store(env, items)
    assert _oi_proposals(_run_cycle()) == []


def test_distinct_titles_do_not_group(env):
    items = [_oi(f"OI-{n}", f"unrelated finding number {'x' * n} here", f"d-{n}") for n in range(4)]
    _write_store(env, items)
    assert _oi_proposals(_run_cycle()) == []


# (b) closed / deferred items do not count
@pytest.mark.parametrize("status", ["done", "deferred", "wontfix", "closed"])
def test_non_open_items_do_not_count(env, status):
    _write_store(env, _recurring(status=status))
    assert _oi_proposals(_run_cycle()) == []


def test_two_open_plus_one_deferred_stays_below_threshold(env):
    items = _recurring()
    items[2]["status"] = "deferred"
    _write_store(env, items)
    assert _oi_proposals(_run_cycle()) == []


# (c) another project's items with colliding ids do not leak
def test_foreign_project_items_with_colliding_ids_do_not_leak(env):
    own = _recurring()[:2]
    foreign = [
        _oi("OI-102", "Codex re-audit pending PR #1802 (merged without quota)", "dispatch-2",
            project_id="seocrawler-v2"),
        _oi("OI-100", "Codex re-audit pending PR #1800 (merged without quota)", "dispatch-9",
            project_id="seocrawler-v2"),
    ]
    _write_store(env, own + foreign)
    assert _oi_proposals(_run_cycle()) == []


def test_own_project_id_stamp_is_kept_and_other_store_is_not_read(env, tmp_path):
    items = _recurring()
    for item in items:
        item["project_id"] = "vnx-dev"
    _write_store(env, items)
    other = tmp_path / "vnx-data" / "seocrawler-v2" / "state"
    other.mkdir(parents=True)
    _write_store(other, [_oi("OI-100", "Something else entirely different", "x")])
    proposals = _oi_proposals(_run_cycle())
    assert [p["source_oi_ids"] for p in proposals] == [["OI-100", "OI-101", "OI-102"]]


# (d) shadow mode persists nothing for this signal either
def test_shadow_mode_persists_nothing_for_open_item_signal(env):
    _write_store(env, _recurring())
    before = _snapshot(env)
    report = _run_cycle()
    assert _oi_proposals(report)
    assert _snapshot(env) == before
    assert not (env / "pending_rules.json").exists()


def test_persist_mode_queues_the_proposal_with_its_oi_ids(env, monkeypatch):
    monkeypatch.setenv("VNX_LEARNING_LOOP_PERSIST", "1")
    _write_store(env, _recurring())
    _run_cycle()
    rules = json.loads((env / "pending_rules.json").read_text())["pending_rules"]
    assert [r["source_oi_ids"] for r in rules] == [["OI-100", "OI-101", "OI-102"]]
    assert rules[0]["signal"] == "open_items"


def test_open_items_store_is_never_written(env):
    _write_store(env, _recurring())
    before = (env / "open_items.json").read_bytes()
    _run_cycle()
    assert (env / "open_items.json").read_bytes() == before
    assert not (env / "open_items.lock").exists()


# negative path: unreadable store is reported, not read as zero
def test_corrupt_store_is_reported_in_steps_not_run(env):
    (env / "open_items.json").write_text("{not json", encoding="utf-8")
    report = _run_cycle()
    assert any(s.startswith("open_items_signal") for s in report["shadow"]["steps_not_run"])
    assert _oi_proposals(report) == []


def test_missing_store_is_zero_signals_without_error(env):
    report = _run_cycle()
    assert _oi_proposals(report) == []
    assert not any(s.startswith("open_items_signal") for s in report["shadow"]["steps_not_run"])


def test_title_normalization_drops_numbers_and_punctuation():
    assert ll.normalize_open_item_title("Codex re-audit pending PR #1847 merged") == \
        ll.normalize_open_item_title("Codex re-audit pending PR #1729 merged")
    assert ll.normalize_open_item_title(None) == ""


# beacon carries the new counts
def test_beacon_counts_carry_open_item_fields(env):
    import learning_loop_nightly as nightly

    _write_store(env, _recurring())
    with patch.object(
        ll, "evaluate_activation_gate",
        return_value={"action": "run", "probe_health": "ok", "detail": "test"},
    ):
        code = nightly.run_phase(state_dir=env)
    assert code == 0
    beacon = json.loads((env / "learning_loop_nightly_beacon.json").read_text())
    assert beacon["counts"]["open_item_signals"] == 1
    assert beacon["counts"]["open_items_read"] == 3
    assert beacon["counts"]["proposals"] >= 1
