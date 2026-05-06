"""Unit tests for scripts.lib.strategy.decisions."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lib.strategy.decisions import (  # noqa: E402
    Decision,
    DecisionValidationError,
    record_decision,
    recent_decisions,
    supersedes_chain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _record(
    decision_id: str,
    scope: str = "test-scope",
    rationale: str = "test rationale",
    supersedes: str | None = None,
    evidence_path: str | None = None,
    *,
    path: Path,
) -> Decision:
    return record_decision(
        decision_id,
        scope,
        rationale,
        supersedes=supersedes,
        evidence_path=evidence_path,
        path=path,
    )


# ---------------------------------------------------------------------------
# Append + tail roundtrip
# ---------------------------------------------------------------------------
class TestAppendTailRoundtrip:
    def test_single_entry(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        d = _record("OD-2026-05-06-001", path=p)
        assert d.decision_id == "OD-2026-05-06-001"
        assert d.scope == "test-scope"
        assert d.rationale == "test rationale"
        assert d.supersedes is None
        assert d.evidence_path is None

        tail = recent_decisions(n=10, path=p)
        assert len(tail) == 1
        assert tail[0].decision_id == "OD-2026-05-06-001"
        assert tail[0].ts == d.ts

    def test_multiple_entries_order(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", scope="s1", path=p)
        _record("OD-2026-05-06-002", scope="s2", path=p)
        _record("OD-2026-05-06-003", scope="s3", path=p)

        tail = recent_decisions(n=10, path=p)
        assert len(tail) == 3
        ids = [d.decision_id for d in tail]
        assert ids == ["OD-2026-05-06-001", "OD-2026-05-06-002", "OD-2026-05-06-003"]

    def test_optional_fields_roundtrip(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        d = _record(
            "TD-2026-05-06-001",
            scope="scope-with-evidence",
            rationale="multi-line\nrationale",
            supersedes="OD-2026-05-01-001",
            evidence_path="claudedocs/my-adr.md",
            path=p,
        )
        tail = recent_decisions(n=1, path=p)
        assert len(tail) == 1
        assert tail[0].supersedes == "OD-2026-05-01-001"
        assert tail[0].evidence_path == "claudedocs/my-adr.md"
        assert tail[0].rationale == "multi-line\nrationale"

    def test_file_contains_valid_ndjson(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", path=p)
        _record("OD-2026-05-06-002", path=p)
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert "decision_id" in obj
            assert "scope" in obj
            assert "ts" in obj
            assert "rationale" in obj

    def test_no_file_returns_empty_list(self, tmp_path):
        p = tmp_path / "nonexistent.ndjson"
        assert recent_decisions(n=10, path=p) == []


# ---------------------------------------------------------------------------
# recent_decisions honors n
# ---------------------------------------------------------------------------
class TestRecentDecisionsN:
    def test_n_limits_results(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        for i in range(1, 8):
            _record(f"OD-2026-05-06-{i:03d}", scope=f"s{i}", path=p)

        tail3 = recent_decisions(n=3, path=p)
        assert len(tail3) == 3
        assert [d.decision_id for d in tail3] == [
            "OD-2026-05-06-005",
            "OD-2026-05-06-006",
            "OD-2026-05-06-007",
        ]

    def test_n_larger_than_total_returns_all(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", path=p)
        _record("OD-2026-05-06-002", path=p)

        assert len(recent_decisions(n=100, path=p)) == 2

    def test_n_zero_returns_empty(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", path=p)
        assert recent_decisions(n=0, path=p) == []

    def test_default_n_is_10(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        for i in range(1, 16):
            _record(f"OD-2026-05-06-{i:03d}", scope=f"s{i}", path=p)

        tail = recent_decisions(path=p)
        assert len(tail) == 10
        assert tail[0].decision_id == "OD-2026-05-06-006"
        assert tail[-1].decision_id == "OD-2026-05-06-015"


# ---------------------------------------------------------------------------
# Concurrent writers — no interleaving
# ---------------------------------------------------------------------------
class TestConcurrentWriters:
    def test_no_corruption_under_threading(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        errors: list[Exception] = []
        n_threads = 4
        entries_per_thread = 10

        def write_entries(thread_idx: int) -> None:
            try:
                for j in range(1, entries_per_thread + 1):
                    seq = thread_idx * 100 + j
                    _record(f"OD-2026-05-06-{seq:03d}", scope=f"t{thread_idx}-entry{j}", path=p)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_entries, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"

        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) == n_threads * entries_per_thread

        for line in lines:
            obj = json.loads(line)
            assert "decision_id" in obj
            assert "scope" in obj
            assert "ts" in obj
            assert "rationale" in obj


# ---------------------------------------------------------------------------
# Schema rejection
# ---------------------------------------------------------------------------
class TestSchemaRejection:
    def test_missing_decision_id(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        with pytest.raises(DecisionValidationError, match="decision_id"):
            record_decision("", "scope", "rationale", path=p)

    def test_missing_scope(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        with pytest.raises(DecisionValidationError, match="scope"):
            record_decision("OD-2026-05-06-001", "", "rationale", path=p)

    def test_missing_rationale(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        with pytest.raises(DecisionValidationError, match="rationale"):
            record_decision("OD-2026-05-06-001", "scope", "", path=p)

    def test_bad_prefix_rejected(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        with pytest.raises(DecisionValidationError):
            _record("XX-2026-05-06-001", path=p)

    def test_lowercase_prefix_rejected(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        with pytest.raises(DecisionValidationError):
            _record("od-2026-05-06-001", path=p)

    def test_no_file_written_on_rejection(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        with pytest.raises(DecisionValidationError):
            _record("XX-2026-05-06-001", path=p)
        assert not p.exists()


# ---------------------------------------------------------------------------
# Decision-ID format validation
# ---------------------------------------------------------------------------
class TestDecisionIdFormat:
    @pytest.mark.parametrize("valid_id", [
        "OD-2026-05-06-001",
        "OD-2026-12-31-999",
        "TD-2026-05-06-001",
        "TD-2000-01-01-001",
        "OD-2026-05-06-042",
    ])
    def test_valid_ids_accepted(self, tmp_path, valid_id):
        p = tmp_path / "decisions.ndjson"
        d = _record(valid_id, path=p)
        assert d.decision_id == valid_id

    @pytest.mark.parametrize("invalid_id", [
        "XX-2026-05-06-001",        # wrong prefix
        "od-2026-05-06-001",        # lowercase
        "OD-2026-13-01-001",        # month 13 invalid
        "OD-2026-00-01-001",        # month 0 invalid
        "OD-2026-05-00-001",        # day 0 invalid
        "OD-2026-05-32-001",        # day 32 invalid
        "OD-26-05-06-001",          # 2-digit year
        "OD-2026-5-6-001",          # unpadded month/day
        "OD-2026-05-06-01",         # 2-digit sequence
        "OD-2026-05-06-0001",       # 4-digit sequence
        "2026-05-06-001",           # missing prefix
        "",                          # empty
        "OD-2026-05-06",            # missing sequence
    ])
    def test_invalid_ids_rejected(self, tmp_path, invalid_id):
        p = tmp_path / "decisions.ndjson"
        with pytest.raises(DecisionValidationError):
            _record(invalid_id, path=p)


# ---------------------------------------------------------------------------
# supersedes_chain
# ---------------------------------------------------------------------------
class TestSupersedesChain:
    def test_three_deep_chain(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", scope="original", path=p)
        _record("OD-2026-05-06-002", scope="revised", supersedes="OD-2026-05-06-001", path=p)
        _record("OD-2026-05-06-003", scope="final", supersedes="OD-2026-05-06-002", path=p)

        chain = supersedes_chain("OD-2026-05-06-003", path=p)
        assert len(chain) == 3
        assert chain[0].decision_id == "OD-2026-05-06-001"
        assert chain[1].decision_id == "OD-2026-05-06-002"
        assert chain[2].decision_id == "OD-2026-05-06-003"

    def test_root_node_chain_length_one(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", path=p)
        chain = supersedes_chain("OD-2026-05-06-001", path=p)
        assert len(chain) == 1
        assert chain[0].decision_id == "OD-2026-05-06-001"

    def test_chain_from_middle(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", scope="root", path=p)
        _record("OD-2026-05-06-002", scope="mid", supersedes="OD-2026-05-06-001", path=p)
        _record("OD-2026-05-06-003", scope="leaf", supersedes="OD-2026-05-06-002", path=p)

        chain = supersedes_chain("OD-2026-05-06-002", path=p)
        assert len(chain) == 2
        assert chain[0].decision_id == "OD-2026-05-06-001"
        assert chain[1].decision_id == "OD-2026-05-06-002"

    def test_missing_decision_returns_empty(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", path=p)
        assert supersedes_chain("OD-2026-05-06-999", path=p) == []

    def test_no_file_returns_empty(self, tmp_path):
        p = tmp_path / "nonexistent.ndjson"
        assert supersedes_chain("OD-2026-05-06-001", path=p) == []

    def test_cycle_guard(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        # Manually write a cycle
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"decision_id": "OD-2026-05-06-001", "scope": "a", "ts": "2026-05-06T00:00:00+00:00", "rationale": "r", "supersedes": "OD-2026-05-06-002"}),
            json.dumps({"decision_id": "OD-2026-05-06-002", "scope": "b", "ts": "2026-05-06T00:00:01+00:00", "rationale": "r", "supersedes": "OD-2026-05-06-001"}),
        ]
        p.write_text("\n".join(lines) + "\n")
        chain = supersedes_chain("OD-2026-05-06-001", path=p)
        # Should not infinite loop; length is bounded
        assert len(chain) <= 2

    def test_chain_scopes_preserved(self, tmp_path):
        p = tmp_path / "decisions.ndjson"
        _record("OD-2026-05-06-001", scope="signing-key-location", path=p)
        _record("OD-2026-05-06-002", scope="signing-key-location-v2", supersedes="OD-2026-05-06-001", path=p)

        chain = supersedes_chain("OD-2026-05-06-002", path=p)
        assert chain[0].scope == "signing-key-location"
        assert chain[1].scope == "signing-key-location-v2"
