"""tests/test_t0_skill_guard_binding.py — OI-2208.

The t0-orchestrator skill scripts must answer the same from any cwd and must not
read the ``t0_brief.json`` presentation cache:

* ``dispatch_guard.sh`` runs every ``vnx`` call bound to its own repo root.
* ``intelligence.sh`` ``can_dispatch`` delegates to the guard and maps its exit code.
* the two shipped copies of both scripts stay byte-identical.

Each test builds a throwaway git repo that holds a copy of the scripts under test and a
stub ``bin/vnx`` that answers per working directory, so the real runtime is never read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIRS = (
    ROOT / "skills" / "t0-orchestrator",
    ROOT / ".claude" / "skills" / "t0-orchestrator",
)
SCRIPTS = ("dispatch_guard.sh", "intelligence.sh")


def _status(busy: bool) -> dict:
    live = [{"dispatch_id": "20261020-busy", "state": "running", "lock": "held", "age_seconds": 5}] if busy else []
    return {
        "t0_state_available": True,
        "system_health": {"status": "healthy"},
        "queues": {"pending_count": 0, "conflict_count": 0},
        "live_work": {
            "available": True,
            "live": live,
            "starting": [],
            "stale": [],
            "unmeasured": [],
        },
    }


def _make_repo(tmp_path: Path) -> Path:
    """A git repo with the scripts and a vnx stub: busy inside the repo, idle elsewhere."""
    repo = tmp_path / "repo"
    scripts = repo / "skills" / "t0-orchestrator" / "scripts"
    scripts.mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy(SKILL_DIRS[0] / "scripts" / name, scripts / name)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    busy_json = tmp_path / "busy.json"
    idle_json = tmp_path / "idle.json"
    busy_json.write_text(json.dumps(_status(True)), encoding="utf-8")
    idle_json.write_text(json.dumps(_status(False)), encoding="utf-8")
    stub = repo / "bin" / "vnx"
    stub.parent.mkdir()
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "status --json" ]; then\n'
        f'  if [ "$(pwd -P)" = "{repo.resolve()}" ]; then cat "{busy_json}"; else cat "{idle_json}"; fi\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return repo


def _env() -> dict:
    env = dict(os.environ)
    for key in ("VNX_BIN", "VNX_HOME", "VNX_STATE_DIR", "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT"):
        env.pop(key, None)
    return env


def _guard(repo: Path, cwd: Path) -> subprocess.CompletedProcess:
    script = repo / "skills" / "t0-orchestrator" / "scripts" / "dispatch_guard.sh"
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=_env(), cwd=cwd)


def _intel(repo: Path, cwd: Path, function: str) -> subprocess.CompletedProcess:
    script = repo / "skills" / "t0-orchestrator" / "scripts" / "intelligence.sh"
    return subprocess.run(
        ["bash", "-c", f'source "{script}" >/dev/null; {function}'],
        capture_output=True, text=True, env=_env(), cwd=cwd,
    )


class TestGuardBoundToRepoRoot:
    def test_same_answer_from_inside_and_outside_the_repo(self, tmp_path):
        repo = _make_repo(tmp_path)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        inside_run = _guard(repo, repo)
        outside_run = _guard(repo, outside)
        assert inside_run.returncode == 2, inside_run.stdout + inside_run.stderr
        assert outside_run.returncode == 2, (
            f"guard answered {outside_run.returncode} from outside the repo: "
            f"{outside_run.stdout}{outside_run.stderr}"
        )
        assert "WAIT" in outside_run.stdout


class TestCanDispatchDelegatesToGuard:
    def _write_idle_brief(self, repo: Path) -> None:
        state = repo / ".vnx-data" / "state"
        state.mkdir(parents=True)
        (state / "t0_brief.json").write_text(
            json.dumps({
                "terminals": {"T1": {"status": "idle", "status_age_seconds": 1}},
                "queues": {"pending": 0, "active": 0, "conflicts": 0},
            }),
            encoding="utf-8",
        )

    def test_wait_from_guard_is_no_even_with_idle_brief(self, tmp_path):
        repo = _make_repo(tmp_path)
        self._write_idle_brief(repo)
        result = _intel(repo, repo, "can_dispatch")
        assert result.stdout.startswith("NO - "), result.stdout + result.stderr
        assert "busy: live_work" in result.stdout
        assert "YES" not in result.stdout

    def test_go_from_guard_is_yes(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "bin" / "vnx").write_text(
            "#!/usr/bin/env bash\n"
            f"cat <<'JSON'\n{json.dumps(_status(False))}\nJSON\n",
            encoding="utf-8",
        )
        result = _intel(repo, tmp_path, "can_dispatch")
        assert result.stdout.startswith("YES"), result.stdout + result.stderr

    def test_missing_state_is_unknown_never_yes(self, tmp_path):
        repo = _make_repo(tmp_path)
        self._write_idle_brief(repo)
        (repo / "bin" / "vnx").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        result = _intel(repo, repo, "can_dispatch")
        assert result.stdout.startswith("UNKNOWN - "), result.stdout + result.stderr
        assert "YES" not in result.stdout

    def test_status_readers_never_use_the_brief(self, tmp_path):
        repo = _make_repo(tmp_path)
        self._write_idle_brief(repo)
        queue = _intel(repo, tmp_path, "check_queue")
        assert queue.stdout.strip() == "pending=0 conflicts=0", queue.stdout + queue.stderr
        live = _intel(repo, tmp_path, "check_live_work")
        assert "20261020-busy" in live.stdout, live.stdout + live.stderr


class TestScriptCopiesIdentical:
    @pytest.mark.parametrize("name", SCRIPTS)
    def test_byte_identical(self, name):
        shipped = (SKILL_DIRS[0] / "scripts" / name).read_bytes()
        local = (SKILL_DIRS[1] / "scripts" / name).read_bytes()
        assert shipped == local


class TestLaneParagraph:
    @pytest.mark.parametrize("skill_dir", SKILL_DIRS, ids=["skills", ".claude/skills"])
    def test_lane_rule_names_only_the_headless_claude_lane(self, skill_dir):
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        start = text.index("Lane rule the door enforces:")
        paragraph = text[start:text.index("Known interim side door", start)]
        assert "`claude_headless`" in paragraph
        assert "2026-09-18" in paragraph
        assert "tmux-spawn lane**" not in paragraph
        assert "usable-not-yet-governed" not in paragraph
        assert "`provider_dispatch`" in paragraph
