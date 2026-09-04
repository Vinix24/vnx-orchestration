"""Guard: docs/core/00_VNX_ARCHITECTURE.md must not cite a path that doesn't
exist, and must not carry an unfalsifiable "Integration Status" stamp.

Measured 2026-09-04 (before this test existed): the doc carried six broken
bare-path citations (five distinct targets -- `core/technical/DISPATCHER_SYSTEM.md`
cited twice) and two "Integration Status" claims that could not be
re-verified by a reader: a bare "✅ FULLY OPERATIONAL" checkmark, and a
"OPERATIONAL (2026-03-07)" stamp six months stale at measurement time. Both
of the doc's linked "Full Reference"/demo targets
(`docs/intelligence/GOVERNANCE_MEASUREMENT.md`, `demo/setup_demo.sh`) had been
deleted from the repo in #193 on 2026-04-08 -- five months before this doc was
next touched -- and nothing caught it, because the sibling check
(`check_docs_file_line_refs.py`) deliberately only validates ``file.py:123``
citations with an explicit line number, not a bare path.

``scripts/ci/check_architecture_doc_paths.py`` closes that gap for this one
document. This test file pins its exclusion rules (so it never false-alarms
on the doc's many legitimate generic/runtime/config mentions), proves it
actually detects a broken reference (not just that today's count happens to
be zero), and locks the two stale-claim patterns out of recurring.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "core" / "00_VNX_ARCHITECTURE.md"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
import check_architecture_doc_paths as check  # noqa: E402


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


# ---------------------------------------------------------------------------
# Exclusion rules -- what looks like a path and what is a designed placeholder
# ---------------------------------------------------------------------------


def test_directory_prefix_mentions_are_not_flagged():
    # A bare directory-prefix mention names no single file, so it carries no
    # location claim to drift.
    assert check._looks_like_repo_path(".claude/skills/") is False
    assert check._looks_like_repo_path("state/") is False


def test_home_relative_paths_are_not_flagged():
    # vnx init creates these on demand; the repo never ships them.
    assert check._looks_like_repo_path("~/.claude/skills/") is False


def test_http_routes_and_slash_commands_are_not_flagged():
    assert check._looks_like_repo_path("/api/events") is False
    assert check._looks_like_repo_path("/model") is False


def test_gitignored_runtime_roots_are_not_flagged():
    # The doc's own File System Layout marks .vnx-data/ and .vnx/ "(gitignored)".
    assert check._looks_like_repo_path(".vnx-data/config.env") is False
    assert check._looks_like_repo_path(".vnx/config.yml") is False


def test_state_and_logs_shorthand_is_not_flagged():
    # Shorthand for the same gitignored .vnx-data/ root, used throughout the doc.
    assert check._looks_like_repo_path("state/t0_receipts.ndjson") is False
    assert check._looks_like_repo_path("logs/supervisor.log") is False


def test_settings_key_shorthand_is_not_flagged():
    # "allow/deny" means "permissions.allow or permissions.deny", not a path:
    # the final segment has no extension and no recognized root prefix.
    assert check._looks_like_repo_path("allow/deny") is False
    assert check._looks_like_repo_path("permissions.allow/deny") is False


def test_extensionless_known_root_path_is_flagged():
    # bin/vnx has no extension but starts from a real top-level repo root --
    # it is a genuine citation, not settings-key shorthand.
    assert check._looks_like_repo_path("bin/vnx") is True


def test_real_repo_path_is_flagged():
    assert check._looks_like_repo_path("scripts/lib/foo.py") is True


# ---------------------------------------------------------------------------
# scan_doc — fenced blocks skipped, line-number suffix stripped
# ---------------------------------------------------------------------------


def test_scan_doc_skips_fenced_blocks():
    text = "```\nscripts/lib/fake_example.py\n```\nreal `scripts/lib/real.py`\n"
    citations = check.scan_doc(text)
    assert citations == [(4, "scripts/lib/real.py")]


def test_scan_doc_strips_line_number_suffix():
    # The suffix is a `check_docs_file_line_refs.py` concern (line-bounds);
    # this checker only needs the file itself to exist.
    text = "see `scripts/vnx_supervisor_simple.sh:198`\n"
    citations = check.scan_doc(text)
    assert citations == [(1, "scripts/vnx_supervisor_simple.sh")]


# ---------------------------------------------------------------------------
# check() — a known-broken path is actually caught (not just "count is zero")
# ---------------------------------------------------------------------------


def test_check_catches_a_planted_broken_path():
    # A path that provably does not exist anywhere in this repo.
    planted = "docs/core/DOES_NOT_EXIST_OI_ARCH_DOC_TEST.md"
    assert planted not in _tracked_paths()
    text = f"see `{planted}` for details\n"
    violations = check.check(text, _tracked_paths())
    assert len(violations) == 1
    assert planted in violations[0]


def test_check_passes_a_real_path():
    text = "see `scripts/gather_intelligence.py` for details\n"
    assert check.check(text, _tracked_paths()) == []


# ---------------------------------------------------------------------------
# Integration — the live doc is clean
# ---------------------------------------------------------------------------


def test_current_doc_has_no_broken_path_citations():
    violations = check.check(_doc_text(), _tracked_paths())
    assert violations == [], "\n" + "\n".join(violations)


def test_check_architecture_doc_paths_main_passes():
    rc = check.main(["--root", str(REPO_ROOT)])
    assert rc == 0


# ---------------------------------------------------------------------------
# Stale "Integration Status" stamp — the second class of finding
# ---------------------------------------------------------------------------


def test_no_static_integration_status_label():
    # Both stale claims (a bare "✅ FULLY OPERATIONAL" checkmark, and a
    # "OPERATIONAL (2026-03-07)" stamp six months stale at measurement time)
    # were carried under this exact bold-field label. The fix replaced both
    # with a "Verify liveness" line naming a runnable command instead of a
    # frozen assertion -- so the label itself should never reappear.
    assert "**Integration Status**" not in _doc_text()


def test_no_bare_operational_checkmark_claim():
    text = _doc_text()
    assert "✅ FULLY OPERATIONAL" not in text
    assert "✅ OPERATIONAL" not in text


def test_no_dated_operational_stamp():
    import re

    # Guards against the same disease recurring with a different date:
    # "**<Anything> Status**: [✅] OPERATIONAL (YYYY-MM-DD)".
    pattern = re.compile(r"\*\*[^*]*Status\*\*:\s*(?:✅\s*)?OPERATIONAL\s*\(\d{4}-\d{2}-\d{2}\)")
    assert pattern.search(_doc_text()) is None


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
