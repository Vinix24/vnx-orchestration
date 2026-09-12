#!/usr/bin/env python3
"""Tests for C3 (golf C) — receipt-processor as a per-project launchd job,
wired through `vnx init` (OI-1509/OI-1510).

Two real defects existed before this dispatch:

  1. ``vnx init`` installed ``com.vnx.gate-obligation-runner`` and
     ``com.vnx.ledger-health`` via ``_install_launchd_agent``, but never
     ``com.vnx.receipt-processor`` — even though the template
     (``scripts/launchd/com.vnx.receipt-processor.plist``, #1769) and the
     guard (``scripts/launchd/launchd_project_scope.py``) both already
     existed. The receipt processor kept running only as a hand-started
     foreground supervisor.

  2. ``_install_launchd_agent`` wrote to
     ``~/Library/LaunchAgents/<plist_name>.plist`` — derived from the literal
     argument, not the template's resolved (per-project) Label. Two projects
     installing the SAME family landed on the SAME destination file: the
     second project's install silently unloaded and overwrote the first
     project's job (OI-1510). ``reload_plist.sh`` already fixed the
     equivalent bug in the manual-install path; `vnx init`'s own
     `_install_launchd_agent` did not.

Mirrors ``tests/test_ledger_health_launchd.py``'s pattern: fake engine root
+ fake home + monkeypatched ``subprocess.run``, exercising the REAL install
path (`_install_launchd_agent` / its per-family wrappers), never a
reimplementation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(VNX_ROOT))

from vnx_cli.commands import init_cmd  # noqa: E402
from vnx_cli.commands.init_cmd import vnx_init  # noqa: E402


def _init_args(tmp_path, **overrides):
    ns = argparse.Namespace(
        project_path=None,
        project_dir=str(tmp_path),
        project_id=None,
        template="default",
        force=False,
        non_interactive=False,
        set_version=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns

RECEIPT_PROCESSOR_LABEL = "com.vnx.receipt-processor"
GATE_OBLIGATION_LABEL = "com.vnx.gate-obligation-runner"


def _fake_engine_root(tmp_path: Path) -> Path:
    """A fake engine root under tmp_path (never under .vnx-data/worktrees/,
    which this suite's own real repo path is) so the OI-1117 worktree guard
    in _install_launchd_agent does not fire and silently skip the install."""
    real_engine = init_cmd._engine.engine_root()
    fake_engine_root = tmp_path / "fake-vnx-engine"
    fake_engine_root.mkdir()
    (fake_engine_root / "scripts").symlink_to(real_engine / "scripts", target_is_directory=True)
    return fake_engine_root


def _patch_launchctl(monkeypatch, loaded_labels: "set[str]") -> None:
    """Fake launchctl: 'load' registers the label (read from the just-written
    plist file path, launchd-style — the label IS the destination basename),
    'list' reports whatever is currently 'loaded'."""

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        if cmd[:2] == ["launchctl", "load"]:
            dest = Path(cmd[-1])
            loaded_labels.add(dest.stem)
            m.stdout = ""
        elif cmd[:2] == ["launchctl", "unload"]:
            dest = Path(cmd[-1])
            loaded_labels.discard(dest.stem)
            m.stdout = ""
        elif cmd == ["launchctl", "list"]:
            m.stdout = "\n".join(loaded_labels)
        else:
            m.stdout = ""
        return m

    monkeypatch.setattr(init_cmd.subprocess, "run", fake_run)


class TestReceiptProcessorInstalled:
    """Defect 1: vnx init must install com.vnx.receipt-processor."""

    def test_install_receipt_processor_runner_wrapper_exists(self, tmp_path, monkeypatch):
        fake_engine_root = _fake_engine_root(tmp_path)
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: fake_engine_root)

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        _patch_launchctl(monkeypatch, set())

        installed = init_cmd._install_receipt_processor_runner(
            str(fake_engine_root), project_id="test-project"
        )
        assert installed is True, (
            "_install_receipt_processor_runner returned False — the plist "
            "template or the install wiring is missing"
        )

        dest = fake_home / "Library" / "LaunchAgents" / f"{RECEIPT_PROCESSOR_LABEL}.test-project.plist"
        assert dest.is_file(), (
            f"expected a project-scoped plist at {dest} — receipt-processor "
            "was not installed under a per-project Label"
        )

    def test_vnx_init_calls_receipt_processor_installer(self, tmp_path, monkeypatch):
        """Behavioral guard, not a symbol check: drive the REAL `vnx init`
        CLI entry point (the same one every other test in test_cli_init.py
        exercises against this worktree's own real engine root, which trips
        the OI-1117 worktree guard and safely no-ops every launchd install)
        and assert the receipt-processor installer was actually invoked from
        the scaffold, with this project's id. A test asserting only that
        `_install_receipt_processor_runner` exists as a symbol would go
        green even if `_vnx_init_scaffold` never called it.

        Isolates from any ambient `VNX_PROJECT_ID` (e.g. this very repo's own
        CI job runs as project "vnx-dev") — belt-and-suspenders, kept from the
        prior round even though it no longer drives the failure below.

        Stubs out `_bootstrap_runtime_dbs` (DB-bootstrap side effect, unused
        return value, nothing later in the scaffold reads back from it) rather
        than letting it run for real. Ticket: the round-1 fix (delenv above)
        closed a DIFFERENT CI-only failure (run 34394050973, an ADR-007
        marker/env mismatch), but a SEPARATE failure surfaced right after,
        ONLY in full-suite collection (run 34398777391), never in this file
        alone: `assert rc == 0` failing on "dispatches missing
        UNIQUE(dispatch_id, project_id) — was added in migration 0017, must
        be preserved".

        Root cause, measured by reproducing the exact `_bootstrap_runtime_dbs`
        sequence standalone: on a from-scratch DB, `schemas/
        runtime_coordination_v10.sql` unconditionally stamps
        `runtime_schema_version=12`, even though ITS OWN `CREATE TABLE IF NOT
        EXISTS dispatches (... UNIQUE(dispatch_id, project_id) ...)` is a
        no-op — `dispatches` already exists in its v1 (no-project_id,
        single-column-UNIQUE) shape by then. `scripts/lib/migrations/
        apply_0017.py` trusts that stamp (`MAX(runtime_schema_version) >= 12`
        => skip) and never runs the real composite-UNIQUE rebuild. This is
        silent in a standalone `vnx init` process: the ONLY thing that
        notices is `scripts/migrate_future_system.py`'s preflight
        (`schema_migration.register_preflight(22, _assert_dispatches_schema_
        intact)`), and that registration is a process-global side effect of
        IMPORTING that module — `_bootstrap_runtime_dbs` never imports it.
        In an isolated run of this file, nothing imports it either, so the
        gap stays silent and the test passes; in the full suite, some OTHER
        test module imports `migrate_future_system` first, the preflight
        stays registered for the rest of the pytest process, and this test's
        real `vnx init` call is the next one to reach migration 0022's
        preflight and trip it — a collection-order-dependent false negative
        for what this test actually checks. Confirmed the underlying gap is
        not test-only: this repo's own live store
        (~/.vnx-data/vnx-dev/state/runtime_coordination.db, PRAGMA
        user_version=33) has the same single-column-only
        `sqlite_autoindex_dispatches_1` and no composite index. Left as a
        real, separate defect for `## Open Items` — out of scope here."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.setattr(init_cmd, "_bootstrap_runtime_dbs", MagicMock(return_value=None))
        spy = MagicMock(return_value=True)
        monkeypatch.setattr(init_cmd, "_install_receipt_processor_runner", spy)

        rc = vnx_init(_init_args(tmp_path, template="minimal"))
        assert rc == 0
        assert spy.called, "vnx init did not call _install_receipt_processor_runner"
        _, kwargs = spy.call_args
        assert kwargs.get("project_id"), (
            f"_install_receipt_processor_runner called without a project_id: {spy.call_args}"
        )


class TestDestinationPathDoesNotCollide:
    """Defect 2 (OI-1510): two projects installing the same family must not
    overwrite each other's plist file or Label."""

    def test_two_projects_installing_receipt_processor_get_separate_files(
        self, tmp_path, monkeypatch
    ):
        fake_engine_root = _fake_engine_root(tmp_path)
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: fake_engine_root)

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        _patch_launchctl(monkeypatch, set())

        assert init_cmd._install_receipt_processor_runner(
            str(fake_engine_root), project_id="project-alpha"
        )
        assert init_cmd._install_receipt_processor_runner(
            str(fake_engine_root), project_id="project-beta"
        )

        la_dir = fake_home / "Library" / "LaunchAgents"
        alpha_dest = la_dir / f"{RECEIPT_PROCESSOR_LABEL}.project-alpha.plist"
        beta_dest = la_dir / f"{RECEIPT_PROCESSOR_LABEL}.project-beta.plist"

        assert alpha_dest.is_file(), "project-alpha's plist is missing — beta's install overwrote it"
        assert beta_dest.is_file(), "project-beta's plist was never written"

        alpha_content = alpha_dest.read_text(encoding="utf-8")
        beta_content = beta_dest.read_text(encoding="utf-8")
        assert "project-alpha" in alpha_content
        assert "project-beta" not in alpha_content, (
            "project-alpha's installed plist carries project-beta's id — "
            "the second install overwrote the first project's file (OI-1510)"
        )
        assert "project-beta" in beta_content
        assert "project-alpha" not in beta_content

    def test_two_projects_installing_gate_obligation_runner_get_separate_files(
        self, tmp_path, monkeypatch
    ):
        """Same fix, same handler class (gate-obligation-runner), applied
        through the identical shared _install_launchd_agent code path —
        the fix must not be receipt-processor-only."""
        fake_engine_root = _fake_engine_root(tmp_path)
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: fake_engine_root)

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        _patch_launchctl(monkeypatch, set())

        assert init_cmd._install_gate_obligation_runner(
            str(fake_engine_root), project_id="project-alpha"
        )
        assert init_cmd._install_gate_obligation_runner(
            str(fake_engine_root), project_id="project-beta"
        )

        la_dir = fake_home / "Library" / "LaunchAgents"
        alpha_dest = la_dir / f"{GATE_OBLIGATION_LABEL}.project-alpha.plist"
        beta_dest = la_dir / f"{GATE_OBLIGATION_LABEL}.project-beta.plist"
        assert alpha_dest.is_file()
        assert beta_dest.is_file()

    def test_destination_filename_matches_resolved_label_not_plist_name_arg(
        self, tmp_path, monkeypatch
    ):
        fake_engine_root = _fake_engine_root(tmp_path)
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: fake_engine_root)

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        _patch_launchctl(monkeypatch, set())

        init_cmd._install_launchd_agent(
            str(fake_engine_root), "com.vnx.receipt-processor", project_id="vnx-dev"
        )

        legacy_dest = fake_home / "Library" / "LaunchAgents" / "com.vnx.receipt-processor.plist"
        scoped_dest = fake_home / "Library" / "LaunchAgents" / "com.vnx.receipt-processor.vnx-dev.plist"
        assert not legacy_dest.is_file(), (
            "install wrote to the bare plist_name-derived path — destination "
            "must be derived from the resolved (project-scoped) Label"
        )
        assert scoped_dest.is_file()


class TestPostLoadVerificationIsExactLabelMatch:
    """The post-load verification must match the RESOLVED label exactly, not
    the base family name as a substring (OI-1721: 'project' prefixes
    'project-alpha', and 'com.vnx.receipt-processor' prefixes EVERY
    per-project label)."""

    def test_verification_warns_when_only_another_projects_instance_is_loaded(
        self, tmp_path, monkeypatch, capsys
    ):
        fake_engine_root = _fake_engine_root(tmp_path)
        monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: fake_engine_root)

        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            m.stdout = ""
            if cmd[:2] == ["launchctl", "list"]:
                # Only a DIFFERENT project's instance is loaded. The base name
                # 'com.vnx.receipt-processor' is a substring of this line, but
                # THIS project's resolved label is not present at all.
                m.stdout = "com.vnx.receipt-processor.other-project\n"
            return m

        monkeypatch.setattr(init_cmd.subprocess, "run", fake_run)

        installed = init_cmd._install_receipt_processor_runner(
            str(fake_engine_root), project_id="project"
        )
        assert installed is True
        out = capsys.readouterr().out
        assert "not found in launchctl list" in out, (
            "verification reported the agent as installed when only another "
            "project's instance was in launchctl list (substring match)"
        )
        assert "installed launchd agent" not in out
