"""Fixtures for the OI-1849 merge-target tests: a fake central install, a fake
consumer repo, and a ``gh`` stub on PATH.

Nothing here touches a real repo, a real ``gh``, or ``~/.vnx-system``. The
installs and consumers are tmp git repos with an ``origin`` URL and nothing
else; the stub is a small script that answers from a list of rules the test
hands it.

The stub is the point. It resolves ``{owner}/{repo}`` the way ``gh`` does, from
the ``origin`` of the directory it is run in, and writes every call to a log
with the repo it resolved. A test can therefore assert WHICH repo a call was
about, not merely that a call was made, and a call that lands in the wrong
repo either matches a rule written for the other repo or matches nothing.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

FABRIC_ORIGIN = "https://github.com/fabric/x.git"
CONSUMER_ORIGIN = "https://github.com/consumer/y.git"
FABRIC_REPO = "fabric/x"
CONSUMER_REPO = "consumer/y"

HEAD_SHA = "c" * 40
MAIN_SHA = "d" * 40

_GH_STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys

argv = sys.argv[1:]
cwd = os.getcwd()


def origin_repo(path):
    proc = subprocess.run(
        ["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ""
    url = proc.stdout.strip()
    if url.endswith(".git"):
        url = url[:-4]
    parts = url.replace(":", "/").split("/")
    return "/".join(parts[-2:])


repo = os.environ.get("GH_REPO") or origin_repo(cwd)
for index, arg in enumerate(argv):
    if arg in ("-R", "--repo") and index + 1 < len(argv):
        repo = argv[index + 1]
resolved = [arg.replace("{owner}/{repo}", repo) for arg in argv]
line = " ".join(resolved)

scenario = json.load(open(os.environ["GH_STUB_SCENARIO"]))
state_path = os.environ["GH_STUB_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {}

outcome = {"rc": 99, "stdout": "", "stderr": "gh stub: no rule for: " + line + "\n", "matched": False}
if argv[:2] == ["repo", "view"] and not repo:
    outcome = {"rc": 1, "stdout": "", "stderr": "not a git repository\n", "matched": True}
else:
    for index, rule in enumerate(scenario["rules"]):
        if rule.get("exact") is not None and rule["exact"] != line:
            continue
        if rule.get("match") is not None and rule["match"] not in line:
            continue
        if rule.get("repo") not in (None, repo):
            continue
        stdouts = rule.get("stdouts") or [rule.get("stdout", "")]
        seen = state.get(str(index), 0)
        state[str(index)] = seen + 1
        outcome = {
            "rc": rule.get("rc", 0),
            "stdout": stdouts[min(seen, len(stdouts) - 1)],
            "stderr": rule.get("stderr", ""),
            "matched": True,
        }
        break

with open(state_path, "w") as handle:
    json.dump(state, handle)
with open(os.environ["GH_STUB_LOG"], "a") as handle:
    handle.write(json.dumps({"argv": resolved, "cwd": cwd, "repo": repo, "matched": outcome["matched"]}) + "\n")
sys.stdout.write(outcome["stdout"])
sys.stderr.write(outcome["stderr"])
sys.exit(outcome["rc"])
'''


def git_repo(path: Path, origin: Optional[str]) -> Path:
    """A tmp git repo with an ``origin`` URL, nothing committed."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path.resolve()


def make_install(tmp_path: Path, *, version: str = "1.6.3", central: bool = True) -> Path:
    """A fake ``~/.vnx-system/versions/<v>``: a clone of the fabric repo that carries the
    ``.vnx-install-mode`` marker and a ``VERSION`` file."""
    install = git_repo(tmp_path / "install" / f"v{version}", FABRIC_ORIGIN)
    (install / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    if central:
        (install / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    return install


def make_consumer(tmp_path: Path) -> Path:
    """A consumer project checkout whose origin is ``consumer/y``."""
    return git_repo(tmp_path / "consumer", CONSUMER_ORIGIN)


def contents_response(text: str) -> str:
    """What ``gh api .../contents/<path>?ref=...`` prints for a file."""
    return json.dumps({"content": base64.b64encode(text.encode("utf-8")).decode("ascii")})


def consumer_yaml(*, enforcement: Optional[str] = None, ci_workflow: Optional[str] = None) -> str:
    """The smallest valid ``branch_protection.yaml`` a consumer could carry."""
    lines = [
        "branch: main",
        "required_status_checks:",
        "  strict: false",
        "  checks:",
        '    - {context: "CI", app_id: 15368}',
        "pending_checks: []",
        "required_pull_request_reviews: null",
        "enforce_admins: false",
        "required_signatures: false",
        "required_linear_history: false",
        "allow_force_pushes: false",
        "allow_deletions: false",
        "allow_fork_syncing: false",
        "block_creations: false",
        "lock_branch: false",
        "required_conversation_resolution: false",
        "restrictions: null",
        "repo:",
        "  allow_auto_merge: false",
        "rulesets: []",
    ]
    if enforcement is not None:
        lines.append(f"enforcement: {enforcement}")
    if ci_workflow is not None:
        lines.append(f'ci_workflow: "{ci_workflow}"')
    return "\n".join(lines) + "\n"


def github_protection(**overrides: Any) -> str:
    """What ``GET .../branches/main/protection`` returns for the ``consumer_yaml()``
    protection, with ``overrides`` applied to the flag blocks (``allow_force_pushes=True``)."""
    flags = {
        "enforce_admins": False,
        "required_signatures": False,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "allow_fork_syncing": False,
        "block_creations": False,
        "lock_branch": False,
        "required_conversation_resolution": False,
    }
    flags.update(overrides)
    body: Dict[str, Any] = {
        "required_status_checks": {"strict": False, "checks": [{"context": "CI", "app_id": 15368}]},
    }
    body.update({name: {"enabled": enabled} for name, enabled in flags.items()})
    return json.dumps(body)


def pr_view_json(*, state: str = "OPEN", sha: str = HEAD_SHA, title: str = "fix: something") -> str:
    return json.dumps({
        "number": 7, "title": title, "state": state, "headRefName": "feature/x",
        "baseRefName": "main", "headRefOid": sha, "mergedAt": None, "mergeCommit": None,
    })


def not_found_rules(repo: str, *paths: str) -> List[Dict[str, Any]]:
    """Confirmed 404s for ``paths`` at main, in ``repo``, with main itself resolvable."""
    rules: List[Dict[str, Any]] = [
        {"match": f"contents/{path}?ref=main", "repo": repo, "rc": 1, "stderr": "gh: Not Found (HTTP 404)\n"}
        for path in paths
    ]
    rules.append({"match": "commits/main", "repo": repo, "stdout": MAIN_SHA + "\n"})
    return rules


@dataclass
class GhStub:
    """Handle on the installed stub: read back what was called, and in which repo."""

    log_path: Path

    def calls(self) -> List[Dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]

    def repos_of(self, needle: str) -> set:
        """The repos every call whose command line contains ``needle`` resolved to."""
        return {call["repo"] for call in self.calls() if needle in " ".join(call["argv"])}

    def calls_with(self, needle: str) -> List[Dict[str, Any]]:
        return [call for call in self.calls() if needle in " ".join(call["argv"])]

    def unmatched(self) -> List[str]:
        return [" ".join(call["argv"]) for call in self.calls() if not call["matched"]]


def install_gh_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules: List[Dict[str, Any]],
) -> GhStub:
    """Put a ``gh`` on PATH that answers from ``rules`` (first match wins) and logs every call.

    A rule is ``{"match": <substring of the resolved command line> | "exact":
    <the whole line>, "repo": <only answer for this repo>, "stdout"|"stdouts":
    ..., "stderr": ..., "rc": ...}``; ``stdouts`` is answered in turn, the last
    one repeating.
    """
    bindir = tmp_path / "ghbin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "gh"
    script.write_text(_GH_STUB, encoding="utf-8")
    script.chmod(0o755)

    scenario = tmp_path / "gh_scenario.json"
    scenario.write_text(json.dumps({"rules": rules}), encoding="utf-8")
    log_path = tmp_path / "gh_calls.jsonl"
    monkeypatch.setenv("GH_STUB_SCENARIO", str(scenario))
    monkeypatch.setenv("GH_STUB_STATE", str(tmp_path / "gh_state.json"))
    monkeypatch.setenv("GH_STUB_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GH_REPO", raising=False)
    return GhStub(log_path=log_path)


def isolate_project_env(monkeypatch: pytest.MonkeyPatch, install: Path) -> None:
    """Make ``vnx_paths`` resolve as it does for a central install run from a project:
    ``VNX_HOME`` is the install, and nothing else names a project root."""
    monkeypatch.setenv("VNX_HOME", str(install))
    for name in ("VNX_PROJECT_ROOT", "PROJECT_ROOT", "VNX_BIN", "VNX_EXECUTABLE", "GH_REPO"):
        monkeypatch.delenv(name, raising=False)
