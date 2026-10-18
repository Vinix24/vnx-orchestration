"""OI-2141: the hint ``vnx release`` prints names the fragment assembler.

A release PR may write only a prose intro by hand; every bullet comes from a
fragment. The hint must therefore carry the assembler steps and the pin text
without bullet syntax.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.release import _release_notes_hint


def test_hint_names_the_assembler_check_and_the_run_with_the_tag_version():
    hint = _release_notes_hint("v1.7.2")
    assert "python3 scripts/release/assemble_changelog.py --version 1.7.2 --check" in hint
    lines = [line.strip() for line in hint.splitlines()]
    assert "python3 scripts/release/assemble_changelog.py --version 1.7.2" in lines
    assert lines.index("python3 scripts/release/assemble_changelog.py --version 1.7.2 --check") < lines.index(
        "python3 scripts/release/assemble_changelog.py --version 1.7.2"
    )


def test_hint_does_not_tell_the_operator_to_add_bullets_by_hand():
    hint = _release_notes_hint("v1.7.2")
    assert "add to CHANGELOG.md" not in hint


def test_hint_carries_the_pin_line_for_the_tag():
    hint = _release_notes_hint("v1.7.2")
    assert "echo 'v1.7.2' > <project>/.vnx-version" in hint
    assert "must update their pin to v1.7.2" in hint


def test_hint_holds_no_line_that_starts_like_a_bullet():
    for tag in ("v1.7.2", "1.7.2"):
        for line in _release_notes_hint(tag).splitlines():
            stripped = line.lstrip()
            assert not stripped.startswith(("- ", "* ")), line


def test_hint_version_has_no_leading_v_for_a_bare_tag():
    hint = _release_notes_hint("1.7.2")
    assert "--version 1.7.2 --check" in hint
    assert "--version v1.7.2" not in hint
