"""ADR-079 category fixtures and real Git snapshot/CLI failure tests."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/check_docs_layers.py"
FIXTURES = Path(__file__).parent / "fixtures/docs_layers"
spec = importlib.util.spec_from_file_location("check_docs_layers", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _git(root, *args):
    return subprocess.check_output([
        "git", "-C", str(root), "-c", "user.name=Docs Tests",
        "-c", "user.email=docs@example.test", "-c", "commit.gpgsign=false", *args,
    ], text=True).strip()


def _commit(root):
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Documentation snapshot", "--allow-empty")
    return _git(root, "rev-parse", "HEAD")


def _repo(root, files):
    _git(root, "init", "-q")
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
    _commit(root)
    return root


def _run(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=root,
                          capture_output=True, text=True)


def test_historical_fixture_categories_and_counts():
    rows = [line.split("\t") for line in (FIXTURES / "a052c0e38.tsv").read_text().splitlines()]
    assert len(rows) == len({path for path, _ in rows}) == 100
    measured = Counter()
    for path, expected in rows:
        if expected in ("Ingang", "Verwijsstub"):
            name = "entrance.txt" if expected == "Ingang" else "redirect.txt"
            actual = mod.classify_document(path, (FIXTURES / name).read_text())
        else:
            actual = mod.path_category(path)
        assert actual == expected, path
        measured[actual] += 1
    assert measured == Counter({"M-achtergrond": 44, "R": 35, "Verhaal": 9,
                               "Ingang": 6, "Verwijsstub": 2, "C": 2, "A": 1,
                               "M-index": 1, "F": 0, "no category": 0})


@pytest.mark.parametrize("path", ["README.md", "CONTRIBUTING.md", "docs/DOCS_INDEX.md",
    "docs/README.md", "docs/operations/README.md", "docs/applications/README.md",
    "docs/features/README.md"])
def test_closed_entrance_list_precedes_stub_and_patterns(path):
    content = (FIXTURES / "entrance.txt").read_text()
    assert mod.is_redirect_stub(content)
    assert mod.classify_document(path, content) == "Ingang"


def test_stub_precedes_core_pattern():
    content = (FIXTURES / "redirect.txt").read_text()
    assert mod.classify_document("docs/core/moved.md", content) == "Verwijsstub"
    assert mod.classify_document("docs/unassigned.md", content) == "Verwijsstub"


@pytest.mark.parametrize("content", ["", "[Moved](../old.md)", "## Title\n[Go](x.md)",
    "# One\n# Two\n[Go](x.md)", "# One\n## Two\n[Go](x.md)",
    "# Title\n[Go](x.md)\na\nb\nc\nd", "# Title\n[Go]()", "# Title\n[Go](broken",
    "# Title\n![Image](x.md)", "# Title\n```\n[Go](x.md)\n```",
    *[f"# Title\n[Go]({target})" for target in
      ("https://example.test", "mailto:docs@example.test", "custom:page", "//host/x", "/x.md", "#part")]])
def test_incomplete_or_nonrelative_stub_is_not_a_category(content):
    assert not mod.is_redirect_stub(content)
    assert mod.classify_document("docs/unassigned.md", content) == "no category"


@pytest.mark.parametrize("target", ["x.md", "./x.md", "../x.md#part", '<../x.md> "Moved"'])
def test_stub_five_nonempty_lines_and_relative_links(target):
    content = f"\n# Title\n\n[Go]({target})\n\na\nb\nc\n"
    assert mod.is_redirect_stub(content)


@pytest.mark.parametrize("path,expected", [
    ("docs/core/technical/X.md", "M-achtergrond"), ("docs/core/X.md", "M-achtergrond"),
    ("docs/core/00_VNX_ARCHITECTURE.md", "A"), ("docs/core/00_GETTING_STARTED.md", "R"),
    ("docs/core/SUBSYSTEMS.md", "M-index"), ("docs/modules/X.md", "M-index"),
    ("docs/features/X.md", "F"), ("docs/features/nested/X.md", "no category"),
    ("docs/unassigned/README.md", "no category"), ("docs/CORE/X.md", "no category"),
])
def test_ordered_rules_are_case_sensitive_and_segment_limited(path, expected):
    assert mod.classify_document(path, "# Title\n") == expected
    assert mod.matches_pattern("docs/core/technical/X.md", "docs/core/*.md") is False


def test_cli_exact_scanset_and_unassigned_finding(tmp_path):
    included = ["README.md", "CONTRIBUTING.md", "docs/core/X.md", "docs/new.md",
                "docs/_archived/X.md", "docs/governance/decisions-extra/X.md"]
    excluded = ["CHANGELOG.md", "other/docs/X.md", "docs/core/X.MD", "docs/X.txt",
                *[f"docs/{folder}/X.md" for folder in
                  ("_archive", "archive", "investigations", "compliance", "governance/decisions")]]
    _repo(tmp_path, dict.fromkeys(included + excluded, "# Title\n"))
    (tmp_path / "docs/untracked.md").write_text("# Title\n")
    for args, status in [((), 1), (("--report",), 0)]:
        result = _run(tmp_path, *args)
        assert result.returncode == status, result.stderr
        assert [line.split(": ")[0] for line in result.stdout.splitlines()] == sorted(included)
        assert "docs/new.md: no category\n" in result.stdout


def test_sha_controls_paths_and_content_even_with_dirty_worktree(tmp_path):
    _repo(tmp_path, {"docs/core/X.md": "# Title\n"})
    before = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "docs/core/X.md").write_text((FIXTURES / "redirect.txt").read_text())
    (tmp_path / "README.md").write_text("# Index\n")
    _commit(tmp_path)
    (tmp_path / "docs/core/X.md").unlink()
    old = _run(tmp_path, "--sha", before, "--root", str(tmp_path))
    current = _run(tmp_path)
    assert old.returncode == current.returncode == 0
    assert old.stdout == "docs/core/X.md: M-achtergrond\n"
    assert current.stdout == "README.md: Ingang\ndocs/core/X.md: Verwijsstub\n"


@pytest.mark.parametrize("report", [(), ("--report",)])
@pytest.mark.parametrize("failure", ["invalid_utf8", "missing_blob", "corrupt_blob"])
def test_unreadable_document_never_passes(tmp_path, report, failure):
    _repo(tmp_path, {"README.md": b"\xff" if failure == "invalid_utf8" else b"# Index\n"})
    if failure != "invalid_utf8":
        oid = _git(tmp_path, "rev-parse", "HEAD:README.md")
        blob = tmp_path / ".git/objects" / oid[:2] / oid[2:]
        if failure == "missing_blob":
            blob.unlink()
        else:
            blob.chmod(0o600)
            blob.write_bytes(b"corrupt")
    result = _run(tmp_path, *report)
    assert result.returncode == 2
    assert "ERROR:" in result.stderr and "README.md" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("sha", ["", "unknown-ref", "HEAD:README.md", "--help", "HEAD..HEAD", "../HEAD", "/HEAD"])
def test_invalid_revision_is_an_error_even_in_report_mode(tmp_path, sha):
    _repo(tmp_path, {"README.md": "# Index\n"})
    result = _run(tmp_path, "--report", "--sha=" + sha)
    assert result.returncode == 2 and "ERROR:" in result.stderr


@pytest.mark.parametrize("files", [None, {}, {"source.py": "x = 1\n"}])
def test_missing_repository_or_empty_scanset_is_an_error(tmp_path, files):
    if files is not None:
        _repo(tmp_path, files)
    result = _run(tmp_path, "--root", str(tmp_path), "--report")
    assert result.returncode == 2 and "ERROR:" in result.stderr


def test_missing_git_is_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(mod.CheckError, match="git.*failed"):
        mod.run_check(tmp_path)


@pytest.mark.parametrize("report", [(), ("--report",)])
def test_tracked_symlink_is_refused(tmp_path, report):
    _repo(tmp_path, {"source.md": "# Source\n"})
    (tmp_path / "README.md").symlink_to("source.md")
    _commit(tmp_path)
    result = _run(tmp_path, *report)
    assert result.returncode == 2
    assert "README.md: not a regular Git blob" in result.stderr


def test_root_resolution_failure_is_visible(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_CANONICAL_ROOT", raising=False)
    result = _run(tmp_path, "--report")
    assert result.returncode == 2 and "Cannot resolve project root" in result.stderr
