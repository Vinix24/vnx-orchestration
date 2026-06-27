#!/usr/bin/env python3
"""Tests for `vnx status` data-dir resolution (issue #225).

Dispatch-ID: 20260627-vnx-status-resolver

`vnx status` used `resolve_data_dir(__file__)`, which returns `$PROJECT_ROOT/.vnx-data` —
under a central install (`__file__` in ~/.vnx-system/versions/<v>/...) that resolves relative
to the INSTALL, missing the project's per-project central store, so status always printed
"not initialised". It now resolves via `vnx_paths.ensure_env()` (the same resolver `vnx doctor`
uses), so status reads `~/.vnx-data/<project_id>` — green where doctor is green.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "cli"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import vnx_status  # noqa: E402


def test_status_module_uses_ensure_env_not_caller_file_resolver():
    # The fix swaps the install-relative resolver for the per-project one.
    assert hasattr(vnx_status, "ensure_env")
    assert not hasattr(vnx_status, "resolve_data_dir")


def test_status_reads_ensure_env_data_dir(tmp_path, capsys, monkeypatch):
    # ensure_env resolves to a per-project store that HAS runtime state → status renders it,
    # never "not initialised".
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "t0_state.json").write_text("{}")
    monkeypatch.setattr(vnx_status, "ensure_env", lambda: {"VNX_DATA_DIR": str(tmp_path)})
    rc = vnx_status.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not initialised" not in out


def test_status_json_resolves_via_ensure_env(tmp_path, capsys, monkeypatch):
    # An empty store resolved via ensure_env → the not_initialised JSON payload. This proves the
    # JSON path consults the ensure_env-resolved dir (tmp_path has no strategy/ or t0_state.json).
    monkeypatch.setattr(vnx_status, "ensure_env", lambda: {"VNX_DATA_DIR": str(tmp_path)})
    rc = vnx_status.main(["--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["error"] == "not_initialised"


def test_status_honours_explicit_data_dir_arg(tmp_path, capsys, monkeypatch):
    # The explicit data_dir parameter still wins (ensure_env not consulted).
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        return {"VNX_DATA_DIR": "/should/not/be/used"}

    monkeypatch.setattr(vnx_status, "ensure_env", _boom)
    rc = vnx_status.main([], data_dir=tmp_path)
    assert rc == 0
    assert called["n"] == 0  # explicit arg short-circuits resolution


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
