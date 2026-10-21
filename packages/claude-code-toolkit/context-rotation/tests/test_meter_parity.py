"""The context-meter plugin ports the guard's settings and its min_work rule; both read one fixture file.

The file is tests/fixtures/meter_parity.ts in the context-meter plugin, a JSON document after
`export default` (a test module of that plugin can import code files only). Its TypeScript test
(parity.test.ts) holds the port to the same expectations.
"""
from __future__ import annotations

import json

import pytest

import band_guard
import context_budget as cb
import enforcement_state
from conftest import PKG

FIXTURE = PKG.parent / "context-meter" / "tests" / "fixtures" / "meter_parity.ts"
PREFIX = "export default "
TOKEN_KEYS = ("warn_tokens", "force_tokens", "hard_tokens")


def load_cases():
    text = FIXTURE.read_text(encoding="utf-8")
    body = text[text.index(PREFIX) + len(PREFIX):].strip()
    return json.loads(body)


CASES = load_cases()


def settings_for(tmp_path, project, home):
    project_dir, home_dir = tmp_path / "project", tmp_path / "home"
    for base, text in ((project_dir, project), (home_dir, home)):
        (base / ".claude").mkdir(parents=True)
        if text is not None:
            (base / ".claude" / cb.SETTINGS_FILE).write_text(text, encoding="utf-8")
    return cb.load_settings(str(project_dir), str(home_dir))


def test_the_fixture_has_the_cases_the_dispatch_names():
    assert len(CASES["settings_cases"]) >= 12
    names = " ".join(case["name"] for case in CASES["settings_cases"])
    for needle in ("invalid JSON", "float", "boolean", "out of order", "alone", "user file", "enabled", "show_meter"):
        assert needle in names
    assert len(CASES["guard_cases"]) >= 8


@pytest.mark.parametrize("case", CASES["settings_cases"], ids=[c["name"] for c in CASES["settings_cases"]])
def test_settings_and_thresholds_match_the_fixture(tmp_path, capsys, case):
    settings = settings_for(tmp_path, case["project"], case["home"])
    expect = case["expect"]
    assert list(cb.thresholds(settings)) == expect["thresholds"]
    assert (not all(key in settings for key in TOKEN_KEYS)) is expect["from_pct"]
    assert settings["enabled"] is expect["enabled"]
    assert settings["show_meter"] is expect["show_meter"]
    assert settings["window_tokens"] == expect["window_tokens"]
    assert settings["min_work_tokens"] == expect["min_work_tokens"]


@pytest.mark.parametrize("case", CASES["guard_cases"], ids=[c["name"] for c in CASES["guard_cases"]])
def test_the_min_work_rule_matches_the_fixture(tmp_path, capsys, case):
    settings = settings_for(tmp_path, case["project"], None)
    state = enforcement_state.fresh()
    state["start_size"] = case["start_size"]
    assert band_guard._loop_level(state, case["size"], settings) == case["expect_level"]


def test_show_meter_is_a_known_bool_key(tmp_path, capsys):
    settings_for(tmp_path, json.dumps({"show_meter": False}), None)
    assert capsys.readouterr().err == ""
    settings_for(tmp_path / "again", json.dumps({"show_meter": "no"}), None)
    assert "bad value for show_meter" in capsys.readouterr().err
