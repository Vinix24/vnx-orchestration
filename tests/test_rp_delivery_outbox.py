"""rp_delivery.sh keeps only the receipt outbox; the T0 pane push is gone (config-een-oppervlak N11).

Runs the real shell functions through bash. The zero-read test pins that no
shipped file reads the three deleted push variables any more.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "receipt_processor"
DELETED = ("VNX_RECEIPT_T0_PUSH", "VNX_RECEIPT_DIGEST_THRESHOLD", "VNX_RECEIPT_VERIFY_MAX_RETRIES")


def _bash(tmp_path: Path, body: str, extra_env: str = "") -> subprocess.CompletedProcess:
    script = f"""
set -u
{extra_env}
RECEIPTS_PENDING_DIR="{tmp_path}/pending"
RECEIPTS_PROCESSED_DIR="{tmp_path}/processed"
log() {{ echo "$1 $2" >> "{tmp_path}/log"; }}
tmux() {{ echo "tmux $*" >> "{tmp_path}/tmux_calls"; return 0; }}
source "{LIB}/rp_delivery.sh"
{body}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def _count(d: Path) -> int:
    return len(list(d.glob("*.json"))) if d.exists() else 0


def test_deleted_names_have_zero_reads_in_shipped_code():
    out = subprocess.run(
        ["git", "grep", "-nwE", "|".join(DELETED), "--", "scripts", "bin"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.returncode == 1 and out.stdout == "", out.stdout


def test_push_functions_are_gone():
    text = (LIB / "rp_delivery.sh").read_text(encoding="utf-8")
    for name in ("_deliver_receipt_to_t0_pane", "_rpd_deliver_digest", "_rpd_paste_and_verify", "tmux "):
        assert name not in text


def test_send_persists_to_processed_and_never_calls_tmux(tmp_path):
    # Setting the old push switch must have no effect any more.
    r = _bash(tmp_path, 'send_receipt_to_t0 \'{"dispatch_id":"d1"}\' T1; echo rc=$?',
              extra_env="export VNX_RECEIPT_T0_PUSH=1")
    assert "rc=0" in r.stdout, r.stderr
    assert _count(tmp_path / "pending") == 0
    assert _count(tmp_path / "processed") == 1
    assert not (tmp_path / "tmux_calls").exists()


def test_send_failure_is_visible_when_pending_dir_unwritable(tmp_path):
    (tmp_path / "pending").write_text("not a dir")
    r = _bash(tmp_path, 'send_receipt_to_t0 \'{"dispatch_id":"d1"}\' T1; echo rc=$?')
    assert "rc=1" in r.stdout or "rc=0" not in r.stdout


def test_retry_sweep_drains_legacy_pending_files(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    for i in range(7):  # more than the old digest threshold
        (pending / f"{i}.json").write_text('{"dispatch_id":"d%d"}' % i)
    r = _bash(tmp_path, "mkdir -p \"$RECEIPTS_PROCESSED_DIR\"; _retry_pending_receipts; echo rc=$?")
    assert "rc=0" in r.stdout, r.stderr
    assert _count(pending) == 0
    assert _count(tmp_path / "processed") == 7
    assert not (tmp_path / "tmux_calls").exists()


def test_retry_sweep_reports_failure_when_move_fails(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "a.json").write_text("{}")
    # processed/ is a plain file, so every mv fails and the file must stay pending
    (tmp_path / "processed").write_text("blocker")
    r = _bash(tmp_path, "_retry_pending_receipts; echo rc=$?")
    assert "rc=1" in r.stdout, r.stdout
    assert _count(pending) == 1
