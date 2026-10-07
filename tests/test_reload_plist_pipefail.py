"""The registration check of the launchd reload scripts must survive SIGPIPE (OI-1965).

Under ``set -o pipefail`` a ``launchctl list | grep -q <label>`` check fails on a
successful install: ``grep -q`` exits at the first match, ``launchctl`` is killed by
SIGPIPE when it keeps writing, and the pipeline ends on 141. These tests run the REAL
scripts in a subprocess with a tmp HOME and a stub ``launchctl`` whose ``list`` prints
the label early, followed by more than a pipe buffer of further lines, so the writer is
still writing after the match and the SIGPIPE is deterministic.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from launchd_test_support import make_central_engine, register_engine

REPO = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO / "scripts" / "launchd"
PROJECT = "pipefail-test"

# label on line 109, then ~90 KB of further lines (a pipe buffer is 64 KB)
LIST_STUB = r"""#!/bin/bash
case "$1" in
  list)
    i=1
    while [ "$i" -le 3000 ]; do
      if [ "$i" -eq 109 ] && [ "${STUB_LIST_LABEL:-}" != "" ]; then
        printf -- "-\t0\t%s\n" "$STUB_LIST_LABEL"
      else
        printf -- "-\t0\tcom.example.filler-agent-number-%05d\n" "$i"
      fi
      i=$((i + 1))
    done
    ;;
esac
exit 0
"""


def _setup(tmp_path: Path) -> tuple[Path, dict]:
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "launchctl"
    stub.write_text(LIST_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return home, env


def _run(args: list, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=60)


def _engine(tmp_path: Path, home: Path) -> Path:
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "scripts").symlink_to(REPO / "scripts", target_is_directory=True)
    register_engine(home, engine, PROJECT)
    return engine


class TestReloadPlist:
    LABEL = f"com.vnx.dashboard-generator.{PROJECT}"

    def _go(self, tmp_path: Path, list_label: str) -> subprocess.CompletedProcess:
        home, env = _setup(tmp_path)
        engine = _engine(tmp_path, home)
        env.update(VNX_HOME=str(engine), VNX_PROJECT_ID=PROJECT, STUB_LIST_LABEL=list_label)
        return _run(["bash", str(LAUNCHD_DIR / "reload_plist.sh"), "com.vnx.dashboard-generator"], env)

    def test_registered_label_early_in_a_long_list_is_success(self, tmp_path: Path) -> None:
        result = self._go(tmp_path, self.LABEL)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK: agent registered in launchctl" in result.stdout
        assert "WARNING" not in result.stderr

    def test_absent_label_is_a_failure_with_the_warning(self, tmp_path: Path) -> None:
        result = self._go(tmp_path, "")
        assert result.returncode != 0
        assert "WARNING: agent not found in launchctl list" in result.stderr
        assert "OK: agent registered" not in result.stdout


@pytest.mark.parametrize(
    "script,label,target",
    [
        (
            "reload_conversation_analyzer.sh",
            "com.vnx.conversation-analyzer",
            "scripts/conversation_analyzer_nightly.sh",
        ),
        (
            "reload_receipt_classifier.sh",
            "com.vnx.receipt-classifier-batch",
            "scripts/lib/receipt_classifier_batch.py",
        ),
    ],
)
class TestSiblingReloadScripts:
    def _go(self, tmp_path: Path, script: str, label: str, target: str, list_label: str):
        home, env = _setup(tmp_path)
        engine = make_central_engine(tmp_path / "engine")
        target_path = engine / target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("", encoding="utf-8")
        env.update(VNX_HOME=str(engine), STUB_LIST_LABEL=list_label)
        result = _run(["bash", str(LAUNCHD_DIR / script)], env)
        return home, result

    def test_registered_label_early_in_a_long_list_is_success(
        self, tmp_path: Path, script: str, label: str, target: str
    ) -> None:
        home, result = self._go(tmp_path, script, label, target, label)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK: agent registered in launchctl" in result.stdout
        assert "WARNING" not in result.stderr
        assert (home / "Library" / "LaunchAgents" / f"{label}.plist").is_file()

    def test_absent_label_is_a_failure_with_the_warning(
        self, tmp_path: Path, script: str, label: str, target: str
    ) -> None:
        _, result = self._go(tmp_path, script, label, target, "")
        assert result.returncode != 0
        assert "WARNING: agent not found in launchctl list" in result.stderr
        assert "OK: agent registered" not in result.stdout
