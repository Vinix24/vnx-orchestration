"""tests/test_k6b1_backfill_reader.py — the harness transcripts read as runs (K6b-1, ADR-049).

Covers the reader half of ``scripts/cost_ledger_backfill.py`` against real JSONL
files under ``tmp_path`` (never a real harness-config dir or ledger): the dispatch
id comes from the transcript's ``cwd``, a rewritten message counts once, overlapping
roots yield one run per session, ``since`` drops older runs only, ``scan`` is
deterministic, and the provider is the lane directory rather than the reported model.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    from cost_ledger_backfill import lane_roots, scan
except Exception as exc:
    raise SystemExit(f"Failed to load cost_ledger_backfill: {exc}")

ENCODED_CWD = "-repo-fixture-project"
WORKTREE_CWD = "/repo/.vnx-data/worktrees/dispatch-dlv-b1"


def _entry(message_id, timestamp, cwd, input_tokens=10, output_tokens=1, model="deepseek-v4-pro"):
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
                "output_tokens": output_tokens,
            },
        },
    }


class Lab:
    """Fixture transcripts: ``<tmp>/<store>/<lane>/projects/<encoded-cwd>/<session>.jsonl``."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    def lane(self, name, store="harness"):
        root = self.tmp_path / store / name
        (root / "projects").mkdir(parents=True, exist_ok=True)
        return root

    def transcript(self, session, entries, lane="deepseek-harness", store="harness"):
        project = self.lane(lane, store) / "projects" / ENCODED_CWD
        project.mkdir(parents=True, exist_ok=True)
        path = project / f"{session}.jsonl"
        path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
        return path


def test_a_worktree_cwd_names_the_dispatch_and_a_root_cwd_names_none(tmp_path):
    lab = Lab(tmp_path)
    lane = lab.lane("deepseek-harness")
    lab.transcript("sess-b1", [_entry("m1", "2026-10-05T02:00:00Z", WORKTREE_CWD)])
    lab.transcript("sess-b2", [_entry("m1", "2026-10-05T03:00:00Z", "/")])

    runs = {run.session_id: run for run in scan([lane], date(2026, 10, 5))}

    assert runs["sess-b1"].dispatch_id == "dlv-b1"
    assert runs["sess-b1"].ref == "dlv-b1"
    assert runs["sess-b2"].dispatch_id is None
    assert runs["sess-b2"].ref == "sess-b2"


def test_a_rewritten_message_counts_once(tmp_path):
    lab = Lab(tmp_path)
    lane = lab.lane("deepseek-harness")
    lab.transcript("sess-w", [
        _entry("m1", "2026-10-05T02:00:00Z", WORKTREE_CWD, input_tokens=10, output_tokens=1),
        _entry("m1", "2026-10-05T02:00:02Z", WORKTREE_CWD, input_tokens=100, output_tokens=7),
    ])

    run, = scan([lane], date(2026, 10, 5))

    assert len(run.messages) == 1
    assert run.messages[0].tokens["input_uncached"] == 100
    assert run.messages[0].tokens["output"] == 7


def test_a_parent_dir_and_its_lane_dir_book_one_run(tmp_path):
    lab = Lab(tmp_path)
    lane = lab.lane("deepseek-harness")
    lab.transcript("sess-p", [_entry("m1", "2026-10-05T02:00:00Z", WORKTREE_CWD)])

    assert [root.resolve() for root in lane_roots([str(lane.parent), str(lane)])] == [lane.resolve()]
    assert [run.session_id for run in scan([lane.parent, lane], date(2026, 10, 5))] == ["sess-p"]


def test_one_session_reachable_from_two_parents_books_once(tmp_path):
    lab = Lab(tmp_path)
    for store in ("store-a", "store-b"):
        lab.transcript("sess-s", [_entry("m1", "2026-10-05T02:00:00Z", WORKTREE_CWD)], store=store)

    runs = scan([tmp_path / "store-a", tmp_path / "store-b"], date(2026, 10, 5))

    assert [run.session_id for run in runs] == ["sess-s"]
    assert runs[0].identity == "deepseek-harness:sess-s"


def test_a_transcript_before_since_is_dropped_and_one_on_since_is_kept(tmp_path):
    lab = Lab(tmp_path)
    lane = lab.lane("deepseek-harness")
    lab.transcript("old", [_entry("m1", "2026-10-04T23:59:59Z", WORKTREE_CWD)])
    lab.transcript("edge", [_entry("m1", "2026-10-05T00:00:00Z", WORKTREE_CWD)])

    assert [run.session_id for run in scan([lane], date(2026, 10, 5))] == ["edge"]


def test_scan_returns_the_same_order_twice(tmp_path):
    lab = Lab(tmp_path)
    lane = lab.lane("deepseek-harness")
    lab.transcript("b-later", [_entry("m1", "2026-10-05T05:00:00Z", WORKTREE_CWD)])
    lab.transcript("a-earlier", [_entry("m1", "2026-10-05T01:00:00Z", WORKTREE_CWD)])

    first = [(run.session_id, run.path) for run in scan([lane], date(2026, 10, 4))]
    second = [(run.session_id, run.path) for run in scan([lane], date(2026, 10, 4))]

    assert first == second
    assert [session for session, _path in first] == ["a-earlier", "b-later"]


def test_the_provider_is_the_lane_directory_not_the_reported_model(tmp_path):
    lab = Lab(tmp_path)
    lane = lab.lane("deepseek-harness")
    lab.transcript("sess-m", [_entry("m1", "2026-10-05T02:00:00Z", WORKTREE_CWD, model="glm-5.2")])

    run, = scan([lane], date(2026, 10, 5))

    assert run.provider == "deepseek-harness"
    assert run.reported_model == "glm-5.2"
