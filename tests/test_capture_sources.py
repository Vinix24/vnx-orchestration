"""Capture PR-2: project-bound sources, fail-closed per source.

Every fixture lives under tmp_path and HOME points there, so nothing touches
~/.claude or ~/.vnx-data. The CLI tests start the real script as a subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import capture_sources as cs  # noqa: E402

SCRIPT = _REPO_ROOT / "scripts" / "capture_audit.py"
PROJECT = "proj-a"
SECRET_TITLE = "geheime-titel-van-de-nugget"
SECRET_BODY = "geheime lopende tekst van de nugget"
SECRET_FOREIGN = "klantwerk-titel-van-ander-project"
SECRET_NOTE = "geheime-notitienaam"
SECRET_DESC = "geheime beschrijving van de notitie"
SECRET_OI = "geheime open item titel"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = home / ".vnx-data" / PROJECT / "state"
    memory = home / ".claude" / "projects" / "-x" / "memory"
    bucket = tmp_path / "bucket"
    for d in (state, memory, bucket):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "state": state, "memory": memory, "bucket": bucket, "tmp": tmp_path}


def _binding(env, **override):
    data = {
        "schema": cs.BINDING_SCHEMA,
        "project_id": PROJECT,
        "memory_dir": str(env["memory"]),
        "bucket_dir": str(env["bucket"]),
        "bucket_slug": "mine",
        "bound_at": "2026-11-10T00:00:00+00:00",
    }
    data.update(override)
    (env["state"] / cs.BINDING_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    return data


def _month() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _fill(env):
    (env["memory"] / f"{SECRET_NOTE}.md").write_text(
        f"---\nname: {SECRET_NOTE}\ndescription: {SECRET_DESC}\nmetadata:\n  modified: 2026-11-09T10:00:00Z\n---\n"
        "Zie OI-12 en [[andere-notitie|alias]].\n", encoding="utf-8")
    (env["memory"] / "kaal.md").write_text("zonder frontmatter OI-7\n", encoding="utf-8")
    (env["memory"] / "MEMORY.md").write_text("- index\n", encoding="utf-8")
    (env["bucket"] / f"{_month()}.md").write_text(
        "# Bucket\n\n"
        f"### 2026-11-01 · mine · {SECRET_TITLE}\n- **wat:** {SECRET_BODY} OI-99\n\n"
        f"### 2026-11-02 · other · {SECRET_FOREIGN}\n- **wat:** vreemd\n\n"
        f"### 2026-11-03 · third · nog-een-vreemde\n- x\n", encoding="utf-8")
    (env["state"] / "open_items.json").write_text(json.dumps({"items": [
        {"id": "OI-1", "status": "open", "title": SECRET_OI, "created_at": "2026-11-01T10:00:00"},
        {"id": "OI-2", "status": "open", "title": "vreemd", "project_id": "proj-b"},
        {"id": "OI-3", "status": "done", "title": "eigen", "project_id": PROJECT},
    ]}), encoding="utf-8")


def _run(*args, home):
    proc_env = {**os.environ, "HOME": str(home)}
    for key in ("VNX_PROJECT_ID", "VNX_DISPATCH_PATHS", "VNX_ENFORCE_WORKER_PERMISSIONS"):
        proc_env.pop(key, None)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=proc_env, cwd=str(home))


# --- load_binding: every unusable binding is unbound, never ok --------------

def test_binding_ok(env):
    _binding(env)
    binding, status = cs.load_binding(env["state"])
    assert status == "ok" and binding["bucket_slug"] == "mine"


def test_binding_file_missing_is_unbound(env):
    binding, status = cs.load_binding(env["state"])
    assert binding is None and cs.state_of(status) == "unbound"


@pytest.mark.parametrize("content", ["{not json", "[]", "null", '"text"', ""])
def test_binding_corrupt_is_unbound(env, content):
    (env["state"] / cs.BINDING_FILENAME).write_text(content, encoding="utf-8")
    binding, status = cs.load_binding(env["state"])
    assert binding is None and cs.state_of(status) == "unbound"


def test_binding_project_id_missing_is_unbound(env):
    data = _binding(env)
    del data["project_id"]
    (env["state"] / cs.BINDING_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    assert cs.state_of(cs.load_binding(env["state"])[1]) == "unbound"


@pytest.mark.parametrize("value", ["proj-b", "", None, 5])
def test_binding_project_id_different_is_unbound(env, value):
    _binding(env, project_id=value)
    binding, status = cs.load_binding(env["state"])
    assert binding is None and cs.state_of(status) == "unbound"


@pytest.mark.parametrize("field", ["memory_dir", "bucket_dir", "bucket_slug", "schema"])
@pytest.mark.parametrize("value", [None, "", 3, "  "])
def test_binding_bad_field_is_unbound(env, field, value):
    _binding(env, **{field: value})
    binding, _status = cs.load_binding(env["state"])
    assert binding is None


def test_binding_empty_derived_project_is_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = tmp_path / "plain" / "state"
    state.mkdir(parents=True)
    (state / cs.BINDING_FILENAME).write_text(json.dumps({"schema": cs.BINDING_SCHEMA, "project_id": PROJECT}))
    assert cs.state_of(cs.load_binding(state)[1]) == "unbound"


def test_binding_unknown_schema_version_is_unbound(env):
    _binding(env, schema="capture_sources/9")
    assert cs.state_of(cs.load_binding(env["state"])[1]) == "unbound"


def test_binding_memory_dir_outside_projects_is_unbound(env):
    outside = env["tmp"] / "elsewhere"
    outside.mkdir()
    _binding(env, memory_dir=str(outside))
    assert cs.state_of(cs.load_binding(env["state"])[1]) == "unbound"


def test_binding_memory_dir_symlink_resolving_outside_is_unbound(env):
    outside = env["tmp"] / "elsewhere"
    outside.mkdir()
    link = env["home"] / ".claude" / "projects" / "link"
    link.symlink_to(outside)
    _binding(env, memory_dir=str(link))
    assert cs.state_of(cs.load_binding(env["state"])[1]) == "unbound"


# --- memory notes ------------------------------------------------------------

def test_memory_notes_read(env):
    _fill(env)
    notes, status = cs.read_memory_notes(_binding(env))
    assert status == "ok"
    by_key = {n["key"]: n for n in notes}
    assert set(by_key) == {SECRET_NOTE, "kaal"}
    full = by_key[SECRET_NOTE]
    assert full["body_refs"] == {"oi": ["OI-12"], "memory": ["andere-notitie"]}
    assert full["modified_source"] == "frontmatter" and full["modified"].startswith("2026-11-09T10:00:00")
    assert by_key["kaal"]["modified_source"] == "mtime"


def test_memory_notes_direct_binding_outside_tree_is_error(env):
    outside = env["tmp"] / "elsewhere"
    outside.mkdir()
    (outside / "n.md").write_text("x", encoding="utf-8")
    notes, status = cs.read_memory_notes(_binding(env, memory_dir=str(outside)))
    assert notes == [] and cs.state_of(status) == "error"


def test_memory_notes_symlink_dir_outside_is_error(env):
    outside = env["tmp"] / "elsewhere"
    outside.mkdir()
    (outside / "n.md").write_text("x", encoding="utf-8")
    link = env["home"] / ".claude" / "projects" / "link"
    link.symlink_to(outside)
    notes, status = cs.read_memory_notes(_binding(env, memory_dir=str(link)))
    assert notes == [] and cs.state_of(status) == "error"


def test_memory_note_symlinked_outside_is_error(env):
    outside = env["tmp"] / "secret.md"
    outside.write_text("buiten", encoding="utf-8")
    (env["memory"] / "stiekem.md").symlink_to(outside)
    notes, status = cs.read_memory_notes(_binding(env))
    assert notes == [] and cs.state_of(status) == "error"


def test_memory_dir_missing_or_unreadable_note_is_error(env):
    (env["memory"] / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    notes, status = cs.read_memory_notes(_binding(env))
    assert notes == [] and cs.state_of(status) == "error"
    missing = env["home"] / ".claude" / "projects" / "gone"
    notes, status = cs.read_memory_notes(_binding(env, memory_dir=str(missing)))
    assert notes == [] and cs.state_of(status) == "error"


def test_memory_notes_without_memory_dir_key_is_error():
    assert cs.state_of(cs.read_memory_notes({})[1]) == "error"


# --- bucket ------------------------------------------------------------------

def test_bucket_reads_only_bound_slug_and_counts_foreign(env):
    _fill(env)
    entries, status, foreign = cs.read_bucket_entries(_binding(env), [_month()])
    assert status == "ok" and foreign == 2
    assert len(entries) == 1
    assert entries[0]["date"] == "2026-11-01"
    assert entries[0]["body_refs"]["oi"] == ["OI-99"]
    blob = json.dumps(entries)
    assert SECRET_FOREIGN not in blob and "nog-een-vreemde" not in blob


def test_bucket_unknown_slug_reads_nothing_and_counts_all(env):
    _fill(env)
    entries, status, foreign = cs.read_bucket_entries(_binding(env, bucket_slug="nope"), [_month()])
    assert entries == [] and status == "ok" and foreign == 3


def test_bucket_malformed_heading_is_never_read_as_ours(env):
    (env["bucket"] / "2026-11.md").write_text("### geen datum · mine · titel\n- x\n", encoding="utf-8")
    entries, status, foreign = cs.read_bucket_entries(_binding(env), ["2026-11"])
    assert entries == [] and foreign == 1 and status == "ok"


def test_bucket_month_file_absent_is_absent_not_empty_ok(env):
    entries, status, foreign = cs.read_bucket_entries(_binding(env), ["2020-01"])
    assert entries == [] and status == "absent" and foreign == 0


@pytest.mark.parametrize("months", [[], None, ["2026-13"], ["../x"], ["2026-1"], [5]])
def test_bucket_bad_months_is_error(env, months):
    assert cs.state_of(cs.read_bucket_entries(_binding(env), months)[1]) == "error"


def test_bucket_dir_missing_or_unreadable_is_error(env):
    gone = _binding(env, bucket_dir=str(env["tmp"] / "gone"))
    assert cs.state_of(cs.read_bucket_entries(gone, ["2026-11"])[1]) == "error"
    (env["bucket"] / "2026-11.md").write_bytes(b"\xff\xfe\x00")
    assert cs.state_of(cs.read_bucket_entries(_binding(env), ["2026-11"])[1]) == "error"
    assert cs.state_of(cs.read_bucket_entries({}, ["2026-11"])[1]) == "error"


# --- open items --------------------------------------------------------------

def test_open_items_absent_is_not_an_empty_list(env):
    items, status, foreign = cs.read_open_items(env["state"])
    assert items == [] and status == "absent" and foreign == 0


def test_open_items_foreign_project_skipped_and_counted(env):
    _fill(env)
    items, status, foreign = cs.read_open_items(env["state"])
    assert status == "ok" and foreign == 1
    assert [i["id"] for i in items] == ["OI-1", "OI-3"]


@pytest.mark.parametrize("content", ["{broken", "[]", '{"items": null}', '{"items": ["x"]}', '{}'])
def test_open_items_corrupt_or_malformed_is_error(env, content):
    (env["state"] / "open_items.json").write_text(content, encoding="utf-8")
    items, status, _ = cs.read_open_items(env["state"])
    assert items == [] and cs.state_of(status) == "error"


def test_open_items_without_derivable_project_is_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = tmp_path / "plain" / "state"
    state.mkdir(parents=True)
    (state / "open_items.json").write_text('{"items": []}')
    assert cs.state_of(cs.read_open_items(state)[1]) == "unbound"


# --- CLI ---------------------------------------------------------------------

def test_cli_sources_unbound_without_binding(env):
    proc = _run("sources", "--state-dir", str(env["state"]), "--json", home=env["home"])
    assert proc.returncode == 0, f"proc.returncode == 0: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert {row["state"] for row in data["sources"].values()} == {"unbound"}


def test_cli_sources_json_reports_states_and_counts_without_content(env):
    _fill(env)
    _binding(env)
    proc = _run("sources", "--state-dir", str(env["state"]), "--json", home=env["home"])
    assert proc.returncode == 0, f"proc.returncode == 0: {proc.stderr}"
    data = json.loads(proc.stdout)["sources"]
    assert data["binding"]["state"] == "ok"
    assert data["open_items"] == {"state": "ok", "count": 2, "foreign_project_skipped": 1}
    assert data["memory"] == {"state": "ok", "count": 2}
    assert data["bucket"] == {"state": "ok", "count": 1, "foreign_slug_skipped": 2}
    for secret in (SECRET_TITLE, SECRET_BODY, SECRET_FOREIGN, SECRET_NOTE, SECRET_DESC, SECRET_OI, "OI-99", "OI-12"):
        assert secret not in proc.stdout


def test_cli_sources_text_output_has_no_content(env):
    _fill(env)
    _binding(env)
    proc = _run("sources", "--state-dir", str(env["state"]), home=env["home"])
    assert proc.returncode == 0, f"proc.returncode == 0: {proc.stderr}"
    assert "bucket: ok" in proc.stdout and "open_items: ok" in proc.stdout
    assert SECRET_TITLE not in proc.stdout and SECRET_NOTE not in proc.stdout


def test_cli_sources_absent_open_items_is_absent(env):
    _binding(env)
    proc = _run("sources", "--state-dir", str(env["state"]), "--json", home=env["home"])
    assert json.loads(proc.stdout)["sources"]["open_items"]["state"] == "absent"


def test_cli_bind_writes_atomically_and_loads(env):
    args = ["bind", "--state-dir", str(env["state"]), "--memory-dir", str(env["memory"]),
            "--bucket-dir", str(env["bucket"]), "--bucket-slug", "mine"]
    proc = _run(*args, home=env["home"])
    assert proc.returncode == 0, proc.stderr
    written = json.loads((env["state"] / cs.BINDING_FILENAME).read_text())
    assert set(written) == set(cs.BINDING_KEYS) and written["project_id"] == PROJECT
    assert cs.load_binding(env["state"])[1] == "ok"
    assert sorted(p.name for p in env["state"].iterdir()) == [cs.BINDING_FILENAME]


@pytest.mark.parametrize("case", ["memory_outside", "memory_missing", "bucket_missing", "slug_empty",
                                  "state_missing", "no_project"])
def test_cli_bind_failed_check_writes_nothing(env, case):
    outside = env["tmp"] / "elsewhere"
    outside.mkdir()
    values = {"state": env["state"], "memory": env["memory"], "bucket": env["bucket"], "slug": "mine"}
    if case == "memory_outside":
        values["memory"] = outside
    elif case == "memory_missing":
        values["memory"] = env["memory"] / "gone"
    elif case == "bucket_missing":
        values["bucket"] = env["bucket"] / "gone"
    elif case == "slug_empty":
        values["slug"] = " "
    elif case == "state_missing":
        values["state"] = env["tmp"] / "gone"
    else:
        values["state"] = env["tmp"] / "plain-state"
        values["state"].mkdir()
    proc = _run("bind", "--state-dir", str(values["state"]), "--memory-dir", str(values["memory"]),
                "--bucket-dir", str(values["bucket"]), "--bucket-slug", values["slug"], home=env["home"])
    assert proc.returncode != 0 and "bind refused" in proc.stderr
    assert not (values["state"] / cs.BINDING_FILENAME).exists()
    assert not (env["state"] / cs.BINDING_FILENAME).exists()


def test_cli_rejects_unknown_subcommand(env):
    assert _run("audit", home=env["home"]).returncode == 2
