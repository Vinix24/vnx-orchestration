#!/usr/bin/env python3
"""``vnx main-window status``: exit codes and output, with a fake runner (no real gh,
no real git, no network) and a tmp state dir."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import main_window as mw  # noqa: E402
import main_window_cli as cli  # noqa: E402

BASE, M1 = "b" * 40, "1" * 40
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
RECENT = "2026-11-13T11:55:00Z"


def _runner(runs, *, gh_down=False):
    def run(argv, *, timeout, cwd=None):
        if gh_down:
            raise FileNotFoundError("gh")
        if argv[1] == "api" and "/commits/" in argv[2]:
            return SimpleNamespace(returncode=0, stdout="scripts/x.py\n", stderr="")
        if argv[1] == "api":
            return SimpleNamespace(returncode=0, stdout=f"{M1}\t{RECENT}\n{BASE}\t{RECENT}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(runs), stderr="")

    return run


def _run(sha, run_id, status="completed", conclusion="success"):
    return {"databaseId": run_id, "headSha": sha, "status": status, "conclusion": conclusion,
            "createdAt": RECENT, "attempt": 1}


@pytest.fixture(autouse=True)
def _clean_keys(monkeypatch):
    for key in (mw.UNVERIFIED_MAX_KEY, mw.RUN_MAX_AGE_KEY):
        monkeypatch.delenv(key, raising=False)


def _status(tmp_path, runs, *args, gh_down=False):
    lines = []
    code = cli.main(["status", *args], runner=_runner(runs, gh_down=gh_down), now=NOW, project_root=tmp_path,
                    state_dir=tmp_path / "state", project_id=None, out=lines.append)
    return code, "\n".join(lines)


def test_exit_0_on_go_with_mode_off(tmp_path):
    code, text = _status(tmp_path, [_run(BASE, 1), _run(M1, 2, "in_progress", None)])
    assert code == 0
    assert "mode: off" in text and "niet geconfigureerd" in text and M1 in text and BASE in text


def test_exit_1_on_nogo(tmp_path, monkeypatch):
    monkeypatch.setenv(mw.UNVERIFIED_MAX_KEY, "1")
    code, text = _status(tmp_path, [_run(BASE, 1), _run(M1, 2, "in_progress", None)])
    assert code == 1
    assert "verdict: NO-GO" in text and "unverified_limit" in text


def test_a_red_run_names_the_first_red_in_the_summary(tmp_path, monkeypatch):
    monkeypatch.setenv(mw.UNVERIFIED_MAX_KEY, "3")
    code, text = _status(tmp_path, [_run(BASE, 1), _run(M1, 2, conclusion="failure")])
    assert code == 1 and f"first red: {M1} run 2" in text


def test_json_output_has_sorted_keys_and_the_record(tmp_path, monkeypatch):
    monkeypatch.setenv(mw.UNVERIFIED_MAX_KEY, "3")
    code, text = _status(tmp_path, [_run(BASE, 1), _run(M1, 2, "in_progress", None)], "--json")
    record = json.loads(text)
    assert code == 0 and list(record) == sorted(record)
    assert (record["mode"], record["max_unverified"], record["max_age_min"]) == ("on", 3, 40)
    assert record["running_shas"] == [M1] and record["base_sha"] == BASE
    assert text == json.dumps(record, sort_keys=True, indent=2)


def test_an_invalid_key_value_is_named_and_reads_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv(mw.UNVERIFIED_MAX_KEY, "0")
    code, text = _status(tmp_path, [_run(BASE, 1)])
    assert code == 0 and "invalid_config" in text and "mode: off" in text


def test_gh_down_with_the_gate_on_is_nogo_not_go(tmp_path, monkeypatch):
    monkeypatch.setenv(mw.UNVERIFIED_MAX_KEY, "1")
    code, text = _status(tmp_path, [], gh_down=True)
    assert code == 1 and "commits_gh_missing" in text


def test_exit_2_when_the_probe_cannot_run(tmp_path, capsys):
    def broken(*_a, **_k):
        raise RuntimeError("no store")

    code = cli.main(["status"], runner=broken, now=NOW, project_root=tmp_path, state_dir=tmp_path,
                    project_id=None)
    assert code == 2
    assert "could not run" in capsys.readouterr().err


def test_exit_2_on_a_bad_command(capsys):
    assert cli.main(["nonsense"]) == 2


def test_the_probe_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(mw.UNVERIFIED_MAX_KEY, "1")
    before = sorted(p.name for p in tmp_path.rglob("*"))
    _status(tmp_path, [_run(BASE, 1)])
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
