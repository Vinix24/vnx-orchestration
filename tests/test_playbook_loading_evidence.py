"""The drift meter's playbook axis and the loading-evidence reader (skills-port SP-4, ADR-058).

Present is not loaded: a pointer block says the bytes are there, only a valid record in
`playbook_loading.ndjson` says a provider read them. Everything runs in tmp_path with
HOME, VNX_HOME and the data dir pointed there; the real store and ~/.vnx-system are untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import fleet_role_drift as frd
import playbook_loading_evidence as ple
import t0_playbook as tp

CANON = "# T0\n\nYou are T0.\n"
PROJECT_ID = "sp4-proj"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A hermetic fabric: one playbook at the fabric step, a data dir, a project id."""
    fabric = tmp_path / "fabric"
    (fabric / "scripts" / "lib").mkdir(parents=True)
    (fabric / "scripts" / "lib" / "vnx_paths.py").write_text("", encoding="utf-8")
    skill = fabric / "skills" / "t0-orchestrator" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("playbook v1\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VNX_HOME", raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setattr(tp, "_scripts_root_candidates", lambda *_a: [fabric / "scripts"])
    return {"skill": skill, "state": data / "state", "fabric": fabric, "tmp": tmp_path}


def _project(root: Path, *, agents: str | None = None, gemini: str | None = None) -> Path:
    t0 = root / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    (t0 / "role-orchestrator.md").write_text(CANON, encoding="utf-8")
    (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n", encoding="utf-8")
    for name, extra in (("AGENTS.md", agents), ("GEMINI.md", gemini)):
        if extra is not None:
            body = f"{frd.ROLE_MARKER_BEGIN}\n{CANON}\n{frd.ROLE_MARKER_END}\n{extra}"
            (t0 / name).write_text(body, encoding="utf-8")
    return root


def _pointer(world, **over) -> str:
    cur = tp.resolve(world["tmp"] / "x")
    fields = {"path": cur.path, "source_id": cur.source_id, "sha256": cur.sha256, **over}
    return (f"{tp.BEGIN_MARKER}\npath: {fields['path']}\nsource_id: {fields['source_id']}\n"
            f"sha256: {fields['sha256']}\n{tp.END_MARKER}\n")


def _record(world, provider="codex", **over) -> dict:
    cur = tp.resolve(world["tmp"] / "x")
    rec = {"schema": 1, "provider": provider, "project_id": PROJECT_ID,
           "source_id": cur.source_id, "path": cur.path, "sha256": cur.sha256,
           "observed_at": "2026-11-08T10:00:00Z", "evidence": "session transcript"}
    rec.update(over)
    return rec


def _write_evidence(world, lines: list) -> None:
    (world["state"] / ple.EVIDENCE_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _playbook(world, project: Path) -> dict:
    return frd.measure_project(project, CANON)["playbook"]


def _cli_report(world, project: Path, capsys) -> tuple:
    canon = world["tmp"] / "canon.md"
    canon.write_text(CANON, encoding="utf-8")
    rc = frd.main(["--canon", str(canon), "--project-dir", str(project), "--json"])
    return rc, json.loads(capsys.readouterr().out)["projects"][project.name]


# --- the playbook states ---------------------------------------------------


def test_absent_when_a_provider_file_has_the_role_block_but_no_playbook_block(world, tmp_path, capsys):
    project = _project(tmp_path / "p", agents="")
    rc, report = _cli_report(world, project, capsys)
    assert report.get("playbook", {}).get("status") == "absent"
    assert report["behind"] is True
    assert rc == 1


def test_ok_pointer_is_current_on_this_axis(world, tmp_path, capsys):
    project = _project(tmp_path / "p", agents=_pointer(world), gemini=_pointer(world))
    rc, report = _cli_report(world, project, capsys)
    pb = report["playbook"]
    assert pb["status"] == "ok" and pb["source_id"] == "fabric" and pb["degraded"] is False
    assert pb["path"] == str(world["skill"].resolve())
    assert frd._playbook_behind(pb) is False
    assert rc in (0, 1)  # startup (no hook in this fixture) decides the exit here, not the playbook axis


def test_stale_sha_after_the_file_is_edited(world, tmp_path):
    project = _project(tmp_path / "p", agents=_pointer(world))
    world["skill"].write_text("playbook v2\n", encoding="utf-8")
    pb = _playbook(world, project)
    assert pb["status"] == "stale_sha"
    assert frd.measure_project(project, CANON)["behind"] is True


def test_stale_source_when_the_stored_source_differs(world, tmp_path):
    project = _project(tmp_path / "p", agents=_pointer(world, source_id="installed"))
    pb = _playbook(world, project)
    assert pb["status"] == "stale_source"
    assert frd.measure_project(project, CANON)["behind"] is True


def test_worst_file_decides_the_aggregate(world, tmp_path):
    project = _project(tmp_path / "p", agents=_pointer(world), gemini="")
    assert _playbook(world, project)["status"] == "absent"


def test_no_provider_file_stays_a_coverage_gap_not_drift(world, tmp_path):
    project = _project(tmp_path / "p")
    pb = _playbook(world, project)
    assert pb["files"] == {} and pb["status"] == "absent"
    assert not frd._playbook_behind(pb)


def test_all_sources_missing_is_behind_with_the_probed_paths(world, tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "_scripts_root_candidates", lambda *_a: [])
    project = _project(tmp_path / "p")
    report = frd.measure_project(project, CANON)
    pb = report["playbook"]
    assert pb["status"] == "missing" and pb["source_missing"] is True
    assert len(pb["probed"]) == 5 and pb["probed"][0].startswith("fabric:")
    assert pb["loading"] == "unverified"
    assert all(pb[p]["loading"] == "unverified" for p in ple.PROVIDERS)
    assert report["behind"] is True


def test_missing_source_exit_code_is_one(world, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tp, "_scripts_root_candidates", lambda *_a: [])
    project = _project(tmp_path / "p", agents=_pointer(world))
    rc, report = _cli_report(world, project, capsys)
    assert report["playbook"]["status"] == "missing"
    assert rc == 1


# --- the evidence reader ---------------------------------------------------


def _loading(world, project) -> dict:
    pb = _playbook(world, project)
    return {p: pb[p]["loading"] for p in ple.PROVIDERS}


def test_loading_is_a_value_for_every_provider_without_evidence(world, tmp_path):
    project = _project(tmp_path / "p", agents=_pointer(world))
    assert _loading(world, project) == {"claude": "unverified", "codex": "unverified", "kimi": "unverified"}


@pytest.mark.parametrize("line", [
    "null",
    "{not json",
    json.dumps({"schema": 1, "provider": "gpt", "project_id": PROJECT_ID}),
    "",
])
def test_bad_evidence_lines_stay_unverified(world, tmp_path, line):
    project = _project(tmp_path / "p", agents=_pointer(world))
    _write_evidence(world, [line])
    assert set(_loading(world, project).values()) == {"unverified"}


def test_unknown_provider_with_otherwise_valid_fields_is_unverified(world, tmp_path):
    project = _project(tmp_path / "p")
    _write_evidence(world, [json.dumps(_record(world, provider="gemini"))])
    assert set(_loading(world, project).values()) == {"unverified"}


def test_a_valid_record_loads_only_its_own_provider(world, tmp_path):
    project = _project(tmp_path / "p", agents=_pointer(world))
    _write_evidence(world, [json.dumps(_record(world, "codex"))])
    assert _loading(world, project) == {"claude": "unverified", "codex": "loaded", "kimi": "unverified"}
    assert _playbook(world, project)["loading"] == "unverified"


def test_a_bad_line_does_not_hide_a_valid_neighbour(world, tmp_path):
    project = _project(tmp_path / "p")
    _write_evidence(world, ["null", "{x", json.dumps(_record(world, "kimi"))])
    assert _loading(world, project)["kimi"] == "loaded"


def test_changed_sha_invalidates_the_record(world, tmp_path):
    project = _project(tmp_path / "p")
    _write_evidence(world, [json.dumps(_record(world, "codex"))])
    world["skill"].write_text("playbook v2\n", encoding="utf-8")
    assert _loading(world, project)["codex"] == "unverified"


@pytest.mark.parametrize("field,value", [
    ("project_id", "other-proj"),
    ("source_id", "installed"),
    ("path", "/elsewhere/SKILL.md"),
    ("sha256", "z" * 64),
    ("sha256", None),
    ("observed_at", "yesterday"),
    ("observed_at", None),
    ("schema", 2),
])
def test_any_mismatching_or_malformed_field_is_unverified(world, tmp_path, field, value):
    project = _project(tmp_path / "p")
    _write_evidence(world, [json.dumps(_record(world, "codex", **{field: value}))])
    assert _loading(world, project)["codex"] == "unverified"


def test_missing_source_never_reads_loaded_even_with_a_record(world, tmp_path, monkeypatch):
    project = _project(tmp_path / "p")
    _write_evidence(world, [json.dumps(_record(world, "codex"))])
    monkeypatch.setattr(tp, "_scripts_root_candidates", lambda *_a: [])
    assert _loading(world, project)["codex"] == "unverified"


def test_unresolvable_project_id_is_unverified(world, tmp_path, monkeypatch):
    project = _project(tmp_path / "p")
    _write_evidence(world, [json.dumps(_record(world, "codex"))])
    monkeypatch.setattr(ple, "resolve_project_state", lambda _r: (None, None))
    assert _loading(world, project)["codex"] == "unverified"


def test_reader_leaves_the_evidence_file_untouched(world, tmp_path):
    project = _project(tmp_path / "p")
    _write_evidence(world, [json.dumps(_record(world, "codex"))])
    before = (world["state"] / ple.EVIDENCE_FILENAME).read_bytes()
    _loading(world, project)
    assert (world["state"] / ple.EVIDENCE_FILENAME).read_bytes() == before
    assert sorted(p.name for p in world["state"].iterdir()) == [ple.EVIDENCE_FILENAME]
