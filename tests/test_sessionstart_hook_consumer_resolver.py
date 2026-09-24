#!/usr/bin/env python3
"""tests/test_sessionstart_hook_consumer_resolver.py — the SessionStart hook
finds the central store from inside a CONSUMER project (v1.6.2 regression).

Defect: hooks/sessionstart.sh located the Python resolver at
``$_HOOK_DIR/../scripts/lib/vnx_paths.py``. In this fabric repo the hook sits in
``hooks/`` so ``../scripts`` is the repo's own ``scripts/``. In a consumer the
deployed hook sits in ``<project>/.claude/hooks/``, so the same expression points
at ``<project>/.claude/scripts/lib/vnx_paths.py``, which does not exist. The hook
then reported "VNX STATE STORE NOT FOUND" and every T0 section (terminal states,
open items, receipts, beacon health, producer freshness, state freshness) was
UNMEASURED: a consumer T0 was blind.

The fix picks ONE scripts root, once, in this order:
  (a) ``$_HOOK_DIR/../scripts``           the fabric repo itself
  (b) ``$VNX_HOME/scripts``               an explicit fabric location
  (c) ``$HOME/.vnx-system/current/scripts``   the installed fabric
and every later script call uses that root. ``vnx_paths.resolve_paths()`` stays
the only resolver.

Isolation: every test runs the hook with a bare environment (PATH + a tmp HOME)
so it can never read the real ``~/.vnx-data`` or ``~/.vnx-system``. The "installed
fabric" is a real copy of ``scripts/`` in a tmp git repo carrying the
``.vnx-install-mode = central`` marker, i.e. the same shape ``install-central.sh``
produces: a symlink would make ``vnx_paths`` resolve back into this checkout and
never exercise the central-install branch.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "sessionstart.sh"

CONSUMER_ID = "consumer-proj"
FABRIC_ID = "fabric-proj"

CONSUMER_TERMINAL_MARKER = "CONSUMER-TERMINAL-MARKER-4c1e"
CONSUMER_OI_MARKER = "CONSUMER-OI-MARKER-83d0"
FABRIC_OI_MARKER = "FABRIC-OI-DECOY-MARKER-e5a7"

POISONED_RESOLVER = "raise RuntimeError('this scripts root must not have been chosen')\n"


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


def _write_state(state: Path, terminal_marker: str, oi_marker: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "terminal_state.json").write_text(
        json.dumps({"terminals": {"T1": {"status": "busy", "last_activity": terminal_marker}}}),
        encoding="utf-8",
    )
    (state / "open_items.json").write_text(
        json.dumps({"items": [
            {"status": "open", "severity": "blocker", "id": "OI-1", "title": oi_marker},
        ]}),
        encoding="utf-8",
    )


def _write_store(home: Path, project_id: str, terminal_marker: str, oi_marker: str) -> None:
    _write_state(home / ".vnx-data" / project_id / "state", terminal_marker, oi_marker)


def _poisoned_fabric(root: Path) -> Path:
    """A scripts root whose vnx_paths.py exists (so it is FOUND) but raises when
    run: choosing it over the real one turns into STATE STORE NOT FOUND."""
    lib = root / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "vnx_paths.py").write_text(POISONED_RESOLVER, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def installed_fabric(tmp_path_factory) -> Path:
    """A tmp copy of the fabric shaped like ~/.vnx-system/versions/<v>: a git repo
    whose top level is the install itself, marked central. It carries a
    .vnx-project-id of its own so a resolution that wrongly collapsed onto the
    fabric would land on FABRIC_ID's store and show the decoy."""
    fabric = tmp_path_factory.mktemp("fabric") / "v-test"
    shutil.copytree(
        REPO / "scripts", fabric / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (fabric / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    (fabric / ".vnx-project-id").write_text(FABRIC_ID + "\n", encoding="utf-8")
    _git_init(fabric)
    return fabric


@pytest.fixture
def home(tmp_path) -> Path:
    """A tmp HOME holding a consumer store AND a decoy store for the fabric's own id."""
    h = tmp_path / "home"
    h.mkdir()
    _write_store(h, CONSUMER_ID, CONSUMER_TERMINAL_MARKER, CONSUMER_OI_MARKER)
    _write_store(h, FABRIC_ID, "FABRIC-TERMINAL-DECOY", FABRIC_OI_MARKER)
    return h


@pytest.fixture
def consumer(tmp_path):
    """A consumer project as `vnx init` leaves it: a deployed hook in
    .claude/hooks/, a T0 terminal dir, a .vnx-project-id, and NO .claude/scripts/."""
    project = tmp_path / "consumer"
    (project / ".vnx").mkdir(parents=True)
    (project / ".vnx-project-id").write_text(CONSUMER_ID + "\n", encoding="utf-8")
    hooks = project / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "sessionstart.sh"
    shutil.copy2(HOOK, hook)
    t0 = project / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    _git_init(project)
    assert not (project / ".claude" / "scripts").exists()
    return hook, t0


def _run_hook(hook: Path, cwd: Path, env: dict) -> str:
    r = subprocess.run(
        ["bash", str(hook)], capture_output=True, text=True, cwd=str(cwd), env=env, timeout=60,
    )
    assert r.returncode == 0, f"hook must exit 0: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def _bare_env(home: Path, **extra: str) -> dict:
    """PATH + HOME and nothing else: no VNX_* variable can leak in from the caller."""
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    env.update(extra)
    return env


def _link_installed_fabric(home: Path, fabric: Path) -> None:
    """Make `fabric` reachable as $HOME/.vnx-system/current, the way install-central.sh does."""
    system = home / ".vnx-system"
    system.mkdir()
    (system / "current").symlink_to(fabric)


def _assert_consumer_store_read(ctx: str) -> None:
    assert "VNX STATE STORE NOT FOUND" not in ctx, ctx
    assert CONSUMER_TERMINAL_MARKER in ctx, ctx
    assert CONSUMER_OI_MARKER in ctx, ctx
    assert FABRIC_OI_MARKER not in ctx, "resolved the fabric's own store instead of the consumer's"
    # The three script-driven sections must have reached their scripts too: every
    # later `$_HOOK_DIR/../scripts/...` call has to follow the same root.
    assert "scripts/health_check.py missing" not in ctx, ctx
    assert "scripts/producer_freshness_monitor.py missing" not in ctx, ctx
    assert "scripts/lib/session_state_freshness.py missing" not in ctx, ctx
    assert "BEACON HEALTH UNAVAILABLE" not in ctx, ctx
    assert "STATE FRESHNESS UNAVAILABLE" not in ctx, ctx


class TestConsumerFindsItsStore:
    def test_installed_fabric_under_home(self, tmp_path, installed_fabric, home, consumer):
        """(c): no .claude/scripts, no VNX_HOME, the fabric only under
        $HOME/.vnx-system/current. The store resolved must be the CONSUMER's."""
        hook, t0 = consumer
        _link_installed_fabric(home, installed_fabric)
        ctx = _run_hook(hook, t0, _bare_env(home))
        _assert_consumer_store_read(ctx)

    def test_vnx_home_env(self, tmp_path, installed_fabric, home, consumer):
        """(b): VNX_HOME names the fabric, and there is no ~/.vnx-system at all."""
        hook, t0 = consumer
        ctx = _run_hook(hook, t0, _bare_env(home, VNX_HOME=str(installed_fabric)))
        _assert_consumer_store_read(ctx)

    def test_vnx_home_wins_over_installed_fabric(self, tmp_path, installed_fabric, home, consumer):
        """(b) before (c): VNX_HOME is the good fabric, the one under $HOME is
        poisoned. Choosing (c) would end in STATE STORE NOT FOUND."""
        hook, t0 = consumer
        poisoned = _poisoned_fabric(tmp_path / "poisoned-installed")
        _link_installed_fabric(home, poisoned)
        ctx = _run_hook(hook, t0, _bare_env(home, VNX_HOME=str(installed_fabric)))
        _assert_consumer_store_read(ctx)


class TestFabricRepoUnchanged:
    def test_repo_hook_keeps_using_its_own_scripts(self, tmp_path, home):
        """(a): the hook in this repo's hooks/ resolves ../scripts and never
        looks further, even when the installed fabric under $HOME is poisoned."""
        _link_installed_fabric(home, _poisoned_fabric(tmp_path / "poisoned-installed"))
        data_home = tmp_path / "data-home"
        _write_state(data_home / CONSUMER_ID / "state", CONSUMER_TERMINAL_MARKER, CONSUMER_OI_MARKER)

        t0 = tmp_path / "terminal-project" / ".claude" / "terminals" / "T0"
        t0.mkdir(parents=True)
        (tmp_path / "terminal-project" / ".vnx").mkdir()

        ctx = _run_hook(
            HOOK, t0,
            _bare_env(home, VNX_DATA_HOME=str(data_home), VNX_PROJECT_ID=CONSUMER_ID),
        )
        assert "VNX STATE STORE NOT FOUND" not in ctx, ctx
        assert CONSUMER_TERMINAL_MARKER in ctx, ctx
        assert CONSUMER_OI_MARKER in ctx, ctx


class TestNoScriptsRootAvailable:
    def test_unmeasured_names_the_three_places_tried(self, tmp_path, home, consumer):
        """No root anywhere: still UNMEASURED (never a silent zero), and the line
        says which three places were probed so a reader sees WHY."""
        hook, t0 = consumer
        ctx = _run_hook(hook, t0, _bare_env(home))
        assert "VNX STATE STORE NOT FOUND" in ctx, ctx
        assert "UNMEASURED" in ctx, ctx
        assert f"{hook.parent.resolve()}/../scripts" in ctx, ctx
        assert "$VNX_HOME/scripts" in ctx, ctx
        assert "not set" in ctx, ctx
        assert f"{home}/.vnx-system/current/scripts" in ctx, ctx
        assert "No open items data" not in ctx
        assert "No terminal state data" not in ctx

    def test_vnx_home_set_but_without_a_resolver_is_named_in_full(self, tmp_path, home, consumer):
        hook, t0 = consumer
        empty_home = tmp_path / "vnx-home-without-scripts"
        empty_home.mkdir()
        ctx = _run_hook(hook, t0, _bare_env(home, VNX_HOME=str(empty_home)))
        assert "VNX STATE STORE NOT FOUND" in ctx, ctx
        assert f"{empty_home}/scripts" in ctx, ctx

    def test_a_root_that_is_found_but_cannot_resolve_says_so(self, tmp_path, home, consumer):
        """Found-but-failing is a different cause than not-found: the message must
        not claim the resolver was unreachable when it ran and failed."""
        hook, t0 = consumer
        _link_installed_fabric(home, _poisoned_fabric(tmp_path / "poisoned-installed"))
        ctx = _run_hook(hook, t0, _bare_env(home))
        assert "VNX STATE STORE NOT FOUND" in ctx, ctx
        assert "UNMEASURED" in ctx, ctx
        assert f"{home}/.vnx-system/current/scripts" in ctx, ctx
        assert "no scripts root" not in ctx, ctx


def test_hook_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
