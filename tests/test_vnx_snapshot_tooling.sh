#!/usr/bin/env bash
# test_vnx_snapshot_tooling.sh — bash integration tests for W0 PR4 snapshot tooling

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VNX_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SNAPSHOT_PY="$VNX_ROOT/scripts/lib/vnx_snapshot.py"

PASS_COUNT=0
FAIL_COUNT=0
TMP_DIR="$(mktemp -d)"

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1 — $2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_make_project() {
  local base="$1"
  mkdir -p "$base/.vnx-data/state"
  mkdir -p "$base/.vnx-data/dispatches/active"
  mkdir -p "$base/.vnx-data/dispatches/pending"
  echo '{}' > "$base/.vnx-data/state/t0_receipts.ndjson"
}

_snapshots_dir() {
  echo "$TMP_DIR/vnx-snapshots"
}

_run_snapshot() {
  local project="$1"
  local snap_dir
  snap_dir="$(_snapshots_dir)"
  mkdir -p "$snap_dir"
  PYTHONPATH="$VNX_ROOT/scripts/lib" \
    python3 -c "
import sys, vnx_snapshot
vnx_snapshot._snapshots_dir = lambda: __import__('pathlib').Path('$snap_dir')
sys.exit(vnx_snapshot.do_snapshot('$project'))
"
}

_run_restore() {
  local tarball="$1"; shift
  PYTHONPATH="$VNX_ROOT/scripts/lib" \
    python3 -c "
import sys, vnx_snapshot
sys.exit(vnx_snapshot.do_restore('$tarball', $([ -n "${1:-}" ] && echo "'$1'" || echo "None"), force=True))
"
}

_run_quiesce() {
  local project="$1"
  PYTHONPATH="$VNX_ROOT/scripts/lib" \
    python3 -c "
import sys, vnx_snapshot
sys.exit(vnx_snapshot.do_quiesce_check('$project'))
"
}

# ---------------------------------------------------------------------------
# test_snapshot_creates_tarball
# ---------------------------------------------------------------------------
t1_dir="$TMP_DIR/t1_project"
_make_project "$t1_dir"
snap_dir="$TMP_DIR/vnx-snapshots"
mkdir -p "$snap_dir"

PYTHONPATH="$VNX_ROOT/scripts/lib" python3 -c "
import sys; sys.path.insert(0,'$VNX_ROOT/scripts/lib')
import pathlib, vnx_snapshot
vnx_snapshot._snapshots_dir = lambda: pathlib.Path('$snap_dir')
sys.exit(vnx_snapshot.do_snapshot('$t1_dir'))
"
tarball_count=$(find "$snap_dir" -name "*.tar.gz" | wc -l | tr -d ' ')
if [ "$tarball_count" -eq "1" ]; then
  pass "test_snapshot_creates_tarball"
else
  fail "test_snapshot_creates_tarball" "expected 1 tarball, found $tarball_count"
fi

# ---------------------------------------------------------------------------
# test_snapshot_includes_vnx_data
# ---------------------------------------------------------------------------
tarball_path=$(find "$snap_dir" -name "*.tar.gz" | head -1)
if tar -tzf "$tarball_path" 2>/dev/null | grep -q "^\.vnx-data"; then
  pass "test_snapshot_includes_vnx_data"
else
  fail "test_snapshot_includes_vnx_data" "tarball does not contain .vnx-data/"
fi

# ---------------------------------------------------------------------------
# test_restore_roundtrip
# ---------------------------------------------------------------------------
t3_dir="$TMP_DIR/t3_project"
_make_project "$t3_dir"
snap3_dir="$TMP_DIR/snap3"
mkdir -p "$snap3_dir"
PYTHONPATH="$VNX_ROOT/scripts/lib" python3 -c "
import sys, pathlib, vnx_snapshot
vnx_snapshot._snapshots_dir = lambda: pathlib.Path('$snap3_dir')
sys.exit(vnx_snapshot.do_snapshot('$t3_dir'))
"
tarball3=$(find "$snap3_dir" -name "*.tar.gz" | head -1)
rm -rf "$t3_dir/.vnx-data"
PYTHONPATH="$VNX_ROOT/scripts/lib" python3 -c "
import sys, vnx_snapshot
sys.exit(vnx_snapshot.do_restore('$tarball3', '$t3_dir', force=True))
"
if [ -f "$t3_dir/.vnx-data/state/t0_receipts.ndjson" ]; then
  pass "test_restore_roundtrip"
else
  fail "test_restore_roundtrip" ".vnx-data/state/t0_receipts.ndjson not restored"
fi

# ---------------------------------------------------------------------------
# test_quiesce_check_clean
# ---------------------------------------------------------------------------
t4_dir="$TMP_DIR/t4_project"
_make_project "$t4_dir"
if PYTHONPATH="$VNX_ROOT/scripts/lib" python3 -c "
import sys, vnx_snapshot
sys.exit(vnx_snapshot.do_quiesce_check('$t4_dir'))
" 2>/dev/null; then
  pass "test_quiesce_check_clean"
else
  fail "test_quiesce_check_clean" "clean project failed quiesce-check"
fi

# ---------------------------------------------------------------------------
# test_quiesce_check_active_dispatch_fails
# ---------------------------------------------------------------------------
t5_dir="$TMP_DIR/t5_project"
_make_project "$t5_dir"
echo "# dispatch" > "$t5_dir/.vnx-data/dispatches/active/recent.md"
if ! PYTHONPATH="$VNX_ROOT/scripts/lib" python3 -c "
import sys, vnx_snapshot
sys.exit(vnx_snapshot.do_quiesce_check('$t5_dir'))
" 2>/dev/null; then
  pass "test_quiesce_check_active_dispatch_fails"
else
  fail "test_quiesce_check_active_dispatch_fails" "expected non-zero exit for active dispatch"
fi

# ---------------------------------------------------------------------------
# test_quiesce_check_held_lease_fails
# ---------------------------------------------------------------------------
t6_dir="$TMP_DIR/t6_project"
_make_project "$t6_dir"
python3 -c "
import sqlite3
db = '$t6_dir/.vnx-data/state/runtime_coordination.db'
con = sqlite3.connect(db)
con.execute('CREATE TABLE terminal_leases (terminal_id TEXT, state TEXT)')
con.execute(\"INSERT INTO terminal_leases VALUES ('T1', 'leased')\")
con.commit(); con.close()
"
if ! PYTHONPATH="$VNX_ROOT/scripts/lib" python3 -c "
import sys, vnx_snapshot
sys.exit(vnx_snapshot.do_quiesce_check('$t6_dir'))
" 2>/dev/null; then
  pass "test_quiesce_check_held_lease_fails"
else
  fail "test_quiesce_check_held_lease_fails" "expected non-zero exit for held lease"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS_COUNT passed, $FAIL_COUNT failed"
[ "$FAIL_COUNT" -eq 0 ]
