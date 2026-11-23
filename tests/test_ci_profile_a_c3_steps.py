"""The shell of two C3 workflow steps, run as GitHub runs it (``bash -eo pipefail``), read out of vnx-ci.yml itself.

``Profile A aggregate decision`` must put a mode in ``$GITHUB_OUTPUT`` only on a green decision; ``Select the
duration source`` must leave a file the shard plan accepts, a source or a named reason, on every path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ci_workflow_shape as shape

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "vnx-ci.yml"
SHARDS = [f"profile-a-shard-{k}" for k in range(1, 5)]
sys.path.insert(0, str(REPO / "scripts" / "ci"))
import profile_a_shard  # noqa: E402  (a sibling script, not a package)


def _run_block(step_name: str, role: str) -> str:
    return shape.profile_a(WORKFLOW).steps_named(step_name, role)[0].step["run"]


def _bash(script: str, tmp_path: Path, env: dict) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "RUNNER_"))}
    base.update(GITHUB_WORKSPACE=str(REPO), RUNNER_TEMP=str(tmp_path), GITHUB_OUTPUT=str(tmp_path / "output"),
                GITHUB_STEP_SUMMARY=str(tmp_path / "summary"))
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                          env={**base, **env}, capture_output=True, text=True, timeout=60)


def _decide(tmp_path, needs, xdist_off="false", profile="heavy", evidence=()):
    root = tmp_path / "evidence"
    for k in evidence:
        (root / f"shard-{k}").mkdir(parents=True)
        (root / f"shard-{k}" / "junit-shard.xml").write_text("<testsuites/>", encoding="utf-8")
    env = {"NEEDS_JSON": needs if isinstance(needs, str) else json.dumps(needs), "XDIST_OFF": xdist_off,
           "CI_PROFILE": profile, "PROFILE_A_EVIDENCE": str(root)}
    done = _bash(_run_block("Profile A aggregate decision", shape.ROLE_AGGREGATOR), tmp_path, env)
    output = tmp_path / "output"
    return done.returncode, output.read_text(encoding="utf-8") if output.exists() else ""


def _needs(result="success", profile="heavy", guards="success", **overrides):
    needs = {j: {"result": result, "outputs": {"profile": profile}} for j in SHARDS}
    needs["profile-a-guards"] = {"result": guards, "outputs": {}}
    needs.update(overrides)
    return needs


@pytest.mark.parametrize("needs, xdist_off, profile, evidence, mode", [
    (_needs(), "false", "heavy", (1, 2, 3, 4), "sharded"),
    (_needs(profile="light"), "false", "light", (), "light"),
    (_needs(result="skipped", profile=""), "true", "heavy", (), "serial"),
], ids=["sharded", "light", "serial"])
def test_a_green_decision_writes_exactly_its_mode(tmp_path, needs, xdist_off, profile, evidence, mode):
    assert _decide(tmp_path, needs, xdist_off, profile, evidence) == (0, f"mode={mode}\n")


@pytest.mark.parametrize("needs, xdist_off, profile, evidence, rc", [
    (_needs(), "false", "heavy", (1, 2, 4), 1),
    (_needs(**{"profile-a-shard-3": {"result": "failure", "outputs": {"profile": "heavy"}}}), "false", "heavy", (1, 2, 3, 4), 1),
    (_needs(result="skipped"), "false", "heavy", (1, 2, 3, 4), 1),
    (_needs(result="cancelled"), "false", "heavy", (1, 2, 3, 4), 1),
    (_needs(guards="failure"), "false", "heavy", (1, 2, 3, 4), 1),
    (_needs(), "true", "heavy", (1, 2, 3, 4), 1),
    (_needs(), "", "heavy", (1, 2, 3, 4), 2),
    (_needs(), "yes", "heavy", (1, 2, 3, 4), 2),
    (_needs(), "false", "", (1, 2, 3, 4), 2),
    ("", "false", "heavy", (1, 2, 3, 4), 2),
    ("{not json", "false", "heavy", (1, 2, 3, 4), 2),
], ids=["shard-without-evidence", "shard-failure", "shards-skipped-while-xdist-on", "shards-cancelled", "guards-failure",
        "shards-ran-while-xdist-off", "flag-empty", "flag-unknown", "profile-missing", "needs-empty", "needs-malformed"])
def test_a_red_or_unreadable_decision_writes_no_mode_and_fails_the_step(tmp_path, needs, xdist_off, profile, evidence, rc):
    assert _decide(tmp_path, needs, xdist_off, profile, evidence) == (rc, "")


FAKE_GH = """#!/usr/bin/env python3
import json, pathlib, sys
mode, log = sys.argv[0] + ".mode", sys.argv[0] + ".log"
with open(log, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
mode, args = pathlib.Path(mode).read_text().strip(), sys.argv[1:]
if mode == "down":
    sys.exit(1)
if args[:2] == ["api", "repos/o/r/actions/runs/7"]:
    print("2026-11-18T10:00:00Z")
elif mode == "created-only":
    sys.exit(1)
elif args[0] == "api" and "/workflows/" in args[1]:
    print(json.dumps({"workflow_runs": [{"id": 5, "conclusion": "success", "created_at": "2026-11-18T09:00:00Z", "updated_at": "2026-11-18T09:05:00Z"}]}))
elif args[0] == "api":
    print(json.dumps({"artifacts": [{"name": "profile-a-evidence", "expired": False}]}))
elif args[:2] == ["run", "download"]:
    dest = pathlib.Path(args[args.index("-D") + 1]) / "shard-1"
    dest.mkdir(parents=True)
    (dest / "junit-shard.xml").write_text("<testsuites/>")
"""


def _select_source(tmp_path: Path, mode: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh.mode").write_text(mode, encoding="utf-8")
    (bin_dir / "sleep").write_text("#!/bin/sh\necho \"$@\" >> \"$0.log\"\n", encoding="utf-8")
    for name in ("gh", "sleep"):
        (bin_dir / name).chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7"}
    done = _bash(_run_block("Select the duration source", shape.ROLE_SHARD), tmp_path, env)
    reads = (bin_dir / "gh.log").read_text().count("api repos/o/r/actions/runs/7 ")
    sleeps = (bin_dir / "sleep.log").read_text().split() if (bin_dir / "sleep.log").exists() else []
    return done, profile_a_shard.read_source(str(tmp_path / "shard-source.json")), reads, sleeps


def test_a_found_source_is_used_at_the_first_try(tmp_path):
    done, (source, xmls, reason), reads, sleeps = _select_source(tmp_path, "up")
    assert done.returncode == 0, done.stderr
    assert (source, reason, reads, sleeps) == ("5", None, 1, [])
    assert [p.name for p in xmls] == ["junit-shard.xml"]


@pytest.mark.parametrize("mode, why", [("down", "created_at of run 7 unreadable"), ("created-only", "gh api")],
                         ids=["created-at-unreadable", "no-source"])
def test_no_source_is_three_tries_ten_seconds_apart_and_a_reason_the_plan_accepts(tmp_path, mode, why):
    done, (source, xmls, reason), reads, sleeps = _select_source(tmp_path, mode)
    assert done.returncode == 0, done.stderr
    assert (source, xmls, reads, sleeps) == ("none", [], 3, ["10", "10"])
    assert why in reason
