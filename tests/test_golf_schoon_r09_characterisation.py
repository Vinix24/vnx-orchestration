"""Characterisation tests for golf-schoon R09 (class C, extract-method only).

Pins the observable behaviour of two functions before they are split into
private helpers, and stays green across the split:

* ``scripts/migrate_to_central_vnx.py::_run_apply`` — the post-bootstrap tail:
  the order of its side effects (import loop → FTS5 rebuild 0016 → verify →
  report → backup cleanup → presnap rotation), the exact stdout lines, the
  JSON payload (``backup_cleanup_removed`` is added *after* the JSON was
  printed, so it never reaches stdout), and the 1/3/4 exits of the tail.
  The entry and gate paths are already pinned by
  ``tests/test_migrate_to_central_vnx_run_apply_paths.py``, whose real-SQLite
  ``apply_env`` fixture this file reuses.
* ``scripts/open_items_manager.py::main`` — parser and routing: exit codes,
  stdout/stderr lines and the files each command writes under a tmp state dir.

Dispatch-ID: dlv-50b34b446e04
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

import open_items_manager as oim  # noqa: E402
import test_migrate_to_central_vnx_run_apply_paths as _paths  # noqa: E402
from scripts import migrate_to_central_vnx as M  # noqa: E402

apply_env = _paths.apply_env
_LOGGER = "vnx.migrate.apply"


# ---------------------------------------------------------------------------
# _run_apply: the tail after the bootstrap
# ---------------------------------------------------------------------------


def _spy(monkeypatch, calls: list, name: str) -> None:
    """Record each call to ``M.<name>`` and delegate to the real function."""
    real = getattr(M, name)

    def _wrapped(*a, **kw):
        # A rotation is named by its prefix: the snapshot rotates too.
        calls.append(f"rotate:{a[1]}" if name == "rotate_backups_safe" else name)
        return real(*a, **kw)

    monkeypatch.setattr(M, name, _wrapped)


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def test_tail_side_effect_order(apply_env, monkeypatch):
    calls: list[str] = []
    for name in ("import_project", "apply_migration_0016", "verify_import",
                 "raise_for_verification_failures", "cleanup_old_backups",
                 "rotate_backups_safe"):
        _spy(monkeypatch, calls, name)

    assert _paths._apply(apply_env, "--cleanup-backups") == 0
    snap = ["rotate:quality_intelligence.db.presnap.",
            "rotate:runtime_coordination.db.presnap."]
    assert calls == [
        *snap, "import_project", "import_project", "apply_migration_0016",
        "verify_import", "raise_for_verification_failures",
        "cleanup_old_backups", "rotate:.presnap.", *snap,
    ]


def test_plain_stdout_lines_in_order(apply_env, capsys):
    assert _paths._apply(apply_env, "--cleanup-backups", "--keep-backups", "5") == 0
    lines = capsys.readouterr().out.splitlines()
    qi, rc = apply_env["central_qi"], apply_env["central_rc"]

    assert lines[0].startswith("P4 import complete. Backup: ")
    assert lines[1:3] == [f"  Central QI: {qi}", f"  Central RC: {rc}"]
    assert "  [proj-a] quality_intelligence.db success_patterns: +2 (0 idempotent skips)" in lines
    assert "  [proj-b] runtime_coordination.db dispatches: +1 (0 idempotent skips)" in lines
    assert lines[-1] == "  Backup cleanup: nothing to remove (keep_n=5)"
    assert not any("FAILED projects" in line for line in lines)


def test_plain_stdout_reports_removed_backups(apply_env, capsys):
    for i in range(3):
        assert _paths._apply(apply_env) == 0
    capsys.readouterr()
    assert _paths._apply(apply_env, "--cleanup-backups", "--keep-backups", "1") == 0
    last = capsys.readouterr().out.splitlines()[-1]
    assert last == "  Backup cleanup: removed 3 old backup dir(s) (keep_n=1)"


def test_json_payload_is_printed_before_cleanup_key_is_added(apply_env, capsys):
    assert _paths._apply(apply_env, "--json", "--cleanup-backups") == 0
    payload = json.loads(capsys.readouterr().out)

    assert sorted(payload) == [
        "applied_at", "backup_dir", "central_qi", "central_rc",
        "failed_projects", "imported_summary", "verification",
    ]
    assert payload["applied_at"].endswith("Z")
    assert Path(payload["backup_dir"]).parent == apply_env["backup_base"]
    assert payload["imported_summary"][0] == {
        "project_id": "proj-a", "db": "quality_intelligence.db",
        "table": "success_patterns", "rows_inserted": 2,
        "rows_skipped_existing": 0,
    }


def test_test_apply_plain_stdout_and_cleanup(apply_env, caplog, capsys):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    assert _paths._apply(apply_env, "--test-apply", "--cleanup-backups") == 0
    lines = capsys.readouterr().out.splitlines()
    tmp_dir = _paths._test_apply_tmp_dir_from_log(caplog)

    assert lines[0] == f"TEST MODE -- no writes to live central DB at {apply_env['central_state']}"
    # The canonical bootstrap prints its own lines in between.
    start = lines.index("TEST MODE complete. Temp central dir will be cleaned up.")
    assert lines[start + 1].startswith("  Central QI: ")
    assert not any("Backup cleanup" in line for line in lines)
    assert lines[-1] == f"TEST MODE -- temp central dir cleaned up: {tmp_dir}"
    assert not tmp_dir.exists()
    assert f"test-apply: cleaning up temp central dir {tmp_dir}" in _messages(caplog)


def test_test_apply_json_marks_payload(apply_env, caplog, capsys):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    assert _paths._apply(apply_env, "--test-apply", "--json") == 0
    out = capsys.readouterr().out
    tmp_dir = _paths._test_apply_tmp_dir_from_log(caplog)
    lines = out.splitlines()
    payload = json.loads("\n".join(lines[lines.index("{"):-1]))
    assert lines[0].startswith("TEST MODE -- no writes to live central DB at ")
    last = lines[-1]
    assert payload["test_apply"] is True
    assert payload["test_apply_temp_dir"] == str(tmp_dir)
    assert payload["backup_dir"] is None
    assert last == f"TEST MODE -- temp central dir cleaned up: {tmp_dir}"


def test_abort_inside_import_loop_exits_one(apply_env, monkeypatch, caplog, capsys):
    caplog.set_level(logging.ERROR, logger=_LOGGER)
    real_import = M.import_project
    calls: list[str] = []

    def _import_then_abort(*a, **kw):
        out = real_import(*a, **kw)
        apply_env["abort_flag"].parent.mkdir(parents=True, exist_ok=True)
        apply_env["abort_flag"].write_text("stop")
        return out

    monkeypatch.setattr(M, "import_project", _import_then_abort)
    _spy(monkeypatch, calls, "apply_migration_0016")

    assert _paths._apply(apply_env) == 1
    assert calls == []
    assert any(m.startswith("aborting: ") for m in _messages(caplog))
    assert capsys.readouterr().out == ""


def test_fts5_rebuild_failure_exits_three_and_restores(apply_env, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=_LOGGER)
    calls: list[str] = []
    _spy(monkeypatch, calls, "verify_import")

    def _boom(_qi):
        raise sqlite3.OperationalError("synthetic fts5")

    monkeypatch.setattr(M, "apply_migration_0016", _boom)

    assert _paths._apply(apply_env) == 3
    assert calls == []
    assert "FTS5 rebuild (migration 0016) failed: synthetic fts5" in _messages(caplog)
    assert _paths._row_count(apply_env["central_qi"], "success_patterns") == 0


def test_verification_exception_exits_four(apply_env, monkeypatch, caplog, capsys):
    caplog.set_level(logging.ERROR, logger=_LOGGER)

    def _raise(*_a, **_kw):
        raise RuntimeError("synthetic verify")

    monkeypatch.setattr(M, "verify_import", _raise)

    assert _paths._apply(apply_env) == 4
    assert "verification raised: synthetic verify" in _messages(caplog)
    assert capsys.readouterr().out == ""
    assert _paths._row_count(apply_env["central_qi"], "success_patterns") == 0


# ---------------------------------------------------------------------------
# open_items_manager.main: parser + routing
# ---------------------------------------------------------------------------


@pytest.fixture
def oi_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(oim, "STATE_DIR", state)
    monkeypatch.setattr(oim, "OPEN_ITEMS_FILE", state / "open_items.json")
    monkeypatch.setattr(oim, "DIGEST_FILE", state / "open_items_digest.json")
    monkeypatch.setattr(oim, "MARKDOWN_FILE", state / "open_items.md")
    monkeypatch.setattr(oim, "AUDIT_LOG", state / "open_items_audit.jsonl")
    monkeypatch.setattr(oim, "LEGACY_OPEN_ITEMS_FILE", tmp_path / "legacy.json")
    monkeypatch.delenv(oim.ROLLBACK_ENV_FLAG, raising=False)
    # The central decision-log fan-out is an external store.
    monkeypatch.setattr(oim, "_log_oi_close_decision", lambda **kw: None)
    return state


def _main(monkeypatch, *argv: str):
    monkeypatch.setattr(sys, "argv", ["open_items_manager.py", *argv])
    return oim.main()


def _items(state: Path) -> list[dict]:
    return json.loads((state / "open_items.json").read_text())["items"]


def _actions(state: Path) -> list[str]:
    lines = (state / "open_items_audit.jsonl").read_text().splitlines()
    return [json.loads(line)["action"] for line in lines]


def _add(monkeypatch, title: str = "Disk fills up", *extra: str):
    return _main(monkeypatch, "add", "--dispatch", "d-1", "--title", title, *extra)


def test_no_command_prints_help_and_returns_one(oi_env, monkeypatch, capsys):
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert out.startswith("usage: ")
    assert "Open Items Manager" in out


def test_rollback_warning_is_first_line(oi_env, monkeypatch, capsys):
    monkeypatch.setenv(oim.ROLLBACK_ENV_FLAG, "1")
    assert _main(monkeypatch, "list") == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == (
        "[CUTOVER] WARNING: rollback mode enabled "
        f"({oim.ROLLBACK_ENV_FLAG}=1). Legacy open-items fallback reads are active."
    )


def test_add_writes_item_audit_and_digest(oi_env, monkeypatch, capsys):
    assert _add(monkeypatch, "Disk fills up", "--severity", "warn") == 0
    out = capsys.readouterr().out
    assert "✅ Added OI-001: Disk fills up" in out
    item = _items(oi_env)[0]
    assert (item["id"], item["severity"], item["status"]) == ("OI-001", "warn", "open")
    assert _actions(oi_env) == ["add"]
    assert (oi_env / "open_items_digest.json").exists()


def test_add_rejected_title_returns_one_on_stderr(oi_env, monkeypatch, capsys):
    assert _add(monkeypatch, "CI green") == 1
    err = capsys.readouterr().err
    assert err.startswith("❌ REJECTED: title describes")
    assert not (oi_env / "open_items.json").exists()


@pytest.mark.parametrize("command,status", [
    ("close", "done"), ("defer", "deferred"), ("wontfix", "wontfix"),
])
def test_close_family_sets_status(oi_env, monkeypatch, command, status):
    assert _add(monkeypatch) == 0
    assert _main(monkeypatch, command, "OI-001", "--reason", "r", "--dispatch-id", "d-2") == 0
    item = _items(oi_env)[0]
    assert (item["status"], item["closed_reason"]) == (status, "r")


def test_attach_evidence_without_target_returns_its_code(oi_env, monkeypatch, capsys):
    assert _main(monkeypatch, "attach-evidence") == 1
    assert "attach-evidence requires --pr and/or --item" in capsys.readouterr().err


def test_amend_returns_its_code_and_writes(oi_env, monkeypatch):
    assert _add(monkeypatch) == 0
    assert _main(monkeypatch, "amend", "OI-001", "--title", "Disk fills fast",
                 "--reason", "typo") == 0
    assert _items(oi_env)[0]["title"] == "Disk fills fast"
    assert _actions(oi_env)[-1] == "amend"
    assert _main(monkeypatch, "amend", "OI-404", "--title", "x", "--reason", "y") != 0


def test_digest_with_rescan_runs_rescan_first(oi_env, monkeypatch):
    # rescan_items writes its own digest first; main then writes one more.
    calls: list[str] = []
    real_rescan, real_digest = oim.rescan_items, oim.generate_digest
    monkeypatch.setattr(oim, "rescan_items", lambda a: (calls.append("rescan"), real_rescan(a))[1])
    monkeypatch.setattr(oim, "generate_digest", lambda: (calls.append("digest"), real_digest())[1])

    assert _main(monkeypatch, "digest", "--rescan") == 0
    assert calls == ["rescan", "digest", "digest"]
    assert _main(monkeypatch, "digest") == 0
    assert calls[3:] == ["digest"]
    assert _main(monkeypatch, "rescan", "--dry-run") == 0
    assert calls[4] == "rescan"


@pytest.mark.parametrize("command", ["defer-pattern", "wontfix-pattern"])
def test_pattern_commands_ignore_return_code(oi_env, monkeypatch, command):
    assert _add(monkeypatch, "Flaky socket test") == 0
    monkeypatch.setattr(oim, "_apply_pattern", lambda args, status: 7)
    assert _main(monkeypatch, command, "--title-match", "flaky", "--reason", "r") == 0


def test_pattern_apply_closes_matching_items(oi_env, monkeypatch):
    assert _add(monkeypatch, "Flaky socket test") == 0
    assert _main(monkeypatch, "wontfix-pattern", "--title-match", "flaky",
                 "--reason", "noise", "--apply") == 0
    assert _items(oi_env)[0]["status"] == "wontfix"


def test_parser_choices_and_defaults(oi_env, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "list", "--status", "bogus")
    assert exc.value.code == 2
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(oim, "generate_digest", lambda: None)
    monkeypatch.setattr(oim, "rescan_items", lambda a: seen.append(a))
    assert _main(monkeypatch, "rescan") == 0
    assert vars(seen[0]) == {"command": "rescan", "dry_run": False}
