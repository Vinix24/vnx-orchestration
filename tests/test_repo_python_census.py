"""tests/test_repo_python_census.py — test-suite-kosten TSK-3: the shared census
behind ``tests/conftest.py::repo_python_census``.

The three call-site contracts each scanned every git-tracked ``.py`` file and parsed
it themselves. These tests pin the census they now share: one parse per file per
census, a changed file re-read and re-parsed, and an unreadable or unparseable file
reported as an entry instead of silently dropped.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from conftest import CensusEntry, repo_python_census


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True)


def _tracked_repo(tmp_path: Path, files: dict[str, str]) -> None:
    _git(tmp_path, "init", "-q")
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "--", *files)


def _count_parses(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Wrap ``ast.parse`` so a test can count (and name) every real parse."""
    real_parse = ast.parse

    def counting_parse(source, *args, **kwargs):
        calls.append(Path(str(kwargs.get("filename"))).name)
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting_parse)


def test_three_scans_of_one_census_parse_each_file_once(tmp_path, monkeypatch):
    _tracked_repo(tmp_path, {name: f"VALUE = {name!r}\n" for name in ("a.py", "b.py", "c.py")})
    census = repo_python_census(tmp_path)
    parses: list[str] = []
    _count_parses(monkeypatch, parses)

    for _ in range(3):
        entries = census.entries()
        assert sorted(entries) == ["a.py", "b.py", "c.py"]
        assert all(entry.tree is not None for entry in entries.values())

    assert sorted(parses) == ["a.py", "b.py", "c.py"]


def test_a_file_that_changed_between_two_calls_is_parsed_again(tmp_path, monkeypatch):
    _tracked_repo(tmp_path, {"target.py": "VALUE = 1\n"})
    census = repo_python_census(tmp_path)
    parses: list[str] = []
    _count_parses(monkeypatch, parses)

    first = census.entries()["target.py"].tree
    assert first.body[0].value.value == 1

    (tmp_path / "target.py").write_text("VALUE = 2  # rewritten, so size and mtime move\n")
    second = census.entries()["target.py"].tree

    assert second is not first
    assert second.body[0].value.value == 2
    assert parses == ["target.py", "target.py"]


def test_a_file_with_a_syntax_error_is_reported_not_dropped(tmp_path):
    _tracked_repo(tmp_path, {"broken.py": "def broken(:\n    pass\n"})

    entries = repo_python_census(tmp_path).entries()

    assert "broken.py" in entries
    assert isinstance(entries["broken.py"].error, SyntaxError)
    with pytest.raises(SyntaxError):
        entries["broken.py"].tree


def test_a_tracked_file_deleted_from_disk_is_reported_not_dropped(tmp_path):
    _tracked_repo(tmp_path, {"gone.py": "VALUE = 1\n"})
    (tmp_path / "gone.py").unlink()

    entries = repo_python_census(tmp_path).entries()

    assert "gone.py" in entries
    with pytest.raises(OSError):
        entries["gone.py"].tree


class _PlantedCensus:
    """Census standing in for the session fixture: exactly the entries a test planted."""

    def __init__(self, entries: dict[str, CensusEntry]) -> None:
        self._entries = entries

    def entries(self) -> dict[str, CensusEntry]:
        return dict(self._entries)


def test_the_scrub_env_keys_contract_fails_on_a_planted_call_site(tmp_path):
    from test_spawn_scrub_env_keys_contract import TestEveryCallSiteSuppliesScrubEnvKeys

    (tmp_path / "planted.py").write_text(
        "from provider_spawns.claude_spawn import spawn_claude\n"
        "spawn_claude(prompt=p, model=m, dispatch_id=d, terminal_id=t)\n",
        encoding="utf-8",
    )
    census = _PlantedCensus(
        {"planted.py": CensusEntry(path=tmp_path / "planted.py", rel="planted.py", key=None)}
    )
    watcher = TestEveryCallSiteSuppliesScrubEnvKeys()

    with pytest.raises(AssertionError, match="missing or None scrub_env_keys"):
        watcher.test_every_call_site_supplies_non_none_scrub_env_keys(census)
