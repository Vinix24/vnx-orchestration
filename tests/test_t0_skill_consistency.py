"""
Tests for T0 CLAUDE.md and SKILL.md consistency.

Case A: every script referenced in SKILL.md actually exists in skill scripts dir
Case B: every command pattern shown in CLAUDE.md is syntactically valid (bash -n)
Case C: no broken markdown links within the two doc files
Case D: policy codes (A1, B1, etc) in CLAUDE.md have no duplicates
"""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SKILL_MD = REPO_ROOT / "skills" / "t0-orchestrator" / "SKILL.md"
CLAUDE_MD = REPO_ROOT / ".claude" / "terminals" / "T0" / "CLAUDE.md"
SCRIPTS_DIR = REPO_ROOT / "skills" / "t0-orchestrator" / "scripts"


# ---------------------------------------------------------------------------
# Case A: scripts referenced in SKILL.md must exist on disk
# ---------------------------------------------------------------------------

def _extract_script_references(md_text: str) -> list[str]:
    """Return relative script paths mentioned in markdown code spans or bare paths."""
    patterns = [
        # code-fenced paths like `skills/t0-orchestrator/scripts/foo.sh`
        r"`(skills/t0-orchestrator/scripts/[\w._-]+\.sh)`",
        # bare paths in text
        r"\b(skills/t0-orchestrator/scripts/[\w._-]+\.sh)\b",
    ]
    found = []
    for pat in patterns:
        found.extend(re.findall(pat, md_text))
    return list(dict.fromkeys(found))  # deduplicate, preserve order


class TestCaseA:
    def test_all_referenced_scripts_exist(self):
        text = SKILL_MD.read_text()
        refs = _extract_script_references(text)
        missing = [r for r in refs if not (REPO_ROOT / r).exists()]
        assert not missing, f"Scripts referenced in SKILL.md but missing on disk: {missing}"

    def test_no_dead_scripts_in_skill_md(self):
        """Confirm the four deleted scripts are no longer referenced."""
        text = SKILL_MD.read_text()
        dead = [
            "provider_capabilities.sh",
            "staging_helper.sh",
            "queue_status.sh",
            "deliverable_review.sh",
        ]
        found = [d for d in dead if d in text]
        assert not found, f"Dead scripts still referenced in SKILL.md: {found}"

    def test_surviving_scripts_exist(self):
        for name in ("dispatch_guard.sh", "intelligence.sh"):
            assert (SCRIPTS_DIR / name).exists(), f"Surviving script missing: {name}"


# ---------------------------------------------------------------------------
# Case B: bash code blocks in CLAUDE.md are syntactically valid
# ---------------------------------------------------------------------------

def _extract_bash_blocks(md_text: str) -> list[str]:
    """Return content of ```bash ... ``` fenced code blocks."""
    return re.findall(r"```bash\n(.*?)```", md_text, re.DOTALL)


class TestCaseB:
    def test_bash_blocks_parse(self):
        text = CLAUDE_MD.read_text()
        blocks = _extract_bash_blocks(text)
        assert blocks, "No bash blocks found in CLAUDE.md — check extraction regex"
        failures = []
        for i, block in enumerate(blocks):
            # Replace placeholder tokens so bash -n doesn't choke on them
            sanitized = re.sub(r"<[^>]+>", "placeholder", block)
            with tempfile.NamedTemporaryFile(suffix=".sh", mode="w", delete=False) as f:
                f.write("#!/bin/bash\n")
                f.write(sanitized)
                tmp_path = f.name
            result = subprocess.run(
                ["bash", "-n", tmp_path], capture_output=True, text=True
            )
            if result.returncode != 0:
                failures.append((i, result.stderr.strip()))
            Path(tmp_path).unlink(missing_ok=True)
        assert not failures, f"Bash syntax errors in CLAUDE.md blocks: {failures}"


# ---------------------------------------------------------------------------
# Case C: no broken markdown links within the two doc files
# ---------------------------------------------------------------------------

def _extract_md_links(md_text: str) -> list[str]:
    """Return local (non-URL) link targets from [text](target) patterns."""
    all_links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", md_text)
    return [target for _, target in all_links if not target.startswith("http")]


class TestCaseC:
    def _check_links(self, md_file: Path):
        text = md_file.read_text()
        links = _extract_md_links(text)
        missing = []
        for link in links:
            # Strip anchor fragments
            path_part = link.split("#")[0]
            if not path_part:
                continue
            # Resolve relative to repo root
            target = REPO_ROOT / path_part
            if not target.exists():
                missing.append(link)
        return missing

    def test_skill_md_links(self):
        missing = self._check_links(SKILL_MD)
        assert not missing, f"Broken links in SKILL.md: {missing}"

    def test_claude_md_links(self):
        missing = self._check_links(CLAUDE_MD)
        assert not missing, f"Broken links in CLAUDE.md: {missing}"


# ---------------------------------------------------------------------------
# Case D: policy codes in CLAUDE.md have no duplicates
# ---------------------------------------------------------------------------

class TestCaseD:
    def test_policy_codes_no_duplicates(self):
        text = CLAUDE_MD.read_text()
        # Match bold policy codes like **A1**, **B2**, **E6**, etc.
        codes = re.findall(r"\*\*([A-Z]\d+)(?:\s+\([^)]+\))?\*\*", text)
        seen = {}
        duplicates = []
        for code in codes:
            if code in seen:
                seen[code] += 1
                if seen[code] == 2:
                    duplicates.append(code)
            else:
                seen[code] = 1
        assert not duplicates, f"Duplicate policy codes in CLAUDE.md: {duplicates}"

    def test_policy_codes_present(self):
        text = CLAUDE_MD.read_text()
        required = ["A1", "A2", "B1", "B2", "B3", "B4", "C1", "D1", "D2", "E1"]
        # Codes may appear as **A1**, **A1 (label)**, or **A1:** — accept any bold start
        missing = [c for c in required if not re.search(rf"\*\*{re.escape(c)}[\s(*:]", text)]
        assert not missing, f"Expected policy codes missing from CLAUDE.md: {missing}"
