"""OI-1553: staged dispatch bundles with no receipt and no worktree are shown as stale."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import build_t0_state as bts  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("VNX_STALE_STAGED_MINUTES", "VNX_USE_CENTRAL_DB"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env(tmp_path):
    state, root = tmp_path / "state", tmp_path / "root"
    state.mkdir()
    root.mkdir()
    (tmp_path / "dispatches" / "pending").mkdir(parents=True)
    return state, tmp_path / "dispatches", root


def _stage(dispatches: Path, did: str, age_min: float) -> None:
    d = dispatches / "pending" / did
    d.mkdir()
    spec = d / "dispatch-spec.json"
    spec.write_text("{}")
    t = datetime.now(timezone.utc).timestamp() - age_min * 60
    os.utime(spec, (t, t))


def _build(env):
    state, dispatches, root = env
    return bts._build_stale_staged(dispatches, state, project_root=root)


def test_stale_bundle_is_shown(env):
    _stage(env[1], "dlv-old", 45)
    out = _build(env)
    assert out["available"] is True and out["count"] == 1
    assert out["items"][0]["dispatch_id"] == "dlv-old"


def test_bundle_with_receipt_not_shown(env):
    _stage(env[1], "dlv-rec", 45)
    (env[0] / "t0_receipts.ndjson").write_text(
        "garbage\n" + json.dumps({"dispatch_id": "dlv-rec"}) + "\n"
    )
    assert _build(env)["count"] == 0


def test_bundle_with_worktree_not_shown(env):
    _stage(env[1], "dlv-wt", 45)
    (env[2] / ".vnx-data" / "worktrees" / "dispatch-dlv-wt").mkdir(parents=True)
    assert _build(env)["count"] == 0


def test_bundle_with_claim_not_shown(env):
    _stage(env[1], "dlv-claim", 45)
    claims = env[0] / "dispatch_worktree_claims"
    claims.mkdir()
    (claims / "dlv-claim.json").write_text("{}")
    assert _build(env)["count"] == 0


def test_fresh_bundle_not_shown(env):
    _stage(env[1], "dlv-new", 5)
    assert _build(env)["count"] == 0

def test_env_override_and_invalid_fallback(env, monkeypatch):
    _stage(env[1], "dlv-mid", 10)
    monkeypatch.setenv("VNX_STALE_STAGED_MINUTES", "5")
    out = _build(env)
    assert out["count"] == 1 and out["threshold_minutes"] == 5
    for bad in ("abc", "0", "-3"):
        monkeypatch.setenv("VNX_STALE_STAGED_MINUTES", bad)
        out = _build(env)
        assert out["threshold_minutes"] == 30 and "invalid" in out["threshold_note"]
        assert out["count"] == 0


def test_unreadable_receipts_is_unavailable(env):
    _stage(env[1], "dlv-x", 45)
    (env[0] / "t0_receipts.ndjson").mkdir()  # reading a directory raises OSError
    out = _build(env)
    assert out["available"] is False and out["reason"] and "items" not in out


def test_unreadable_pending_dir_is_unavailable(env, monkeypatch):
    real = Path.iterdir

    def boom(self):
        if self.name == "pending":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", boom)
    out = _build(env)
    assert out["available"] is False and "denied" in out["reason"]


def test_caps_items_and_reports_more(env):
    for i in range(13):
        _stage(env[1], f"dlv-{i:02d}", 40 + i)
    out = _build(env)
    assert out["count"] == 13 and len(out["items"]) == 10 and out["more"] == 3
    assert out["items"][0]["dispatch_id"] == "dlv-12"


def test_index_summary():
    sec = {"available": True, "count": 2, "items": [{"dispatch_id": "a", "age_minutes": 9}]}
    assert bts._stale_staged_index_summary(sec) == {"available": True, "count": 2, "oldest_id": "a"}
    for bad in (None, {}, {"available": False, "reason": "r"}, {"available": "yes"}):
        assert bts._stale_staged_index_summary(bad)["available"] is False


def test_build_t0_state_and_index_carry_section(env, monkeypatch):
    import dispatch_worktree_isolation as dwi

    state, dispatches, root = env
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(state.parent))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-test")
    monkeypatch.setattr(dwi, "resolve_consumer_project_root", lambda: root)
    _stage(dispatches, "dlv-e2e", 45)
    result = bts.build_t0_state(state, dispatches)
    assert [i["dispatch_id"] for i in result["stale_staged"]["items"]] == ["dlv-e2e"]
    assert bts._build_t0_index(result)["stale_staged"]["oldest_id"] == "dlv-e2e"
    assert len(json.dumps(result)) < 80 * 1024
