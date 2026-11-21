"""OI-670: rp_delivery.sh must not carry shellcheck SC2155.

SC2155 ("Declare and assign separately to avoid masking return values") fires
when ``local X="$(...)"`` masks the command substitution's exit status.

Like test_receipt_processor_sc2181.py this is a static scan so it needs no
shellcheck binary in CI and fails deterministically on the pre-fix code.
"""

import re
from pathlib import Path

RP_DELIVERY = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "receipt_processor" / "rp_delivery.sh"

# SC2155: ``local X="$(cmd ...)"`` or ``local X=$(cmd ...)`` masks cmd's exit status.
_SC2155_RE = re.compile(r"\blocal\s+\w+\s*=\s*\"?\$\([^)]*\)")


def _lines() -> "list[tuple[int, str]]":
    return list(enumerate(RP_DELIVERY.read_text(encoding="utf-8").splitlines(), 1))


def test_no_local_masking_command_substitution_in_rp_delivery():
    hits = [(ln, line.strip()) for ln, line in _lines() if _SC2155_RE.search(line)]
    assert not hits, (
        "shellcheck SC2155 local-masking patterns at:\n"
        + "\n".join(f"  {ln}: {text}" for ln, text in hits)
    )
