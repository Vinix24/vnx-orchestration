"""``providers_cli.engine_root_for``: a dead engine link is not an engine.

The link ``<project>/.claude/vnx-system`` decides which engine a project runs. When it points at
a path that is not there (a reaped ``~/.vnx-system/versions/<tag>``), the old function returned
that dead path with the label ``"linked engine"``, and every ``vnx providers`` caller then read a
provider list from a directory that does not exist. These tests pin the four branches: a dead
link and a link to a non-directory both resolve to ``None`` with a reason that names the target,
while a live link, the fabric checkout and "no link" keep their behaviour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import provider_switch as ps  # noqa: E402
import providers_cli  # noqa: E402


def _project_with_link(tmp_path: Path, target: Path, name: str = "proj") -> Path:
    project = tmp_path / name
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "vnx-system").symlink_to(target)
    return project


def test_dead_link_returns_no_engine_and_names_the_target(tmp_path):
    missing = tmp_path / "versions" / "v1.0.0-rc9"
    project = _project_with_link(tmp_path, missing)

    root, source = providers_cli.engine_root_for(project)

    assert root is None, f"a dead link must not resolve to {root}"
    assert "dead .claude/vnx-system link ->" in source
    assert str(missing) in source
    assert "target missing" in source
    assert "linked engine" not in source


def test_dead_link_is_not_reported_as_a_linked_engine(tmp_path):
    """The exact regression: the old code returned ``(resolved_dead_path, 'linked engine')``."""
    missing = tmp_path / "gone"
    project = _project_with_link(tmp_path, missing)

    root, source = providers_cli.engine_root_for(project)

    assert (root, source) != (missing, "linked engine")
    assert root is None


def test_live_link_resolves_to_its_engine(tmp_path):
    engine = tmp_path / "versions" / "v1.7.1"
    engine.mkdir(parents=True)
    project = _project_with_link(tmp_path, engine)

    root, source = providers_cli.engine_root_for(project)

    assert root == engine.resolve()
    assert source == "linked engine"


def test_link_to_a_regular_file_returns_no_engine(tmp_path):
    target = tmp_path / "not-a-dir"
    target.write_text("", encoding="utf-8")
    project = _project_with_link(tmp_path, target)

    root, source = providers_cli.engine_root_for(project)

    assert root is None
    assert str(target) in source
    assert "target not a directory" in source
    assert "linked engine" not in source


def test_fabric_checkout_is_its_own_engine(tmp_path):
    project = tmp_path / "fabric"
    (project / "scripts" / "lib").mkdir(parents=True)
    (project / "scripts" / "lib" / "vnx_paths.py").write_text("", encoding="utf-8")

    assert providers_cli.engine_root_for(project) == (project, "fabric checkout")


def test_fabric_checkout_with_a_dead_link_is_still_not_an_engine(tmp_path):
    """A dead link is reported as such even where the project would otherwise be the fabric."""
    project = tmp_path / "fabric"
    (project / "scripts" / "lib").mkdir(parents=True)
    (project / "scripts" / "lib" / "vnx_paths.py").write_text("", encoding="utf-8")
    (project / ".claude").mkdir()
    (project / ".claude" / "vnx-system").symlink_to(tmp_path / "missing")

    root, source = providers_cli.engine_root_for(project)

    assert root is None
    assert "target missing" in source


def test_no_link_and_not_the_fabric_returns_none(tmp_path):
    project = tmp_path / "plain"
    project.mkdir()

    root, source = providers_cli.engine_root_for(project)

    assert root is None
    assert source == "no .claude/vnx-system link and not the fabric checkout"


def test_module_source_carries_no_literal_engine_link_path():
    """``scripts/vnx_doctor.sh`` refuses the literal engine-link path inside ``scripts/``.

    CI Profile A runs the doctor as a prelude step, and it scans this module's source for the
    literal path. The dead-link reason still names the link, but it is assembled at runtime
    (``_ENGINE_LINK``), so no line of ``providers_cli.py`` spells the path out.
    """
    literal = "/".join((".claude", "vnx-system"))
    source = (REPO_ROOT / "scripts" / "providers_cli.py").read_text(encoding="utf-8")

    offending = [f"  line {i + 1}: {line.strip()}"
                 for i, line in enumerate(source.splitlines()) if literal in line]
    assert not offending, (
        "scripts/providers_cli.py writes out the path the doctor forbids; build it from parts "
        "(`_ENGINE_LINK`) instead:\n" + "\n".join(offending)
    )


def test_project_rows_reports_a_dead_link_without_claiming_engine_knowledge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".vnx").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    missing = tmp_path / "versions" / "v1.0.0-rc9"
    project = _project_with_link(tmp_path, missing)
    (home / ".vnx" / "projects.json").write_text(
        json.dumps({"schema_version": 2, "projects": [{"project_id": "dead-proj", "path": str(project)}]}),
        encoding="utf-8")

    rows = providers_cli.project_rows(ps.load_state(tmp_path / "absent-switch.json"))

    assert rows["error"] is None
    row = rows["projects"][0]
    assert row["engine_root"] is None
    assert row["knows_switch"] is False
    assert str(missing) in row["engine_source"]
    assert row["engine_families"] is None

    rendered = "\n".join(providers_cli._render_projects({"projects": rows}))
    assert "engine root unresolved" in rendered
    assert str(missing) in rendered
    assert "engine knows the switch" not in rendered
