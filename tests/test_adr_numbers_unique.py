#!/usr/bin/env python3
"""ADR-nummer-uniciteit op de bestandsboom (Golf B, B6).

#1790 en #1792 botsten op 06-09 allebei op ADR-038 omdat de VNX CI-workflow
op de merge-ref draait (niet de echte, post-merge main) en
``required_status_checks.strict`` uit staat — een CI-test op de PR-diff kan
die botsing nooit vangen, want beide runs zien onafhankelijk een main die nog
bij ADR-037 eindigt. Deze test toetst een andere, complementaire invariant:
op de checkout zelf (waar ``pytest`` op draait, dus altijd de post-merge
main in de CI-sweep) mag geen ADR-nummer dubbel voorkomen. De merge-time
kant van dezelfde invariant — een PR die een nummer claimt dat al op de
ECHTE main staat, bewaakt via de GitHub API — zit in
``scripts/lib/merge_preflight_adr_check.py`` (getest in
``test_merge_preflight_adr_check.py``); deze test hier is stdlib-only en
draait daarom ook in de CI-light-lijst voor een docs-only PR.
"""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

VNX_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DIR = VNX_ROOT / "docs" / "governance" / "decisions"

ADR_FILENAME_RE = re.compile(r"^ADR-(\d+)-.+\.md$")


def find_duplicate_adr_numbers(decisions_dir: Path) -> Dict[str, List[str]]:
    """Return {number: [filenames, ...]} for every ADR number claimed by more
    than one file in ``decisions_dir``. Numbers are normalised (int-cast) so
    ``ADR-007`` and a hypothetical ``ADR-7`` collide too. Empty dict means
    every number is unique.
    """
    by_number: Dict[str, List[str]] = defaultdict(list)
    for path in sorted(decisions_dir.glob("ADR-*.md")):
        m = ADR_FILENAME_RE.match(path.name)
        if not m:
            continue
        number = str(int(m.group(1)))
        by_number[number].append(path.name)
    return {number: names for number, names in by_number.items() if len(names) > 1}


def _format_duplicates(duplicates: Dict[str, List[str]]) -> str:
    return "; ".join(
        f"ADR-{number}: {', '.join(names)}" for number, names in sorted(duplicates.items(), key=lambda kv: int(kv[0]))
    )


class TestAdrNumbersUniqueOnRealTree:
    def test_no_duplicate_adr_numbers(self):
        duplicates = find_duplicate_adr_numbers(DECISIONS_DIR)
        assert not duplicates, (
            "ADR-nummer(s) dubbel geclaimd op docs/governance/decisions/: "
            + _format_duplicates(duplicates)
        )


class TestFindDuplicateAdrNumbersDetection:
    """Proves the detector actually fires (nul is eerst een meetfout): a
    duplicate injected into a scratch copy of the real tree must be caught
    and must name both files, not just report a bare count.
    """

    def test_injected_duplicate_is_detected_with_both_filenames(self, tmp_path):
        scratch = tmp_path / "decisions"
        shutil.copytree(DECISIONS_DIR, scratch)

        existing = sorted(scratch.glob("ADR-038-*.md"))
        assert existing, (
            "fixture assumption failed: geen ADR-038-*.md op de echte boom om te dupliceren "
            "(pas het nummer hierboven aan als ADR-038 ooit hernummerd wordt)"
        )
        original_name = existing[0].name
        duplicate = scratch / "ADR-038-injected-duplicate-for-test.md"
        duplicate.write_text("# injected duplicate for test\n")

        duplicates = find_duplicate_adr_numbers(scratch)

        assert "38" in duplicates
        assert original_name in duplicates["38"]
        assert duplicate.name in duplicates["38"]
        assert len(duplicates) == 1, f"onverwachte extra botsingen: {duplicates}"

    def test_no_false_positive_on_distinct_numbers(self, tmp_path):
        scratch = tmp_path / "decisions"
        scratch.mkdir()
        (scratch / "ADR-001-a.md").write_text("# a\n")
        (scratch / "ADR-002-b.md").write_text("# b\n")

        assert find_duplicate_adr_numbers(scratch) == {}

    def test_zero_padding_does_not_hide_a_collision(self, tmp_path):
        """ADR-007 and a hypothetical ADR-7 must be treated as the same number."""
        scratch = tmp_path / "decisions"
        scratch.mkdir()
        (scratch / "ADR-007-padded.md").write_text("# padded\n")
        (scratch / "ADR-7-unpadded.md").write_text("# unpadded\n")

        duplicates = find_duplicate_adr_numbers(scratch)

        assert "7" in duplicates
        assert set(duplicates["7"]) == {"ADR-007-padded.md", "ADR-7-unpadded.md"}
