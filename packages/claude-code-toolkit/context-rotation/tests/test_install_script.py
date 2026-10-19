"""tests/live/install_test.sh with a fake CLI on PATH: what its exit trap removes.

The fake keeps its marketplaces in a state file and logs every call, so each test reads
back what was removed. The real CLI never runs and no login is touched.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from conftest import PKG

SCRIPT = PKG / "tests" / "live" / "install_test.sh"
MARKETPLACE = "claude-code-toolkit"
OPERATORS = f"{MARKETPLACE} /operators/own\n"

FAKE_CLAUDE = r'''#!/bin/sh
state=$FAKE_STATE
echo "$*" >> "$state/calls"
case "$1 $2 $3" in
"plugin marketplace list")
    n=$(cat "$state/list_calls" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$state/list_calls"
    [ "$n" -lt "${FAKE_LIST_OK_AFTER:-0}" ] && exit 1
    [ -n "${FAKE_LIST_HIDES_EXISTING:-}" ] && [ "$n" -eq 0 ] && exit 0
    while read -r name path; do
        [ -n "$name" ] && printf '  > %s\n    Source: Directory (%s)\n' "$name" "$path"
    done < "$state/marketplaces"
    exit 0 ;;
"plugin marketplace add")
    [ -n "${FAKE_ADD_FAILS:-}" ] && { echo "already exists" >&2; exit 1; }
    echo "$FAKE_MARKETPLACE ${FAKE_ADD_POINTS_TO:-$4}" > "$state/marketplaces"
    exit 0 ;;
"plugin marketplace remove")
    : > "$state/marketplaces"
    exit 0 ;;
esac
case "$1 $2" in
"plugin install") exit "${FAKE_INSTALL_EXIT:-0}" ;;
"plugin uninstall") exit 0 ;;
"plugin list")
    printf '[{"id": "%s", "scope": "project", "projectPath": "%s"}]\n' "$FAKE_PLUGIN_ID" "$(pwd -P)"
    exit 0 ;;
esac
exit 1
'''

FAKE_EXPORT = r'''#!/bin/sh
while [ $# -gt 0 ]; do
    case "$1" in
        --target) target=$2; shift 2 ;;
        --tag) tag=$2; shift 2 ;;
        *) shift ;;
    esac
done
version=${tag#context-rotation-v}
git -C "$target" checkout -q -b "export/context-rotation-$version"
mkdir -p "$target/.claude-plugin" "$target/context-rotation/.claude-plugin"
echo '{}' > "$target/.claude-plugin/marketplace.json"
echo '{}' > "$target/context-rotation/.claude-plugin/plugin.json"
'''


@pytest.fixture
def rig(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)
    source = tmp_path / "source"
    (source / "scripts" / "maintenance").mkdir(parents=True)
    (source / "scripts" / "maintenance" / "export_toolkit_plugin.sh").write_text(FAKE_EXPORT)
    state = tmp_path / "state"
    state.mkdir()
    (state / "marketplaces").write_text("")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_STATE=str(state),
               FAKE_MARKETPLACE=MARKETPLACE, FAKE_PLUGIN_ID=f"context-rotation@{MARKETPLACE}", TMPDIR=str(tmp))

    class Rig:
        def run(self, **extra):
            return subprocess.run(
                ["sh", str(SCRIPT), "--commit", "abc", "--tag", "context-rotation-v9.9.9", "--source", str(source)],
                capture_output=True, text=True, timeout=120, env=dict(env, **extra), cwd=str(tmp_path))

        def calls(self):
            return (state / "calls").read_text().splitlines() if (state / "calls").exists() else []

        def marketplaces(self):
            return (state / "marketplaces").read_text()

        def own(self, text):
            (state / "marketplaces").write_text(text)

    return Rig()


def removals(calls):
    return [c for c in calls if c.startswith(("plugin marketplace remove", "plugin uninstall"))]


def test_the_script_passes_the_syntax_check_of_sh():
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0


def test_a_failing_listing_stops_before_anything_is_added(rig):
    rig.own(OPERATORS)
    proc = rig.run(FAKE_LIST_OK_AFTER="1")
    assert proc.returncode == 1 and "marketplace list failed" in proc.stderr
    assert removals(rig.calls()) == []
    assert not any(c.startswith("plugin marketplace add") for c in rig.calls())
    assert rig.marketplaces() == OPERATORS


def test_an_add_that_fails_because_the_marketplace_exists_removes_nothing(rig):
    rig.own(OPERATORS)
    proc = rig.run(FAKE_LIST_HIDES_EXISTING="1", FAKE_ADD_FAILS="1")
    assert proc.returncode == 1 and "marketplace add failed" in proc.stderr
    assert removals(rig.calls()) == []
    assert rig.marketplaces() == OPERATORS


def test_a_marketplace_that_does_not_point_at_this_runs_clone_is_never_removed(rig):
    rig.own(OPERATORS)
    proc = rig.run(FAKE_LIST_HIDES_EXISTING="1", FAKE_ADD_POINTS_TO="/operators/own")
    assert proc.returncode == 1 and "does not point at" in proc.stderr
    assert removals(rig.calls()) == []


def test_a_successful_run_removes_exactly_its_own_marketplace_and_plugin(rig):
    proc = rig.run()
    assert proc.returncode == 0, proc.stderr
    assert removals(rig.calls()) == [f"plugin uninstall context-rotation@{MARKETPLACE} --scope project",
                                     f"plugin marketplace remove {MARKETPLACE}"]
    assert rig.marketplaces() == ""


def test_a_failed_install_removes_the_marketplace_but_does_not_uninstall(rig):
    proc = rig.run(FAKE_INSTALL_EXIT="1")
    assert proc.returncode == 1
    assert removals(rig.calls()) == [f"plugin marketplace remove {MARKETPLACE}"]
