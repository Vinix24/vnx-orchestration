"""tests/test_context_manifest_report.py: `vnx context-manifest` reads the sidecar per provider (agents-als-map PR-4c).

The CLI is run through ``bin/vnx`` against a tmp store, so the subcommand wiring
and the script are both under test. The real central store is never read.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

OWN = "proj-own"


def _manifest(status: str = "ok", *, config_home: bool = True, user_skills: bool = False, hooks: bool = False) -> Dict[str, Any]:
    return {
        "schema": 1, "status": status,
        "instructions": {"files": [{"path": "~/.claude/CLAUDE.md", "type": "User", "chars": 10,
                                    "sha256": "a" * 64, "in_config_home": config_home, "seen": "initial"}]},
        "skills": {"initial": {"repo": [], "user": ["s1"] if user_skills else [], "synced": [],
                               "namespaced": [], "other": []}},
        "hooks": {"hooks": [{"event": "Stop", "name": "h", "count": 1}] if hooks else [], "total": 1 if hooks else 0},
    }


def _sidecar_line(dispatch_id: str, project_id: str, manifest: Dict[str, Any]) -> str:
    return json.dumps({"dispatch_id": dispatch_id, "session_id": "s-" + dispatch_id,
                       "project_id": project_id, "manifest": manifest},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _store(tmp_path: Path, lines: List[str], receipts: Optional[List[Dict[str, Any]]] = None) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (state / "context_manifests.ndjson").write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    (state / "t0_receipts.ndjson").write_text(
        "".join(json.dumps(r) + "\n" for r in (receipts or [])), encoding="utf-8")
    return state


def _run(state: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update({"VNX_PROJECT_ID": OWN, "VNX_MODE_GATING_ENABLED": "0"})
    return subprocess.run([str(REPO_ROOT / "bin" / "vnx"), "context-manifest", *args, "--state-dir", str(state)],
                          capture_output=True, text=True, env=env, timeout=60)


def _three_receipt_store(tmp_path: Path) -> Path:
    lines = [
        _sidecar_line("d1", OWN, _manifest("ok", user_skills=True, hooks=True)),
        _sidecar_line("d2", OWN, _manifest("partial", config_home=False)),
        _sidecar_line("d3", "proj-other", _manifest("ok")),
    ]
    receipts = [{"dispatch_id": "d1", "provider": "claude"}, {"dispatch_id": "d2", "provider": "claude"},
                {"dispatch_id": "d3", "provider": "codex"}]
    return _store(tmp_path, lines, receipts)


def test_per_provider_counts_shares_and_foreign(tmp_path: Path) -> None:
    result = _run(_three_receipt_store(tmp_path), "--json", "--by", "provider")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["lines"] == 3
    assert len(report["by_provider"]) == 2
    claude, codex = report["by_provider"]["claude"], report["by_provider"]["codex"]
    assert (claude["lines"], codex["lines"]) == (2, 1)
    assert claude["status"] == {"ok": 1, "partial": 1}
    assert claude["in_config_home"] == {"lines": 1, "share": 0.5}
    assert claude["user_skills"] == {"lines": 1, "share": 0.5}
    assert claude["hooks"] == {"lines": 1, "share": 0.5}
    assert codex["in_config_home"]["share"] == 1.0
    assert report["status"] == {"ok": 2, "partial": 1}
    assert report["foreign_project_lines"] == 1
    assert report["store_project_id"] == OWN


def test_unreadable_lines_counted_and_named(tmp_path: Path) -> None:
    good = _sidecar_line("d1", OWN, _manifest())
    no_project = json.dumps({"dispatch_id": "d9", "manifest": _manifest()})
    state = _store(tmp_path, [good, "{not json", "[1,2]", no_project, good],
                   [{"dispatch_id": "d1", "provider": "claude"}])
    report = json.loads(_run(state, "--json").stdout)
    assert report["unreadable_lines"] == [2, 3, 4]
    assert report["lines"] == 2
    assert report["foreign_project_lines"] == 0


def test_unknown_provider_when_no_receipt(tmp_path: Path) -> None:
    state = _store(tmp_path, [_sidecar_line("dx", OWN, _manifest())])
    report = json.loads(_run(state, "--json").stdout)
    assert list(report["by_provider"]) == ["unknown"]


def test_receipt_hash_without_sidecar_line_is_named(tmp_path: Path) -> None:
    line = _sidecar_line("d1", OWN, _manifest())
    sha = hashlib.sha256(line.encode("utf-8")).hexdigest()
    state = _store(tmp_path, [line], [
        {"dispatch_id": "d1", "provider": "claude", "context_manifest": {"schema": 1, "status": "ok", "sha256": sha}},
        {"dispatch_id": "d2", "provider": "claude", "context_manifest": {"schema": 1, "status": "ok", "sha256": "f" * 64}},
    ])
    report = json.loads(_run(state, "--json").stdout)
    assert report["receipt_hash_without_line"] == ["d2"]


def test_absent_sidecar_reports_zero_and_absent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    report = json.loads(_run(state, "--json").stdout)
    assert report["lines"] == 0 and report["sidecar_absent"] is True
    text = _run(state)
    assert text.returncode == 0
    assert "absent" in text.stdout and "lines: 0" in text.stdout


def test_text_output_names_providers_and_foreign(tmp_path: Path) -> None:
    out = _run(_three_receipt_store(tmp_path)).stdout
    assert "claude: lines=2" in out and "codex: lines=1" in out
    assert "foreign_project_lines: 1" in out


def test_unknown_flag_exits_2_and_names_it(tmp_path: Path) -> None:
    result = _run(_three_receipt_store(tmp_path), "--bogus")
    assert result.returncode == 2
    assert "--bogus" in result.stderr


def test_json_is_one_parseable_object(tmp_path: Path) -> None:
    out = _run(_three_receipt_store(tmp_path), "--json").stdout
    assert isinstance(json.loads(out), dict) and out.count("\n") == 1


def test_unresolvable_store_identity_fails_closed(tmp_path: Path) -> None:
    state = _three_receipt_store(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env["VNX_MODE_GATING_ENABLED"] = "0"
    result = subprocess.run([str(REPO_ROOT / "bin" / "vnx"), "context-manifest", "--json", "--state-dir", str(state)],
                            capture_output=True, text=True, env=env, timeout=60, cwd=str(tmp_path))
    assert result.returncode == 1
    assert "cannot build report" in result.stderr and result.stdout == ""


def test_report_never_prints_content_or_address(tmp_path: Path) -> None:
    out = _run(_three_receipt_store(tmp_path), "--json").stdout
    assert "@" not in out and "CLAUDE.md" not in out


def test_writes_nothing(tmp_path: Path) -> None:
    state = _three_receipt_store(tmp_path)
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    _run(state, "--json")
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before


def test_context_manifest_is_allowed_in_the_mode_tiers() -> None:
    import vnx_mode
    assert "context-manifest" in vnx_mode.TIER_STARTER_OPERATOR
