"""Measurement, settings and bands."""
from __future__ import annotations

import json

import context_budget as cb
from conftest import assistant_entry, load_fixture, write_transcript


def test_assistant_entry_sums_three_fields(tmp_path):
    entry = load_fixture("transcript", "assistant_entry.json")
    path = write_transcript(tmp_path / "t.jsonl", [entry])
    assert cb.measure(str(path)) == 4 + 76157 + 468


def test_missing_usage_fields_count_zero(tmp_path):
    entry = assistant_entry(0)
    entry["message"]["usage"] = {"cache_read_input_tokens": 900}
    path = write_transcript(tmp_path / "t.jsonl", [entry])
    assert cb.measure(str(path)) == 900


def test_usage_without_any_of_the_three_is_skipped(tmp_path):
    older = assistant_entry(5000)
    empty = assistant_entry(0)
    empty["message"]["usage"] = {"output_tokens": 12}
    path = write_transcript(tmp_path / "t.jsonl", [older, empty])
    assert cb.measure(str(path)) == 5000


def test_newer_compact_boundary_uses_post_tokens(tmp_path):
    boundary = load_fixture("transcript", "compact_boundary.json")
    path = write_transcript(tmp_path / "t.jsonl", [assistant_entry(76000), boundary])
    assert cb.measure(str(path)) == 15713


def test_compact_boundary_without_post_tokens_is_unknown(tmp_path):
    boundary = load_fixture("transcript", "compact_boundary.json")
    del boundary["compactMetadata"]["postTokens"]
    path = write_transcript(tmp_path / "t.jsonl", [assistant_entry(76000), boundary])
    assert cb.measure(str(path)) is None


def test_assistant_after_compact_boundary_wins(tmp_path):
    boundary = load_fixture("transcript", "compact_boundary.json")
    path = write_transcript(tmp_path / "t.jsonl", [boundary, assistant_entry(20000)])
    assert cb.measure(str(path)) == 20000


def test_synthetic_entry_is_skipped(tmp_path):
    synthetic = load_fixture("transcript", "synthetic_entry.json")
    path = write_transcript(tmp_path / "t.jsonl", [assistant_entry(42000), synthetic])
    assert cb.measure(str(path)) == 42000


def test_sidechain_entry_is_skipped(tmp_path):
    path = write_transcript(
        tmp_path / "t.jsonl", [assistant_entry(30000), assistant_entry(99000, sidechain=True)]
    )
    assert cb.measure(str(path)) == 30000


def test_subagent_fixture_is_skipped(tmp_path):
    sub = load_fixture("transcript", "subagent_assistant_entry.json")
    path = write_transcript(tmp_path / "t.jsonl", [assistant_entry(30000), sub])
    assert cb.measure(str(path)) == 30000


def test_other_entries_are_skipped(tmp_path):
    other = [{"type": "user", "message": {"role": "user", "content": "hi"}}, {"type": "system", "subtype": "x"}]
    path = write_transcript(tmp_path / "t.jsonl", [assistant_entry(7000)] + other)
    assert cb.measure(str(path)) == 7000


def test_nothing_deciding_is_unknown_not_zero(tmp_path):
    path = write_transcript(tmp_path / "t.jsonl", [{"type": "user"}])
    assert cb.measure(str(path)) is None


def test_malformed_lines_are_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(assistant_entry(11000)) + "\nnot json\n\n", encoding="utf-8")
    assert cb.measure(str(path)) == 11000


def test_unreadable_and_missing_are_unknown(tmp_path):
    assert cb.measure(str(tmp_path / "absent.jsonl")) is None
    assert cb.measure(str(tmp_path)) is None
    assert cb.measure("") is None
    assert cb.measure(None) is None


def test_deciding_line_straddles_block_boundary(tmp_path):
    block = cb.BLOCK_SIZE
    assert block == 64 * 1024
    deciding = json.dumps(assistant_entry(123456), ensure_ascii=False) + "\n"
    head = json.dumps({"type": "user", "pad": "h" * 3000}) + "\n"
    tail_len = block - len(deciding.encode()) // 2
    tail = json.dumps({"type": "user", "pad": "x" * (tail_len - 40)}) + "\n"
    path = tmp_path / "t.jsonl"
    path.write_bytes((head + deciding + tail).encode())
    size = path.stat().st_size
    start = size - len(tail.encode()) - len(deciding.encode())
    assert start < size - block < start + len(deciding.encode())
    assert cb.measure(str(path)) == 123456


def test_multibyte_text_across_block_boundary(tmp_path):
    block = cb.BLOCK_SIZE
    entry = assistant_entry(777)
    entry["note"] = "é" * 40000
    deciding = json.dumps(entry, ensure_ascii=False) + "\n"
    path = tmp_path / "t.jsonl"
    path.write_bytes(deciding.encode("utf-8"))
    assert path.stat().st_size > block
    assert cb.measure(str(path)) == 777


def write_settings(directory, content):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "context-rotation.json").write_text(
        content if isinstance(content, str) else json.dumps(content), encoding="utf-8"
    )


def test_defaults_when_no_file(project, isolated_env):
    s = cb.load_settings(str(project))
    assert s["enabled"] is True
    assert (s["window_tokens"], s["warn_pct"], s["force_pct"], s["hard_pct"]) == (200000, 55, 65, 75)
    assert s["auto_clear"] is True
    assert s["handover_max_chars"] == 8000
    assert s["handover_max_age_minutes"] == 60
    assert s["hard_max_denials"] == 3
    assert s["archive_keep"] == 20
    assert s["busy_pattern"] == r"^\S+ \S+… \(\d+s"
    assert s["prompt_pattern"] == "^❯ "
    assert s["ack_timeout_seconds"] == 20


def test_project_file_wins_over_user_file(project, isolated_env):
    write_settings(isolated_env / ".claude", {"window_tokens": 111})
    write_settings(project / ".claude", {"window_tokens": 222})
    assert cb.load_settings(str(project))["window_tokens"] == 222


def test_user_file_used_without_project_file(project, isolated_env):
    write_settings(isolated_env / ".claude", {"window_tokens": 111})
    assert cb.load_settings(str(project))["window_tokens"] == 111


def test_malformed_json_falls_back_with_stderr_line(project, capsys):
    write_settings(project / ".claude", "{not json")
    s = cb.load_settings(str(project))
    assert s["window_tokens"] == 200000
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1


def test_one_bad_value_only_resets_that_key(project, capsys):
    write_settings(project / ".claude", {"window_tokens": "big", "archive_keep": 5})
    s = cb.load_settings(str(project))
    assert s["window_tokens"] == 200000
    assert s["archive_keep"] == 5
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_bool_is_not_an_int(project):
    write_settings(project / ".claude", {"archive_keep": True, "enabled": 1})
    s = cb.load_settings(str(project))
    assert s["archive_keep"] == 20
    assert s["enabled"] is True


def test_invalid_triple_resets_all_three(project, capsys):
    write_settings(project / ".claude", {"warn_pct": 70, "force_pct": 60, "hard_pct": 90})
    s = cb.load_settings(str(project))
    assert (s["warn_pct"], s["force_pct"], s["hard_pct"]) == (55, 65, 75)
    assert capsys.readouterr().err.strip()


def test_hard_above_100_resets_triple(project):
    write_settings(project / ".claude", {"hard_pct": 101})
    s = cb.load_settings(str(project))
    assert (s["warn_pct"], s["force_pct"], s["hard_pct"]) == (55, 65, 75)


def test_valid_custom_triple_with_floats(project):
    write_settings(project / ".claude", {"warn_pct": 40.5, "force_pct": 50, "hard_pct": 100})
    s = cb.load_settings(str(project))
    assert (s["warn_pct"], s["force_pct"], s["hard_pct"]) == (40.5, 50, 100)


def test_bad_regex_falls_back(project):
    write_settings(project / ".claude", {"busy_pattern": "(", "prompt_pattern": "^> "})
    s = cb.load_settings(str(project))
    assert s["busy_pattern"] == r"^\S+ \S+… \(\d+s"
    assert s["prompt_pattern"] == "^> "


def test_enabled_false(project):
    write_settings(project / ".claude", {"enabled": False})
    assert cb.load_settings(str(project))["enabled"] is False


def test_non_object_json_falls_back(project, capsys):
    write_settings(project / ".claude", "[1, 2]")
    assert cb.load_settings(str(project))["window_tokens"] == 200000
    assert capsys.readouterr().err.strip()


def test_band_thresholds():
    s = {"window_tokens": 1000, "warn_pct": 55, "force_pct": 65, "hard_pct": 75}
    assert cb.band(None, s) is None
    assert cb.band(549, s) == "below"
    assert cb.band(550, s) == "warn"
    assert cb.band(650, s) == "force"
    assert cb.band(750, s) == "hard"
    assert cb.band(5000, s) == "hard"
