"""Capture PR-2: the binding, the memory, bucket and open-item readers, and the CLI.

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
NOTE = "geheime-notitienaam"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = home / ".vnx-data" / PROJECT / "state"
    memory = home / ".claude" / "projects" / "-x" / "memory"
    outside = tmp_path / "elsewhere"
    bucket = tmp_path / "bucket"
    for d in (state, memory, outside, bucket):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "state": state, "memory": memory, "outside": outside, "bucket": bucket, "tmp": tmp_path}


def _binding(env, **override):
    data = {"schema": cs.BINDING_SCHEMA, "project_id": PROJECT, "memory_dir": str(env["memory"]),
            "bucket_dir": str(env["bucket"]), "bucket_slug": "mine",
            "bound_at": "2026-11-10T00:00:00+00:00"}
    data.update(override)
    data = {k: v for k, v in data.items() if v is not cs}
    (env["state"] / cs.BINDING_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    return data


def _state(env):
    binding, status = cs.load_binding(env["state"])
    return binding, cs.state_of(status)


# --- load_binding: every unusable binding is unbound, never ok ---------------

def test_binding_ok(env):
    _binding(env)
    binding, status = cs.load_binding(env["state"])
    assert status == "ok" and binding["bucket_slug"] == "mine"


def test_binding_file_missing_is_unbound(env):
    assert _state(env) == (None, "unbound")


@pytest.mark.parametrize("content", ["{not json", "[]", "null", '"text"', ""])
def test_binding_corrupt_is_unbound(env, content):
    (env["state"] / cs.BINDING_FILENAME).write_text(content, encoding="utf-8")
    assert _state(env) == (None, "unbound")


@pytest.mark.parametrize("field", ["project_id", "memory_dir", "bucket_dir", "bucket_slug", "schema"])
@pytest.mark.parametrize("value", [cs, None, "", 3, "  "])
def test_binding_bad_field_is_unbound(env, field, value):
    # ``cs`` as the value means: leave the field out of the file.
    _binding(env, **{field: value})
    assert _state(env) == (None, "unbound")


@pytest.mark.parametrize("value", ["proj-b", 5, ["proj-a"]])
def test_binding_project_id_different_is_unbound(env, value):
    _binding(env, project_id=value)
    assert _state(env) == (None, "unbound")


def test_binding_unknown_schema_version_is_unbound(env):
    _binding(env, schema="capture_sources/9")
    assert _state(env) == (None, "unbound")


def test_binding_empty_derived_project_is_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = tmp_path / "plain" / "state"
    state.mkdir(parents=True)
    (state / cs.BINDING_FILENAME).write_text(json.dumps({"schema": cs.BINDING_SCHEMA, "project_id": PROJECT}))
    assert cs.state_of(cs.load_binding(state)[1]) == "unbound"


@pytest.mark.parametrize("kind", ["outside", "symlink_outside", "projects_root_itself"])
def test_binding_memory_dir_not_under_projects_is_unbound(env, kind):
    target = {"outside": env["outside"], "projects_root_itself": env["home"] / ".claude" / "projects"}.get(kind)
    if kind == "symlink_outside":
        target = env["home"] / ".claude" / "projects" / "link"
        target.symlink_to(env["outside"])
    _binding(env, memory_dir=str(target))
    assert _state(env) == (None, "unbound")


# --- memory notes -------------------------------------------------------------

def test_memory_notes_read(env):
    (env["memory"] / f"{NOTE}.md").write_text(
        f"---\nname: {NOTE}\ndescription: d\nmodified: 2026-11-09T10:00:00Z\n---\n"
        "Zie OI-12 en [[andere-notitie|alias]].\n", encoding="utf-8")
    (env["memory"] / "kaal.md").write_text("zonder frontmatter OI-7\n", encoding="utf-8")
    (env["memory"] / "MEMORY.md").write_text("- index\n", encoding="utf-8")
    notes, status = cs.read_memory_notes(_binding(env))
    by_key = {n["key"]: n for n in notes}
    assert status == "ok" and set(by_key) == {NOTE, "kaal"}
    assert by_key[NOTE]["body_refs"] == {"oi": ["OI-12"], "memory": ["andere-notitie"]}
    assert by_key[NOTE]["modified_source"] == "frontmatter"
    assert by_key[NOTE]["modified"].startswith("2026-11-09T10:00:00")
    assert by_key["kaal"]["modified_source"] == "mtime"


@pytest.mark.parametrize("kind", ["outside", "symlink_dir", "note_symlink", "bad_bytes", "dir_missing"])
def test_memory_notes_unsafe_or_unreadable_is_error(env, kind):
    memory_dir = env["memory"]
    (env["outside"] / "n.md").write_text("x", encoding="utf-8")
    if kind == "outside":
        memory_dir = env["outside"]
    elif kind == "symlink_dir":
        memory_dir = env["home"] / ".claude" / "projects" / "link"
        memory_dir.symlink_to(env["outside"])
    elif kind == "note_symlink":
        (env["memory"] / "stiekem.md").symlink_to(env["outside"] / "n.md")
    elif kind == "bad_bytes":
        (env["memory"] / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    else:
        memory_dir = env["home"] / ".claude" / "projects" / "gone"
    notes, status = cs.read_memory_notes(_binding(env, memory_dir=str(memory_dir)))
    assert notes == [] and cs.state_of(status) == "error"


@pytest.mark.parametrize("kind", ["dangling_outside", "dangling_inside", "escape_existing", "self_loop"])
def test_note_symlink_dangling_or_escaping_is_error_not_empty_ok(env, kind):
    link = env["memory"] / "broken.md"
    if kind == "dangling_outside":
        link.symlink_to(env["outside"] / "missing.md")
    elif kind == "dangling_inside":
        link.symlink_to(env["memory"] / "missing.md")
    elif kind == "self_loop":
        link.symlink_to(link)
    else:
        (env["outside"] / "real.md").write_text("x", encoding="utf-8")
        link.symlink_to(env["outside"] / "real.md")
    notes, status = cs.read_memory_notes(_binding(env))
    assert notes == [] and cs.state_of(status) == "error"
    assert "broken.md" in status


def test_note_symlink_inside_memory_dir_is_read(env):
    (env["memory"] / "echt.md").write_text("zie OI-5\n", encoding="utf-8")
    (env["memory"] / "alias.md").symlink_to(env["memory"] / "echt.md")
    notes, status = cs.read_memory_notes(_binding(env))
    assert status == "ok" and {n["key"] for n in notes} == {"echt", "alias"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_memory_dir_unlistable_is_error_not_empty_ok(env):
    (env["memory"] / "n.md").write_text("x", encoding="utf-8")
    binding = _binding(env)
    os.chmod(env["memory"], 0)
    try:
        notes, status = cs.read_memory_notes(binding)
    finally:
        os.chmod(env["memory"], 0o755)
    assert notes == [] and cs.state_of(status) == "error"


def test_memory_notes_without_memory_dir_key_is_error():
    assert cs.state_of(cs.read_memory_notes({})[1]) == "error"


# --- open items ---------------------------------------------------------------

def test_open_items_absent_is_not_an_empty_list(env):
    assert cs.read_open_items(env["state"]) == ([], "absent", 0)


def test_open_items_foreign_project_skipped_and_counted(env):
    (env["state"] / "open_items.json").write_text(json.dumps({"items": [
        {"id": "OI-1", "status": "open", "title": "t", "created_at": "2026-11-01T10:00:00"},
        {"id": "OI-2", "status": "open", "title": "vreemd", "project_id": "proj-b"},
        {"id": "OI-3", "status": "done", "title": "eigen", "project_id": PROJECT},
    ]}), encoding="utf-8")
    items, status, foreign = cs.read_open_items(env["state"])
    assert status == "ok" and foreign == 1
    assert [i["id"] for i in items] == ["OI-1", "OI-3"]


@pytest.mark.parametrize("content", ["{broken", "[]", '{"items": null}', '{"items": ["x"]}', "{}"])
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


def test_bucket_dir_unreadable_is_error_not_empty_ok(env):
    if os.geteuid() == 0:
        pytest.skip("root reads any directory")
    (env["bucket"] / "2026-11.md").write_text("### 2026-11-01 \u00b7 mine \u00b7 t\n", encoding="utf-8")
    binding = _binding(env)
    os.chmod(env["bucket"], 0)
    try:
        entries, status, foreign = cs.read_bucket_entries(binding, ["2026-11"])
    finally:
        os.chmod(env["bucket"], 0o755)
    assert entries == [] and foreign == 0
    assert cs.state_of(status) == "error"


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
    outside = env["outside"]
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
