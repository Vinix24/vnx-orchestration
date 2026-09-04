"""Tests for scripts/audit_chain.py's ``verify`` CLI subcommand (OI D1).

Bug: ``verify_chain()`` reports ``status="unchained"`` for a ledger where no
entry carries ``prev_hash`` — correct in isolation (chaining is default-off
fleet-wide, VNX_CHAIN_RECEIPTS). But the CLI wrapper in audit_chain.py turned
that into ``{"verified": true, "status": "unchained"}`` with exit code 0. A
caller that reads only the exit code or only the ``verified`` field sees a
green "chain is fine" result for a ledger that was never chained at all —
absence of evidence read as evidence of presence.

This does NOT touch verify_chain() or the hash-chain mechanism itself — only
the CLI's translation of "unchained" into an exit code / verified field. The
CLI must stop asserting a positive that it never measured.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AUDIT_CHAIN = _REPO_ROOT / "scripts" / "audit_chain.py"

GENESIS_HASH = "0" * 64


def _run_verify(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_AUDIT_CHAIN), "verify", str(path)],
        capture_output=True,
        text=True,
    )


def test_unchained_ledger_is_not_reported_verified_true(tmp_path: Path) -> None:
    """An unchained ledger (no entry carries prev_hash) must NOT come back
    verified:true / exit 0 — that reads as 'the chain is fine' when there is
    no chain to check."""
    ledger = tmp_path / "t0_receipts.ndjson"
    ledger.write_text(
        json.dumps({"dispatch_id": "d-1", "status": "success"}) + "\n"
        + json.dumps({"dispatch_id": "d-2", "status": "success"}) + "\n",
        encoding="utf-8",
    )

    result = _run_verify(ledger)

    assert result.returncode != 0, (
        f"unchained ledger must exit non-zero, got 0. stdout={result.stdout!r}"
    )
    payload = json.loads(result.stdout)
    assert payload.get("status") == "unchained"
    assert payload.get("verified") is not True, (
        f"unchained ledger must not report verified:true, got {payload!r}"
    )


def test_missing_ledger_is_not_reported_verified_true(tmp_path: Path) -> None:
    """A ledger file that does not exist at all is the same 'unchained'
    branch in verify_chain() — must not silently read as a verified chain."""
    ledger = tmp_path / "does_not_exist.ndjson"

    result = _run_verify(ledger)

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload.get("status") == "unchained"
    assert payload.get("verified") is not True


def test_genuinely_verified_chain_still_exits_zero(tmp_path: Path) -> None:
    """Sanity: a real, intact chain must still report verified:true / exit 0
    — the fix must not turn the good case red."""
    ledger = tmp_path / "t0_receipts.ndjson"
    first = {"dispatch_id": "d-1", "status": "success", "prev_hash": GENESIS_HASH}

    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
    from ndjson_hash_chain import compute_entry_hash  # noqa: E402

    second = {
        "dispatch_id": "d-2",
        "status": "success",
        "prev_hash": compute_entry_hash(first),
    }
    ledger.write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n",
        encoding="utf-8",
    )

    result = _run_verify(ledger)

    assert result.returncode == 0, f"verified chain must exit 0: {result.stdout!r} {result.stderr!r}"
    payload = json.loads(result.stdout)
    assert payload.get("verified") is True
    assert payload.get("status") == "verified"


def test_broken_chain_still_exits_nonzero(tmp_path: Path) -> None:
    """Sanity: a genuinely broken chain must still exit 1 / verified:false —
    unrelated to this fix, but must not regress."""
    ledger = tmp_path / "t0_receipts.ndjson"
    ledger.write_text(
        json.dumps({"dispatch_id": "d-1", "status": "success", "prev_hash": GENESIS_HASH}) + "\n"
        + json.dumps({"dispatch_id": "d-2", "status": "success", "prev_hash": "deadbeef" * 8}) + "\n",
        encoding="utf-8",
    )

    result = _run_verify(ledger)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload.get("verified") is False
    assert payload.get("status") == "broken"
