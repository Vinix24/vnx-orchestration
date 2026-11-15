"""ADR-078 M5b test duties: invalid_at candidates and validity warnings, from the bound project's own evidence."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
import memory_cli
import memory_doctor as md
import memory_ledger as ml


def _hash_tree(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def _store(state: Path, items: dict, merged=()):
    state.mkdir(parents=True, exist_ok=True)
    (state / "open_items.json").write_text(json.dumps({"items": [{"id": k, "status": v} for k, v in items.items()]}))
    (state / "t0_receipts.ndjson").write_text("".join(json.dumps({"event_type": "pr_merged", "pr_number": n}) + "\n" for n in merged))


@pytest.fixture
def env(tmp_path):
    mem, state = tmp_path / "mem", tmp_path / "state"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("- [n](n.md)\n")
    (mem / "n.md").write_text("note\n")
    _store(state, {"OI-1": "done", "OI-2": "wontfix", "OI-3": "deferred", "OI-4": "open"}, merged=[100])
    (state / ml.SOURCES_FILE).write_text(json.dumps({"schema": ml.SOURCES_SCHEMA, "project_id": "proj",
                                                     "memory_dirs": [str(mem.resolve())], "bound_at": "2026-11-15T00:00:00Z"}))
    return mem, state


def cands(doc):
    return {p["note"]: p["evidence"] for p in doc["proposals"] if p["kind"] == "invalid_at_candidate"}


def test_two_done_ois_is_a_candidate(env):
    mem, state = env
    (mem / "n.md").write_text("closed by OI-1 and OI-2\n")
    assert cands(md.run(state)) == {"n.md": {"oi": {"OI-1": "done", "OI-2": "wontfix"}, "pr_merged": []}}


@pytest.mark.parametrize("body", ["OI-1 and OI-99", "OI-3", "OI-1 and OI-3", "OI-4", "no refs at all", "OI-1 and #111", "#100 only"])
def test_unknown_deferred_open_or_unmerged_never_a_candidate(env, body):
    mem, state = env
    (mem / "n.md").write_text(body + "\n")
    assert cands(md.run(state)) == {}


def test_done_oi_with_merged_pr_is_candidate(env):
    mem, state = env
    (mem / "n.md").write_text("OI-1 fixed in PR #100\n")
    assert cands(md.run(state))["n.md"]["pr_merged"] == [100]


@pytest.mark.parametrize("receipt", ['{"event_type": "pr_merged"}', '{"event_type": "pr_merged", "pr_number": null}',
                                     '{"event_type": "other", "pr_number": 100}', "not json", "[]"])
def test_bad_receipt_line_is_no_merge_evidence(env, receipt):
    mem, state = env
    (state / "t0_receipts.ndjson").write_text(receipt + "\n")
    (mem / "n.md").write_text("OI-1 in #100\n")
    assert cands(md.run(state)) == {}


@pytest.mark.parametrize("store", [None, "not json", "[]", '{"items": "x"}'])
def test_missing_or_malformed_store_makes_every_oi_unknown(env, store):
    mem, state = env
    (mem / "n.md").write_text("OI-1\n")
    if store is None:
        (state / "open_items.json").unlink()
    else:
        (state / "open_items.json").write_text(store)
    doc = md.run(state)
    assert cands(doc) == {} and doc["status"] == "degraded"


def test_oi_known_only_in_another_projects_store_is_unknown(env, tmp_path):
    mem, state = env
    _store(tmp_path / "other", {"OI-77": "done"})
    (mem / "n.md").write_text("OI-77\n")
    assert cands(md.run(state)) == {}


def test_item_of_a_foreign_project_id_is_unknown(env):
    mem, state = env
    (state / "open_items.json").write_text(json.dumps({"items": [{"id": "OI-1", "status": "done", "project_id": "elsewhere"}]}))
    (mem / "n.md").write_text("OI-1\n")
    assert cands(md.run(state)) == {}


def test_unreadable_date_and_bad_frontmatter_are_proposed_and_notes_kept(env):
    mem, state = env
    (mem / "n.md").write_text("---\nname: n\nmetadata:\n  invalid_at: 15/11/2026\n---\nbody\n")
    (mem / "bad.md").write_text("---\nname: [unclosed\n---\nbody\n")
    (mem / "MEMORY.md").write_text("- [n](n.md)\n- [b](bad.md)\n")
    doc = md.run(state)
    by = {(p["kind"], p["note"]): p["evidence"] for p in doc["proposals"]}
    assert by[("validity_warning", "n.md")] == {"kind": "unreadable_date", "field": "invalid_at"}
    assert by[("frontmatter_unparseable", "bad.md")] == "unparseable"
    assert (mem / "n.md").exists() and (mem / "bad.md").exists()


def test_absent_frontmatter_is_no_proposal(env):
    _, state = env
    assert not [p for p in md.run(state)["proposals"] if p["kind"] in ("frontmatter_unparseable", "validity_warning")]


def test_chain_warnings_reuse_the_indexer(env):
    mem, state = env
    (mem / "n.md").write_text("---\nsuperseded_by: gone.md\n---\n")
    (mem / "a.md").write_text("---\nsuperseded_by: b.md\n---\n")
    (mem / "b.md").write_text("---\ninvalid_at: 2020-01-01\nsuperseded_by: a.md\n---\n")
    kinds = {(p["note"], p["evidence"]["kind"]) for p in md.run(state)["proposals"] if p["kind"] == "validity_warning"}
    assert {("n.md", "successor_missing"), ("a.md", "cycle")} <= kinds


def test_ids_are_stable_and_folder_untouched(env):
    mem, state = env
    (mem / "n.md").write_text("OI-1\n")
    before = _hash_tree(mem)
    first, second = md.run(state), md.run(state)
    assert [p["id"] for p in first["proposals"]] == [p["id"] for p in second["proposals"]] and _hash_tree(mem) == before


def test_cli_doctor_json_and_exit_codes(env, monkeypatch, capsys):
    mem, state = env
    (mem / "n.md").write_text("OI-1\n")
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    assert memory_cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["proposals"][0]["kind"] == "invalid_at_candidate"
    assert memory_cli.main(["doctor"]) == 0 and "invalid_at_candidate=1" in capsys.readouterr().out
    (state / "open_items.json").unlink()
    assert memory_cli.main(["doctor"]) == 1
    (state / ml.SOURCES_FILE).unlink()
    assert memory_cli.main(["doctor"]) == 3
