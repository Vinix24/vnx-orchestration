#!/usr/bin/env python3
"""Tests for OI-1086: report_parser must lift model/provider onto the receipt.

Dispatch-ID: 20260808-170000-oi1086-parser-model-drop

Regression: extract_metadata() captured `**Model**:`/`**Provider**:` (and the
dispatch-envelope `---` frontmatter) but _build_enhanced_receipt() dropped
both keys, so every report-derived receipt (receipt_kind='dispatch', no
exempt source) was refused by append_receipt's fail-closed missing_model
check (#1338) and the receipt processor looped on 109 healthy reports.

These tests run the REAL parser and the REAL append_receipt CLI (subprocess,
sandboxed state dir) — no reimplemented logic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

from report_parser import ReportParser  # noqa: E402

REPORT_PARSER = REPO / "scripts" / "report_parser.py"
APPEND_RECEIPT = REPO / "scripts" / "append_receipt.py"

_BOLD_MODEL_BODY = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260808-oi1086-bold-model
**Model**: opus
**Provider**: claude

## Summary
This is a genuine completion report whose summary is comfortably longer than fifty non-whitespace
characters so the body contract validator accepts it.

## Changes
- edited scripts/foo.py

## Verification
- ran pytest tests/test_foo.py; all green

## Open Items
None
"""

_FRONTMATTER_MODEL_BODY = """---
schema_version: 1
dispatch_id: 20260808-oi1086-frontmatter-model
provider: kimi
sub_provider: moonshot
model: kimi-k3
terminal_id: plan-gate
role: plan-reviewer
---

# Dispatch 20260808-oi1086-frontmatter-model

- Provider: kimi

## Summary

Headless plan-gate seat report whose identity lives entirely in the dispatch-envelope
frontmatter; the body carries no bold Model field at all, only prose.
"""

# Measured on the 2026-08-03/04 envelope series: frontmatter says
# `model: unknown` while the body carries the real `**Model**: sonnet`.
# The body value must win; the placeholder must never shadow it.
_MIXED_BODY = """---
schema_version: 1
dispatch_id: 20260808-oi1086-mixed
provider: claude
model: unknown
---

# Completion Report
**Status**: success
**Dispatch-ID**: 20260808-oi1086-mixed
**Model**: sonnet
**Provider**: claude

## Summary
Envelope report whose frontmatter model placeholder is unknown while the body carries the real
model value, which must take precedence in the parsed receipt output.

## Changes
- none

## Verification
- manual inspection

## Open Items
None
"""

_NO_MODEL_BODY = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260808-oi1086-no-model

## Summary
A report that names no model anywhere — the receipt must omit the key entirely so the
fail-closed validator still refuses it, honestly, instead of seeing a fake placeholder.

## Changes
- none

## Verification
- none

## Open Items
None
"""

# Frontmatter genuinely records model: unknown everywhere (measured:
# 20260803-202008-pr0-envelope-census.md) — a correct fail-closed refusal.
_FRONTMATTER_UNKNOWN_BODY = """---
schema_version: 1
dispatch_id: 20260808-oi1086-census
provider: claude
model: unknown
route_decision:
  selected_model: unknown
---

Dispatch-ID: 20260808-oi1086-census

## Summary

Census-style report with no model recorded anywhere except unknown placeholders, so the
receipt must carry no model key and must still be refused fail-closed by append validation.
"""

# Reports that embed their full dispatch instruction carry the door-stamped
# **Model**/**Provider** block mid-file, outside the 2000-char header window
# (measured: 20260804-m00-oi917.md, block at line 196/282).
_EMBEDDED_INSTRUCTION_BODY = (
    "# Dispatch 20260808-oi1086-embedded\n\n"
    "## Instruction\n\n"
    + "# filler line with enough prose to push the metadata block well past the two thousand character header window that the parser scans for bold fields.\n" * 20
    + """
---

DISPATCH INSTRUCTION:

# Some open item

**Dispatch-ID**: 20260808-oi1086-embedded
**Provider**: deepseek
**Model**: deepseek-v4-pro
**Terminal**: T1

## Summary

The actual worker report body follows the embedded instruction; the model identity lives in
the door-stamped dispatch metadata block above and must be recovered from there.
"""
)

# Plain-text header block (measured: 20260804-065952-triage-b1-blockers.md
# and the 2026-08-04 m-series) — no bold, no frontmatter.
_PLAIN_HEADER_BODY = """# Dispatch 20260808-oi1086-plain

Dispatch-ID: 20260808-oi1086-plain
Provider: deepseek
Model: deepseek-v4-flash
Terminal: T2

## Summary

Triage-note style report whose identity block is plain `Key: value` lines instead of bold
fields; the parser must still recover model and provider from it.
"""

# A header bold-field value must always win over an embedded-instruction
# copy deeper in the file.
_HEADER_WINS_BODY = (
    "# Completion Report\n"
    "**Status**: success\n"
    "**Dispatch-ID**: 20260808-oi1086-header-wins\n"
    "**Model**: opus\n"
    "**Provider**: claude\n\n"
    + "# filler line to push the embedded instruction past the header window, long enough to overflow two thousand characters of content.\n" * 20
    + "**Model**: glm-5.2\n**Provider**: glm\n"
)


def _parse(tmp_path: Path, body: str, name: str = "report.md") -> dict:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return ReportParser().parse_report(str(p))


def _sandbox_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["PROJECT_ROOT"] = str(tmp_path)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(REPO)
    env["VNX_REPORTS_DIR"] = str(tmp_path)
    return env


def _parser_then_append(tmp_path: Path, body: str) -> tuple[dict, subprocess.CompletedProcess]:
    """Run the REAL report_parser CLI, pipe its JSON into the REAL
    append_receipt CLI (exactly the receipt_processor.sh pipeline shape),
    against a fully sandboxed state dir. Returns (receipt, append_result)."""
    report = tmp_path / "report.md"
    report.write_text(body, encoding="utf-8")
    env = _sandbox_env(tmp_path)

    parse = subprocess.run(
        [sys.executable, str(REPORT_PARSER), str(report)],
        capture_output=True, text=True, env=env,
    )
    assert parse.returncode == 0, f"report_parser failed: {parse.stderr}"
    receipt = json.loads(parse.stdout)

    append = subprocess.run(
        [sys.executable, str(APPEND_RECEIPT)],
        input=json.dumps(receipt),
        capture_output=True, text=True, env=env,
    )
    return receipt, append


def test_bold_model_provider_reach_receipt(tmp_path):
    """The exact bug case: bold **Model**/**Provider** fields must land on the receipt."""
    r = _parse(tmp_path, _BOLD_MODEL_BODY)
    assert r["model"] == "opus"
    assert r["provider"] == "claude"
    assert r["receipt_kind"] == "dispatch"


def test_frontmatter_model_provider_reach_receipt(tmp_path):
    """Dispatch-envelope frontmatter (headless seats) carries the identity."""
    r = _parse(tmp_path, _FRONTMATTER_MODEL_BODY)
    assert r["model"] == "kimi-k3"
    assert r["provider"] == "kimi"


def test_bold_model_wins_over_unknown_frontmatter(tmp_path):
    """A `model: unknown` placeholder must never shadow a real body value."""
    r = _parse(tmp_path, _MIXED_BODY)
    assert r["model"] == "sonnet"
    assert r["provider"] == "claude"


def test_missing_model_omits_keys_not_empty_strings(tmp_path):
    """Chosen contract (OI-1086 task 1): absent/placeholder model or provider
    means the KEY IS OMITTED — never '', never 'unknown'. For the fail-closed
    validator all three forms refuse identically; omission is the honest form
    and keeps the refusal reason accurate."""
    r = _parse(tmp_path, _NO_MODEL_BODY)
    assert "model" not in r
    assert "provider" not in r


def test_unknown_frontmatter_model_omits_key(tmp_path):
    r = _parse(tmp_path, _FRONTMATTER_UNKNOWN_BODY)
    assert "model" not in r
    # provider: claude is a real value and is kept.
    assert r["provider"] == "claude"


def test_embedded_dispatch_instruction_model_recovered(tmp_path):
    """Mid-file door-stamped **Model**/**Provider** (embedded dispatch
    instruction) must be recovered even outside the 2000-char header window."""
    r = _parse(tmp_path, _EMBEDDED_INSTRUCTION_BODY)
    assert r["model"] == "deepseek-v4-pro"
    assert r["provider"] == "deepseek"


def test_plain_header_model_recovered(tmp_path):
    r = _parse(tmp_path, _PLAIN_HEADER_BODY)
    assert r["model"] == "deepseek-v4-flash"
    assert r["provider"] == "deepseek"


def test_header_bold_model_wins_over_embedded_copy(tmp_path):
    """Precedence: a real header value beats an embedded-instruction copy."""
    r = _parse(tmp_path, _HEADER_WINS_BODY)
    assert r["model"] == "opus"
    assert r["provider"] == "claude"


def test_end_to_end_receipt_with_model_passes_append_validation(tmp_path):
    """End-to-end: parser output through the REAL append_receipt CLI must NOT
    fire missing_model and must be persisted — the receipt processor's exact
    pipeline shape, sandboxed. Without this leg we'd test the parser, not the
    fix."""
    receipt, append = _parser_then_append(tmp_path, _BOLD_MODEL_BODY)
    assert append.returncode == 0, f"append_receipt refused a healthy receipt: {append.stderr}"
    assert "missing_model" not in append.stderr

    ledger = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    assert ledger.is_file(), "receipt was not persisted to the sandboxed ledger"
    stored = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert stored["dispatch_id"] == "20260808-oi1086-bold-model"
    assert stored["model"], "persisted receipt must name a real model"
    assert stored["model"].lower() not in {"unknown", "none", "null", ""}
    assert stored["receipt_kind"] == "dispatch"


def test_end_to_end_receipt_without_model_still_refused_fail_closed(tmp_path):
    """The fail-closed check itself is correct and stays: a report with no
    model anywhere must still be refused with code=missing_model (and now the
    dead-letter path quarantines it instead of looping — see
    tests/test_receipt_processor_deadletter.py)."""
    _receipt, append = _parser_then_append(tmp_path, _NO_MODEL_BODY)
    assert append.returncode != 0
    codes = [
        json.loads(line).get("code")
        for line in append.stderr.splitlines()
        if line.strip().startswith("{")
    ]
    assert "missing_model" in codes


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
