"""tests/test_skills_consumer_contract.py — what the shipped skills promise a consumer.

Dispatch 20261021-skills-consumer-fixes (SEOcrawler #778 codex review):

* a skill or template never names the repo-relative ``.vnx-data/unified_reports/`` as the place
  a worker writes its report: a worker's cwd is an isolated worktree, so the file would land
  outside the audit trail. ``$VNX_DATA_DIR/unified_reports/`` is the only spelling.
* ``skills/skills.yaml`` registers every skill directory that ships, and every directory in
  ``.claude/skills`` is either registered or declared repo-local in the registry.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from vnx_skills import resolve_sync_set  # noqa: E402

SKILL_TREES = (ROOT / "skills", ROOT / ".claude" / "skills")
RELATIVE_REPORT_DIR = re.compile(r"(?<![\w$}/~])\.vnx-data/unified_reports")


def _skill_files() -> list[Path]:
    files: list[Path] = []
    for tree in SKILL_TREES:
        files.extend(p for p in sorted(tree.rglob("*")) if p.is_file() and p.suffix in {".md", ".sh", ".py", ".yaml"})
    return files


def _registry() -> dict:
    return yaml.safe_load((ROOT / "skills" / "skills.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_skill_file_names_the_relative_report_dir(path: Path) -> None:
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if RELATIVE_REPORT_DIR.search(line)
    ]
    assert not hits, f"{path.relative_to(ROOT)} names `.vnx-data/unified_reports` instead of `$VNX_DATA_DIR/unified_reports`: {hits}"


def test_every_shipped_template_names_the_data_dir_variable() -> None:
    templates = sorted((ROOT / "skills").glob("*/template.md"))
    assert templates
    for template in templates:
        if "unified_reports" in template.read_text(encoding="utf-8"):
            assert "$VNX_DATA_DIR/unified_reports/" in template.read_text(encoding="utf-8"), template


def test_shipped_directories_are_registered() -> None:
    registered = set(_registry()["skills"])
    shipped = set(resolve_sync_set(ROOT / "skills"))
    assert shipped == registered, (
        f"shipped but unregistered: {sorted(shipped - registered)}; "
        f"registered but not shipped: {sorted(registered - shipped)}"
    )


def test_every_registered_skill_names_an_existing_file() -> None:
    for key, entry in _registry()["skills"].items():
        assert (ROOT / "skills" / entry["file"]).is_file(), f"{key}: {entry['file']} missing"


def test_every_claude_skill_dir_is_registered_or_declared_repo_local() -> None:
    registry = _registry()
    registered = set(registry["skills"])
    repo_local = set(registry.get("repo_local") or [])
    present = {p.name for p in (ROOT / ".claude" / "skills").iterdir() if (p / "SKILL.md").is_file()}
    assert not (registered & repo_local), "a skill cannot be both shipped and repo-local"
    assert present <= registered | repo_local, (
        f"in .claude/skills but in neither `skills` nor `repo_local`: {sorted(present - registered - repo_local)}"
    )


def test_registry_header_explains_the_repo_local_split() -> None:
    header = (ROOT / "skills" / "skills.yaml").read_text(encoding="utf-8").split("version:", 1)[0]
    assert "repo_local" in header
