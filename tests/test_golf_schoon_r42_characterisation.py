#!/usr/bin/env python3
"""Characterisation tests for golf schoon R42 (dlv-f7aa5ef48ec9).

Pins the observable behaviour of ``scan_and_convert``, ``convert_dispatch_ids``
(report_to_receipt_converter) and ``SubprocessAdapter.deliver``
(subprocess_adapter) before their extract-method refactor: return values,
watermark rows, log lines and the order of side effects. Real temporary dirs,
a real EventStore; only Popen / process-group calls are patched.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("receipt_project")

import logging
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import report_to_receipt_converter as conv  # noqa: E402
import subprocess_adapter as sa  # noqa: E402
from event_store import EventStore  # noqa: E402

_LOGGER = "report_to_receipt_converter"


def _report(path: Path, dispatch_id: str) -> Path:
    fm = {
        "schema_version": 1, "dispatch_id": dispatch_id, "provider": "claude",
        "sub_provider": "anthropic", "model": "claude-sonnet-4-6", "terminal_id": "T1",
        "pool_id": "headless", "role": "identity_unresolved", "task_class": "implementation",
        "pr_id": "none", "duration_seconds": 12.5, "exit_code": 0,
        "token_usage": {"input": 1, "output": 2, "cache_read": 3}, "cost_usd": 0.01,
        "route_decision": {"strategy": "default", "selected_provider": "claude",
                           "selected_model": "claude-sonnet-4-6", "reason": "primary route"},
        "status": "unknown", "terminal": "T1", "timestamp": "2026-06-01T21:34:16Z",
    }
    path.write_text(
        f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n## Changes\n\n- x\n\n"
        "## Verification\n\npytest: 1 passed\n\n## Open Items\n\nNone\n",
        encoding="utf-8",
    )
    return path


def _fields(stats) -> tuple:
    return (stats.new_count, stats.duplicate_count, stats.rejected_count,
            stats.malformed_count, stats.error_count,
            stats.skipped_non_dispatch_count, stats.would_append_count)


def _lines(path: Path) -> list:
    return path.read_text().split() if path.exists() else []


def _messages(caplog) -> list:
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == _LOGGER]


def _dirs(tmp_path: Path):
    reports, state = tmp_path / "unified_reports", tmp_path / "state"
    reports.mkdir()
    state.mkdir()
    return reports, state


# --------------------------------------------------------------------------- scan_and_convert

def test_scan_books_once_marks_both_watermarks_and_logs(tmp_path, caplog):
    reports, state = _dirs(tmp_path)
    p = _report(reports / "20260821-r42-a.md", "20260821-r42-a")
    caplog.set_level(logging.INFO, logger=_LOGGER)
    with patch.object(conv, "_write_scan_heartbeat", wraps=conv._write_scan_heartbeat) as hb:
        stats = conv.scan_and_convert([reports, tmp_path / "absent"], state)
    assert _fields(stats) == (1, 0, 0, 0, 0, 0, 0)
    assert hb.call_count == 1 and hb.call_args[0] == (state, stats)
    digest = conv._compute_sha256(p)
    assert _lines(state / conv._WATERMARK_FILENAME) == [digest]
    assert _lines(state / conv._BASH_WATERMARK_FILENAME) == [digest]
    infos = [m for lvl, m in _messages(caplog) if "receipt emitted" in m]
    assert infos and infos[0].startswith("report_to_receipt_converter: receipt emitted dispatch=")
    assert infos[0].endswith(" file=20260821-r42-a.md")
    again = conv.scan_and_convert([str(reports)], state)
    assert _fields(again) == (0, 0, 0, 0, 0, 0, 0)


def test_scan_dry_run_counts_and_writes_nothing(tmp_path):
    reports, _ = _dirs(tmp_path)
    _report(reports / "20260821-r42-dry.md", "20260821-r42-dry")
    state = tmp_path / "fresh-state"
    stats = conv.scan_and_convert([reports], state, dry_run=True)
    assert _fields(stats) == (0, 0, 0, 0, 0, 0, 1)
    assert not state.exists()


def test_scan_and_ids_unresolvable_state_dir_returns_empty_stats(caplog):
    for call in (lambda: conv.scan_and_convert([]), lambda: conv.convert_dispatch_ids(["x"])):
        caplog.clear()
        with patch.object(conv, "_resolve_state_dir", side_effect=RuntimeError("nope")) as m:
            stats = call()
        assert m.called
        assert stats == conv.ScanStats()
        assert ("ERROR", "report_to_receipt_converter: cannot resolve state_dir: nope") in _messages(caplog)



# --------------------------------------------------------------------------- convert_dispatch_ids

def test_ids_rejects_bad_and_missing_then_books_valid(tmp_path, caplog):
    reports, state = _dirs(tmp_path)
    p = _report(reports / "20260821-r42-id.md", "20260821-r42-id")
    caplog.set_level(logging.INFO, logger=_LOGGER)
    stats = conv.convert_dispatch_ids(["../evil", "  ", "20260821-r42-none", " 20260821-r42-id "], state)
    assert _fields(stats) == (1, 0, 0, 2, 0, 0, 0)
    msgs = _messages(caplog)
    assert msgs[0] == ("WARNING", "report_to_receipt_converter: --dispatch-id '../evil' rejected — "
                       "does not match the dispatch id pattern (dispatch_spec._ID_RE); refusing "
                       "to resolve a report path from it")
    assert msgs[1] == ("WARNING", "report_to_receipt_converter: --dispatch-id 20260821-r42-none — "
                       f"no report file found under {tmp_path}/unified_reports")
    emitted = [m for _, m in msgs if "receipt emitted" in m]
    assert emitted[0].startswith("report_to_receipt_converter: --dispatch-id receipt emitted dispatch=")
    assert emitted[0].endswith(" file=20260821-r42-id.md")
    digest = conv._compute_sha256(p)
    assert _lines(state / conv._WATERMARK_FILENAME) == [digest]
    assert _lines(state / conv._BASH_WATERMARK_FILENAME) == [digest]
    assert _fields(conv.convert_dispatch_ids(["20260821-r42-id"], state)) == (0,) * 7


def test_ids_unhashable_report_counts_malformed(tmp_path, caplog):
    reports, state = _dirs(tmp_path)
    p = _report(reports / "20260821-r42-oserr.md", "20260821-r42-oserr")
    with patch.object(conv, "_compute_sha256", side_effect=OSError("denied")) as m:
        stats = conv.convert_dispatch_ids(["20260821-r42-oserr"], state)
    assert m.called and _fields(stats) == (0, 0, 0, 1, 0, 0, 0)
    assert ("WARNING", f"report_to_receipt_converter: --dispatch-id 20260821-r42-oserr — "
            f"cannot hash {p}: denied") in _messages(caplog)


# --------------------------------------------------------------------------- SubprocessAdapter.deliver

def _proc(pid: int, alive: bool = True) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = pid
    proc.poll.return_value = None if alive else 0
    return proc


def _adapter(tmp_path: Path) -> sa.SubprocessAdapter:
    adapter = sa.SubprocessAdapter()
    adapter._event_store = EventStore(events_dir=tmp_path / "events")
    adapter._event_store_loaded = True
    return adapter


def test_deliver_side_effect_order(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    adapter = _adapter(tmp_path)
    adapter._event_store.append("T1", {"type": "text", "dispatch_id": "prev-d"}, dispatch_id="prev-d")
    old, new = _proc(111), _proc(222)
    old.wait.side_effect = [subprocess.TimeoutExpired("c", 5), subprocess.TimeoutExpired("c", 5)]
    adapter._processes["T1"] = old
    wt = tmp_path / "repo" / ".vnx-data" / "worktrees" / "dispatch-x"
    wt.mkdir(parents=True)
    order = []
    with patch("subprocess.Popen", side_effect=lambda *a, **k: order.append("popen") or new), \
            patch.object(sa.os, "getpgid", side_effect=lambda pid: pid + 1), \
            patch.object(sa.os, "killpg", side_effect=lambda g, s: order.append(("kill", g, s))), \
            patch("dispatch_process_registry.record_dispatch_pgids",
                  side_effect=lambda d, g, repo_root: order.append(("rec", d, g, repo_root))) as rec:
        res = adapter.deliver("T1", "new-d", cwd=wt, on_spawn=lambda pid: order.append(("spawn", pid)))
    assert rec.called
    assert order == ["popen", ("spawn", 222), ("kill", 112, signal.SIGTERM),
                     ("kill", 112, signal.SIGKILL), ("rec", "new-d", [223], (tmp_path / "repo").resolve())]
    assert (res.success, res.terminal_id, res.dispatch_id, res.pane_id, res.path_used) == \
        (True, "T1", "new-d", None, "subprocess")
    assert adapter._processes["T1"] is new and adapter._dispatch_ids["T1"] == "new-d"
    assert (tmp_path / "events" / "archive" / "T1" / "prev-d.ndjson").exists()
    assert adapter.get_stderr_log_path("T1") == adapter._resolve_stderr_log_path("T1", "new-d")


def test_deliver_skips_dead_prior_and_non_worktree_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    adapter = _adapter(tmp_path)
    adapter._processes["T1"] = _proc(5, alive=False)
    with patch("subprocess.Popen", return_value=_proc(6)), \
            patch.object(sa.os, "killpg") as kill, \
            patch("dispatch_process_registry.record_dispatch_pgids") as rec:
        assert adapter.deliver("T1", "d2", cwd=tmp_path).success
    assert not kill.called and not rec.called


def test_deliver_stderr_open_failure_falls_back_to_devnull(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "data"
    blocker.write_text("file, not dir")
    monkeypatch.setenv("VNX_DATA_DIR", str(blocker))
    adapter = _adapter(tmp_path)
    with patch("subprocess.Popen", return_value=_proc(7)) as popen:
        assert adapter.deliver("T1", "d3").success
    assert popen.call_args[1]["stderr"] is subprocess.DEVNULL
    assert adapter.get_stderr_log_path("T1") == adapter._resolve_stderr_log_path("T1", "d3")
    assert any(r.getMessage().startswith("subprocess_adapter: cannot open stderr log ")
               and r.getMessage().endswith("; falling back to DEVNULL") for r in caplog.records)


def test_deliver_popen_failure_refuses_without_tracking(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    adapter = _adapter(tmp_path)
    with patch("subprocess.Popen", side_effect=FileNotFoundError("no claude")):
        res = adapter.deliver("T1", "d4")
    assert res.success is False and "no claude" in (res.failure_reason or "")
    assert "T1" not in adapter._processes and "T1" not in adapter._dispatch_ids
