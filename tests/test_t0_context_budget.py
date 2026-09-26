"""tests/test_t0_context_budget.py — the T0 context measurement (dispatch 20260925-t0-context-rotation-enforced).

The measurement reads the LAST real assistant usage block of a session transcript and sums
input + cache_read + cache_creation. Sidechain and <synthetic> entries are skipped; an empty
or unreadable transcript is None, never 0 and never an exception.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import t0_context_budget as budget


def _assistant(inp: int, read: int, create: int, *, model: str = "claude-opus-5-5",
               sidechain: bool = False) -> dict:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "model": model,
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": create,
                "output_tokens": 999,
            },
        },
    }


def _write(path: Path, entries: list, trailer: str = "") -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n" + trailer, encoding="utf-8")
    return path


def test_sums_the_last_assistant_usage_block(tmp_path):
    transcript = _write(tmp_path / "t.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        _assistant(10, 100_000, 5_000),
        {"type": "user", "message": {"role": "user", "content": "more"}},
        _assistant(361, 420_000, 6_000),
        {"type": "system", "subtype": "turn_duration"},
    ])
    assert budget.context_tokens(transcript) == 361 + 420_000 + 6_000


def test_output_tokens_are_not_counted(tmp_path):
    transcript = _write(tmp_path / "t.jsonl", [_assistant(1, 2, 3)])
    assert budget.context_tokens(transcript) == 6


def test_skips_sidechain_and_synthetic_entries(tmp_path):
    transcript = _write(tmp_path / "t.jsonl", [
        _assistant(0, 450_000, 0),
        _assistant(0, 900_000, 0, sidechain=True),
        _assistant(0, 0, 0, model="<synthetic>"),
    ])
    assert budget.context_tokens(transcript) == 450_000


def test_skips_a_broken_trailing_line(tmp_path):
    transcript = _write(tmp_path / "t.jsonl", [_assistant(5, 500_000, 0)], trailer='{"type": "assist')
    assert budget.context_tokens(transcript) == 500_005


def test_reads_across_block_boundaries(tmp_path):
    filler = {"type": "user", "message": {"role": "user", "content": "x" * 5000}}
    entries = [_assistant(7, 123_456, 0)] + [filler] * 40  # ~200 KB after the usage line
    transcript = _write(tmp_path / "t.jsonl", entries)
    assert transcript.stat().st_size > 3 * budget._BLOCK_SIZE
    assert budget.context_tokens(transcript) == 123_463


@pytest.mark.parametrize("content", ["", "\n\n", "not json\n{also not\n",
                                     json.dumps({"type": "user"}) + "\n"])
def test_unmeasurable_transcript_is_none(tmp_path, content):
    path = tmp_path / "t.jsonl"
    path.write_text(content, encoding="utf-8")
    assert budget.context_tokens(path) is None


def test_missing_or_empty_path_is_none(tmp_path):
    assert budget.context_tokens(tmp_path / "absent.jsonl") is None
    assert budget.context_tokens("") is None
    assert budget.context_tokens(None) is None


def test_thresholds_default_from_the_registry(monkeypatch):
    for key in (budget.WARN_KEY, budget.FORCE_KEY, budget.HARD_KEY):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("VNX_OVERRIDE_" + key[len("VNX_"):], raising=False)
    assert budget.thresholds() == budget.Thresholds(400_000, 500_000, 600_000)


def test_thresholds_follow_env_and_reject_garbage(monkeypatch, capsys):
    monkeypatch.setenv(budget.WARN_KEY, "1000")
    monkeypatch.setenv(budget.FORCE_KEY, "not-a-number")
    monkeypatch.setenv(budget.HARD_KEY, "-5")
    th = budget.thresholds()
    assert th == budget.Thresholds(1000, 500_000, 600_000)
    assert "VNX_T0_ROTATE_FORCE_TOKENS" in capsys.readouterr().err


def test_registry_carries_the_three_keys_with_their_defaults():
    import config_registry

    for key, default in ((budget.WARN_KEY, "400000"), (budget.FORCE_KEY, "500000"),
                         (budget.HARD_KEY, "600000")):
        entry = config_registry.CONFIG_REGISTRY[key]
        assert entry.default == default
        assert entry.subsystem == "t0-context-rotation"
    assert config_registry.canonical_flags()["t0-context-rotation"] == budget.FORCE_KEY


@pytest.mark.parametrize("tokens,expected", [
    (None, budget.LEVEL_OK), (0, budget.LEVEL_OK), (399_999, budget.LEVEL_OK),
    (400_000, budget.LEVEL_WARN), (499_999, budget.LEVEL_WARN),
    (500_000, budget.LEVEL_FORCE), (599_999, budget.LEVEL_FORCE),
    (600_000, budget.LEVEL_HARD), (2_000_000, budget.LEVEL_HARD),
])
def test_level_bands(tokens, expected):
    assert budget.level(tokens, budget.Thresholds(400_000, 500_000, 600_000)) == expected
