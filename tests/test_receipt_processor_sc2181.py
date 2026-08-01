"""OI-678: receipt_processor.sh must not check exit status via $?.

shellcheck SC2181 ("Check exit code directly with e.g. 'if ! mycmd;', not
indirectly with $?") fires when a command's exit status is inspected through
``$?`` in a separate test expression instead of testing the command directly
(``if ! cmd`` / ``if cmd``). The finding flagged the report_parser.py call in
_psr_parse_receipt_json (scripts/receipt_processor.sh).

This test encodes the rule as a static scan so it needs no shellcheck binary
in CI and fails deterministically on the pre-fix code.
"""

from __future__ import annotations

import re
from pathlib import Path

_RECEIPT_PROCESSOR_SH = Path(__file__).resolve().parent.parent / "scripts" / "receipt_processor.sh"

# SC2181: a test expression ``[ $? ... ]`` (also ``[$?``) is an indirect
# exit-status check. Escaped ``\$?`` matches the literal two-character ``$?``
# sequence so ordinary ``$var`` tests (e.g. ``[ $append_rc -eq 1 ]``) are not
# flagged.
_SC2181_RE = re.compile(r"\[\s*\$\?")


def test_no_indirect_exit_code_check_in_receipt_processor():
    assert _RECEIPT_PROCESSOR_SH.exists(), f"receipt_processor.sh missing at {_RECEIPT_PROCESSOR_SH}"
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(_RECEIPT_PROCESSOR_SH.read_text(encoding="utf-8").splitlines(), 1):
        if _SC2181_RE.search(line):
            hits.append((lineno, line.strip()))
    assert not hits, (
        "shellcheck SC2181 indirect $? exit-code checks at:\n"
        + "\n".join(f"  {ln}: {text}" for ln, text in hits)
    )
