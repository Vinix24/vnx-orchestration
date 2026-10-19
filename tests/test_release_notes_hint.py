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

sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from check_changelog_fragments import compare_release
from test_check_changelog_fragments import ASSEMBLER, BASE_CHANGELOG, init_repo, release_branch
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
    assert "`echo 'v1.7.2' > <project>/.vnx-version`" in hint
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


INTRO_LEAD_IN = "never a bullet):"


def _suggested_intro(tag: str) -> list[str]:
    """The lines the hint prints after its lead-in: the intro, the last thing printed."""
    lines = _release_notes_hint(tag).splitlines()
    start = next(i for i, line in enumerate(lines) if INTRO_LEAD_IN in line) + 1
    return lines[start:]


def test_suggested_intro_is_one_line_with_no_leading_whitespace():
    intro = _suggested_intro("v1.7.2")
    assert len(intro) == 1, intro
    assert intro[0] == intro[0].lstrip()
    assert "`echo 'v1.7.2' > <project>/.vnx-version`" in intro[0]


def _release_with_intro(tmp_path: Path, intro_lines: list[str]) -> tuple[Path, str]:
    repo = init_repo(tmp_path / "repo", {
        "CHANGELOG.md": BASE_CHANGELOG,
        "VERSION": "1.0.0\n",
        "changelog.d/README.md": "# rule\n",
    })
    release_branch(repo, version="1.7.2")
    path = repo / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    heading = "## [1.7.2] - 2026-10-17\n\n"
    assert heading in text
    path.write_text(text.replace(heading, heading + "\n".join(intro_lines) + "\n\n", 1), encoding="utf-8")
    return repo, text


def _compare(repo: Path, original: str) -> str | None:
    actual = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    return compare_release(original, actual, "1.7.2")


def test_the_suggested_intro_passes_compare_release(tmp_path):
    repo, assembled = _release_with_intro(tmp_path, _suggested_intro("v1.7.2"))
    assert _compare(repo, assembled) is None


def test_the_old_indented_intro_fails_compare_release(tmp_path):
    old_layout = [
        "  v1.7.2 is now the active `current`. Consumers with a tracked `.vnx-version` pin "
        "are NOT moved by this flip and must update their pin to v1.7.2 explicitly:",
        "      echo 'v1.7.2' > <project>/.vnx-version",
    ]
    repo, assembled = _release_with_intro(tmp_path, old_layout)
    message = _compare(repo, assembled)
    assert message is not None
    assert "only prose may precede the first bullet" in message
