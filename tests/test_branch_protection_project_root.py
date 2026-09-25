#!/usr/bin/env python3
"""``apply_branch_protection.py`` and ``vnx doctor`` use the same project root and
the same reading order as the merge door (OI-1849).

Both used to work from the install: the doctor read the FABRIC's YAML and asked
live state of whatever repo the install's git remote named, and
``apply_branch_protection.py`` defaulted to the fabric's YAML and the install as
the project, so a bare run from a central install would have applied
vnx-orchestration's protection to the repo the install's remote points at. Here the
project is a consumer checkout and the install is somewhere else entirely.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "forge"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_branch_protection as abp
import forge_protection_drift as fpd
import vnx_doctor
from merge_target_helpers import (
    CONSUMER_REPO,
    FABRIC_ORIGIN,
    consumer_yaml,
    git_repo,
    install_gh_stub,
    isolate_project_env,
    make_consumer,
    make_install,
)


def _project_yaml(project: Path, text: str, relative: str = ".vnx/branch_protection.yaml") -> Path:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _live_matching(text: str) -> Dict[str, Any]:
    return fpd.to_normalized_dict(fpd.parse_protection_config(text))


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A consumer checkout as the resolved project; the install is elsewhere and is what
    runs (``VNX_HOME`` and ``ENGINE_ROOT`` agree, as they do for a real install run)."""
    install = make_install(tmp_path)
    consumer = make_consumer(tmp_path)
    isolate_project_env(monkeypatch, install)
    monkeypatch.setattr(abp, "ENGINE_ROOT", install, raising=False)
    monkeypatch.chdir(consumer)
    return consumer


class TestApplyDefaults:
    def test_a_bare_dry_run_reads_the_projects_yaml_from_the_projects_root(self, project, monkeypatch, capsys):
        _project_yaml(project, consumer_yaml())
        seen: List[Path] = []

        def fake_live(project_root, **kwargs):
            seen.append(Path(project_root))
            return _live_matching(consumer_yaml())

        monkeypatch.setattr(abp, "fetch_live_protection", fake_live)

        rc = abp.main(["--dry-run"])

        assert rc == 0
        assert seen == [project]
        payload = json.loads(capsys.readouterr().out)
        assert [c["context"] for c in payload["required_status_checks"]["checks"]] == ["CI"]

    def test_the_fabric_style_path_is_the_fallback(self, project, monkeypatch, capsys):
        _project_yaml(project, consumer_yaml(), "scripts/forge/branch_protection.yaml")
        monkeypatch.setattr(abp, "fetch_live_protection", lambda *a, **k: _live_matching(consumer_yaml()))

        rc = abp.main(["--dry-run"])

        assert rc == 0
        assert [c["context"] for c in json.loads(capsys.readouterr().out)["required_status_checks"]["checks"]] == ["CI"]

    def test_a_project_without_a_file_is_refused_naming_where_it_looked(self, project, monkeypatch, capsys):
        called: List[Any] = []
        monkeypatch.setattr(abp, "run_apply", lambda **k: called.append(k))

        rc = abp.main(["--dry-run"])

        err = capsys.readouterr().err
        assert rc == 1
        assert called == []
        assert ".vnx/branch_protection.yaml" in err and "scripts/forge/branch_protection.yaml" in err

    def test_the_explicit_arguments_still_win(self, project, tmp_path, monkeypatch, capsys):
        other = _project_yaml(tmp_path / "elsewhere", consumer_yaml(enforcement="warn"), "custom.yaml")
        _project_yaml(project, consumer_yaml())
        seen: List[Dict[str, Any]] = []

        def fake_run_apply(**kwargs):
            seen.append(kwargs)
            return {"verdict": "DRY-RUN", "payload": {}, "weak_fields": []}

        monkeypatch.setattr(abp, "run_apply", fake_run_apply)

        rc = abp.main(["--dry-run", "--yaml-path", str(other), "--project-root", str(tmp_path / "elsewhere")])

        assert rc == 0
        assert seen[0]["yaml_path"] == other
        assert seen[0]["project_root"] == tmp_path / "elsewhere"


class TestApplyRefusesTheInstallAsTarget:
    """The merge door refuses a central install as its own target (``resolve_merge_target``).
    Without the same guard here, a run from inside an install resolves the project root to
    the install and applies the install's YAML to the repo its remote names (review
    finding I2 on #1915)."""

    @pytest.fixture()
    def inside_the_install(self, tmp_path, monkeypatch):
        install = make_install(tmp_path)
        _project_yaml(install, consumer_yaml(), "scripts/forge/branch_protection.yaml")
        isolate_project_env(monkeypatch, install)
        monkeypatch.chdir(install)
        monkeypatch.setattr(abp, "ENGINE_ROOT", install, raising=False)
        install_gh_stub(tmp_path, monkeypatch, [{"match": "repo view", "stdout": CONSUMER_REPO + "\n"}])
        return install

    @pytest.fixture()
    def applied(self, monkeypatch) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []

        def fake_run_apply(**kwargs):
            calls.append(kwargs)
            return {"verdict": "DRY-RUN", "payload": {}, "weak_fields": []}

        monkeypatch.setattr(abp, "run_apply", fake_run_apply)
        return calls

    @pytest.mark.parametrize("argv", [[], ["--dry-run"]])
    def test_a_run_from_inside_a_central_install_is_refused(self, inside_the_install, applied, capsys, argv):
        rc = abp.main(argv)

        assert rc == 1
        assert "installatie zelf" in capsys.readouterr().err
        assert applied == []

    def test_naming_the_install_as_the_project_root_is_refused_too(self, inside_the_install, applied, capsys):
        rc = abp.main(["--dry-run", "--project-root", str(inside_the_install)])

        assert rc == 1
        assert "installatie zelf" in capsys.readouterr().err
        assert applied == []

    def test_a_project_root_outside_the_install_is_still_allowed(self, inside_the_install, applied, tmp_path):
        consumer = make_consumer(tmp_path)
        _project_yaml(consumer, consumer_yaml())

        rc = abp.main(["--dry-run", "--project-root", str(consumer)])

        assert rc == 0
        assert [call["project_root"] for call in applied] == [consumer]

    def test_a_vnx_home_that_is_not_the_running_script_is_refused(
        self, inside_the_install, applied, tmp_path, monkeypatch, capsys,
    ):
        """Run from a project with ``VNX_HOME`` left on another checkout, the resolver lands on
        that checkout and would apply its YAML to its repo (review W2 on #1916)."""
        consumer = make_consumer(tmp_path)
        _project_yaml(consumer, consumer_yaml())
        other = git_repo(tmp_path / "fabric-checkout", FABRIC_ORIGIN)
        _project_yaml(other, consumer_yaml(), "scripts/forge/branch_protection.yaml")
        monkeypatch.setenv("VNX_HOME", str(other))
        monkeypatch.chdir(consumer)

        rc = abp.main(["--dry-run"])

        assert rc == 1
        assert "VNX_HOME" in capsys.readouterr().err
        assert applied == []

    def test_a_fabric_checkout_is_its_own_project(self, tmp_path, monkeypatch, applied):
        """No install marker: the checkout the script runs from is the project."""
        checkout = make_install(tmp_path, central=False)
        _project_yaml(checkout, consumer_yaml(), "scripts/forge/branch_protection.yaml")
        isolate_project_env(monkeypatch, checkout)
        monkeypatch.chdir(checkout)
        monkeypatch.setattr(abp, "ENGINE_ROOT", checkout, raising=False)

        rc = abp.main(["--dry-run"])

        assert rc == 0
        assert [call["project_root"] for call in applied] == [checkout]


class TestApplyNamesTheRepoItWillChange:
    def test_a_real_apply_states_the_target_repo_before_writing(self, project, tmp_path, monkeypatch, capsys):
        _project_yaml(project, consumer_yaml())
        install_gh_stub(tmp_path, monkeypatch, [{"match": "repo view", "stdout": CONSUMER_REPO + "\n"}])
        monkeypatch.setattr(abp, "run_apply", lambda **k: {"verdict": "OK", "message": "nul wijzigingen"})

        rc = abp.main([])

        assert rc == 0
        assert f"doelrepo: {CONSUMER_REPO}" in capsys.readouterr().err

    def test_a_repo_that_cannot_be_named_is_not_written_to(self, project, tmp_path, monkeypatch, capsys):
        _project_yaml(project, consumer_yaml())
        install_gh_stub(tmp_path, monkeypatch, [{"match": "repo view", "rc": 1, "stderr": "HTTP 401\n"}])
        called: List[Any] = []
        monkeypatch.setattr(abp, "run_apply", lambda **k: called.append(k))

        rc = abp.main([])

        assert rc == 1
        assert called == []
        assert "doelrepo kon niet worden bepaald" in capsys.readouterr().err


class TestDoctorFollowsTheProject:
    def _paths(self, project: Path) -> Dict[str, str]:
        # The doctor runs from the install; the project is somewhere else.
        return {"VNX_HOME": str(project.parent / "install" / "v1.6.3"), "PROJECT_ROOT": str(project)}

    def test_the_projects_yaml_and_the_projects_repo_are_checked(self, project, monkeypatch):
        _project_yaml(project, consumer_yaml())
        seen: List[Path] = []

        def fake_live(project_root, **kwargs):
            seen.append(Path(project_root))
            return _live_matching(consumer_yaml())

        monkeypatch.setattr(fpd, "fetch_live_protection", fake_live)

        results = vnx_doctor.check_branch_protection_drift(self._paths(project))

        assert results[0].status == vnx_doctor.PASS
        assert seen == [project]

    def test_enforce_drift_is_a_fail(self, project, monkeypatch):
        _project_yaml(project, consumer_yaml(enforcement="enforce"))
        drifted = _live_matching(consumer_yaml())
        drifted["allow_force_pushes"] = True
        monkeypatch.setattr(fpd, "fetch_live_protection", lambda *a, **k: drifted)

        results = vnx_doctor.check_branch_protection_drift(self._paths(project))

        assert results[0].status == vnx_doctor.FAIL
        assert results[0].details == ["allow_force_pushes"]

    def test_warn_drift_is_a_warn_because_the_door_would_let_it_through(self, project, monkeypatch):
        _project_yaml(project, consumer_yaml(enforcement="warn"))
        drifted = _live_matching(consumer_yaml())
        drifted["allow_force_pushes"] = True
        monkeypatch.setattr(fpd, "fetch_live_protection", lambda *a, **k: drifted)

        results = vnx_doctor.check_branch_protection_drift(self._paths(project))

        assert results[0].status == vnx_doctor.WARN
        assert results[0].details == ["allow_force_pushes"]

    def test_off_looks_at_nothing_and_says_so(self, project, monkeypatch):
        _project_yaml(project, consumer_yaml(enforcement="off"))
        calls: List[Any] = []
        monkeypatch.setattr(fpd, "fetch_live_protection", lambda *a, **k: calls.append(1) or {})

        results = vnx_doctor.check_branch_protection_drift(self._paths(project))

        assert results[0].status == vnx_doctor.PASS
        assert "staat uit" in results[0].message
        assert calls == []

    def test_no_file_is_a_warn_naming_both_places_it_looked(self, project):
        results = vnx_doctor.check_branch_protection_drift(self._paths(project))

        assert results[0].status == vnx_doctor.WARN
        assert ".vnx/branch_protection.yaml" in results[0].message
        assert "scripts/forge/branch_protection.yaml" in results[0].message

    def test_the_fabrics_own_yaml_in_the_install_is_not_the_projects(self, project, monkeypatch):
        """The doctor used to read ``VNX_HOME``'s YAML. Put one there and none in the project:
        the project has no file, whatever the install carries."""
        install = project.parent / "install" / "v1.6.3"
        _project_yaml(install, consumer_yaml(), "scripts/forge/branch_protection.yaml")
        monkeypatch.setattr(fpd, "fetch_live_protection", lambda *a, **k: pytest.fail("looked at live state"))

        results = vnx_doctor.check_branch_protection_drift(self._paths(project))

        assert results[0].status == vnx_doctor.WARN
        assert "ontbreekt" in results[0].message
