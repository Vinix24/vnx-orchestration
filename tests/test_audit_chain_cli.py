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
    assert payload.get("violation_count") == 1
    assert payload["violations"][0]["line_number"] == 2


def _segmented_ledger(path: Path) -> None:
    """Unchained epoch 0, a chain_epoch_seal, then three chained entries."""
    path.write_text(
        json.dumps({"seq": 0, "old": True}) + "\n"
        + json.dumps({"seq": 1, "old": True}) + "\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
    from ndjson_hash_chain import append_chained_entry
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "chain_epoch_seal", _REPO_ROOT / "scripts" / "chain_epoch_seal.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.seal_ledger(path)["action"] == "sealed"
    for i in range(3):
        append_chained_entry(path, {"dispatch_id": f"d-{i}", "status": "success"})


def test_segmented_ledger_exits_zero_with_real_status(tmp_path: Path) -> None:
    ledger = tmp_path / "t0_receipts.ndjson"
    _segmented_ledger(ledger)

    result = _run_verify(ledger)

    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    payload = json.loads(result.stdout)
    assert payload["verified"] is True
    assert payload["status"] == "verified-segmented"
    assert payload["path"] == str(ledger)


def test_fully_chained_ledger_reports_verified(tmp_path: Path) -> None:
    ledger = tmp_path / "t0_receipts.ndjson"
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
    from ndjson_hash_chain import append_chained_entry

    for i in range(3):
        append_chained_entry(ledger, {"dispatch_id": f"d-{i}"})

    result = _run_verify(ledger)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["verified"] is True and payload["status"] == "verified"


def test_changed_byte_in_chained_line_shows_violation_line(tmp_path: Path) -> None:
    ledger = tmp_path / "t0_receipts.ndjson"
    _segmented_ledger(ledger)
    lines = ledger.read_text(encoding="utf-8").splitlines()
    target = len(lines) - 2  # second-to-last chained entry (0-based)
    lines[target] = lines[target].replace("success", "failure!")
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _run_verify(ledger)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["verified"] is False
    assert payload["status"] == "broken"
    assert payload["violation_count"] == len(payload["violations"]) >= 1
    assert target + 2 in [v["line_number"] for v in payload["violations"]]


def test_unchained_line_after_chained_ones_is_a_violation(tmp_path: Path) -> None:
    ledger = tmp_path / "t0_receipts.ndjson"
    _segmented_ledger(ledger)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"dispatch_id": "late", "status": "success"}) + "\n")

    result = _run_verify(ledger)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["verified"] is False
    assert any(
        v.get("note") == "unchained entry inside a chained epoch"
        for v in payload["violations"]
    )


def test_violations_capped_at_twenty_with_total_count(tmp_path: Path) -> None:
    ledger = tmp_path / "t0_receipts.ndjson"
    rows = [{"i": 0, "prev_hash": GENESIS_HASH}]
    rows += [{"i": i, "prev_hash": "ab" * 32} for i in range(1, 31)]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    payload = json.loads(_run_verify(ledger).stdout)

    assert len(payload["violations"]) == 20
    assert payload["violation_count"] == 30


def test_unknown_status_exits_nonzero_and_names_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("audit_chain_under_test", _AUDIT_CHAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "verify_chain", lambda p: (True, [], "mystery-state"))
    monkeypatch.setattr(sys, "argv", ["audit_chain.py", "verify", str(tmp_path / "x")])

    try:
        mod.main()
    except SystemExit as exc:
        code = exc.code
    else:
        raise AssertionError("main() must exit")

    assert code not in (0, None)
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is False
    assert payload["status"] == "mystery-state"
