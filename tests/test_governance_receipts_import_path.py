"""``governance_receipts`` imports from a lib-only entry point.

``scripts/lib/governance_receipts.py`` imports the ``append_receipt`` facade,
which lives one directory above at ``scripts/append_receipt.py``. Until this
regression was pinned, that bare import only resolved because some other
module had already put ``scripts/`` on ``sys.path``: a standalone caller
(``scripts/analysis/plan_gate_panel_effectiveness.py``, or
``tests/test_plan_gate_panel_effectiveness.py`` run on its own) got
``ModuleNotFoundError: No module named 'append_receipt'``. The guard in
``governance_receipts`` now makes the facade importable itself.

The second test pins why importing ``append_receipt_internals`` alone is not
an equivalent fix: ``append_receipt_payload`` reads ``common.facade``, which
needs the facade module registered, so the facade must still be the import.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"


def test_imports_with_only_scripts_lib_on_sys_path() -> None:
    """A lib-only entry point (no ``scripts/`` on the path) can import it."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS_LIB)!r})\n"
        "assert 'scripts' not in [p.rsplit('/', 1)[-1] for p in sys.path if p]\n"
        "import governance_receipts as g\n"
        "assert g.utc_now_iso().endswith('Z')\n"
        "assert callable(g.emit_governance_receipt)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_facade_objects_are_the_ones_re_exported() -> None:
    """The import still goes through the facade the internals proxy reads."""
    for path in (str(SCRIPTS_LIB), str(REPO_ROOT / "scripts")):
        if path not in sys.path:
            sys.path.insert(0, path)

    import append_receipt
    import governance_receipts

    assert governance_receipts.append_receipt_payload is append_receipt.append_receipt_payload
    assert governance_receipts.AppendReceiptError is append_receipt.AppendReceiptError
