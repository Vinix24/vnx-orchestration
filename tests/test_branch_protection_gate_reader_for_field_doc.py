#!/usr/bin/env python3
"""Guards the cross-reference between `_run_branch_protection_gate`'s
docstring (step d) and docs/operations/FORGE_GATE.md (OI-1672, golf Bx D6).

The reader-for-field contract — extending ``branch_protection.yaml``'s
schema always takes two PRs, reader first, field second — is documented
prose, not enforced code. Nothing else in the suite reads docstrings, so a
future rewrite of the docstring or of FORGE_GATE.md could silently drop the
link between them. This test fails when either half disappears: the
reference in the docstring, or the anchored section in FORGE_GATE.md.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge  # noqa: E402

FORGE_GATE_DOC = VNX_ROOT / "docs" / "operations" / "FORGE_GATE.md"
ANCHOR = "OI-1672"


def _docstring() -> str:
    doc = inspect.getdoc(pr_merge._run_branch_protection_gate)
    assert doc, "_run_branch_protection_gate has no docstring"
    return doc


def test_docstring_references_forge_gate_doc_and_anchor() -> None:
    doc = _docstring()
    assert "docs/operations/FORGE_GATE.md" in doc, (
        "step (d) docstring lost its cross-reference to "
        "docs/operations/FORGE_GATE.md (the reader-for-field contract, OI-1672)"
    )
    assert ANCHOR in doc, (
        f"step (d) docstring lost its {ANCHOR} anchor — the link to the "
        "FORGE_GATE.md section documenting the two-PR schema-extension contract"
    )


def test_forge_gate_doc_has_the_anchored_section() -> None:
    assert FORGE_GATE_DOC.exists(), f"{FORGE_GATE_DOC} is missing"
    text = FORGE_GATE_DOC.read_text(encoding="utf-8")
    headings_with_anchor = [
        line
        for line in text.splitlines()
        if re.match(r"^#{1,6}\s", line) and ANCHOR in line
    ]
    assert headings_with_anchor, (
        f"docs/operations/FORGE_GATE.md no longer has a heading carrying {ANCHOR} "
        "— the section the pr_merge.py docstring points at is gone or was "
        "renamed without updating the anchor"
    )
