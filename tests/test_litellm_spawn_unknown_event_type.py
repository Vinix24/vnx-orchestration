"""OI-2873: an unknown runner event type becomes an ``info`` event, not a ValueError."""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from provider_spawns.litellm_spawn import _parse_agentic_events

DID = "d-oi2873"


def _run(lines):
    written = []

    def writer(terminal_id, event, dispatch_id=None):
        written.append(event)

    text = "\n".join(json.dumps(x) for x in lines)
    result = _parse_agentic_events(text, writer, DID, "T1", "zai", "headless")
    return written, result


def test_unknown_type_becomes_info_and_run_continues():
    written, (parts, usage, error, n, fails) = _run([
        {"event_type": "text", "content": "hello"},
        {"event_type": "tool", "name": "grep"},
        {"event_type": "usage_complete", "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
    ])
    assert [e["event_type"] for e in written] == ["text", "info", "usage_complete"]
    assert written[1]["data"]["runner_event_type"] == "tool"
    assert written[1]["data"]["name"] == "grep"
    assert parts == ["hello"]
    assert usage == {"prompt_tokens": 3, "completion_tokens": 4}
    assert (n, fails, error) == (3, 0, None)


def test_known_type_passes_through_unchanged():
    written, _ = _run([{"event_type": "tool_use", "name": "x"}])
    assert written[0]["event_type"] == "tool_use"
    assert "runner_event_type" not in written[0]["data"]


def test_one_warning_per_unknown_type_per_call(caplog):
    with caplog.at_level(logging.WARNING, logger="provider_spawns.litellm_spawn"):
        _run([{"event_type": "tool", "a": 1}, {"event_type": "tool", "a": 2}])
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "tool" in r.getMessage()]
    assert len(warns) == 1
    assert DID in warns[0].getMessage()
