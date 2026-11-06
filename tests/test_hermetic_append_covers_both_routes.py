"""Guide: a hermetic append wrapper must cover both append routes.

Since #2401 the lifecycle start and end line go through
``governance_receipts.append_receipt_payload`` (the registered facade) and no
longer through ``append_receipt_internals.payload.append_receipt_payload``
directly. A test file that wraps only the internal route lets the lifecycle
line past the wrapper, so the post-append hooks run for real.

What the scan matches (as text, the test files are never imported):

* a file counts as a wrapper when it contains ``skip_enrichment`` and patches the
  internal route, via ``setattr(payload_module|payload, "append_receipt_payload"``,
  ``patch.object(payload_module|payload, "append_receipt_payload"`` or the string
  ``append_receipt_internals.payload.append_receipt_payload``;
* such a file must also contain ``governance_receipts.append_receipt_payload``.
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()

INTERNAL_ROUTE = re.compile(
    r'(?:setattr|patch\.object)\(\s*(?:payload_module|payload)\s*,\s*"append_receipt_payload"'
    r"|append_receipt_internals\.payload\.append_receipt_payload"
)
FACADE_ROUTE = "governance_receipts.append_receipt_payload"


def _wrapping_files() -> list[Path]:
    found = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.resolve() == SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "skip_enrichment" in text and INTERNAL_ROUTE.search(text):
            found.append(path)
    return found


def test_the_scan_finds_the_wrapping_files():
    names = {p.name for p in _wrapping_files()}
    assert len(names) >= 18, sorted(names)
    for expected in (
        "test_k5b_governed_end_state.py",
        "test_k_act_activation.py",
        "test_k9e_projection_successors.py",
        "test_k4c_spawn_file.py",
    ):
        assert expected in names


def test_every_hermetic_wrapper_also_covers_the_facade_route():
    missing = [
        p.name
        for p in _wrapping_files()
        if FACADE_ROUTE not in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert not missing, (
        "these files wrap append_receipt_payload on the internal route only; "
        f"also patch {FACADE_ROUTE}: {missing}"
    )
