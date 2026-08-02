#!/usr/bin/env python3
"""OI-904 — `vnx gate` must derive --branch from the PR's headRefName, not the
local checkout branch.

T0 routinely runs `vnx gate <pr>` from main; the local checkout branch is then
'main' while the branch under review is the PR's head branch. Passing the local
branch to review_gate_manager made create_gate_worktree checkout origin/main
instead of the PR branch, so the gate agent's own file reads (sed/rg/cat)
missed the diff under review.

These tests run cmd_gate from scripts/commands/gate.sh with a mocked `gh`
binary (headRefName resolution) and a capturing fake review_gate_manager.py to
prove --branch carries the PR head ref — and that the local branch is only used
as an explicitly-logged fallback when gh cannot resolve the PR.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SH = REPO_ROOT / "scripts" / "commands" / "gate.sh"

PR_HEAD = "dispatch/20260801-oi898c-sidedoor-detector-exclusion"


def _write_script(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def env(tmp_path):
    """Build a self-contained harness: fake `gh`, a capturing fake
    review_gate_manager.py, a real git repo (for the local-branch fallback),
    and a bash driver that sources gate.sh and calls cmd_gate."""
    harness = tmp_path / "harness"
    fake_bin = harness / "bin"
    fake_home = harness / "vnx-home"
    (fake_home / "scripts").mkdir(parents=True)
    fake_state = harness / "state"
    (fake_state / "review_gates" / "results").mkdir(parents=True)
    project = harness / "project"

    # A real git repo on main, so the local-branch fallback is deterministic.
    subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
    subprocess.run(
        ["git", "-C", str(project), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
    (project / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "base"], check=True)

    # Fake gh: answer `pr view <n> --json headRefName --jq .headRefName` for
    # the known PR, fail loudly for any other PR number.
    _write_script(
        fake_bin / "gh",
        f"""#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ge 3 ] && [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  pr="$3"
  if [ "$pr" = "1281" ]; then
    printf '%s' '{PR_HEAD}'
    exit 0
  fi
  echo "gh: pr $pr not found" >&2
  exit 1
fi
echo "gh: unexpected args: $*" >&2
exit 1
""",
    )

    # Capturing fake review_gate_manager.py: persist argv, exit 0.
    capture = harness / "captured_args.json"
    _write_script(
        fake_home / "scripts" / "review_gate_manager.py",
        f"""#!/usr/bin/env python3
import json, sys
json.dump(sys.argv[1:], open({str(capture)!r}, "w"))
print(json.dumps({{"has_required_failure": False, "gates": []}}))
""",
    )

    return {
        "fake_bin": fake_bin,
        "fake_home": fake_home,
        "fake_state": fake_state,
        "project": project,
        "capture": capture,
    }


def _run_cmd_gate(env, *args: str) -> subprocess.CompletedProcess:
    """Source gate.sh (stubbing the bin/vnx-provided log/err helpers), then call
    cmd_gate with the mocked environment on PATH."""
    driver = env["fake_home"].parent / "run_gate.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
log() {{ printf '%s\\n' "$*"; }}
err() {{ printf 'ERROR: %s\\n' "$*" >&2; }}
export VNX_HOME={env['fake_home']}
export VNX_STATE_DIR={env['fake_state']}
export PROJECT_ROOT={env['project']}
export PATH={env['fake_bin']}:$PATH
source {GATE_SH}
cmd_gate "$@"
""",
        encoding="utf-8",
    )
    driver.chmod(0o755)
    return subprocess.run(
        ["bash", str(driver), *args],
        capture_output=True, text=True, timeout=30,
    )


def _captured_branch(env) -> str:
    captured = json.loads(env["capture"].read_text(encoding="utf-8"))
    assert "--branch" in captured
    return captured[captured.index("--branch") + 1]


class TestGateBranchDerivation:
    def test_branch_is_pr_head_ref_not_local_branch(self, env):
        """`vnx gate 1281` must pass --branch <headRefName>, never 'main'."""
        result = _run_cmd_gate(env, "1281", "--only", "codex")
        assert result.returncode == 0, result.stderr
        assert _captured_branch(env) == PR_HEAD

    def test_resolution_logged_explicitly(self, env):
        """The headRefName-derived branch is reported on stdout."""
        result = _run_cmd_gate(env, "1281", "--only", "gemini")
        assert result.returncode == 0, result.stderr
        assert "Resolved PR 1281 head branch" in result.stdout
        assert PR_HEAD in result.stdout

    def test_falls_back_to_local_branch_when_gh_fails(self, env):
        """When gh cannot resolve the PR, --branch must be the local checkout
        branch and the fallback is logged explicitly."""
        result = _run_cmd_gate(env, "9999", "--only", "codex")
        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stdout
        assert "fell back to local branch" in result.stdout
        assert _captured_branch(env) == "main"

    def test_head_ref_wins_even_when_local_branch_differs(self, env):
        """Regression guard: on a non-main local branch, the PR head ref must
        still win over the local branch name."""
        subprocess.run(
            ["git", "-C", str(env["project"]), "checkout", "-q", "-b", "feature/x"],
            check=True,
        )
        result = _run_cmd_gate(env, "1281", "--only", "codex")
        assert result.returncode == 0, result.stderr
        assert _captured_branch(env) == PR_HEAD
