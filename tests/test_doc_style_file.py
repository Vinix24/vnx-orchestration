"""VNX_DOC_STYLE_FILE: staging appends a configured style file to a docs dispatch.

Stores and files live under tmp_path; the key is set through the environment, as the
VNX_BUILDER_SETTING_SOURCES tests do (the same config_registry precedence chain).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_bridge  # noqa: E402

KEY = "VNX_DOC_STYLE_FILE"
HEADING = "## Writing style (VNX_DOC_STYLE_FILE)"
DID = "20261113-120000-docstyle"
STYLE = "Write short sentences.\nNo buzzwords.\n"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (KEY, f"VNX_OVERRIDE_{KEY[4:]}"):
        monkeypatch.delenv(name, raising=False)


def _paths(*names):
    return tuple({"path": n, "access": "read_write"} for n in names)


def _stage(tmp_path, **over):
    base = dict(
        instruction_text="write the doc", dispatch_id=DID, role="backend-developer",
        target_slot="T1", project_id="p1", provider="claude", data_dir=tmp_path / "data",
    )
    base.update(over)
    return dispatch_bridge.stage_spec_bundle(**base)


def _style_file(tmp_path, content=STYLE) -> Path:
    path = tmp_path / "style.md"
    path.write_text(content, encoding="utf-8")
    return path


def _instruction(spec: Path) -> str:
    return (spec.parent / "instruction.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "task_class, names, expected",
    [
        ("04_documentation", (), True),
        ("06_design", _paths("scripts/a.py"), True),
        ("01_code_generation", _paths("docs/x.md", "changelog.d/a.md"), True),
        ("01_code_generation", _paths("scripts/a.py", "changelog.d/a.md"), False),
        ("01_code_generation", _paths("changelog.d/a.md"), False),
        ("01_code_generation", _paths("docs/x.md", "scripts/a.py"), False),
        ("01_code_generation", _paths("README.md"), True),
        ("01_code_generation", (), False),
        (None, _paths("./docs/y.txt"), True),
        ("", _paths("docs/x.md", "scripts/a.py", "changelog.d/a.md"), False),
        ("05_testing", _paths("changelog.d/a.md", "tests/test_a.py"), False),
    ],
)
def test_docs_dispatch_rule(task_class, names, expected):
    assert dispatch_bridge.is_docs_dispatch(task_class, names) is expected


def test_key_empty_leaves_instruction_and_payload_untouched(tmp_path):
    spec = _stage(tmp_path, task_class="04_documentation")
    assert _instruction(spec) == "write the doc"
    assert "doc_style_file" not in json.loads(spec.read_text(encoding="utf-8"))


def test_docs_dispatch_appends_heading_and_content(tmp_path, monkeypatch):
    style = _style_file(tmp_path)
    monkeypatch.setenv(KEY, str(style))
    spec = _stage(tmp_path, task_class="04_documentation")
    text = _instruction(spec)
    appended = text[len("write the doc"):]
    assert text.startswith("write the doc\n\n" + HEADING)
    assert text.endswith(HEADING + "\n\n" + STYLE)
    payload = json.loads(spec.read_text(encoding="utf-8"))
    assert payload["doc_style_file"] == {
        "path": str(style.resolve()),
        "sha256": hashlib.sha256(appended.encode("utf-8")).hexdigest(),
    }
    assert payload["instruction_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_docs_dispatch_by_paths_appends(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, str(_style_file(tmp_path)))
    spec = _stage(tmp_path, dispatch_paths=_paths("docs/x.md", "changelog.d/a.md"))
    assert HEADING in _instruction(spec)


def test_instruction_ending_in_newline_gets_one_blank_line(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, str(_style_file(tmp_path)))
    spec = _stage(tmp_path, instruction_text="body\n", task_class="06_design")
    assert _instruction(spec).startswith("body\n\n" + HEADING)


def test_tilde_path_is_expanded(tmp_path, monkeypatch):
    _style_file(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(KEY, "~/style.md")
    spec = _stage(tmp_path, task_class="04_documentation")
    assert json.loads(spec.read_text(encoding="utf-8"))["doc_style_file"]["path"] == str((tmp_path / "style.md").resolve())


def test_code_dispatch_is_unchanged_even_with_key_set(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, str(_style_file(tmp_path)))
    spec = _stage(tmp_path, task_class="01_code_generation", dispatch_paths=_paths("scripts/a.py", "changelog.d/a.md"))
    assert _instruction(spec) == "write the doc"
    assert "doc_style_file" not in json.loads(spec.read_text(encoding="utf-8"))


def test_code_dispatch_does_not_read_a_broken_file(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, str(tmp_path / "missing.md"))
    spec = _stage(tmp_path, task_class="01_code_generation", dispatch_paths=_paths("scripts/a.py"))
    assert _instruction(spec) == "write the doc"


def _assert_refused_and_nothing_written(tmp_path, **over):
    with pytest.raises(ValueError, match=KEY):
        _stage(tmp_path, task_class="04_documentation", **over)
    assert not (tmp_path / "data" / "dispatches" / "pending" / DID).exists()


def test_missing_file_refuses_staging(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, str(tmp_path / "missing.md"))
    _assert_refused_and_nothing_written(tmp_path)


@pytest.mark.parametrize("content", ["", "   \n\n\t\n"])
def test_empty_file_refuses_staging(tmp_path, monkeypatch, content):
    monkeypatch.setenv(KEY, str(_style_file(tmp_path, content)))
    _assert_refused_and_nothing_written(tmp_path)


def test_directory_refuses_staging(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, str(tmp_path))
    _assert_refused_and_nothing_written(tmp_path)


def test_non_utf8_file_refuses_staging(tmp_path, monkeypatch):
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00bad")
    monkeypatch.setenv(KEY, str(bad))
    _assert_refused_and_nothing_written(tmp_path)


def test_relative_path_refuses_staging(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, "style.md")
    _assert_refused_and_nothing_written(tmp_path)


def _set_project_value(tmp_path: Path, project_id: str, value: str) -> None:
    import config_store_db

    state = tmp_path / "data" / "state"
    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state / "runtime_coordination.db")
    try:
        config_store_db.set_config(conn, project_id, KEY, value, actor="test", approval_id="test-approval")
    finally:
        conn.close()


def test_unreadable_config_store_refuses_a_docs_dispatch(tmp_path):
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    (state / "runtime_coordination.db").write_bytes(b"this is not a sqlite database" * 20)
    _assert_refused_and_nothing_written(tmp_path)


def test_project_store_value_is_read_for_the_dispatch_project(tmp_path):
    _set_project_value(tmp_path, "p1", str(_style_file(tmp_path)))
    spec = _stage(tmp_path, task_class="04_documentation")
    assert HEADING in _instruction(spec)
    other = _stage(tmp_path, task_class="04_documentation", project_id="p2", dispatch_id=DID + "-b")
    assert _instruction(other) == "write the doc"
