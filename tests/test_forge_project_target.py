#!/usr/bin/env python3
"""OI-1789: a gate verdict is published to the repo of the project whose store holds it.

The target repo used to come from ``REPO_ROOT`` (derived from ``__file__``), so
every project's verdict ended up on the engine's own repo. These tests drive the
real recorder hook and the real ``forge_check_run.publish_check_run`` against a
recorded ``_api_request``/``gh`` and assert the URLs and argv that actually
left. ADR-007: every scenario has a second project whose ids collide with the
first and must not receive the verdict.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

import forge_check_run
import forge_gate_publisher as fgp
import gate_recorder

HEAD = "a" * 40
PR = 1811

_REAL_RUN = subprocess.run
_REAL_AUTO_MERGE = fgp.auto_merge_is_armed


def _git(cwd: Path, *args: str) -> None:
    _REAL_RUN(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_checkout(
    root: Path, project_id: str, origin: str, *, install_mode: str = ""
) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "remote", "add", "origin", origin)
    (root / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
    if install_mode:
        (root / ".vnx-install-mode").write_text(install_mode + "\n", encoding="utf-8")
    return root


def _results_dir(home: Path, project_id: str) -> Path:
    results = home / ".vnx-data" / project_id / "state" / "review_gates" / "results"
    results.mkdir(parents=True)
    return results


def _register(home: Path, entries: Dict[str, Path]) -> None:
    registry = home / ".vnx" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "projects": [
                    {"project_id": pid, "path": str(path)} for pid, path in entries.items()
                ],
            }
        ),
        encoding="utf-8",
    )


def _payload() -> Dict[str, Any]:
    return {
        "gate": "glm_gate",
        "pr_id": "",
        "pr_number": PR,
        "status": "pass",
        "commit_sha": HEAD,
        "branch": "dispatch/x",
        "contract_hash": "sha256:deadbeef",
        "report_path": "/dev/null",
        "blocking_findings": [],
        "dispatch_id": "glm-gate-pr1811-1788800000",
        "recorded_at": "2026-09-08T00:00:00Z",
    }


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home. ``Path.home`` is patched (not the HOME env): the central-store
    write guard keys on HOME and must keep seeing the real one."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    monkeypatch.delenv("VNX_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    return fake


@pytest.fixture
def posts(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Dict[str, Any]]]:
    """The REAL ``publish_check_run`` over a recording ``_api_request``."""
    seen: List[Tuple[str, Dict[str, Any]]] = []

    def fake_api_request(method: str, url: str, *, headers: Any, payload: Any = None, **_k: Any):
        seen.append((url, json.loads(payload) if payload else {}))
        return 201, json.dumps({"id": 1})

    monkeypatch.setenv(forge_check_run.TEST_POST_OPT_IN_ENV, "1")
    monkeypatch.setattr(forge_check_run, "_api_request", fake_api_request)
    monkeypatch.setattr(
        forge_check_run, "_installation_token_with_freshness", lambda **_k: ("t", True)
    )
    monkeypatch.setattr(fgp, "publish_check_run", forge_check_run.publish_check_run)
    monkeypatch.setattr(
        fgp, "load_app_config", lambda *_a, **_k: forge_check_run.AppConfig("vnx-gate", 42)
    )
    monkeypatch.setattr(fgp, "auto_merge_is_armed", lambda *_a, **_k: False)
    return seen


def _write_and_publish(results: Path) -> Path:
    path = results / f"pr-{PR}-glm_gate.json"
    _on_disk, written = gate_recorder.write_result_guarded(
        path, _payload(), gate="glm_gate", pr_ref=str(PR)
    )
    assert written is True
    return path


def _urls(posts: List[Tuple[str, Dict[str, Any]]]) -> List[str]:
    return [url for url, _ in posts]


def _names(posts: List[Tuple[str, Dict[str, Any]]]) -> List[str]:
    return sorted(body["name"] for _, body in posts)


# ---------------------------------------------------------------------------
# A1: the right repo, for both checks, per project
# ---------------------------------------------------------------------------


def test_both_checks_go_to_the_repo_of_the_store_they_were_written_to(
    tmp_path: Path, home: Path, posts: List[Any]
) -> None:
    project_b = _make_checkout(tmp_path / "b", "project-b", "https://github.com/acme/project-b.git")
    project_c = _make_checkout(tmp_path / "c", "project-c", "https://github.com/acme/project-c.git")
    _register(home, {"project-b": project_b, "project-c": project_c})

    _write_and_publish(_results_dir(home, "project-b"))

    assert _urls(posts) == ["https://api.github.com/repos/acme/project-b/check-runs"] * 2
    assert _names(posts) == ["vnx-gate/glm_gate", "vnx-gate/review"]

    del posts[:]
    _write_and_publish(_results_dir(home, "project-c"))

    # Same PR number, same gate, same head: only the store tells the projects apart.
    assert _urls(posts) == ["https://api.github.com/repos/acme/project-c/check-runs"] * 2


# ---------------------------------------------------------------------------
# A2/A3: refusals post nothing and leave the record alone
# ---------------------------------------------------------------------------


def test_an_unregistered_store_posts_nothing_and_leaves_the_record_alone(
    tmp_path: Path, home: Path, posts: List[Any], caplog: pytest.LogCaptureFixture
) -> None:
    other = _make_checkout(tmp_path / "c", "project-c", "https://github.com/acme/project-c.git")
    _register(home, {"project-c": other})
    results = _results_dir(home, "project-b")

    with caplog.at_level(logging.INFO):
        path = _write_and_publish(results)

    assert posts == []
    before = path.read_bytes()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        gate_recorder.publish_forge_check_run(_payload(), gate="glm_gate", result_path=path)
    assert posts == []
    assert path.read_bytes() == before
    refusal = [r.getMessage() for r in caplog.records if "PUBLICATIE MISLUKT" in r.getMessage()]
    assert refusal, "de weigering hoort als een regel met herstelcommando in het log te staan"
    assert "ForgePublishRefused" in refusal[0]
    assert "herstel met" in refusal[0]
    assert "projects.json" in refusal[0]


def test_a_registered_checkout_with_another_project_id_is_refused(
    tmp_path: Path, home: Path, posts: List[Any]
) -> None:
    wrong = _make_checkout(tmp_path / "w", "project-c", "https://github.com/acme/project-c.git")
    _register(home, {"project-b": wrong})

    _write_and_publish(_results_dir(home, "project-b"))

    assert posts == []


def test_an_ambient_project_root_of_another_project_is_not_trusted(
    tmp_path: Path, home: Path, posts: List[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = _make_checkout(tmp_path / "s", "project-c", "https://github.com/acme/project-c.git")
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(stale))

    _write_and_publish(_results_dir(home, "project-b"))

    assert posts == []


def test_vnx_project_root_is_a_second_source_when_the_marker_matches(
    tmp_path: Path, home: Path, posts: List[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = _make_checkout(tmp_path / "b", "project-b", "https://github.com/acme/project-b.git")
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(mine))

    _write_and_publish(_results_dir(home, "project-b"))

    assert _urls(posts) == ["https://api.github.com/repos/acme/project-b/check-runs"] * 2


def test_a_central_install_is_never_a_project(
    tmp_path: Path, home: Path, posts: List[Any]
) -> None:
    install = _make_checkout(
        tmp_path / "install", "project-b", "https://github.com/Vinix24/vnx-orchestration.git",
        install_mode="central",
    )
    _register(home, {"project-b": install})

    _write_and_publish(_results_dir(home, "project-b"))

    assert posts == []


def test_a_checkout_without_a_github_origin_is_refused(
    tmp_path: Path, home: Path, posts: List[Any]
) -> None:
    local = _make_checkout(tmp_path / "b", "project-b", str(tmp_path / "somewhere"))
    _register(home, {"project-b": local})

    _write_and_publish(_results_dir(home, "project-b"))

    assert posts == []


# ---------------------------------------------------------------------------
# A4: no silent default in the primitive
# ---------------------------------------------------------------------------


def test_publish_check_run_without_a_project_root_raises_before_any_call(
    posts: List[Any]
) -> None:
    with pytest.raises(forge_check_run.ForgeCheckRunError):
        forge_check_run.publish_check_run(HEAD, "vnx-gate/glm_gate", "success", "s")

    assert posts == []


# ---------------------------------------------------------------------------
# A5: one repo for every gh call in the chain
# ---------------------------------------------------------------------------


@pytest.fixture
def gh_calls(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """Record every ``gh`` argv; answer like gh would. Everything else runs for real."""
    calls: List[List[str]] = []

    def fake_run(argv: Any, **kwargs: Any) -> Any:
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "gh":
            calls.append(list(argv))
            field = argv[argv.index("--json") + 1]
            body = {"headRefOid": HEAD, "headRefName": "dispatch/x", "autoMergeRequest": None}
            return subprocess.CompletedProcess(
                list(argv), 0, stdout=json.dumps({field: body[field]}), stderr=""
            )
        return _REAL_RUN(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(fgp.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(gate_recorder.shutil, "which", lambda _n: "/usr/bin/gh")
    return calls


def test_the_auto_merge_lookup_and_the_cli_head_lookup_pin_the_project_repo(
    tmp_path: Path, home: Path, posts: List[Any], gh_calls: List[List[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_b = _make_checkout(tmp_path / "b", "project-b", "https://github.com/acme/project-b.git")
    _register(home, {"project-b": project_b})
    # The fixture stubbed the auto-merge lookup; this test wants the real one.
    monkeypatch.setattr(fgp, "auto_merge_is_armed", _REAL_AUTO_MERGE)
    results = _results_dir(home, "project-b")
    _write_and_publish(results)
    del posts[:]
    del gh_calls[:]

    rc = fgp.main(["publish", "--pr", str(PR), "--gate", "glm_gate", "--results-dir", str(results)])
    assert rc == 0
    rc = fgp.main(["review", "--pr", str(PR), "--results-dir", str(results)])
    assert rc == 0

    assert gh_calls, "er is geen enkele gh-aanroep vastgelegd"
    for argv in gh_calls:
        assert "--repo" in argv, f"gh zonder --repo leest de repo van de cwd: {argv}"
        assert argv[argv.index("--repo") + 1] == "acme/project-b", argv
    assert _urls(posts) == ["https://api.github.com/repos/acme/project-b/check-runs"] * 2


# ---------------------------------------------------------------------------
# A6: the fallback is gone
# ---------------------------------------------------------------------------


def test_the_repo_root_fallback_is_gone_from_the_primitive() -> None:
    source = (VNX_ROOT / "scripts" / "lib" / "forge_check_run.py").read_text(encoding="utf-8")

    assert "else REPO_ROOT" not in source
