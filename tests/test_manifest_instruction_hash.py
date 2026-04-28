#!/usr/bin/env python3
"""T5-PR2: instruction_sha256 field in dispatch manifest.

Covers:
  A. Known instruction → manifest.json contains expected sha256[:16]
  B. Unicode instruction → hash computed correctly
  C. Empty instruction → hash present (sha256 of empty string)
  D. Existing manifest fields preserved (regression)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))

import subprocess_dispatch as sd


def _write_manifest(tmp_path: Path, instruction: str, dispatch_id: str = "test-dispatch-A") -> dict:
    with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
        manifest_path = sd._write_manifest(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            model="sonnet",
            role="backend-developer",
            instruction=instruction,
            commit_hash_before="abc123",
            branch="feat/t5",
        )
    assert manifest_path is not None
    return json.loads(Path(manifest_path).read_text())


def test_manifest_has_instruction_sha256_for_known_instruction(tmp_path):
    instruction = "Do something important"
    expected = hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:16]
    data = _write_manifest(tmp_path, instruction)
    assert "instruction_sha256" in data
    assert data["instruction_sha256"] == expected


def test_manifest_instruction_sha256_unicode(tmp_path):
    instruction = "Execute: café résumé naïve 你好 🚀"
    expected = hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:16]
    data = _write_manifest(tmp_path, instruction, dispatch_id="test-dispatch-B")
    assert data["instruction_sha256"] == expected


def test_manifest_instruction_sha256_empty_instruction(tmp_path):
    instruction = ""
    expected = hashlib.sha256(b"").hexdigest()[:16]
    data = _write_manifest(tmp_path, instruction, dispatch_id="test-dispatch-C")
    assert "instruction_sha256" in data
    assert data["instruction_sha256"] == expected


def test_manifest_existing_fields_preserved(tmp_path):
    instruction = "Some task"
    data = _write_manifest(tmp_path, instruction, dispatch_id="test-dispatch-D")
    assert data["dispatch_id"] == "test-dispatch-D"
    assert data["terminal"] == "T1"
    assert data["model"] == "sonnet"
    assert data["role"] == "backend-developer"
    assert data["commit_hash_before"] == "abc123"
    assert data["branch"] == "feat/t5"
    assert data["instruction_chars"] == len(instruction)
    assert "timestamp" in data
    assert "instruction_sha256" in data
