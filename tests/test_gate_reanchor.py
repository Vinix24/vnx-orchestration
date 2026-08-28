"""Regression guards for the OI-1471 re-anchor condition.

Everything here runs against a real git repository built in tmp_path — the
analysis reads git and parses real source, so a test that faked either would
pass while the thing itself was broken.

The measured case this is calibrated against: #1688 tightened
gate_status.has_complete_evidence; #1692 imports and calls it. #1692's diff was
byte-identical across that shift and its meaning was not. Condition (a) alone
cannot see that, which is why (b) exists.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts" / "lib", REPO_ROOT / "scripts"):
    sys.path.insert(0, str(_p))

import gate_reanchor as gr  # noqa: E402
import gate_reanchor_cli as cli  # noqa: E402


# ---------------------------------------------------------------------------
# A real repository to measure against
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} -> {proc.stderr}"
    return proc.stdout


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path):
    """A repo shaped like the real case: a library, a consumer, a branch."""
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "tests").mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))

    (root / "scripts" / "lib" / "lib_a.py").write_text(
        "CONSTANT = 1\n\n\ndef helper(x):\n    return x + 1\n\n\ndef untouched(x):\n    return x\n"
    )
    (root / "scripts" / "lib" / "lib_b.py").write_text(
        "from lib_a import helper\n\n\ndef middle(x):\n    return helper(x)\n"
    )
    (root / "scripts" / "consumer.py").write_text(
        "from lib_a import helper\n\n\ndef run(x):\n    return helper(x)\n"
    )
    (root / "scripts" / "unrelated.py").write_text("def run(x):\n    return x\n")
    (root / "tests" / "test_lib_a.py").write_text("def test_x():\n    assert True\n")
    base = _commit(root, "base")
    return root, base


def _change_helper(root: Path) -> str:
    path = root / "scripts" / "lib" / "lib_a.py"
    path.write_text(path.read_text().replace("return x + 1", "return x + 2"))
    return _commit(root, "tighten helper")


# ---------------------------------------------------------------------------
# changed_symbols
# ---------------------------------------------------------------------------


def test_changed_symbols_names_the_function_a_commit_touched(repo):
    root, base = repo
    head = _change_helper(root)
    assert gr.changed_symbols(root, base, head) == {("lib_a", "helper")}


def test_changed_symbols_reports_a_module_level_edit_separately(repo):
    """A changed constant or dispatch table touches no def, and still changes
    behaviour for everyone importing the module."""
    root, base = repo
    path = root / "scripts" / "lib" / "lib_a.py"
    path.write_text(path.read_text().replace("CONSTANT = 1", "CONSTANT = 99"))
    head = _commit(root, "bump constant")
    assert gr.changed_symbols(root, base, head) == {("lib_a", gr.MODULE_LEVEL)}


def test_changed_symbols_ignores_test_files(repo):
    """A change under tests/ cannot alter the runtime meaning of a call."""
    root, base = repo
    (root / "tests" / "test_lib_a.py").write_text("def test_x():\n    assert 1 == 1\n")
    head = _commit(root, "touch a test")
    assert gr.changed_symbols(root, base, head) == set()


def test_a_deleted_module_makes_the_whole_module_suspect(repo):
    """Fail-closed: unknown becomes blocking, never invisible."""
    root, base = repo
    (root / "scripts" / "lib" / "lib_a.py").unlink()
    (root / "scripts" / "consumer.py").write_text("def run(x):\n    return x\n")
    (root / "scripts" / "lib" / "lib_b.py").write_text("def middle(x):\n    return x\n")
    head = _commit(root, "delete lib_a")
    assert ("lib_a", gr.WHOLE_MODULE) in gr.changed_symbols(root, base, head)


# ---------------------------------------------------------------------------
# referenced_symbols
# ---------------------------------------------------------------------------


def test_referenced_symbols_captures_a_from_import_and_its_module(repo):
    root, base = repo
    referenced = gr.referenced_symbols(root, base, ["scripts/consumer.py"])
    assert ("lib_a", "helper") in referenced
    assert ("lib_a", gr.MODULE_LEVEL) in referenced


def test_referenced_symbols_captures_module_attribute_access(repo):
    root, base = repo
    (root / "scripts" / "consumer.py").write_text(
        "import lib_a\n\n\ndef run(x):\n    return lib_a.helper(x)\n"
    )
    head = _commit(root, "switch to module import")
    referenced = gr.referenced_symbols(root, head, ["scripts/consumer.py"])
    assert ("lib_a", "helper") in referenced


def test_a_file_the_pr_deleted_contributes_no_references(repo):
    """`gh pr view --json files` lists deleted paths too. Absent at this ref is
    a complete answer, not a gap — refusing on it would refuse every PR that
    removes a file."""
    root, base = repo
    assert gr.referenced_symbols(root, base, ["scripts/does_not_exist.py"]) == set()


def test_an_unparseable_file_refuses_instead_of_shrinking_the_reference_set(repo):
    """Present but unreadable is the opposite case. Skipping it silently
    under-approximates what the PR reaches and then treats the remainder as
    complete — an ALLOW on evidence nobody gathered. Found by codex_gate."""
    root, base = repo
    (root / "scripts" / "broken.py").write_text("def f(:\n    pass\n")
    head = _commit(root, "add an unparseable file")
    with pytest.raises(gr.ReanchorError) as excinfo:
        gr.referenced_symbols(root, head, ["scripts/broken.py"])
    assert "does not parse" in str(excinfo.value)


def test_a_star_import_binds_the_pr_to_the_whole_module(repo):
    """`from x import *` says "any name in x". Recording it as the literal
    symbol `*` matched nothing on the changed side, so a changed symbol in a
    star-imported module slipped through. Found by codex_gate."""
    root, base = repo
    (root / "scripts" / "star.py").write_text("from lib_a import *\n\n\ndef run(x):\n    return helper(x)\n")
    head = _commit(root, "add a star importer")
    referenced = gr.referenced_symbols(root, head, ["scripts/star.py"])
    assert ("lib_a", gr.WHOLE_MODULE) in referenced
    assert gr.find_blocking_symbols({("lib_a", "untouched")}, referenced) == [
        ("lib_a", "untouched")
    ]


def test_a_module_imported_by_name_still_resolves_its_attributes(repo):
    """`from pkg import mod; mod.fn()` recorded (pkg, mod) and never mapped
    `mod` to its own module, so changes to mod.fn were invisible. Found by
    codex_gate."""
    root, base = repo
    (root / "scripts" / "pkgstyle.py").write_text(
        "from lib import lib_a\n\n\ndef run(x):\n    return lib_a.helper(x)\n"
    )
    head = _commit(root, "add a package-style importer")
    referenced = gr.referenced_symbols(root, head, ["scripts/pkgstyle.py"])
    assert ("lib_a", "helper") in referenced


# ---------------------------------------------------------------------------
# The blocking decision
# ---------------------------------------------------------------------------


def test_a_directly_imported_changed_symbol_blocks(repo):
    """The #1688/#1692 shape, in miniature. No indirect modules involved."""
    root, base = repo
    changed = {("lib_a", "helper")}
    referenced = gr.referenced_symbols(root, base, ["scripts/consumer.py"])
    assert gr.find_blocking_symbols(changed, referenced) == [("lib_a", "helper")]


def test_a_changed_sibling_in_an_imported_module_does_not_block(repo):
    """`untouched` lives in lib_a, which the PR imports, but the PR does not
    use it. This is what makes the direct rule symbol-precise rather than
    module-precise — and it is the assertion that keeps the exact-overlap
    branch load-bearing."""
    root, base = repo
    changed = {("lib_a", "untouched")}
    referenced = gr.referenced_symbols(root, base, ["scripts/consumer.py"])
    assert gr.find_blocking_symbols(changed, referenced) == []


def test_the_same_sibling_blocks_once_the_module_is_only_reached_indirectly(repo):
    """Which symbol of an indirectly-reached module the PR depends on is not
    knowable from its own source, so any change there blocks."""
    root, base = repo
    changed = {("lib_a", "untouched")}
    referenced = gr.referenced_symbols(root, base, ["scripts/consumer.py"])
    assert gr.find_blocking_symbols(changed, referenced, {"lib_a"}) == [("lib_a", "untouched")]


def test_a_changed_symbol_the_pr_does_not_reach_does_not_block(repo):
    root, base = repo
    changed = {("lib_a", "helper")}
    referenced = gr.referenced_symbols(root, base, ["scripts/unrelated.py"])
    assert gr.find_blocking_symbols(changed, referenced) == []


def test_a_module_level_change_blocks_anyone_importing_that_module(repo):
    root, base = repo
    changed = {("lib_a", gr.MODULE_LEVEL)}
    referenced = gr.referenced_symbols(root, base, ["scripts/consumer.py"])
    assert gr.find_blocking_symbols(changed, referenced) == [("lib_a", gr.MODULE_LEVEL)]


def test_reachability_is_what_separates_direct_from_transitive(repo):
    """lib_b imports lib_a; a PR touching only lib_b reaches lib_a at depth 1."""
    root, base = repo
    graph = gr.build_import_graph(root, base)
    assert graph["lib_b"] == {"lib_a"}
    direct = gr.reachable_modules({"lib_b"}, graph, gr.DEPTH_DIRECT)
    full = gr.reachable_modules({"lib_b"}, graph, gr.DEPTH_FULL)
    assert direct == {"lib_b"}
    assert full == {"lib_a", "lib_b"}


def test_the_default_depth_is_direct_and_that_choice_is_deliberate():
    """Measured: at DEPTH_FULL the analysis refuses #1691 — the PR OI-1471 was
    written about — because glm_gate transitively reaches gate_status. The
    default is the depth a diff-reading gate can itself reason about."""
    assert gr.DEPTH_DEFAULT == gr.DEPTH_DIRECT


# ---------------------------------------------------------------------------
# can_reanchor: both halves, and every unestablished fact refuses
# ---------------------------------------------------------------------------


def _decide(root, old, new, files, old_hash="h1", new_hash="h1", **kw):
    return gr.can_reanchor(
        root, old_sha=old, new_sha=new, pr_files=files,
        old_contract_hash=old_hash, new_contract_hash=new_hash,
        base_ref="main", **kw,
    )


@pytest.mark.parametrize("old_hash,new_hash", [("", "h1"), ("h1", ""), ("", "")])
def test_an_empty_contract_hash_refuses(repo, old_hash, new_hash):
    root, base = repo
    d = _decide(root, base, base, [], old_hash=old_hash, new_hash=new_hash)
    assert not d.allowed and "empty" in d.reason


def test_a_changed_contract_hash_refuses_without_touching_git(repo):
    """Condition (a) is free and settles most refusals, so it runs first."""
    root, base = repo
    d = _decide(root, "old", "new", [], old_hash="h1", new_hash="h2")
    assert not d.allowed and "contract hash changed" in d.reason
    assert d.commits_in_range == 0


def test_the_same_commit_has_nothing_to_re_anchor(repo):
    root, base = repo
    d = _decide(root, base, base, [])
    assert not d.allowed and "nothing to re-anchor" in d.reason


def test_an_unresolvable_merge_base_refuses(repo):
    """A rebased-away commit whose objects are gone: without the old
    merge-base there is no range, so nothing can be proven."""
    root, base = repo
    d = _decide(root, "0" * 40, base, [])
    assert not d.allowed and "merge-base could not be resolved" in d.reason


def test_a_moved_base_with_no_overlap_is_allowed(repo):
    root, base = repo
    _change_helper(root)
    _git(root, "checkout", "-q", "-b", "feature", base)
    (root / "scripts" / "unrelated.py").write_text("def run(x):\n    return x * 3\n")
    head = _commit(root, "unrelated work")
    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-B", "feature2", "main")
    tip = _git(root, "rev-parse", "HEAD").strip()
    d = _decide(root, head, tip, ["scripts/unrelated.py"])
    assert d.allowed, d.reason
    assert d.commits_in_range == 1


def test_a_moved_base_that_changed_a_reached_symbol_refuses(repo):
    """The whole point: identical diff, changed meaning."""
    root, base = repo
    _git(root, "checkout", "-q", "-b", "feature", base)
    old_head = base
    _git(root, "checkout", "-q", "main")
    _change_helper(root)
    _git(root, "checkout", "-q", "-B", "feature2", "main")
    tip = _git(root, "rev-parse", "HEAD").strip()
    d = _decide(root, old_head, tip, ["scripts/consumer.py"])
    assert not d.allowed
    assert ("lib_a", "helper") in d.blocking_symbols
    assert "meaning may" in d.reason


def test_an_unmoved_merge_base_is_allowed_without_any_analysis(repo):
    root, base = repo
    _git(root, "checkout", "-q", "-b", "feature", base)
    (root / "scripts" / "unrelated.py").write_text("def run(x):\n    return x * 5\n")
    head = _commit(root, "branch work")
    (root / "scripts" / "unrelated.py").write_text("def run(x):\n    return x * 5  # note\n")
    head2 = _commit(root, "more branch work")
    d = _decide(root, head, head2, ["scripts/unrelated.py"])
    assert d.allowed and "merge-base did not move" in d.reason


# ---------------------------------------------------------------------------
# The CLI's own refusals
# ---------------------------------------------------------------------------


def test_only_diff_derived_hash_gates_may_be_re_anchored(capsys):
    """_compute_contract_hash's fallback is stable across content changes, so
    hash equality would prove nothing for any other gate."""
    assert cli.main(["--pr", "1", "--gate", "codex_gate"]) == cli.EXIT_BAD_INPUT
    err = capsys.readouterr().err
    assert "no diff-derived contract hash" in err


def test_the_diff_derived_gate_list_is_exactly_the_prompt_hashing_gates():
    assert set(cli.DIFF_DERIVED_HASH_GATES) == {"glm_gate", "kimi_gate"}


def test_a_non_terminal_record_has_no_verdict_to_move(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "pr-5-glm_gate.json").write_text(json.dumps({
        "status": "unavailable", "contract_hash": "h", "report_path": "/x",
    }))
    with pytest.raises(ValueError) as excinfo:
        cli.load_existing_result(results, "glm_gate", 5)
    assert "not terminal" in str(excinfo.value)


def test_an_evidence_less_record_cannot_be_relocated(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "pr-5-glm_gate.json").write_text(json.dumps({
        "status": "pass", "contract_hash": "", "report_path": "",
    }))
    with pytest.raises(ValueError) as excinfo:
        cli.load_existing_result(results, "glm_gate", 5)
    assert "contract_hash" in str(excinfo.value)


def test_a_missing_record_is_a_loud_absence(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    with pytest.raises(FileNotFoundError):
        cli.load_existing_result(results, "glm_gate", 5)


# ---------------------------------------------------------------------------
# Provenance on the written record
# ---------------------------------------------------------------------------


def test_the_reanchored_payload_keeps_the_evidence_and_stamps_where_it_came_from():
    record = {
        "gate": "glm_gate", "status": "pass", "contract_hash": "h1",
        "report_path": "/reports/r.md", "dispatch_id": "glm-gate-pr1-123",
        "commit_sha": "a" * 40, "branch": "old", "evidence_source": "live",
        "blocking_findings": [],
    }
    decision = gr.ReanchorDecision(allowed=True, reason="ok", contract_hash_matches=True)
    payload = cli.build_reanchored_payload(
        record, new_sha="b" * 40, new_branch="new", decision=decision,
    )
    # Evidence carried over verbatim — including the producer that really ran it.
    assert payload["contract_hash"] == "h1"
    assert payload["report_path"] == "/reports/r.md"
    assert payload["dispatch_id"] == "glm-gate-pr1-123"
    assert payload["status"] == "pass"
    # Relocated, and impossible to mistake for a fresh purchase.
    assert payload["commit_sha"] == "b" * 40
    assert payload["branch"] == "new"
    assert payload["evidence_source"] == "reanchored"
    assert payload["reanchored_from_commit_sha"] == "a" * 40
    assert payload["reanchor_basis"]["allowed"] is True
    assert payload["reanchored_at"]


def test_the_reanchored_payload_does_not_mutate_the_record_it_came_from():
    record = {"status": "pass", "commit_sha": "a" * 40, "evidence_source": "live"}
    decision = gr.ReanchorDecision(allowed=True, reason="ok")
    cli.build_reanchored_payload(record, new_sha="b" * 40, new_branch="n", decision=decision)
    assert record["commit_sha"] == "a" * 40
    assert record["evidence_source"] == "live"


# ---------------------------------------------------------------------------
# Deletion-only hunks
# ---------------------------------------------------------------------------


def test_a_deletion_only_hunk_still_marks_the_function_as_changed(repo):
    """`@@ -a,b +c,0 @@` has an EMPTY post-image span.

    Reading it literally records no touched line, so a commit that deletes a
    guard out of a function this PR calls would register as "nothing changed"
    and allow a re-anchor that must be refused. Found by codex_gate on this
    very PR.
    """
    root, base = repo
    path = root / "scripts" / "lib" / "lib_a.py"
    path.write_text(
        "CONSTANT = 1\n\n\ndef helper(x):\n    if x < 0:\n        raise ValueError(x)\n"
        "    return x + 1\n\n\ndef untouched(x):\n    return x\n"
    )
    with_guard = _commit(root, "add a guard to helper")
    path.write_text(
        "CONSTANT = 1\n\n\ndef helper(x):\n    return x + 1\n\n\ndef untouched(x):\n    return x\n"
    )
    without_guard = _commit(root, "delete the guard")

    raw = _git(root, "diff", "--unified=0", with_guard, without_guard)
    assert "+4,0" in raw or ",0 @@" in raw, f"expected a deletion-only hunk, got:\n{raw}"

    assert gr.changed_symbols(root, with_guard, without_guard) == {("lib_a", "helper")}


def test_a_deletion_only_change_refuses_the_re_anchor_end_to_end(repo):
    """The same case through can_reanchor: identical hash, deleted guard."""
    root, base = repo
    path = root / "scripts" / "lib" / "lib_a.py"
    path.write_text(
        "CONSTANT = 1\n\n\ndef helper(x):\n    if x < 0:\n        raise ValueError(x)\n"
        "    return x + 1\n\n\ndef untouched(x):\n    return x\n"
    )
    guarded = _commit(root, "add a guard")
    _git(root, "checkout", "-q", "-b", "feature", guarded)
    old_head = guarded
    _git(root, "checkout", "-q", "main")
    path.write_text(
        "CONSTANT = 1\n\n\ndef helper(x):\n    return x + 1\n\n\ndef untouched(x):\n    return x\n"
    )
    _commit(root, "delete the guard")
    _git(root, "checkout", "-q", "-B", "feature2", "main")
    tip = _git(root, "rev-parse", "HEAD").strip()

    decision = gr.can_reanchor(
        root, old_sha=old_head, new_sha=tip, pr_files=["scripts/consumer.py"],
        old_contract_hash="h1", new_contract_hash="h1", base_ref="main",
    )
    assert not decision.allowed
    assert ("lib_a", "helper") in decision.blocking_symbols


# ---------------------------------------------------------------------------
# The ADR-005 ledger line
# ---------------------------------------------------------------------------


def test_a_reanchor_appends_a_register_line(tmp_path, monkeypatch):
    """A re-anchor is a gate outcome — one of ADR-005's named ledger classes —
    and it is the one gate outcome nobody paid a model for. Writing the result
    record with no register line would leave a state mutation with no trace,
    which is the defect this whole cluster is about.
    """
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    record = {
        "gate": "glm_gate", "status": "pass", "contract_hash": "h1",
        "report_path": "/r.md", "dispatch_id": "glm-gate-pr9-1", "commit_sha": "a" * 40,
    }
    decision = gr.ReanchorDecision(
        allowed=True, reason="identical hash", contract_hash_matches=True,
        commits_in_range=3, depth=gr.DEPTH_DIRECT,
    )
    path = cli.emit_reanchor_event(
        gate="glm_gate", pr_number=9, record=record, new_sha="b" * 40, decision=decision,
    )
    assert path == tmp_path / "dispatch_register.ndjson"
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["event"] == "gate_reanchored"
    assert entry["gate"] == "glm_gate"
    assert entry["pr_number"] == 9
    assert entry["dispatch_id"] == "glm-gate-pr9-1"
    assert entry["from_commit_sha"] == "a" * 40
    assert entry["to_commit_sha"] == "b" * 40
    assert entry["commits_in_range"] == 3
    assert entry["reason"] == "identical hash"


def test_the_register_line_goes_to_the_same_resolver_every_other_gate_line_uses(tmp_path, monkeypatch):
    """One resolver, not a second ledger invented for this writer."""
    import gate_register_emit

    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    assert gate_register_emit.register_path() == tmp_path / "dispatch_register.ndjson"
    assert gate_register_emit._resolve_register_path() == gate_register_emit.register_path()


def test_a_failing_register_write_is_reported_not_swallowed(tmp_path, monkeypatch):
    """The result is already on disk and carries its own provenance, so the
    state is sound — but a missing ledger line has to be said out loud."""
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))

    def _boom(path, record):
        raise OSError("disk full")

    monkeypatch.setattr(cli.state_writer, "append_locked", _boom)
    with pytest.raises(OSError):
        cli.emit_reanchor_event(
            gate="glm_gate", pr_number=9, record={}, new_sha="b" * 40,
            decision=gr.ReanchorDecision(allowed=True, reason="r"),
        )
