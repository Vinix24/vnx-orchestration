"""F2 part 2: scripts/panel.py keeps client and personal content on the codex and claude seats.

Roots are tmp directories written into a tmp boundary file. The dispatcher is a recorder, so
no provider and no claude process starts.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import deliberation_panel as dp
import plan_gate_panel

FULL_FLEET = {"codex", "kimi", "claude", "glm-harness", "deepseek-harness"}


def _load_panel_cli():
    spec = importlib.util.spec_from_file_location("panel_cli_boundary", REPO_ROOT / "scripts" / "panel.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, provider, model, prompt, did):
        self.calls.append(provider)
        return f"seat {provider} says something real about {did}"

    @property
    def providers(self) -> set[str]:
        return set(self.calls)


@pytest.fixture
def run(tmp_path, monkeypatch):
    """A panel runner: returns (panel module, recorder, factory kwargs, helpers)."""
    client_root = tmp_path / "client-root"
    personal_root = tmp_path / "personal-root"
    client = client_root / "group" / "acme"
    fabric = tmp_path / "fabric-checkout"
    for directory in (client, personal_root, fabric):
        directory.mkdir(parents=True)
    (fabric / ".vnx-project-id").write_text("vnx-dev\n", encoding="utf-8")
    boundary = tmp_path / "content_boundary.json"
    boundary.write_text(json.dumps({
        "version": 1,
        "client_roots": [str(client_root)],
        "personal_roots": [str(personal_root)],
        "client_project_ids": ["pacompany-engine"],
        "provider_exceptions": {"pacompany-engine": ["deepseek"]},
    }), encoding="utf-8")
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(boundary))
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(fabric))
    monkeypatch.setattr(dp, "_default_receipts_path",
                        lambda: tmp_path / "no-ledger" / "t0_receipts.ndjson", raising=False)
    monkeypatch.setattr(plan_gate_panel, "load_synthesis_min_seats", lambda: None)
    for key in ("VNX_OVERRIDE_CONTENT_BOUNDARY", "VNX_OVERRIDE_CONTENT_BOUNDARY_REASON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(fabric)

    panel = _load_panel_cli()
    recorder = _Recorder()
    factory_calls: list[dict] = []

    def factory(data_dir, timeout, role=None, **kwargs):
        factory_calls.append(kwargs)
        return recorder

    monkeypatch.setattr(plan_gate_panel, "_make_default_dispatcher", factory)
    return SimpleNamespace(panel=panel, recorder=recorder, factory_calls=factory_calls,
                           client=client, personal=personal_root, fabric=fabric, tmp=tmp_path,
                           out=tmp_path / "report.md")


def _main(run, *extra: str, question: str = "assess the proposal") -> int:
    return run.panel.main(["strategy", question, "--out", str(run.out), *extra])


def test_b7_client_cwd_without_seats_runs_only_codex_and_claude(run, monkeypatch, capsys):
    monkeypatch.chdir(run.client)
    assert _main(run) == 0
    assert run.recorder.providers == {"codex", "claude"}
    err = capsys.readouterr().err
    assert "client content" in err and "cwd (client_root)" in err and "codex, claude" in err
    assert str(run.client) not in err
    assert run.factory_calls == [{}]
    report = run.out.read_text(encoding="utf-8")
    assert "**Content boundary:**" in report and "seats limited to: codex, claude" in report


def test_personal_cwd_without_seats_runs_only_codex_and_claude(run, monkeypatch):
    monkeypatch.chdir(run.personal)
    assert _main(run) == 0
    assert run.recorder.providers == {"codex", "claude"}


def test_context_file_in_a_client_folder_restricts_a_fabric_cwd(run):
    context = run.client / "brief.md"
    context.write_text("background", encoding="utf-8")
    assert _main(run, "--context-file", str(context)) == 0
    assert run.recorder.providers == {"codex", "claude"}


def test_out_path_in_a_client_folder_restricts_a_fabric_cwd(run):
    run.out = run.client / "panel.md"
    assert _main(run) == 0
    assert run.recorder.providers == {"codex", "claude"}


def test_question_naming_a_client_path_restricts_a_fabric_cwd(run):
    assert _main(run, question=f"assess {run.client}/offer.md") == 0
    assert run.recorder.providers == {"codex", "claude"}


def test_b8_explicit_external_seat_from_a_client_cwd_is_rc_2(run, monkeypatch, capsys):
    monkeypatch.chdir(run.client)
    assert _main(run, "--seats", "codex,kimi") == 2
    assert run.recorder.calls == []
    assert run.factory_calls == []
    err = capsys.readouterr().err
    assert "kimi" in err and "--allow-external-seats" in err


def test_b8_allow_external_seats_dispatches_with_override_env_and_names_it_in_the_report(run, monkeypatch):
    monkeypatch.chdir(run.client)
    assert _main(run, "--seats", "codex,kimi", "--allow-external-seats", "operator wants kimi") == 0
    assert run.recorder.providers == {"codex", "kimi"}
    assert run.factory_calls == [{"extra_env": {
        "VNX_OVERRIDE_CONTENT_BOUNDARY": "1",
        "VNX_OVERRIDE_CONTENT_BOUNDARY_REASON": "operator wants kimi",
    }}]
    report = run.out.read_text(encoding="utf-8")
    assert "external seats allowed by --allow-external-seats: kimi" in report
    assert "Reason: operator wants kimi" in report


def test_allow_external_seats_without_seats_runs_the_full_fleet(run, monkeypatch):
    monkeypatch.chdir(run.client)
    assert _main(run, "--allow-external-seats", "full panel") == 0
    assert run.recorder.providers == FULL_FLEET


def test_override_never_reaches_this_process_environment(run, monkeypatch):
    import os
    monkeypatch.chdir(run.client)
    assert _main(run, "--seats", "kimi", "--allow-external-seats", "because") == 0
    assert "VNX_OVERRIDE_CONTENT_BOUNDARY" not in os.environ
    assert "VNX_OVERRIDE_CONTENT_BOUNDARY_REASON" not in os.environ


def test_blank_reason_is_rc_2(run, monkeypatch):
    monkeypatch.chdir(run.client)
    assert _main(run, "--allow-external-seats", "  ") == 2
    assert run.recorder.calls == []


def test_allow_external_seats_in_a_fabric_cwd_changes_nothing(run):
    assert _main(run, "--allow-external-seats", "not needed") == 0
    assert run.recorder.providers == FULL_FLEET
    assert run.factory_calls == [{}]
    assert "Content boundary" not in run.out.read_text(encoding="utf-8")


def test_b9_fabric_cwd_default_is_still_the_full_fleet(run):
    assert _main(run) == 0
    assert run.recorder.providers == FULL_FLEET


def test_b9_fabric_cwd_still_accepts_an_external_seat(run):
    assert _main(run, "--seats", "kimi,claude") == 0
    assert run.recorder.providers == {"kimi", "claude"}


def test_unconfigured_boundary_keeps_the_full_fleet(run, monkeypatch):
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(run.tmp / "missing.json"))
    monkeypatch.chdir(run.client)
    assert _main(run) == 0
    assert run.recorder.providers == FULL_FLEET


def test_project_id_exception_keeps_the_excepted_seat(run, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "pacompany-engine")
    assert _main(run) == 0
    assert run.recorder.providers == {"codex", "claude", "deepseek-harness"}


def test_seat_dispatcher_puts_extra_env_in_the_subprocess_only(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(plan_gate_panel.subprocess, "run", fake_run)
    monkeypatch.setattr(plan_gate_panel, "_read_report", lambda base, did, stderr: "report text")
    dispatch = plan_gate_panel._make_default_dispatcher(
        str(tmp_path / "data"), 60, role="deliberation-panelist",
        extra_env={"VNX_OVERRIDE_CONTENT_BOUNDARY": "1", "VNX_OVERRIDE_CONTENT_BOUNDARY_REASON": "why"})
    assert dispatch("kimi", "kimi-k3", "question", "did-1") == "report text"
    assert seen["env"]["VNX_OVERRIDE_CONTENT_BOUNDARY"] == "1"
    assert seen["env"]["VNX_OVERRIDE_CONTENT_BOUNDARY_REASON"] == "why"
    import os
    assert "VNX_OVERRIDE_CONTENT_BOUNDARY" not in os.environ


def test_seat_dispatcher_without_extra_env_carries_no_override(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(plan_gate_panel.subprocess, "run", fake_run)
    monkeypatch.setattr(plan_gate_panel, "_read_report", lambda base, did, stderr: "report text")
    dispatch = plan_gate_panel._make_default_dispatcher(str(tmp_path / "data"), 60,
                                                        role="deliberation-panelist")
    dispatch("kimi", "kimi-k3", "question", "did-2")
    assert "VNX_OVERRIDE_CONTENT_BOUNDARY" not in seen["env"]
