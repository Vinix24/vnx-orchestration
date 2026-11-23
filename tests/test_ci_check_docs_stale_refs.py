"""Tests for scripts/ci/check_docs_stale_refs.py (rule 1: paths and links)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "check_docs_stale_refs.py"
_spec = importlib.util.spec_from_file_location("check_docs_stale_refs", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_docs_stale_refs"] = mod
_spec.loader.exec_module(mod)

BASE_FILES = {
    "scripts/lib/foo.py": "x = 1\n",
    "scripts/ci/unique_tool.py": "",
    "scripts/a/dup.py": "",
    "scripts/b/dup.py": "",
    "docs/core/other.md": "# other\n",
    "docs/core/sub/page.md": "# page\n",
    "docs/guide/g.md": "# g\n",
}


def _repo(tmp_path: Path, files: dict[str, str], untracked: dict[str, str] | None = None) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel, body in {**BASE_FILES, **files}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    for rel, body in (untracked or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def _findings(tmp_path, doc_body, rules=("paths", "links"), doc="docs/core/doc.md", extra=None, untracked=None):
    root = _repo(tmp_path, {doc: doc_body, **(extra or {})}, untracked)
    return [f for f in mod.run_check(root, set(rules)) if f.path == doc]


# ---- normalisation, one test per step ------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("scripts/lib/foo.py#anchor", "scripts/lib/foo.py"),
    ("scripts/lib/foo.py::Klass.method", "scripts/lib/foo.py"),
    ("scripts/lib/foo.py:12", "scripts/lib/foo.py"),
    ("scripts/lib/foo.py:12-20", "scripts/lib/foo.py"),
    ("scripts/lib/foo.py:12–20", "scripts/lib/foo.py"),
    ("scripts/lib/foo.py:12,15,20-30", "scripts/lib/foo.py"),
    ("scripts/lib/foo.py:some_name", "scripts/lib/foo.py"),
    ("scripts/lib/foo.py::sym#frag", "scripts/lib/foo.py"),
    ("scripts/lib/foo.py", "scripts/lib/foo.py"),
])
def test_normalise_token(raw, expected):
    assert mod.normalise_token(raw) == expected


def test_normalise_order_anchor_before_symbol():
    # '#' is cut first, so a '::' after the anchor never reaches the symbol step.
    assert mod.normalise_token("a/b.py#x::y") == "a/b.py"


def test_name_suffix_requires_identifier():
    # ':1x' is neither a line suffix nor an identifier suffix: left alone.
    assert mod.normalise_token("a/b.py:1x") == "a/b.py:1x"


def test_normalised_suffix_variants_resolve(tmp_path):
    body = "`scripts/lib/foo.py:3-4` `scripts/lib/foo.py::f` `scripts/lib/foo.py#a` `scripts/lib/foo.py:name`\n"
    assert _findings(tmp_path, body, rules=("paths",)) == []


# ---- schema id --------------------------------------------------------------

def test_schema_id_is_not_a_path(tmp_path):
    assert _findings(tmp_path, "`pr_queue/1.1` and `schemas/receipt/2`\n", rules=("paths",)) == []


def test_name_with_digits_and_letters_is_a_path(tmp_path):
    out = _findings(tmp_path, "`scripts/lib/v1.1x.py`\n", rules=("paths",))
    assert len(out) == 1


# ---- resolution, per step ---------------------------------------------------

def test_resolve_exact_file(tmp_path):
    assert _findings(tmp_path, "`scripts/lib/foo.py`\n", rules=("paths",)) == []


def test_resolve_exact_directory(tmp_path):
    assert _findings(tmp_path, "`docs/core/sub`\n", rules=("paths",)) == []


def test_resolve_relative_dot_slash(tmp_path):
    assert _findings(tmp_path, "`./other.md`\n", rules=("paths",)) == []


def test_resolve_relative_dotdot(tmp_path):
    assert _findings(tmp_path, "`../guide/g.md`\n", rules=("paths",)) == []


def test_relative_is_not_resolved_by_suffix(tmp_path):
    # 'foo.py' exists under scripts/lib, but ./foo.py is relative to docs/core.
    out = _findings(tmp_path, "`./foo.py`\n", rules=("paths",))
    assert len(out) == 1


def test_relative_escaping_root_is_unresolved(tmp_path):
    out = _findings(tmp_path, "`../../../etc/hosts.md`\n", rules=("paths",), doc="docs/core/doc.md")
    assert len(out) == 1


def test_resolve_unique_suffix(tmp_path):
    assert _findings(tmp_path, "`ci/unique_tool.py`\n", rules=("paths",)) == []


def test_unique_suffix_matches_a_directory(tmp_path):
    assert _findings(tmp_path, "`core/sub`\n", rules=("paths",)) == []


def test_ambiguous_suffix_is_unresolved(tmp_path):
    out = _findings(tmp_path, "`a/dup.py` `x/dup.py`\n", rules=("paths",))
    # 'a/dup.py' is a unique suffix of scripts/a/dup.py; 'x/dup.py' matches nothing.
    assert [f.text for f in out] == ["`x/dup.py` does not resolve to a tracked path"]


def test_two_suffix_matches_are_unresolved(tmp_path):
    extra = {"other/scripts/a/dup.py": ""}
    out = _findings(tmp_path, "`a/dup.py`\n", rules=("paths",), extra=extra)
    assert len(out) == 1


def test_untracked_file_is_unresolved(tmp_path):
    out = _findings(tmp_path, "`scripts/lib/local_only.py`\n", rules=("paths",),
                    untracked={"scripts/lib/local_only.py": ""})
    assert len(out) == 1


def test_missing_path_is_a_finding_with_position(tmp_path):
    out = _findings(tmp_path, "intro\n\ntext `scripts/gone.py` here\n", rules=("paths",))
    assert [(f.line, f.rule, f.generated) for f in out] == [(3, "paths", False)]
    assert out[0].render().startswith("docs/core/doc.md:3 paths ")


# ---- exceptions ---------------------------------------------------------------

@pytest.mark.parametrize("token", [
    "claudedocs/plan.md", "governance/x.json", "shadow/y.ndjson", "artifacts/z.json",
    "raw_output/o.txt", "unified_reports/r.md", "dispatches/d.md", "daily-log/l.md",
    "build-log-bucket/b.md", "receipt_deadletter/r.json", "review_gates/g.json",
    "health/h.json", "events/e.ndjson", ".vnx-attest/a.json",
    ".claude/vnx-system/skills/x.md",
])
def test_unresolved_exception_prefix_is_not_a_finding(tmp_path, token):
    assert _findings(tmp_path, f"`{token}`\n", rules=("paths",)) == []


def test_exception_never_masks_an_existing_path_resolution(tmp_path):
    # The exception only applies after resolution fails; a tracked path under an
    # excepted first segment still resolves and stays silent.
    extra = {"claudedocs/real.md": ""}
    assert _findings(tmp_path, "`claudedocs/real.md`\n", rules=("paths",), extra=extra) == []


def test_exception_is_first_segment_only(tmp_path):
    assert len(_findings(tmp_path, "`scripts/state/x.py`\n", rules=("paths",))) == 1
    assert len(_findings(tmp_path, "`statex/y.py`\n", rules=("paths",))) == 1


def test_exception_prefix_must_match_whole_install_path(tmp_path):
    assert len(_findings(tmp_path, "`.claude/other/x.md`\n", rules=("paths",))) == 1


def test_path_exceptions_list_is_the_plan_list():
    assert set(mod.PATH_EXCEPTIONS) == {
        ".vnx-data", ".vnx", ".vnx-attest", "state", "logs", "events", "health",
        "review_gates", "receipt_deadletter", "governance", "shadow", "artifacts",
        "raw_output", "unified_reports", "dispatches", "daily-log",
        "build-log-bucket", "claudedocs",
    }
    assert mod.PATH_EXCEPTION_PREFIXES == (".claude/vnx-system/",)


# ---- candidate selection --------------------------------------------------------

def test_fenced_block_is_skipped(tmp_path):
    body = "```\n`scripts/gone.py` [x](nope.md)\n```\n~~~\n`scripts/gone2.py`\n~~~\n"
    assert _findings(tmp_path, body) == []


def test_fence_closes_and_scanning_resumes(tmp_path):
    body = "```\ninside\n```\n`scripts/gone.py`\n"
    assert [f.line for f in _findings(tmp_path, body, rules=("paths",))] == [4]


def test_tilde_fence_not_closed_by_backtick_fence(tmp_path):
    body = "~~~\n```\n`scripts/gone.py`\n~~~\n"
    assert _findings(tmp_path, body, rules=("paths",)) == []


def test_non_path_tokens_are_not_candidates(tmp_path):
    body = "`<x>/y.py` `/api/events` `~/.claude/x.md` `scripts/` `allow/deny` `a b/c.py`\n"
    assert _findings(tmp_path, body, rules=("paths",)) == []


def test_examples_dir_is_skipped_for_paths_not_links(tmp_path):
    doc = "docs/examples/ex.md"
    body = "`scripts/gone.py` [broken](nope.md)\n"
    out = _findings(tmp_path, body, doc=doc)
    assert [f.rule for f in out] == ["links"]


@pytest.mark.parametrize("frozen", [
    "docs/_archive/a.md", "docs/archive/a.md", "docs/investigations/a.md",
    "docs/compliance/a.md", "docs/governance/decisions/a.md",
])
def test_frozen_dirs_are_outside_the_scanset(tmp_path, frozen):
    assert _findings(tmp_path, "`scripts/gone.py` [b](nope.md)\n", doc=frozen) == []


def test_readme_and_contributing_are_scanned(tmp_path):
    root = _repo(tmp_path, {"README.md": "`scripts/gone.py`\n", "CONTRIBUTING.md": "[b](nope.md)\n",
                            "notes.md": "`scripts/gone.py`\n"})
    paths = sorted({f.path for f in mod.run_check(root, {"paths", "links"})})
    assert paths == ["CONTRIBUTING.md", "README.md"]


def test_non_markdown_under_docs_is_not_scanned(tmp_path):
    root = _repo(tmp_path, {"docs/x.txt": "`scripts/gone.py`\n"})
    assert mod.run_check(root, {"paths"}) == []


# ---- links ----------------------------------------------------------------------

def test_link_to_tracked_file_ok(tmp_path):
    assert _findings(tmp_path, "[o](other.md) [p](sub/page.md) [g](../guide/g.md)\n", rules=("links",)) == []


def test_link_to_directory_ok(tmp_path):
    assert _findings(tmp_path, "[d](sub) [d2](sub/) [up](../guide)\n", rules=("links",)) == []


def test_link_anchor_stripped_and_not_checked(tmp_path):
    assert _findings(tmp_path, "[a](other.md#no-such-heading) [b](#local)\n", rules=("links",)) == []


def test_link_title_stripped(tmp_path):
    assert _findings(tmp_path, '[a](other.md "The title") [b](other.md \'t\')\n', rules=("links",)) == []


def test_link_angle_brackets(tmp_path):
    assert _findings(tmp_path, "[a](<other.md>)\n", rules=("links",)) == []


def test_link_external_skipped(tmp_path):
    body = "[a](https://x.invalid/y) [b](http://x.invalid) [c](mailto:a@b.invalid)\n"
    assert _findings(tmp_path, body, rules=("links",)) == []


def test_broken_link_is_a_finding(tmp_path):
    out = _findings(tmp_path, "ok\n[bad](missing.md#h)\n", rules=("links",))
    assert [(f.line, f.rule) for f in out] == [(2, "links")]
    assert "missing.md" in out[0].text


def test_link_is_not_resolved_by_suffix(tmp_path):
    # paths resolve by unique suffix, links must be relative to the document.
    out = _findings(tmp_path, "[x](unique_tool.py) [y](scripts/lib/foo.py)\n", rules=("links",))
    assert len(out) == 2


@pytest.mark.parametrize("target", ["/abs/other.md", "ftp://x.invalid/f", "../../../outside.md", "tel:123"])
def test_link_unknown_scheme_absolute_or_escaping_is_a_finding(tmp_path, target):
    assert len(_findings(tmp_path, f"[x]({target})\n", rules=("links",))) == 1


def test_empty_link_target_is_a_finding(tmp_path):
    assert len(_findings(tmp_path, "[x]()\n", rules=("links",))) == 1


def test_percent_encoded_link_resolves(tmp_path):
    extra = {"docs/core/with space.md": ""}
    assert _findings(tmp_path, "[x](with%20space.md)\n", rules=("links",), extra=extra) == []


def test_untracked_link_target_is_a_finding(tmp_path):
    out = _findings(tmp_path, "[x](local.md)\n", rules=("links",), untracked={"docs/core/local.md": ""})
    assert len(out) == 1


# ---- generated label --------------------------------------------------------------

def test_finding_inside_begin_generated_block_is_labelled(tmp_path):
    body = (
        "`scripts/gone_outside.py`\n"
        "<!-- BEGIN GENERATED: table -->\n"
        "`scripts/gone_inside.py` [b](nope.md)\n"
        "<!-- END GENERATED: table -->\n"
        "`scripts/gone_after.py`\n"
    )
    out = _findings(tmp_path, body)
    by_line = {(f.line, f.rule): f.generated for f in out}
    assert by_line == {(1, "paths"): False, (3, "paths"): True, (3, "links"): True, (5, "paths"): False}
    rendered = [f.render() for f in out if f.line == 3]
    assert all(" generated " in r for r in rendered)
    assert all(" generated " not in f.render() for f in out if f.line != 3)


def test_lowercase_generated_marker_with_script_name(tmp_path):
    body = (
        "<!-- BEGIN generated: scripts/generate_x.py -->\n`scripts/gone.py`\n"
        "<!-- END generated: scripts/generate_x.py -->\n`scripts/gone2.py`\n"
    )
    out = _findings(tmp_path, body, rules=("paths",))
    assert [(f.line, f.generated) for f in out] == [(2, True), (4, False)]


def test_unterminated_generated_block_is_not_labelled(tmp_path):
    body = "<!-- BEGIN GENERATED: t -->\n`scripts/gone.py`\n"
    assert [f.generated for f in _findings(tmp_path, body, rules=("paths",))] == [False]


def test_non_generated_markers_do_not_label(tmp_path):
    body = "<!-- BEGIN section -->\n`scripts/gone.py`\n<!-- END section -->\n"
    assert [f.generated for f in _findings(tmp_path, body, rules=("paths",))] == [False]


def test_second_block_after_first_closes(tmp_path):
    body = (
        "<!-- BEGIN GENERATED: a -->\n<!-- END GENERATED: a -->\n"
        "`scripts/gone.py`\n"
        "<!-- BEGIN GENERATED: b -->\n`scripts/gone2.py`\n<!-- END GENERATED: b -->\n"
    )
    out = _findings(tmp_path, body, rules=("paths",))
    assert [(f.line, f.generated) for f in out] == [(3, False), (5, True)]


def test_daemon_liveness_is_wholly_generated(tmp_path):
    out = _findings(tmp_path, "`scripts/gone.py`\n", doc="docs/core/DAEMON_LIVENESS.md")
    assert [f.generated for f in out] == [True]


# ---- CLI: --report, --rule, exit codes ----------------------------------------------

def _run(root, *args):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(root), *args], capture_output=True, text=True
    )


def test_cli_exit_1_on_finding_and_0_with_report(tmp_path):
    root = _repo(tmp_path, {"docs/core/doc.md": "`scripts/gone.py`\n"})
    plain = _run(root)
    assert plain.returncode == 1
    assert "docs/core/doc.md:1 paths " in plain.stdout
    report = _run(root, "--report")
    assert report.returncode == 0
    assert "docs/core/doc.md:1 paths " in report.stdout
    assert "paths=1" in report.stderr and "links=0" in report.stderr


def test_cli_clean_tree_exit_0(tmp_path):
    root = _repo(tmp_path, {"docs/core/doc.md": "`scripts/lib/foo.py` [o](other.md)\n"})
    r = _run(root)
    assert r.returncode == 0 and r.stdout == ""


def test_cli_rule_selects_and_repeats(tmp_path):
    root = _repo(tmp_path, {"docs/core/doc.md": "`scripts/gone.py` [b](nope.md)\n"})
    only_paths = _run(root, "--rule", "paths")
    assert " paths " in only_paths.stdout and " links " not in only_paths.stdout
    only_links = _run(root, "--rule", "links")
    assert " links " in only_links.stdout and " paths " not in only_links.stdout
    both = _run(root, "--rule", "paths", "--rule", "links")
    assert " paths " in both.stdout and " links " in both.stdout
    default = _run(root)
    assert default.stdout == both.stdout


def test_cli_unknown_rule_is_refused(tmp_path):
    root = _repo(tmp_path, {})
    r = _run(root, "--rule", "versions")
    assert r.returncode == 2 and r.stdout == ""


def test_cli_not_a_git_repo_is_an_error_even_with_report(tmp_path):
    r = _run(tmp_path, "--report")
    assert r.returncode == 2
    assert "ERROR" in r.stderr


def test_unreadable_tracked_doc_is_an_error(tmp_path):
    root = _repo(tmp_path, {"docs/core/bad.md": ""})
    (root / "docs/core/bad.md").write_bytes(b"\xff\xfe\x00bad")
    r = _run(root, "--report")
    assert r.returncode == 2 and "bad.md" in r.stderr


def test_tracked_doc_missing_on_disk_is_an_error(tmp_path):
    root = _repo(tmp_path, {"docs/core/gone.md": "x\n"})
    (root / "docs/core/gone.md").unlink()
    assert _run(root).returncode == 2


def test_empty_repo_is_an_error_not_a_pass(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert _run(tmp_path).returncode == 2


def test_symlinked_doc_pointing_outside_is_read_as_tracked_file(tmp_path):
    # A tracked symlink is a tracked entry; reading it must not crash the run.
    root = _repo(tmp_path, {})
    (root / "docs/core/link.md").symlink_to(root / "docs/core/other.md")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert mod.run_check(root, {"paths", "links"}) == []
