"""tests/test_append_receipt_lock_timeout.py — a bounded wait on the receipt append lock (K2a-3b, codex r2 warning).

``emit_governance_receipt(..., lock_timeout=...)`` passes the bound to
``append_receipt_payload``: a caller that holds a lock of its own (the
cost-ledger replay holds ``provider_costs.lock``) never waits on
``append_receipt.lock`` past it. A lock still taken then is
``AppendReceiptError("lock_timeout")`` with nothing written. A bounded append
queues its central mirror instead of writing it, because that write waits on
the central store's append lock; the next unbounded append drains the queue.
Without ``lock_timeout`` every wait stays unbounded.

The lock is held by a real child process with its own open file description;
nothing mocks a lock.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT / "scripts" / "lib"), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from governance_receipts import AppendReceiptError, emit_governance_receipt

EVENT = "cost_ledger_renumbered"
BOUND_SECONDS = 0.3
# What a bounded wait may take beyond its bound: the poll interval and the append work around it.
MARGIN_SECONDS = 0.7

# argv: the lock path, how long to hold it, the marker to write once it is held.
_HOLDER = textwrap.dedent("""
    import fcntl, sys, time
    from pathlib import Path
    lock, hold, marker = Path(sys.argv[1]), float(sys.argv[2]), Path(sys.argv[3])
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        marker.write_text("1")
        time.sleep(hold)
""")


@pytest.fixture
def receipts(tmp_path: Path) -> Path:
    return tmp_path / "store" / "state" / "t0_receipts.ndjson"


@pytest.fixture
def hold_the_append_lock(receipts: Path, tmp_path: Path):
    """``hold(seconds)`` starts a child holding ``append_receipt.lock`` and returns once it holds it."""
    children: list = []

    def hold(seconds: float) -> subprocess.Popen:
        marker = tmp_path / f"held-{len(children)}"
        child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(receipts.parent / "append_receipt.lock"),
                                  str(seconds), str(marker)], stderr=subprocess.PIPE, text=True)
        children.append(child)
        deadline = time.monotonic() + 30
        while not marker.exists():
            assert child.poll() is None, child.communicate()[1]
            assert time.monotonic() < deadline, "the holder never took the lock"
            time.sleep(0.01)
        return child

    yield hold
    for child in children:
        if child.poll() is None:
            child.kill()
        child.communicate()


def _emit(receipts: Path, section: str, **kwargs):
    return emit_governance_receipt(
        EVENT, receipt_kind="state_mutation", status="success", source="test_append_receipt_lock_timeout",
        receipts_file=str(receipts), project_id="vnx-dev", run_id="vnx-dev:dispatch:r:2",
        renumbered_from="vnx-dev:dispatch:r:1", record_id=section, section=section, **kwargs,
    )


def _events(path: Path) -> list:
    if not path.is_file():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [line for line in lines if line.get("event_type") == EVENT]


def _pending(receipts: Path) -> list:
    queue = receipts.parent / "pending_mirrors.ndjson"
    if not queue.is_file():
        return []
    return [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestABoundedAppend:
    def test_a_held_append_lock_raises_lock_timeout_within_the_bound_and_writes_nothing(
        self, receipts, hold_the_append_lock,
    ):
        holder = hold_the_append_lock(3.0)

        started = time.monotonic()
        with pytest.raises(AppendReceiptError) as raised:
            _emit(receipts, "a" * 32, lock_timeout=BOUND_SECONDS)
        waited = time.monotonic() - started

        assert raised.value.code == "lock_timeout"
        assert BOUND_SECONDS <= waited < BOUND_SECONDS + MARGIN_SECONDS, f"it waited {waited:.2f}s"
        assert holder.poll() is None, "it returned while the lock was still held"
        assert _events(receipts) == [], "nothing was written"

    def test_a_free_lock_appends_and_queues_the_mirror_for_the_next_unbounded_append(
        self, receipts, tmp_path, monkeypatch,
    ):
        central = tmp_path / "central"
        monkeypatch.setenv("VNX_STATE_DIR", str(central / "state"))
        central_receipts = central / "state" / "t0_receipts.ndjson"

        bounded = _emit(receipts, "b" * 32, lock_timeout=BOUND_SECONDS)

        assert bounded["append_status"] == "appended"
        assert [event["section"] for event in _events(receipts)] == ["b" * 32]
        assert _events(central_receipts) == [], "the bounded append never waits on the central lock"
        assert [entry["receipt"]["section"] for entry in _pending(receipts)] == ["b" * 32]

        _emit(receipts, "c" * 32)

        assert [event["section"] for event in _events(central_receipts)] == ["b" * 32, "c" * 32]
        assert _pending(receipts) == [], "the unbounded append drained the queue"

    def test_without_a_central_target_a_bounded_append_queues_nothing(self, receipts, monkeypatch):
        monkeypatch.setenv("VNX_STATE_DIR", str(receipts.parent))

        assert _emit(receipts, "d" * 32, lock_timeout=BOUND_SECONDS)["append_status"] == "appended"

        assert not (receipts.parent / "pending_mirrors.ndjson").exists()

    def test_without_lock_timeout_the_wait_is_unbounded_as_before(self, receipts, hold_the_append_lock):
        holder = hold_the_append_lock(1.0)

        started = time.monotonic()
        written = _emit(receipts, "e" * 32)
        waited = time.monotonic() - started

        assert written["append_status"] == "appended"
        assert holder.wait(timeout=30) == 0
        assert waited >= 0.5, f"it returned after {waited:.2f}s, before the holder let go"
        assert [event["section"] for event in _events(receipts)] == ["e" * 32]
