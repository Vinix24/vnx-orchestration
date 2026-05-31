#!/usr/bin/env python3
"""Wave 2 core — provider-aware intelligence + receipt token/cost capture.

Regression coverage for the four verified findings:

1. ``dispatch_metadata`` gains a ``provider`` column (migration v21) so the
   self-learning layer is no longer provider-blind.
2. Receipt ``token_usage`` carries the same non-zero tokens as the cost-event
   ledger, and ``receipt.report_path`` points at the emitted report.
3. ``cost_usd`` resolves to real dollars per provider/model (registry, with a
   rate-table fallback for API lanes) instead of silently landing at 0.
4. A non-Claude dispatch through ``_emit_governance`` writes a provider-stamped
   ``dispatch_metadata`` row (so the receipt processor's outcome UPDATE and tag/
   pattern ingest see cheap-lane work too).

These run real code (real migration, real ``_emit_governance``) — no
reimplementation of the logic under test.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

import provider_dispatch  # noqa: E402
import quality_db_init  # noqa: E402
from dispatch_metadata_db import upsert_dispatch_provider_row  # noqa: E402


def _bootstrap_db(tmp_path: Path) -> Path:
    db = tmp_path / "state" / "quality_intelligence.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    assert quality_db_init.bootstrap_qi_db(db, _REPO / "schemas" / "quality_intelligence.sql")
    return db


# ---------------------------------------------------------------------------
# Finding 1 — migration adds provider column + index
# ---------------------------------------------------------------------------

def test_migration_adds_provider_column_and_index(tmp_path):
    db = _bootstrap_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()}
        assert "provider" in cols
        idx_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dispatch_metadata'"
            ).fetchall()
        }
        assert "idx_dispatch_meta_provider" in idx_names
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 21
    finally:
        conn.close()


def test_migration_v21_upgrades_to_composite_index_when_project_id_present(tmp_path):
    """ADR-007: once project_id exists, the provider index becomes composite.

    Reproduces the production bootstrap state: the base schema SQL has already
    created the plain (provider) index under the same name. _migrate_v21 must
    drop-then-recreate so the composite is genuinely created — a bare
    CREATE INDEX IF NOT EXISTS would silently skip on the name collision and
    leave the DB stuck on the plain index. This test does NOT manually drop the
    index first, so it fails if v21 relies on IF NOT EXISTS.
    """
    db = _bootstrap_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.isolation_level = None
    try:
        # Sanity: bootstrap left a plain (provider) index in place (no project_id yet).
        pre_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_dispatch_meta_provider'"
        ).fetchone()[0]
        assert "project_id" not in pre_sql

        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'")
        quality_db_init._migrate_v21(conn)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_dispatch_meta_provider'"
        ).fetchone()[0]
        assert "project_id" in sql and "provider" in sql
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Finding 2 — token extraction robustness (normalized + raw shapes)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["codex", "gemini", "kimi"])
def test_extract_token_usage_accepts_raw_shape(provider):
    class _R:
        token_usage = {"input_tokens": 1000, "output_tokens": 250, "cache_read_tokens": 10}
    usage = provider_dispatch._extract_token_usage(_R(), provider)
    assert usage == {"input": 1000, "output": 250, "cache_hit": 10}


@pytest.mark.parametrize("provider", ["codex", "gemini", "kimi"])
def test_extract_token_usage_accepts_normalized_shape(provider):
    """A normalized {input,output,cache_read} dict previously yielded 0 here."""
    class _R:
        token_usage = {"input": 800, "output": 200, "cache_read": 5}
    usage = provider_dispatch._extract_token_usage(_R(), provider)
    assert usage == {"input": 800, "output": 200, "cache_hit": 5}


# ---------------------------------------------------------------------------
# Finding 3 — cost resolution per provider (non-zero for metered lanes)
# ---------------------------------------------------------------------------

def test_compute_cost_resolves_nonzero_for_api_lanes():
    tu = {"input": 10000, "output": 5000, "cache_hit": 0}
    for provider, model in [
        ("claude", "opus"),
        ("codex", "gpt-5.2-codex"),
        ("gemini", "gemini-2.5-pro"),
        ("litellm:deepseek", "deepseek/deepseek-v4-pro"),
    ]:
        cost = provider_dispatch._compute_cost(provider, model, tu)
        assert cost is not None and cost > 0, f"{provider}/{model} resolved {cost}"


def test_compute_cost_rate_table_fallback_on_registry_miss():
    """When the registry has no entry, the provider_costs rate table resolves it."""
    from provider_costs import resolve_cost_usd
    # litellm:zai is in the rate table but not necessarily resolvable via registry
    cost = resolve_cost_usd("litellm:zai", "glm-5.1-default", 1_000_000, 1_000_000)
    assert cost is not None and cost > 0


def test_compute_cost_none_when_zero_tokens():
    """Zero tokens → None via the guard, AND the same provider/model resolves a
    real positive cost for non-zero tokens.

    Asserting only ``is None`` on zero tokens short-circuits at the zero-token
    guard and never exercises the codex cost path — it would pass even if codex
    pricing resolution were entirely broken. Pairing it with a non-zero call
    proves the None is specifically the zero-token guard, not a broken lookup,
    so the provider cost path is genuinely tested.
    """
    provider, model = "codex", "gpt-5.2-codex"
    assert provider_dispatch._compute_cost(
        provider, model, {"input": 0, "output": 0, "cache_hit": 0}
    ) is None
    nonzero = provider_dispatch._compute_cost(
        provider, model, {"input": 10_000, "output": 5_000, "cache_hit": 0}
    )
    assert nonzero is not None and nonzero > 0


# ---------------------------------------------------------------------------
# upsert helper — create-if-absent, non-clobber, negative paths
# ---------------------------------------------------------------------------

def test_upsert_creates_provider_stamped_row(tmp_path):
    db = _bootstrap_db(tmp_path)
    ok = upsert_dispatch_provider_row(
        db, dispatch_id="d-1", terminal="T2", provider="kimi",
        role="backend-developer", outcome_status="success",
        report_path="/tmp/r.md",
    )
    assert ok
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM dispatch_metadata WHERE dispatch_id='d-1'").fetchone()
        assert row["provider"] == "kimi"
        assert row["terminal"] == "T2"
        assert row["track"] == "headless"  # helper default when caller omits track
        assert row["outcome_status"] == "success"
        assert row["outcome_report_path"] == "/tmp/r.md"
    finally:
        conn.close()


def test_upsert_does_not_clobber_existing_richer_row(tmp_path):
    db = _bootstrap_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dispatch_metadata (dispatch_id, terminal, track, role, gate) "
        "VALUES ('d-2', 'T1', 'A', 'frontend-architect', 'codex_gate')"
    )
    conn.commit(); conn.close()
    # Upsert with provider; role/gate already set must be preserved (COALESCE)
    ok = upsert_dispatch_provider_row(
        db, dispatch_id="d-2", terminal="T1", provider="codex",
        role="should-not-override", gate="should-not-override",
    )
    assert ok
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM dispatch_metadata WHERE dispatch_id='d-2'").fetchone()
        assert row["provider"] == "codex"               # provider is authoritative
        assert row["role"] == "frontend-architect"      # preserved
        assert row["gate"] == "codex_gate"              # preserved
    finally:
        conn.close()


def test_upsert_returns_false_when_db_missing(tmp_path):
    assert upsert_dispatch_provider_row(
        tmp_path / "nope.db", dispatch_id="d-3", terminal="T1", provider="kimi"
    ) is False


@pytest.mark.parametrize("field", ["dispatch_id", "terminal", "provider"])
def test_upsert_raises_on_empty_required_field(tmp_path, field):
    db = _bootstrap_db(tmp_path)
    kwargs = dict(dispatch_id="d", terminal="T1", provider="kimi")
    kwargs[field] = ""
    with pytest.raises(ValueError):
        upsert_dispatch_provider_row(db, **kwargs)


# ---------------------------------------------------------------------------
# End-to-end — non-Claude dispatch through real _emit_governance
# ---------------------------------------------------------------------------

class _KimiResult:
    completion_text = "did the work"
    returncode = 0
    token_usage = {"input_tokens": 12000, "output_tokens": 3400, "cache_read_tokens": 0}

    def frontmatter_fields(self):
        u = self.token_usage
        return {
            "provider": "kimi", "sub_provider": "moonshot", "exit_code": 0,
            "token_usage": {"input": u["input_tokens"], "output": u["output_tokens"], "cache_read": 0},
        }


def test_emit_governance_non_claude_full_chain(tmp_path, monkeypatch):
    db = _bootstrap_db(tmp_path)
    state_dir = db.parent
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

    args = argparse.Namespace(
        dispatch_id="20260530-kimi-govtest", terminal_id="T2",
        instruction="do a thing", model="sonnet", role="backend-developer",
        pr_id=None, gate="",
    )
    now = datetime.now(timezone.utc)
    provider_dispatch._emit_governance(args, "kimi", "kimi-default", _KimiResult(), now, now, "success")

    # Receipt: non-zero tokens + populated report_path that exists
    receipt = json.loads((state_dir / "t0_receipts.ndjson").read_text().strip().splitlines()[-1])
    assert receipt["provider"] == "kimi"
    assert receipt["token_usage"] == {"input": 12000, "output": 3400, "cache_hit": 0}
    assert receipt["report_path"], "report_path must be populated"
    assert Path(receipt["report_path"]).exists()
    assert receipt["cost_usd"] is None or receipt["cost_usd"] >= 0

    # Provider-stamped dispatch_metadata row created
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM dispatch_metadata WHERE dispatch_id=?", (args.dispatch_id,)
        ).fetchone()
        assert row is not None, "non-Claude dispatch must create an intelligence row"
        assert row["provider"] == "kimi"
        assert row["terminal"] == "T2"
        assert row["track"] == "B"  # T2 -> Track B
        assert row["outcome_status"] == "success"
        assert row["outcome_report_path"] == receipt["report_path"]
    finally:
        conn.close()


def test_receipt_carries_report_path_field():
    """governance_emit.emit_dispatch_receipt must serialize a report_path field."""
    import inspect
    from governance_emit import emit_dispatch_receipt
    sig = inspect.signature(emit_dispatch_receipt)
    assert "report_path" in sig.parameters


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
