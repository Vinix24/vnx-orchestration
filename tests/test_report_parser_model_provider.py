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


def test_missing_task_id_omits_key_not_sentinel(tmp_path):
    """OI-1408: task_id is not part of the receipt_kind='dispatch' field
    contract. A report that never declares one must produce a receipt with
    no task_id key at all — never the 'unknown' sentinel this used to stamp
    via metadata.get('task_id', 'unknown')."""
    r = _parse(tmp_path, _BOLD_MODEL_BODY)
    assert "task_id" not in r


def test_bold_task_id_kept_when_a_report_declares_a_real_one(tmp_path):
    """The missing-field contract omits the sentinel, but a report that DOES
    declare a real task_id (via the generic **Key**: value bold-field scan)
    still has it carried through onto the receipt."""
    body = (
        "# Completion Report\n"
        "**Status**: success\n"
        "**Dispatch-ID**: 20260823-oi1408-task-id\n"
        "**Task ID**: t-42\n"
        "**Model**: opus\n"
        "**Provider**: claude\n\n"
        "## Summary\nReal task_id declared explicitly in the header block.\n"
    )
    r = _parse(tmp_path, body)
    assert r["task_id"] == "t-42"


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


# ---------------------------------------------------------------------------
# OI-1101: inline code span + shape validation for model/provider extraction
# ---------------------------------------------------------------------------

# A provider-lane report that echoes the full dispatch instruction. Within the
# echoed instruction, a prose sentence mentions `**Model**:` inside backticks.
# The mid-file fallback must NOT extract a model value from inside inline code.
_ECHOED_INSTRUCTION_WITH_INLINE_CODE_BODY = (
    "# Completion Report\n"
    "**Status**: success\n"
    "**Dispatch-ID**: 20260809-oi1101-echoed\n\n"
    "## Summary\n"
    "A provider-lane report that echoes its dispatch instruction in the body, "
    "with the identity field mentioned inside inline code spans. The summary "
    "must be longer than fifty non-whitespace characters for body contract.\n\n"
    "## Changes\n"
    "- edited scripts/foo.py\n\n"
    "## Verification\n"
    "- ran pytest tests/test_foo.py; all green\n\n"
    "## Open Items\n"
    "None\n\n"
    + ("# padding to push the real block past the header window " * 10) + "\n\n"
    "# Embedded dispatch instruction (echoed by provider lane):\n"
    "The dispatch uses `**Model**: kimi-k3` and `**Provider**: kimi` for routing.\n"
    "The worker must stamp its own identity as `**Model**: sonnet` in the report header.\n"
)

# The same report format, but this time the real identity block IS present
# mid-file (outside the 2000-char header) and is NOT inside backticks.
_ECHOED_WITH_REAL_MIDFILE_BLOCK = (
    "# Completion Report\n"
    "**Status**: success\n"
    "**Dispatch-ID**: 20260809-oi1101-echoed-real\n\n"
    "## Summary\n"
    "A provider-lane report that echoes its dispatch instruction in prose "
    "but also carries the real identity block mid-file, un-backticked. "
    "This summary is comfortably longer than fifty non-whitespace characters.\n\n"
    "## Changes\n"
    "- edited scripts/foo.py\n\n"
    "## Verification\n"
    "- ran pytest tests/test_foo.py; all green\n\n"
    "## Open Items\n"
    "None\n\n"
    + ("# padding to push the real block past the header window " * 10) + "\n\n"
    "# Embedded dispatch instruction (echoed by provider lane):\n"
    "The dispatch uses `**Model**: kimi-k3` for routing.\n\n"
    "# Real identity block mid-file (NOT inside backticks):\n"
    "**Model**: deepseek-v4-pro\n"
    "**Provider**: deepseek\n"
)

# Mid-file fallback finds a model value with spaces (prose, not a real model).
_PROSE_MODEL_VALUE_BODY = (
    "# Completion Report\n"
    "**Status**: success\n"
    "**Dispatch-ID**: 20260809-oi1101-prose-model\n\n"
    "## Summary\n"
    "A report whose echoed instruction contains a sentence about the model "
    "field that produces a 70-character prose string as the matched value. "
    "This summary is comfortably longer than fifty non-whitespace characters.\n\n"
    "## Changes\n"
    "- edited scripts/foo.py\n\n"
    "## Verification\n"
    "- ran pytest tests/test_foo.py; all green\n\n"
    "## Open Items\n"
    "None\n\n"
    + ("# padding to push the real block past the header window " * 10) + "\n\n"
    "# The instruction explains that the real model field names the provider:\n"
    "**Model**: the real model that ran this dispatch is named in the header above\n"
)

# Mid-file fallback finds a model value with backticks.
_BACKTICK_MODEL_VALUE_BODY = (
    "# Completion Report\n"
    "**Status**: success\n"
    "**Dispatch-ID**: 20260809-oi1101-backtick-model\n\n"
    "## Summary\n"
    "A report whose mid-file fallback matches a model value that contains "
    "backtick characters, which is structurally not a valid model name. "
    "This summary is comfortably longer than fifty non-whitespace characters.\n\n"
    "## Changes\n"
    "- edited scripts/foo.py\n\n"
    "## Verification\n"
    "- ran pytest tests/test_foo.py; all green\n\n"
    "## Open Items\n"
    "None\n\n"
    + ("# padding " * 50) + "\n\n"
    "**Model**: `sonnet`\n"
)


def test_echoed_instruction_inside_backticks_does_not_produce_model(tmp_path):
    """When `**Model**: value` appears inside inline code (backticks) in an
    echoed dispatch instruction, the mid-file fallback must skip it and not
    extract a model value from it."""
    r = _parse(tmp_path, _ECHOED_INSTRUCTION_WITH_INLINE_CODE_BODY)
    # The real model is NOT in the header (pushed past 2000 chars), and the
    # mid-file match inside backticks is skipped.  Result: no model key.
    assert "model" not in r
    assert "provider" not in r


def test_real_mid_file_block_still_works_outside_backticks(tmp_path):
    """The mid-file fallback still recovers a real identity block that is NOT
    inside backticks, even when an inline-code mention of the same field exists
    elsewhere in the echoed instruction."""
    r = _parse(tmp_path, _ECHOED_WITH_REAL_MIDFILE_BLOCK)
    assert r["model"] == "deepseek-v4-pro"
    assert r["provider"] == "deepseek"


def test_model_value_with_spaces_is_rejected(tmp_path):
    """A mid-file match whose captured value contains spaces (prose, not a real
    model name) is rejected by the shape guard and treated as absent."""
    r = _parse(tmp_path, _PROSE_MODEL_VALUE_BODY)
    # A prose value with spaces is not a valid model name — the key must be absent.
    assert "model" not in r


def test_model_value_with_backticks_is_rejected(tmp_path):
    """A mid-file match whose captured value contains backticks (e.g. `sonnet`)
    is rejected by the shape guard and treated as absent."""
    r = _parse(tmp_path, _BACKTICK_MODEL_VALUE_BODY)
    # A value containing backticks is not a valid model name.
    assert "model" not in r


def test_prose_model_rejected_at_extraction_time(tmp_path):
    """End-to-end: a report whose model field contains spaces is caught by the
    report_parser's own shape guard at extraction time. The receipt is emitted
    without a model key, and append_receipt then refuses it as ``missing_model``.
    The ``invalid_model_shape`` code is the validator's secondary defense for
    values that reach it — the extraction guard catches them first."""
    report_body = (
        "# Completion Report\n"
        "**Status**: success\n"
        "**Dispatch-ID**: 20260809-oi1101-shape\n"
        "**Model**: the real model that ran this dispatch\n"
        "**Provider**: claude\n\n"
        "## Summary\n"
        "This report stamps a prose model value that the fail-closed shape "
        "guard must refuse before it ever lands in the grootboek.\n\n"
        "## Changes\n"
        "- edited scripts/foo.py\n\n"
        "## Verification\n"
        "- ran pytest tests/test_foo.py; all green\n\n"
        "## Open Items\n"
        "None\n"
    )
    # The report_parser must strip the prose model value (spaces = not a model name).
    receipt = _parse(tmp_path, report_body)
    assert "model" not in receipt, (
        f"Prose model value must be stripped at extraction time, got: {receipt.get('model')}"
    )

    # append_receipt then refuses it as missing_model.
    report = tmp_path / "report.md"
    report.write_text(report_body, encoding="utf-8")
    env = _sandbox_env(tmp_path)
    parse = subprocess.run(
        [sys.executable, str(REPORT_PARSER), str(report)],
        capture_output=True, text=True, env=env,
    )
    assert parse.returncode == 0
    receipt_json = json.loads(parse.stdout)
    assert "model" not in receipt_json

    append = subprocess.run(
        [sys.executable, str(APPEND_RECEIPT)],
        input=json.dumps(receipt_json),
        capture_output=True, text=True, env=env,
    )
    assert append.returncode != 0
    codes = [
        json.loads(line).get("code")
        for line in append.stderr.splitlines()
        if line.strip().startswith("{")
    ]
    assert "missing_model" in codes, (
        f"Expected missing_model (stripped at extraction), got: {codes}"
    )


def test_invalid_model_shape_rejected_directly_by_validator(tmp_path):
    """Direct validation test: a receipt with a prose model value that bypasses
    report_parser extraction is caught by the validator's own shape guard with
    code ``invalid_model_shape``."""
    # Build a receipt directly (bypassing report_parser) with a prose model.
    receipt = {
        "event_type": "task_complete",
        "receipt_kind": "dispatch",
        "dispatch_id": "20260809-oi1101-shape-direct",
        "task_id": "unknown",
        "terminal": "unknown",
        "status": "success",
        "model": "a 70-character prose sentence that mentions the model field by name",
        "provider": "claude",
        "timestamp": "2026-08-09T12:00:00Z",
    }
    env = _sandbox_env(tmp_path)
    append = subprocess.run(
        [sys.executable, str(APPEND_RECEIPT)],
        input=json.dumps(receipt),
        capture_output=True, text=True, env=env,
    )
    assert append.returncode != 0
    codes = [
        json.loads(line).get("code")
        for line in append.stderr.splitlines()
        if line.strip().startswith("{")
    ]
    assert "invalid_model_shape" in codes, (
        f"Expected invalid_model_shape from validator, got: {codes}"
    )


def test_valid_model_passes_shape_guard(tmp_path):
    """A real model name (sonnet, no spaces, no backticks) still passes
    validation and the receipt lands in the sandboxed grootboek."""
    receipt, append = _parser_then_append(tmp_path, _BOLD_MODEL_BODY)
    assert append.returncode == 0, f"append_receipt refused a healthy receipt: {append.stderr}"
    assert "invalid_model_shape" not in append.stderr
    assert receipt["model"] == "opus"


# ---------------------------------------------------------------------------
# OI-1194: prose model values are rejected (empty field, fail-closed)
# ---------------------------------------------------------------------------

# The exact prose strings observed in the ledger's `model` field (5 receipts,
# 3 distinct strings). Each is documentation/instruction text that explains
# how to write the Model field, captured verbatim by an unguarded extraction
# path instead of a real model id.
_PROSE_MODEL_VALUES = [
    "` / `**Provider**:`.",
    "sonnet`). Unknown-achtige frontmatter-waarden worden overgeslagen.",
    "` / `**Provider**:` regel. Zonder die identiteitsregels landt je receipt niet.",
]


def _frontmatter_model_body(model_value: str) -> str:
    return f"""---
schema_version: 1
dispatch_id: 20260814-oi1194-frontmatter-prose
provider: claude
model: {model_value}
---

# Completion Report
**Status**: success
**Dispatch-ID**: 20260814-oi1194-frontmatter-prose

## Summary
A report whose dispatch-envelope frontmatter carries documentation text as
the model value; the parser must reject it and omit the model key.

## Changes
- none

## Verification
- none

## Open Items
None
"""


@pytest.mark.parametrize("prose", _PROSE_MODEL_VALUES)
def test_frontmatter_prose_model_is_rejected(tmp_path, prose):
    """The three observed prose strings (5 receipts) lead to an empty model
    field, never an accepted value. The frontmatter `model:` path is the one
    that historically accepted prose verbatim (no shape guard)."""
    r = _parse(tmp_path, _frontmatter_model_body(prose))
    assert "model" not in r, f"prose model value must be stripped: {prose!r}"


_PLAIN_BACKTICK_MODEL_BODY = """# Dispatch 20260814-oi1194-plain-backtick

Dispatch-ID: 20260814-oi1194-plain-backtick
Provider: claude
Model: `sonnet`
Terminal: T1

## Summary

A plain-text header block whose model value is a backtick-wrapped token. The
parser must reject it, not accept the backtick-bearing string as a model id.

## Changes
- none

## Verification
- none

## Open Items
None
"""


def test_plain_header_backtick_model_is_rejected(tmp_path):
    """The plain-text `Model:` fallback captures \\S+ and previously accepted a
    backtick-wrapped value without any shape check."""
    r = _parse(tmp_path, _PLAIN_BACKTICK_MODEL_BODY)
    assert "model" not in r


def test_yaml_block_prose_model_is_rejected(tmp_path):
    """A ```yaml block that echoes a prose model value must be rejected — the
    YAML metadata path previously accepted prose without any shape check."""
    body = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260814-oi1194-yaml-prose

```yaml
model: the real model that ran this dispatch
provider: claude
```

## Summary
A report whose yaml metadata block carries a prose model value; the parser must
reject it and omit the model key, leaving the fail-closed check to refuse it.

## Changes
- none

## Verification
- none

## Open Items
None
"""
    r = _parse(tmp_path, body)
    assert "model" not in r


def test_yaml_block_prose_does_not_clobber_valid_header(tmp_path):
    """A prose model value in a ```yaml block must never clobber a valid header
    value already parsed — the silent-fallback class PR #1491 removed."""
    body = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260814-oi1194-yaml-clobber
**Model**: sonnet
**Provider**: claude

```yaml
model: the real model that ran this dispatch
provider: claude
```

## Summary
A report whose header carries a real model while a trailing yaml block echoes
a prose placeholder; the header value must survive.

## Changes
- none

## Verification
- none

## Open Items
None
"""
    r = _parse(tmp_path, body)
    assert r["model"] == "sonnet"


@pytest.mark.parametrize(
    "variant",
    [
        "deepseek/deepseek-v4-pro",
        "kimi-code/k3",
        "claude-sonnet-5",
        "moonshot/kimi-k2-0905-preview",
    ],
)
def test_resolvable_variant_model_accepted_unharmed(tmp_path, variant):
    """A provider-prefixed / alias spelling that resolves through the registry
    must be accepted intact (not rejected as prose by the plausibility guard)."""
    body = f"""# Completion Report
**Status**: success
**Dispatch-ID**: 20260814-oi1194-variant
**Model**: {variant}
**Provider**: claude

## Summary
A report carrying a provider-prefixed model spelling that normalizes to a
canonical registry key; the plausibility guard must accept it, not reject it.

## Changes
- none

## Verification
- none

## Open Items
None
"""
    r = _parse(tmp_path, body)
    assert r["model"] == variant


# ---------------------------------------------------------------------------
# OI-1102: dispatch register cross-check + cross-processor dedup
# ---------------------------------------------------------------------------

# A report file whose filename does not match any dispatch in the register.
# The converter must cross-check and dead-letter it rather than produce a
# phantom receipt.  The filename "dispatch-20260809-oi1102-phantom.md" has
# a "dispatch-" prefix that produces a phantom dispatch_id.
_PREFIXED_FILENAME_BODY = """# Completion Report
**Status**: success
**Model**: sonnet
**Provider**: claude

## Summary
A report saved with a prefixed filename whose dispatch_id is not in the
register. Must be dead-lettered, not converted to a receipt.

## Changes
- none

## Verification
- none

## Open Items
None
"""


def _make_sandbox_state_dir(tmp_path: Path) -> Path:
    """Create a sandbox state dir with a dispatch register for testing."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _write_dispatch_register(state_dir: Path, dispatch_ids: list) -> None:
    """Write a minimal dispatch_register.ndjson with the given dispatch_ids."""
    import json as _json
    register = state_dir / "dispatch_register.ndjson"
    lines = []
    for did in dispatch_ids:
        lines.append(_json.dumps({
            "timestamp": "2026-08-09T12:00:00Z",
            "event": "dispatch_created",
            "dispatch_id": did,
        }))
    register.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_prefixed_filename_unknown_dispatch_is_dead_lettered(tmp_path: Path):
    """A report file named ``dispatch-<unknown-id>.md`` whose dispatch_id is
    NOT in the register must be dead-lettered, not converted to a receipt."""
    from report_to_receipt_converter import (
        build_receipt_from_report,
    )

    state_dir = _make_sandbox_state_dir(tmp_path)
    # Register knows 20260809-oi1102-known but NOT the phantom ID.
    _write_dispatch_register(state_dir, ["20260809-oi1102-known"])

    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # The filename has a "dispatch-" prefix — the dispatch_id derived from it
    # is "dispatch-20260809-oi1102-phantom", which is not in the register.
    report_path = reports_dir / "dispatch-20260809-oi1102-phantom.md"
    report_path.write_text(_PREFIXED_FILENAME_BODY, encoding="utf-8")

    # The report has NO content-side dispatch_id, so the converter falls back
    # to the filename.
    receipt = build_receipt_from_report(
        report_path,
        _PREFIXED_FILENAME_BODY,
        state_dir=state_dir,
    )

    # Must return None — the phantom ID was dead-lettered.
    assert receipt is None, f"Expected None (dead-lettered), got receipt with dispatch_id={receipt.get('dispatch_id') if receipt else 'N/A'}"

    # The report file must have been moved to the dead-letter directory.
    assert not report_path.exists(), "Report file must be moved (dead-lettered)"

    deadletter_dir = state_dir / "receipt_deadletter"
    assert deadletter_dir.is_dir(), "Dead-letter directory must exist"

    # The quarantined file must be present.
    dead_files = list(deadletter_dir.glob("dispatch-20260809-oi1102-phantom*"))
    assert len(dead_files) >= 1, f"No dead-letter file found in {deadletter_dir}"

    # INDEX.txt must contain an entry.
    index = (deadletter_dir / "INDEX.txt").read_text()
    assert "unknown_dispatch" in index
    assert "dispatch-20260809-oi1102-phantom" in index


def test_known_dispatch_from_filename_is_not_dead_lettered(tmp_path: Path):
    """A filename-derived dispatch_id that IS in the register must NOT be
    dead-lettered — it must proceed to produce a (contract-invalid) receipt,
    which is the existing behavior for filename-only reports."""
    from report_to_receipt_converter import (
        build_receipt_from_report,
    )

    state_dir = _make_sandbox_state_dir(tmp_path)
    _write_dispatch_register(state_dir, ["20260809-oi1102-known"])

    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / "20260809-oi1102-known.md"
    report_path.write_text(_PREFIXED_FILENAME_BODY, encoding="utf-8")

    receipt = build_receipt_from_report(
        report_path,
        _PREFIXED_FILENAME_BODY,
        state_dir=state_dir,
    )

    # Must NOT be None — the dispatch IS known, so it proceeds.
    assert receipt is not None, (
        "Known dispatch from filename must not be dead-lettered"
    )
    # The report file must still exist (not dead-lettered).
    assert report_path.exists(), "Report file for known dispatch must remain in place"


def test_cross_processor_dedup_writes_both_watermarks(tmp_path: Path):
    """After a successful scan-and-convert cycle, both the Python watermark
    (report_to_receipt_processed.txt) AND the Bash watermark
    (processed_receipts.txt) must contain the report's hash."""
    from report_to_receipt_converter import (
        scan_and_convert, _compute_sha256, _BASH_WATERMARK_FILENAME,
    )

    state_dir = _make_sandbox_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # A report with model/provider so it passes validation.
    report_path = reports_dir / "20260809-oi1102-dedup.md"
    report_path.write_text(_BOLD_MODEL_BODY, encoding="utf-8")

    stats = scan_and_convert(
        [reports_dir],
        state_dir=state_dir,
        cache_window_seconds=300,
    )

    assert stats.new_count == 1, f"Expected 1 new receipt, got {stats}"

    file_hash = _compute_sha256(report_path)

    # Python watermark must contain the hash.
    py_watermark = (state_dir / "report_to_receipt_processed.txt").read_text()
    assert file_hash in py_watermark, "Python watermark must contain report hash"

    # Bash watermark must also contain the hash (cross-processor dedup).
    bash_watermark = (state_dir / _BASH_WATERMARK_FILENAME).read_text()
    assert file_hash in bash_watermark, (
        "Bash watermark must contain report hash for cross-processor dedup"
    )


def test_bash_watermark_prevents_reconversion(tmp_path: Path):
    """When a report's hash is already in the Bash watermark (simulating a
    prior Bash-processor conversion), a subsequent Python scan must skip it."""
    from report_to_receipt_converter import (
        scan_and_convert, _compute_sha256, _BASH_WATERMARK_FILENAME,
    )

    state_dir = _make_sandbox_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / "20260809-oi1102-bash-first.md"
    report_path.write_text(_BOLD_MODEL_BODY, encoding="utf-8")

    # Pre-populate the Bash watermark with the report's hash (simulating
    # a prior Bash-processor conversion).
    file_hash = _compute_sha256(report_path)
    bash_watermark = state_dir / _BASH_WATERMARK_FILENAME
    bash_watermark.write_text(file_hash + "\n", encoding="utf-8")

    stats = scan_and_convert(
        [reports_dir],
        state_dir=state_dir,
        cache_window_seconds=300,
    )

    # No new receipts — the file was already in the Bash watermark.
    assert stats.new_count == 0, (
        f"Expected 0 new receipts (Bash watermark should prevent re-conversion), got {stats}"
    )
    assert stats.attempted_count == 0, (
        f"Expected 0 attempted conversions, got {stats}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# OI-1092: receipt timestamp = time of work, not time of processing
# ---------------------------------------------------------------------------


class TestWorkTimestamp:
    """The receipt timestamp must be the time the WORK was done, not when it
    was PROCESSED.  Resolution: explicit report date → file mtime → fail-closed.
    """

    def test_explicit_datum_field_used(self, tmp_path):
        """A report with **Datum**: <date> uses that date."""
        body = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260807-datum-test
**Datum**: 2026-08-07T14:30:00Z
**Model**: sonnet
**Provider**: claude

## Summary
This report carries an explicit Datum field. The receipt timestamp must be
2026-08-07T14:30:00Z, not the processing time.

## Changes
- edited scripts/foo.py

## Verification
- ran pytest tests/test_foo.py; all green

## Open Items
None
"""
        r = _parse(tmp_path, body)
        assert "timestamp" in r
        ts = r["timestamp"]
        assert "2026-08-07" in ts, f"Expected work date, got: {ts}"

    def test_explicit_date_field_used(self, tmp_path):
        """A report with **Date**: <date> uses that date."""
        body = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260806-date-test
**Date**: 2026-08-06T10:15:00Z
**Model**: sonnet
**Provider**: claude

## Summary
This report carries an explicit Date field. The receipt must use 2026-08-06.

## Changes
- edited scripts/foo.py

## Verification
- ran pytest tests/test_foo.py; all green

## Open Items
None
"""
        r = _parse(tmp_path, body)
        assert "2026-08-06" in r["timestamp"]

    def test_metadata_timestamp_field_used(self, tmp_path):
        """A report with **Timestamp**: <date> already in metadata is used."""
        body = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260805-ts-test
**Timestamp**: 2026-08-05T08:00:00Z
**Model**: sonnet
**Provider**: claude

## Summary
This report stamps its own Timestamp field. The receipt must carry that value.

## Changes
- edited scripts/foo.py

## Verification
- ran pytest tests/test_foo.py; all green

## Open Items
None
"""
        r = _parse(tmp_path, body)
        assert "2026-08-05" in r["timestamp"]

    def test_file_mtime_fallback(self, tmp_path):
        """When no explicit date field exists, file mtime is used."""
        body = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260804-mtime-test
**Model**: sonnet
**Provider**: claude

## Summary
No explicit date field — the receipt must use the file's modification time.
This summary is comfortably longer than fifty non-whitespace characters.

## Changes
- edited scripts/foo.py

## Verification
- ran pytest tests/test_foo.py; all green

## Open Items
None
"""
        p = tmp_path / "report.md"
        p.write_text(body, encoding="utf-8")
        r = ReportParser().parse_report(str(p))
        assert "timestamp" in r
        ts = r["timestamp"]
        # Must be parseable as ISO-8601
        from datetime import datetime as _datetime
        _datetime.fromisoformat(ts)
        # Must NOT be the processing-time workaround — the mtime was just set
        # when we wrote the file, so it should be very close to now, but the
        # important thing is that it is parseable ISO-8601 UTC.

    def test_fail_closed_no_timestamp_produces_error(self, tmp_path):
        """When neither metadata date nor file exists, return error (fail-closed).

        The report_parser must refuse to emit a receipt with a false timestamp.
        This is the backfill guard: processing old reports with today's date
        is worse than a gap in the ledger.
        """
        nonexistent = tmp_path / "does-not-exist.md"
        r = ReportParser().parse_report(str(nonexistent))
        assert "error" in r, f"Expected error for missing file, got: {r}"

    def test_timestamp_is_not_processing_time(self, tmp_path):
        """Prove the guard binds: the timestamp is NOT datetime.utcnow().

        We write a report whose explicit **Datum** is 2026-08-03 and assert
        the receipt timestamp CONTAINS that date.  If the fix were removed
        (using datetime.utcnow()), the timestamp would contain today's date
        (2026-08-10) instead — and this test would catch it.
        """
        body = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260803-guard-bind
**Datum**: 2026-08-03T12:00:00Z
**Model**: sonnet
**Provider**: claude

## Summary
Explicit work date of 3 August. The receipt must carry 2026-08-03, not today.

## Changes
- edited scripts/foo.py

## Verification
- ran pytest tests/test_foo.py; all green

## Open Items
None
"""
        r = _parse(tmp_path, body)
        assert r["timestamp"] == "2026-08-03T12:00:00+00:00", (
            f"Expected 2026-08-03T12:00:00+00:00 (work time), "
            f"got {r['timestamp']} (likely processing time — fix not active)"
        )

    def test_recorded_at_field_used(self, tmp_path):
        """A report with **Recorded-At**: <date> uses that date."""
        body = """# Completion Report
**Status**: success
**Dispatch-ID**: 20260802-rec-test
**Recorded-At**: 2026-08-02T16:45:00Z
**Model**: sonnet
**Provider**: claude

## Summary
This report uses Recorded-At as timestamp. Receipt must carry 2026-08-02.

## Changes
- edited scripts/foo.py

## Verification
- ran pytest tests/test_foo.py; all green

## Open Items
None
"""
        r = _parse(tmp_path, body)
        assert "2026-08-02" in r["timestamp"]
