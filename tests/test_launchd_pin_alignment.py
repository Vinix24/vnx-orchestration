"""tests/test_launchd_pin_alignment.py — a job must run the release its project pins.

Measured 2026-10-25 (inventory ``20261025-inv-fleet-release`` §1.4): ZERO of the
six installed jobs of the three pinned projects ran its project's pin — three
followed ``~/.vnx-system/current`` and three a hardcoded ``versions/v1.7.1``
while mission-control pinned 1.7.5. ``check_installed_state`` only looks at
Labels, so the guard answered ``ok`` for exactly that state.

Every fixture lives under ``tmp_path``; no real plist, ``~/Library/LaunchAgents``,
``~/.vnx-system``, ``~/.claude`` or consumer repo is read or written, and no
``launchctl`` subprocess is spawned.
"""
from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_DIR = REPO_ROOT / "scripts" / "launchd"

sys.path.insert(0, str(LAUNCHD_DIR))
import launchd_project_scope as lps  # noqa: E402


def _store(base: Path) -> Path:
    store = base / ".vnx-system"
    for name in ("v1.7.5", "v1.7.6"):
        (store / "versions" / name).mkdir(parents=True, exist_ok=True)
    if not (store / "current").is_symlink():
        (store / "current").symlink_to(store / "versions" / "v1.7.6")
    return store


def _project(base: Path, project_id: str, pin: Optional[str]) -> Path:
    root = base / "projects" / project_id
    root.mkdir(parents=True, exist_ok=True)
    if pin is not None:
        (root / ".vnx-version").write_text(pin + "\n", encoding="utf-8")
    return root


def _plist(
    directory: Path,
    label: str,
    env: Optional[Dict[str, str]] = None,
    argv: Optional[Sequence[str]] = None,
) -> Path:
    argv = list(argv or ["/bin/bash", "-c", "echo hi"])
    payload: Dict[str, Any] = {"Label": label, "ProgramArguments": argv}
    if env is not None:
        payload["EnvironmentVariables"] = dict(env)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}.plist"
    with open(path, "wb") as handle:
        plistlib.dump(payload, handle)
    return path


def test_every_classification_is_reported_per_installed_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One fixture plist per class, the argv-shaped bare job measured live (no
    project suffix, ``VNX_HOME="$HOME/.vnx-system/current"`` in the bash body),
    plus the two unmeasurable shapes a pinned project must never pass."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = _store(tmp_path)
    projects = {
        "mission-control": _project(tmp_path, "mission-control", "1.7.5"),
        "sales-copilot": _project(tmp_path, "sales-copilot", "1.7.6"),
        "legacy-app": _project(tmp_path, "legacy-app", "1.6.0"),
    }
    plists = tmp_path / "LaunchAgents"
    _plist(plists, "com.vnx.ledger-health.mission-control",
           env={"VNX_HOME": str(store / "versions" / "v1.7.5"), "VNX_PROJECT_ID": "mission-control"})
    _plist(plists, "com.vnx.cleanup-reviewed-worktrees.mission-control",
           env={"VNX_HOME": str(store / "current"), "VNX_PROJECT_ID": "mission-control"})
    _plist(plists, "com.vnx.gate-obligation-runner",
           env={"VNX_PROJECT_ID": "mission-control"},
           argv=["/bin/bash", "-c", 'VNX_HOME="$HOME/.vnx-system/current" ; exec python3 x.py'])
    _plist(plists, "com.vnx.ledger-health.sales-copilot",
           env={"VNX_HOME": str(store / "versions" / "v1.7.5"), "VNX_PROJECT_ID": "sales-copilot"})
    _plist(plists, "com.vnx.ledger-health.legacy-app",
           env={"VNX_HOME": str(store / "versions" / "v1.6.0"), "VNX_PROJECT_ID": "legacy-app"})
    _plist(plists, "com.vnx.receipt-processor.mission-control", env={"VNX_PROJECT_ID": "mission-control"})
    (plists / "com.vnx.dashboard.mission-control.plist").write_text("not a plist", encoding="utf-8")

    result = lps.check_pin_alignment(plists, projects, versions_dir=store / "versions")

    assert result["ok"] is False
    found = {entry["label"]: entry["classification"] for entry in result["entries"]}
    assert found == {
        "com.vnx.ledger-health.mission-control": lps.PIN_FOLLOWED,
        "com.vnx.cleanup-reviewed-worktrees.mission-control": lps.PIN_IGNORED_CURRENT,
        "com.vnx.gate-obligation-runner": lps.PIN_IGNORED_CURRENT,
        "com.vnx.ledger-health.sales-copilot": lps.PIN_IGNORED_OTHER_VERSION,
        "com.vnx.ledger-health.legacy-app": lps.PIN_DEAD,
        "com.vnx.receipt-processor.mission-control": lps.PIN_IGNORED_OTHER_VERSION,
    }
    details = {v["label"]: v["detail"] for v in result["violations"]}
    assert "1.7.5" in details["com.vnx.cleanup-reviewed-worktrees.mission-control"]
    assert "v1.7.5" in details["com.vnx.ledger-health.sales-copilot"]
    assert "no usable release directory" in details["com.vnx.receipt-processor.mission-control"]
    assert details["com.vnx.dashboard.mission-control"].startswith("cannot read")


def test_jobs_outside_the_claim_are_unmeasured_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unregistered project's job, a dev checkout without a pin and a
    non-VNX agent are unmeasured/skipped — never ``pin_followed`` — and the
    call spawns nothing and changes nothing."""
    store = _store(tmp_path)
    dev = _project(tmp_path, "vnx-dev", None)
    plists = tmp_path / "LaunchAgents"
    _plist(plists, "com.vnx.ledger-health.stranger", env={"VNX_HOME": str(store / "current")})
    _plist(plists, "com.vnx.ledger-health.vnx-dev", env={"VNX_HOME": str(tmp_path / "dev-tree")})
    _plist(plists, "com.apple.something", env={"VNX_HOME": "/tmp"})
    before = {path.name: path.read_bytes() for path in plists.iterdir()}

    def _no_subprocess(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"check_pin_alignment spawned a process: {args!r}")

    monkeypatch.setattr(lps.subprocess, "run", _no_subprocess)
    result = lps.check_pin_alignment(plists, {"vnx-dev": dev}, versions_dir=store / "versions")

    assert result["ok"] is True
    assert result["entries"] == []
    reasons = {u["label"]: u["reason"] for u in result["unmeasured"]}
    assert reasons["com.vnx.ledger-health.stranger"] == "label names no registered project"
    assert "no .vnx-version pin" in reasons["com.vnx.ledger-health.vnx-dev"]
    assert "com.apple.something" not in reasons
    assert {path.name: path.read_bytes() for path in plists.iterdir()} == before


def test_main_json_names_a_job_that_ignores_its_project_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The red-run case: a job with ``VNX_HOME=<current>`` at pin 1.7.5. The old
    guard (Labels only) answered ``ok``/exit 0; this asserts it names the job."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VNX_HOME_ROOT", raising=False)
    store = _store(home)
    project = _project(home, "mission-control", "1.7.5")
    (home / ".vnx").mkdir(parents=True)
    (home / ".vnx" / "projects.json").write_text(
        '{"projects": [{"project_id": "mission-control", "path": "%s"}]}' % project, encoding="utf-8"
    )
    _plist(home / "Library" / "LaunchAgents", "com.vnx.gate-obligation-runner",
           env={"VNX_PROJECT_ID": "mission-control"},
           argv=["/bin/bash", "-c", 'VNX_HOME="%s" ; exec python3 x.py' % (store / "current")])
    monkeypatch.setattr(lps.sys, "platform", "darwin")
    monkeypatch.setattr(
        lps, "_run_real_launchctl_list",
        lambda: "\n".join(
            ["PID\tStatus\tLabel"]
            + [f"-\t0\t{family}.mission-control" for family in lps.REQUIRED_PER_PROJECT_FAMILIES]
        ),
    )

    rc = lps.main(["--project-id", "mission-control", "--templates-dir", str(LAUNCHD_DIR), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    classes = [entry["classification"] for entry in payload["pin_alignment"]["entries"]]
    assert lps.PIN_IGNORED_CURRENT in classes
    assert lps.PIN_IGNORED_CURRENT in {v["kind"] for v in payload["violations"]}


def test_a_home_that_does_not_exist_is_never_pin_followed(tmp_path: Path) -> None:
    """A basename match is not a measurement: ``/<missing>/v1.7.5`` names the
    pinned release but is not on disk, so the job runs nothing of the sort."""
    store = _store(tmp_path)
    project = _project(tmp_path, "mission-control", "1.7.5")
    missing = tmp_path / "nonexistent" / "v1.7.5"
    plists = tmp_path / "LaunchAgents"
    _plist(plists, "com.vnx.ledger-health.mission-control",
           env={"VNX_HOME": str(missing), "VNX_PROJECT_ID": "mission-control"})

    result = lps.check_pin_alignment(plists, {"mission-control": project}, versions_dir=store / "versions")

    assert result["ok"] is False
    entry = result["entries"][0]
    assert entry["classification"] == lps.PIN_IGNORED_OTHER_VERSION
    assert str(missing) in entry["detail"]
    assert "no existing directory" in entry["detail"]
    assert [violation["kind"] for violation in result["violations"]] == [lps.PIN_IGNORED_OTHER_VERSION]


def test_a_same_named_path_that_resolves_elsewhere_is_never_pin_followed(tmp_path: Path) -> None:
    """``v1.7.5`` is only the pinned release when it RESOLVES there: a symlink
    into ``v1.7.4`` and a plain dir of that name elsewhere are both reported,
    with the directory the job would actually run named in the detail."""
    store = _store(tmp_path)
    (store / "versions" / "v1.7.4").mkdir()
    decoy_versions = tmp_path / "decoy-versions"
    decoy_versions.mkdir()
    link = decoy_versions / "v1.7.5"
    link.symlink_to(store / "versions" / "v1.7.4")
    elsewhere = tmp_path / "elsewhere" / "v1.7.5"
    elsewhere.mkdir(parents=True)
    project = _project(tmp_path, "mission-control", "1.7.5")
    plists = tmp_path / "LaunchAgents"
    _plist(plists, "com.vnx.ledger-health.mission-control",
           env={"VNX_HOME": str(link), "VNX_PROJECT_ID": "mission-control"})
    _plist(plists, "com.vnx.gate-obligation-runner",
           env={"VNX_HOME": str(elsewhere), "VNX_PROJECT_ID": "mission-control"})

    result = lps.check_pin_alignment(plists, {"mission-control": project}, versions_dir=store / "versions")

    assert result["ok"] is False
    details = {entry["label"]: entry["detail"] for entry in result["entries"]}
    assert {entry["label"]: entry["classification"] for entry in result["entries"]} == {
        "com.vnx.ledger-health.mission-control": lps.PIN_IGNORED_OTHER_VERSION,
        "com.vnx.gate-obligation-runner": lps.PIN_IGNORED_OTHER_VERSION,
    }
    assert str((store / "versions" / "v1.7.4").resolve()) in details["com.vnx.ledger-health.mission-control"]
    assert str(elsewhere.resolve()) in details["com.vnx.gate-obligation-runner"]


def test_the_installed_pinned_dir_follows_while_current_to_it_does_not(tmp_path: Path) -> None:
    """The positive case is a real installed dir (or a link to it), and the
    ``current`` rejection is decided on the name as written: a ``current``
    link pointing AT the pinned release is still ``pin_ignored_current``."""
    store = _store(tmp_path)
    (store / "current").unlink()
    (store / "current").symlink_to(store / "versions" / "v1.7.5")
    project = _project(tmp_path, "mission-control", "1.7.5")
    plists = tmp_path / "LaunchAgents"
    link_to_pin = tmp_path / "pinned-link"
    link_to_pin.symlink_to(store / "versions" / "v1.7.5")
    _plist(plists, "com.vnx.ledger-health.mission-control",
           env={"VNX_HOME": str(store / "versions" / "v1.7.5"), "VNX_PROJECT_ID": "mission-control"})
    _plist(plists, "com.vnx.gate-obligation-runner",
           env={"VNX_HOME": str(link_to_pin), "VNX_PROJECT_ID": "mission-control"})
    _plist(plists, "com.vnx.cleanup-reviewed-worktrees.mission-control",
           env={"VNX_HOME": str(store / "current"), "VNX_PROJECT_ID": "mission-control"})

    result = lps.check_pin_alignment(plists, {"mission-control": project}, versions_dir=store / "versions")

    assert {entry["label"]: entry["classification"] for entry in result["entries"]} == {
        "com.vnx.ledger-health.mission-control": lps.PIN_FOLLOWED,
        "com.vnx.gate-obligation-runner": lps.PIN_FOLLOWED,
        "com.vnx.cleanup-reviewed-worktrees.mission-control": lps.PIN_IGNORED_CURRENT,
    }
    assert [violation["label"] for violation in result["violations"]] == [
        "com.vnx.cleanup-reviewed-worktrees.mission-control"
    ]
    assert result["ok"] is False
