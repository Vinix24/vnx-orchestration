"""tests/test_k6a_transcript_identity_at_booking.py — a harness run names its session when it is written (K6a).

Track ``kosten-per-model-in-het-grootboek`` (ADR-049, OI-2419/OI-2426). A
``cost_ledger`` line written by ``harness_run_cost`` for a run that knows its
``session_id`` carries ``transcript_identity`` = ``"<provider>:<session_id>"``,
so a later backfill (K6b) can match a line to a transcript instead of guessing.
Covered against the real ledger writer and real JSONL transcripts under a
temporary projects dir:

- a builder run with a session books the identity on deepseek-harness and glm-harness;
- no session, or a blank one, books no identity (and never a blank one);
- a run without an identity keeps the ``record_id`` of ``origin/main`` (golden),
  because a field that is absent from the body does not enter the hash;
- a blank ``RunCost.transcript_identity`` is refused and nothing is written;
- ``record_classifier_run`` books its session's identity too;
- two sessions of one dispatch are two attempts with two different identities.

Every ledger and transcript lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
for _path in (str(REPO_ROOT), str(_LIB)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import harness_run_cost  # noqa: E402

PROJECT = "vnx-dev"
DISPATCH = "20261021-k6a-test"
START = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)
IDENTITY = "transcript_identity"  # the line's field; ``cost_ledger.TRANSCRIPT_IDENTITY`` is the same name
# The ``record_id`` of ``_golden_run()`` without an identity, computed on ``origin/main``
# (a41c9736). Pinned: it must not move, or every existing line's identity would change.
GOLDEN_RECORDED_AT = "2026-10-21T09:30:00Z"
GOLDEN_RECORD_ID = "0a819cee23c0f38c1159d9cda691a675"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    data = tmp_path / "store"
    state = data / "state"
    state.mkdir(parents=True)
    (data / "unified_reports").mkdir()
    projects = tmp_path / "claude-projects"
    projects.mkdir()
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(projects))
    return SimpleNamespace(projects=projects, ledger=data / "events" / "provider_costs.ndjson")


def _transcript(projects: Path, session_id: str, model: str) -> Path:
    """A two-message transcript of ``session_id``, as the harness lane leaves one."""
    path = projects / "-tmp-k6a-worktree" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 4000,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    entries = [{"type": "assistant", "timestamp": "2026-10-21T09:01:00Z",
                "message": {"id": "k6a-1", "role": "assistant", "model": model, "usage": usage}}]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def _builder(provider="deepseek-harness", model="deepseek-v4-pro", session_id=None):
    return harness_run_cost.BuilderRun(
        provider=provider, model=model, started=START, ended=END, session_id=session_id,
    )


def _record(store, run):
    return harness_run_cost.record_builder_run(
        run, project_id=PROJECT, producer="envelope_govern", dispatch_id=DISPATCH,
        ledger_path=store.ledger,
    )


def _lines(ledger: Path) -> list:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def _golden_run(**overrides) -> cost_ledger.RunCost:
    """A fixed run whose record is byte-identical on ``origin/main`` when no identity is set."""
    fields = dict(
        run_id=f"{PROJECT}:envelope_govern:20261021-kp-golden:1", project_id=PROJECT,
        role="builder", provider="deepseek-harness", model="deepseek-v4-pro",
        occurred_from="2026-10-21T09:00:00Z", occurred_to="2026-10-21T09:30:00Z",
        tokens={"input_uncached": 1000, "cache_hit": 4000, "cache_write_5m": 0,
                "cache_write_1h": 0, "output": 200},
        usage_completeness="complete", billing="metered",
        cost_usd=0.5, cost_source="provider_reported",
    )
    fields.update(overrides)
    return cost_ledger.RunCost(**fields)


def test_the_field_name_and_the_format_have_one_definition():
    assert cost_ledger.TRANSCRIPT_IDENTITY == IDENTITY
    assert harness_run_cost.transcript_identity("glm-harness", "sess-1") == "glm-harness:sess-1"


@pytest.mark.parametrize("provider,model", [("deepseek-harness", "deepseek-v4-pro"),
                                            ("glm-harness", "glm-5.2")])
def test_a_run_with_a_session_books_its_transcript_identity(store, provider, model):
    _transcript(store.projects, "sess-k6a-a", model)
    record = _record(store, _builder(provider=provider, model=model, session_id="sess-k6a-a"))
    assert record[IDENTITY] == f"{provider}:sess-k6a-a"
    assert record[IDENTITY] == harness_run_cost.transcript_identity(provider, "sess-k6a-a")
    lines = _lines(store.ledger)
    assert len(lines) == 1
    assert lines[0][IDENTITY] == f"{provider}:sess-k6a-a"


@pytest.mark.parametrize("session_id", [None, "", "   "])
def test_a_run_without_a_session_books_no_identity(store, session_id):
    # A blank id names no session (``run_fields`` already reads it as none), so it is no identity:
    # a blank one is refused by the ledger and must never cost the run its line.
    record = _record(store, _builder(session_id=session_id))
    assert IDENTITY not in record
    lines = _lines(store.ledger)
    assert len(lines) == 1
    assert IDENTITY not in lines[0]


def test_a_run_without_an_identity_keeps_the_record_id_of_origin_main(monkeypatch):
    monkeypatch.setattr(cost_ledger, "_now_iso", lambda: GOLDEN_RECORDED_AT)
    record = cost_ledger.build_record(_golden_run())
    assert IDENTITY not in record
    assert record["record_id"] == GOLDEN_RECORD_ID


@pytest.mark.parametrize("identity", ["", "   "])
def test_a_blank_transcript_identity_is_refused_and_nothing_is_written(store, identity):
    with pytest.raises(cost_ledger.InvalidRecord):
        cost_ledger.record_run(_golden_run(transcript_identity=identity), ledger_path=store.ledger)
    assert not store.ledger.exists()
    assert not (store.ledger.parent / cost_ledger.SPOOL_DIR_NAME).exists()


def test_the_classifier_books_its_session_identity(store):
    _transcript(store.projects, "cls-k6a-a", "deepseek-v4-pro")
    record, reason = harness_run_cost.record_classifier_run(
        provider="deepseek-harness", model="deepseek-v4-pro", session_id="cls-k6a-a",
        started=START, ended=END, project_id=PROJECT,
    )
    assert reason is None
    assert record["role"] == harness_run_cost.INTELLIGENCE_ROLE
    assert record["run_id"] == f"{PROJECT}:{harness_run_cost.CLASSIFIER_PRODUCER}:cls-k6a-a:1"
    assert record[IDENTITY] == "deepseek-harness:cls-k6a-a"
    assert _lines(store.ledger)[0][IDENTITY] == "deepseek-harness:cls-k6a-a"


def test_two_sessions_of_one_dispatch_are_two_attempts_with_two_identities(store):
    _transcript(store.projects, "sess-k6a-1", "deepseek-v4-pro")
    _transcript(store.projects, "sess-k6a-2", "deepseek-v4-pro")
    first = _record(store, _builder(session_id="sess-k6a-1"))
    second = _record(store, _builder(session_id="sess-k6a-2"))
    assert first["run_id"] == f"{PROJECT}:envelope_govern:{DISPATCH}:1"
    assert second["run_id"] == f"{PROJECT}:envelope_govern:{DISPATCH}:2"
    assert first[IDENTITY] == "deepseek-harness:sess-k6a-1"
    assert second[IDENTITY] == "deepseek-harness:sess-k6a-2"
    assert [line[IDENTITY] for line in _lines(store.ledger)] == [
        "deepseek-harness:sess-k6a-1", "deepseek-harness:sess-k6a-2",
    ]
