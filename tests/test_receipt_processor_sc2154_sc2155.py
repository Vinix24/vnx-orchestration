"""OI-669 + OI-670: rp_delivery.sh must not carry shellcheck SC2154/SC2155.

SC2154 ("_rf_status is referenced but not assigned") fires because ``_rf_status``
is a module-scope field set by ``extract_receipt_fields()`` in rp_extract.sh
before delivery runs. The honest fix is a ``# shellcheck disable=SC2154``
acknowledging that module-scope contract at the reference site, not defensive
default-initialisation of a variable that is always set.

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

# The SC2154 module-scope reference site: _drtp_get_next_action consumes _rf_status.
_SC2154_SITE_RE = re.compile(r'_drtp_get_next_action\s+"\$_rf_status"')
_SC2154_DISABLE_RE = re.compile(r"^\s*#\s+shellcheck\s+disable=SC2154\b", re.MULTILINE)


def _lines() -> "list[tuple[int, str]]":
    return list(enumerate(RP_DELIVERY.read_text(encoding="utf-8").splitlines(), 1))


def test_no_local_masking_command_substitution_in_rp_delivery():
    hits = [(ln, line.strip()) for ln, line in _lines() if _SC2155_RE.search(line)]
    assert not hits, (
        "shellcheck SC2155 local-masking patterns at:\n"
        + "\n".join(f"  {ln}: {text}" for ln, text in hits)
    )


def test_rf_status_reference_is_guarded_by_disable_directive():
    text = RP_DELIVERY.read_text(encoding="utf-8")
    assert _SC2154_SITE_RE.search(text), (
        "expected the module-scope _rf_status reference site in rp_delivery.sh"
    )
    assert _SC2154_DISABLE_RE.search(text), (
        "the module-scope _rf_status reference needs a `# shellcheck disable=SC2154` "
        "acknowledgement (it is set by extract_receipt_fields() in rp_extract.sh)"
    )
