"""tests/test_md_promote_retired.py — the legacy .md promote flow is retired (T-A10).

Track ``terminal-binding-afbouwen``, deliverable P20a. A staged spec bundle
(``dispatches/pending/<id>/dispatch-spec.json``, fired with ``vnx dispatch <id>``)
is the only queue. What this file pins:

A. ``finalize_dispatch_delivery`` no longer promotes a delivered ``.md`` into
   ``dispatches/active/`` and emits no ``dispatch_promoted``: the file is
   consumed where it sits. **Red run**: on the base commit the same harness
   finds the file in ``active/`` and the promotion event in the register.
B. ``pr_queue_manager.py promote`` refuses instead of moving a staged ``.md``
   into ``dispatches/queue/`` (or auto-approved ``pending/``). **Red run**: on
   the base commit the command exits 0 and the file appears in ``pending/``.
C. a staged spec bundle is still the door's queue: ``stage_spec_bundle`` writes
   ``pending/<id>/dispatch-spec.json`` and the door's own staged-form predicate
   (``staging_validator._exists_in_dir``) still sees it.
D. the retired promote entry is in the retired-paths fragments, the check is
   clean on this tree, and it fails on a tree that still carries the symbol.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
LIFECYCLE_SH = PROJECT_ROOT / "scripts" / "lib" / "dispatch_lifecycle.sh"
PR_QUEUE_MANAGER = PROJECT_ROOT / "scripts" / "pr_queue_manager.py"
FRAGMENT = PROJECT_ROOT / "scripts" / "ci" / "retired_paths.d" / "dlv-fa8064ffd4cf.txt"

sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "ci"))
retired_check = importlib.import_module("check_retired_paths")


# ---------------------------------------------------------------------------
# A. finalize_dispatch_delivery: no promote into active/, no dispatch_promoted
# ---------------------------------------------------------------------------

def _extract_function(source_path: Path, fn_name: str) -> str:
    """Return the text of one top-level bash function, brace-balanced."""
    text = source_path.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(fn_name)}\s*\(\s*\)\s*\{{", text, re.MULTILINE)
    assert match, f"Function {fn_name} not found in {source_path}"
    depth = 0
    for index in range(match.end() - 1, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[match.start(): index + 1]
    raise AssertionError(f"Unbalanced braces extracting {fn_name}")


def _run_finalize(tmp_path: Path, *, delivery_confirmed: bool) -> dict:
    """Run the real ``finalize_dispatch_delivery`` with stubbed neighbours.

    The function body is taken from ``dispatch_lifecycle.sh`` itself (not
    re-implemented here); only its side-effect helpers are stubbed: the register
    CLI, ``notify_dispatch.py``, the progress/metadata helpers and
    ``rc_delivery_success``.
    """
    fn_body = _extract_function(LIFECYCLE_SH, "finalize_dispatch_delivery")
    actions_log = tmp_path / "actions.log"
    failures_log = tmp_path / "failures.log"
    pending_dir = tmp_path / "pending"
    active_dir = tmp_path / "active"
    pending_dir.mkdir()
    active_dir.mkdir()
    dispatch_id = "20261027-md-promote-001"
    dispatch_file = pending_dir / f"{dispatch_id}.md"
    dispatch_file.write_text("# legacy .md dispatch\n", encoding="utf-8")

    fake_vnx = tmp_path / "vnx"
    (fake_vnx / "scripts" / "lib").mkdir(parents=True)
    register_stub = fake_vnx / "scripts" / "lib" / "dispatch_register.py"
    register_stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import sys
            with open(r"{actions_log}", "a", encoding="utf-8") as fh:
                fh.write("REGISTER\\t" + " ".join(sys.argv[1:]) + "\\n")
            """
        ),
        encoding="utf-8",
    )
    register_stub.chmod(0o755)
    notify_stub = fake_vnx / "scripts" / "notify_dispatch.py"
    notify_stub.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    notify_stub.chmod(0o755)

    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -uo pipefail

        VNX_DIR="{fake_vnx}"
        ACTIVE_DIR="{active_dir}"
        _DL_RC_ATTEMPT_ID="attempt-1"

        log() {{ true; }}
        log_structured_failure() {{ echo "$1" >> "{failures_log}"; }}
        _fdd_update_progress_state() {{ true; }}
        _fdd_log_dispatch_metadata() {{ true; }}
        rc_delivery_success() {{ return {0 if delivery_confirmed else 1}; }}

        {fn_body}

        # The function turns `set -e` back on before returning 1, so call it in a
        # condition — otherwise the shell exits before the rc is reported.
        if finalize_dispatch_delivery \\
            "{dispatch_file}" "A" "T1" "{dispatch_id}" "" "PR-1" "backend-developer" "instruction" ""; then
            echo "RC=0"
        else
            echo "RC=1"
        fi
        """
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    rc_match = re.search(r"^RC=(\d+)$", proc.stdout, re.MULTILINE)
    assert rc_match, f"finalize did not report an exit code:\n{proc.stdout}\n{proc.stderr}"
    actions = actions_log.read_text(encoding="utf-8").splitlines() if actions_log.exists() else []
    return {
        "rc": int(rc_match.group(1)),
        "actions": actions,
        "pending_dir": pending_dir,
        "active_dir": active_dir,
        "dispatch_file": dispatch_file,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


class TestFinalizeDoesNotPromoteTheLegacyMd:
    def test_delivered_md_does_not_land_in_active(self, tmp_path):
        """The retired promote: a confirmed delivery must leave active/ empty."""
        result = _run_finalize(tmp_path, delivery_confirmed=True)
        assert result["rc"] == 0, result["stdout"] + result["stderr"]
        assert not (result["active_dir"] / result["dispatch_file"].name).exists(), (
            "finalize_dispatch_delivery still promotes a .md into dispatches/active/"
        )
        assert list(result["active_dir"].iterdir()) == []

    def test_delivered_md_is_consumed_from_pending(self, tmp_path):
        """Nothing stays in pending/ for the dispatcher to hand out again."""
        result = _run_finalize(tmp_path, delivery_confirmed=True)
        assert not result["dispatch_file"].exists()

    def test_no_dispatch_promoted_event_on_delivery(self, tmp_path):
        result = _run_finalize(tmp_path, delivery_confirmed=True)
        assert [line for line in result["actions"] if "dispatch_promoted" in line] == []

    def test_unconfirmed_delivery_keeps_the_file_in_pending(self, tmp_path):
        """Fail-closed branch unchanged: no acceptance, no consumption, no active/."""
        result = _run_finalize(tmp_path, delivery_confirmed=False)
        assert result["rc"] != 0
        assert result["dispatch_file"].exists()
        assert list(result["active_dir"].iterdir()) == []
        assert any("dispatch_failed" in line for line in result["actions"])


# ---------------------------------------------------------------------------
# B. the promote entry refuses instead of building a .md queue
# ---------------------------------------------------------------------------

def _promote_env(tmp_path: Path) -> dict:
    data_dir = tmp_path / "data"
    (data_dir / "dispatches" / "staging").mkdir(parents=True)
    (data_dir / "dispatches" / "queue").mkdir(parents=True)
    (data_dir / "dispatches" / "pending").mkdir(parents=True)
    (data_dir / "state").mkdir(parents=True)
    env = dict(os.environ)
    env.update(
        {
            "VNX_DATA_DIR": str(data_dir),
            "VNX_STATE_DIR": str(data_dir / "state"),
            "VNX_DISPATCH_DIR": str(data_dir / "dispatches"),
            "VNX_HOME": str(PROJECT_ROOT),
            # The auto-accept mode is what used to write straight into pending/.
            "VNX_QUEUE_POPUP_ENABLED": "0",
        }
    )
    return env


def test_promote_cli_refuses_and_writes_no_queue_file(tmp_path):
    env = _promote_env(tmp_path)
    staging_md = Path(env["VNX_DISPATCH_DIR"]) / "staging" / "staged-md-001.md"
    staging_md.write_text(
        "# Staged dispatch\n\n**Role**: backend-developer\n**Track**: A\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [sys.executable, str(PR_QUEUE_MANAGER), "promote", "staged-md-001"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    output = (proc.stdout + proc.stderr).lower()
    assert proc.returncode != 0, f"promote must refuse, got rc=0:\n{proc.stdout}"
    assert "retired" in output, output
    dispatches = Path(env["VNX_DISPATCH_DIR"])
    assert list((dispatches / "queue").iterdir()) == []
    assert list((dispatches / "pending").iterdir()) == []
    assert staging_md.exists(), "the staged file must be left where it is"


# ---------------------------------------------------------------------------
# C. a staged spec bundle is still the queue the door fires
# ---------------------------------------------------------------------------

def test_staged_spec_bundle_is_still_dispatchable(tmp_path):
    from dispatch_bridge import stage_spec_bundle
    from staging_validator import _exists_in_dir

    dispatch_id = "dlv-md-promote-retired-001"
    stage_spec_bundle(
        instruction_text="Retired promote flow test.\n",
        dispatch_id=dispatch_id,
        role="backend-developer",
        target_slot="T1",
        provider="claude",
        model="sonnet",
        data_dir=tmp_path,
    )
    bundle = tmp_path / "dispatches" / "pending" / dispatch_id
    assert (bundle / "dispatch-spec.json").is_file()
    assert (bundle / "instruction.md").is_file()
    assert _exists_in_dir(tmp_path / "dispatches" / "pending", dispatch_id) is True


# ---------------------------------------------------------------------------
# D. the retirement is pinned by scripts/ci/retired_paths.d
# ---------------------------------------------------------------------------

OLD_MODULE = """\
class PRQueueManager:
    def promote_dispatch(self, dispatch_id, force=False):
        return True
"""


def _make_tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def test_fragment_names_the_retired_promote_entry():
    text = FRAGMENT.read_text(encoding="utf-8")
    assert "scripts/pr_queue_manager.py::PRQueueManager.promote_dispatch" in text


def test_retired_paths_check_is_clean_on_this_tree():
    assert retired_check.main(["--root", str(PROJECT_ROOT), "--list", str(FRAGMENT)]) == 0


def test_retired_paths_check_fails_on_a_tree_that_still_carries_promote(tmp_path):
    """The fragment has teeth: the pre-change module fails the check."""
    old_tree = _make_tree(tmp_path, {"scripts/pr_queue_manager.py": OLD_MODULE})
    rc = retired_check.main(["--root", str(old_tree), "--list", str(FRAGMENT)])
    assert rc == 1, "a tree that still defines promote_dispatch must fail the retired-paths check"
