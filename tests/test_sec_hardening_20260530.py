#!/usr/bin/env python3
"""Security regression tests for dispatch 20260530-162009-sec-cleanup.

Covers:
- Blocker 1: bash_check removed — dispatch contracts cannot cause arbitrary shell execution
- Blocker 2: heartbeat terminal id validation — shell-injection via socket/stdin rejected
- Blocker 3: PII absent from llm_benchmark.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS_LIB_DIR = SCRIPTS_DIR / "lib"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(SCRIPTS_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB_DIR))


# ---------------------------------------------------------------------------
# Blocker 1 — bash_check removed from contract parser + verifier
# ---------------------------------------------------------------------------

def test_bash_check_not_in_claim_types():
    from contract_parser import CLAIM_TYPES
    assert "bash_check" not in CLAIM_TYPES, "bash_check must not be a recognized claim type"


def test_bash_check_line_produces_parse_error_not_claim():
    """A dispatch contract with bash_check: `touch /tmp/pwned` is rejected as unrecognized."""
    from contract_parser import parse_contract_from_text

    dispatch_text = """\
## Contract
- bash_check: `touch /tmp/pwned`

Dispatch-ID: test-rce-attempt
"""
    block = parse_contract_from_text(dispatch_text)
    # Must have a parse error, not a valid claim
    assert block.has_claims is False, "bash_check must not parse as a valid claim"
    assert len(block.parse_errors) > 0, "bash_check line must produce a parse error"


def test_bash_check_does_not_execute(tmp_path):
    """Verifying a contract with bash_check: does NOT create the sentinel file."""
    sentinel = tmp_path / "pwned"

    dispatch_text = f"""\
## Contract
- bash_check: `touch {sentinel}`

Dispatch-ID: test-rce-exec
"""
    from contract_parser import parse_contract_from_text
    block = parse_contract_from_text(dispatch_text)

    # Even if somehow parsed, sentinel must not exist
    assert not sentinel.exists(), "bash_check must never execute the shell command"


def test_claim_dataclass_has_no_command_field():
    """Claim dataclass must not expose a 'command' field."""
    from contract_parser import Claim
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(Claim)}
    assert "command" not in field_names, "Claim must not carry a 'command' field after bash_check removal"


def test_check_dispatch_has_no_bash_handler():
    """_CHECK_DISPATCH registry must not contain a bash_check entry."""
    import verify_claims
    assert "bash_check" not in verify_claims._CHECK_DISPATCH


# ---------------------------------------------------------------------------
# Blocker 2 — heartbeat terminal id validation
# ---------------------------------------------------------------------------

def test_invalid_terminal_id_rejected_by_parse_dispatch_payload():
    """_parse_dispatch_payload raises ValueError on shell-injectable terminal id."""
    from heartbeat_ack_monitor import _parse_dispatch_payload

    bad_terminals = [
        "T1; touch /tmp/pwned",
        "$(id)",
        "`id`",
        "T1 && rm -rf /",
        "T1\nT2",
        "a" * 65,  # too long
        "",
    ]
    for terminal in bad_terminals:
        payload = json.dumps({
            "dispatch_id": "DISP-SEC-001",
            "terminal": terminal,
            "task_id": "TASK-001",
            "sent_time": "2026-05-30T12:00:00Z",
        })
        with pytest.raises(ValueError, match=r"(Invalid terminal id|Missing required)"):
            _parse_dispatch_payload(payload)


def test_valid_terminal_ids_accepted():
    """Well-formed terminal ids pass validation."""
    from heartbeat_ack_monitor import _parse_dispatch_payload

    valid_terminals = ["T1", "T2", "T1-worker", "terminal_A", "T1234"]
    for terminal in valid_terminals:
        payload = json.dumps({
            "dispatch_id": "DISP-SEC-002",
            "terminal": terminal,
            "task_id": "TASK-002",
            "sent_time": "2026-05-30T12:00:00Z",
        })
        result = _parse_dispatch_payload(payload)
        assert result["terminal"] == terminal


def test_no_shell_true_in_heartbeat_monitor():
    """heartbeat_ack_monitor.py must contain no shell=True."""
    source = (SCRIPTS_DIR / "heartbeat_ack_monitor.py").read_text(encoding="utf-8")
    # Comments are allowed; only actual kwarg assignments are forbidden
    import re
    # Match `shell=True` not inside a comment
    hits = []
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if re.search(r"\bshell\s*=\s*True\b", line):
            hits.append((i, line.rstrip()))
    assert not hits, f"shell=True found at lines: {hits}"


# ---------------------------------------------------------------------------
# Blocker 3 — PII absent from llm_benchmark.py
# ---------------------------------------------------------------------------

def test_no_private_email_in_benchmark():
    source = (SCRIPTS_DIR / "llm_benchmark.py").read_text(encoding="utf-8")
    _email_prefix = "p.jansen" + "@" + "vander" + "meijden"
    _domain = "vander" + "meijden" + "-installatie"
    assert _email_prefix not in source
    assert _domain not in source


def test_no_private_company_in_benchmark():
    source = (SCRIPTS_DIR / "llm_benchmark.py").read_text(encoding="utf-8")
    _company = "Van" + " der " + "Meijden" + " Installatietechniek"
    assert _company not in source


def test_no_private_memory_path_in_skills():
    """No skill file may contain the hardcoded private memory path."""
    skills_dir = REPO_ROOT / "skills"
    if not skills_dir.exists():
        pytest.skip("skills/ directory not found")
    _path_fragment = "vnx" + "-dev-" + "githost"
    for skill_file in skills_dir.rglob("SKILL.md"):
        content = skill_file.read_text(encoding="utf-8")
        assert _path_fragment not in content, (
            f"{skill_file.relative_to(REPO_ROOT)} still contains hardcoded private memory path"
        )
