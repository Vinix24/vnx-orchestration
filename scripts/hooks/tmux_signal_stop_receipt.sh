#!/usr/bin/env bash
# VNX tmux-lane Stop hook — deterministic receipt-guarantee for tmux-spawn workers
#
# WHY THIS EXISTS:
#   The F37 stop_report_hook.sh detects the worker terminal by CWD path
#   (.claude/terminals/T1) and is gated on VNX_AUTO_REPORT=1. tmux-spawn workers
#   run in the shared/isolated worktree ROOT (no terminals/T{n} segment) and
#   without VNX_AUTO_REPORT, so F37 SKIPS them — which is exactly why tmux-spawn
#   workers hit "receipt deadline exceeded": their unified_report existed but no
#   receipt was emitted until the lane's deadline fallback fired much later.
#
#   This hook is scoped to tmux-spawn workers via the worker env (VNX_DISPATCH_ID
#   + VNX_TMUX_SIGNAL_DIR), independent of cwd-based detection and independent of
#   VNX_AUTO_REPORT. On stop it (a) drops a "stopped" sentinel and (b) emits the
#   governed receipt PROMPTLY by reusing the #788 report->receipt converter, so
#   the receipt arrives on stop rather than after the deadline.
#
# COEXISTENCE: This does NOT replace F37. Both Stop hooks run — F37 for
#   terminal-pinned workers, this one for tmux-spawn workers. The converter
#   dedups by file hash, so no double receipt is produced.
#
# Scoped HARD: fires ONLY when BOTH VNX_DISPATCH_ID and VNX_TMUX_SIGNAL_DIR are
#   set. Normal T0/interactive sessions (env unset) drain stdin and exit 0.
#
# Best-effort, < 5s, never blocks. Any error -> exit 0.

# Drain stdin so the hook caller never blocks on an unread pipe.
cat >/dev/null 2>&1 || true

# ── Guard: only fire for tmux-spawn workers ──────────────────────────────────
if [ -z "${VNX_DISPATCH_ID:-}" ] || [ -z "${VNX_TMUX_SIGNAL_DIR:-}" ]; then
  exit 0
fi

# ── (a) Drop the "stopped" sentinel (atomic) ─────────────────────────────────
{
  mkdir -p "$VNX_TMUX_SIGNAL_DIR" 2>/dev/null
  _tmp="$VNX_TMUX_SIGNAL_DIR/stopped.$$.tmp"
  printf '%s\n' "${VNX_DISPATCH_ID}" >"$_tmp" 2>/dev/null \
    && mv -f "$_tmp" "$VNX_TMUX_SIGNAL_DIR/stopped" 2>/dev/null
} || true

# ── (b) Receipt-guarantee via the #788 converter ─────────────────────────────
# Resolve project root and the state/data dirs. Prefer explicit env; fall back
# to git toplevel of CWD. Everything best-effort; never let a failure block stop.
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$PROJECT_ROOT" ]; then
  if [ -n "${VNX_DATA_DIR:-}" ]; then
    PROJECT_ROOT="$(dirname "$VNX_DATA_DIR")"
  fi
fi

VNX_DATA="${VNX_DATA_DIR:-$PROJECT_ROOT/.vnx-data}"
STATE_DIR="${VNX_STATE_DIR:-$VNX_DATA/state}"
REPORTS_DIR="$VNX_DATA/unified_reports"

if [ -z "$PROJECT_ROOT" ] || ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

LIB_DIR="$PROJECT_ROOT/scripts/lib"
SCRIPTS_DIR="$PROJECT_ROOT/scripts"

# Bound the converter to keep stop fast (< 5s). `timeout` is absent on stock
# macOS; use it only when present (or gtimeout from coreutils), else run direct.
TIMEOUT_CMD=""
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD="timeout 4"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD="gtimeout 4"
fi

# Hand off to python with all paths via env (no shell-quoting hazards). The
# converter dedups by file hash, so re-runs do NOT double-write a receipt.
VNX_HOOK_PROJECT_ROOT="$PROJECT_ROOT" \
VNX_HOOK_LIB_DIR="$LIB_DIR" \
VNX_HOOK_SCRIPTS_DIR="$SCRIPTS_DIR" \
VNX_HOOK_STATE_DIR="$STATE_DIR" \
VNX_HOOK_REPORTS_DIR="$REPORTS_DIR" \
VNX_HOOK_DISPATCH_ID="$VNX_DISPATCH_ID" \
$TIMEOUT_CMD python3 - <<'PY' 2>/dev/null || true
import os
import sys
from pathlib import Path

lib_dir = os.environ.get("VNX_HOOK_LIB_DIR", "")
scripts_dir = os.environ.get("VNX_HOOK_SCRIPTS_DIR", "")
for p in (scripts_dir, lib_dir):
    if p and p not in sys.path:
        sys.path.insert(0, p)

try:
    from report_to_receipt_converter import (
        convert_report_to_receipt,
        scan_and_convert,
    )
except Exception:
    sys.exit(0)

dispatch_id = os.environ.get("VNX_HOOK_DISPATCH_ID", "")
state_dir = Path(os.environ.get("VNX_HOOK_STATE_DIR", ""))
reports_dir = Path(os.environ.get("VNX_HOOK_REPORTS_DIR", ""))
receipts_file = str(state_dir / "t0_receipts.ndjson")

# Prefer the dispatch's own unified_report (prompt, single-file convert).
candidates = [
    reports_dir / f"{dispatch_id}.md",
    reports_dir / f"{dispatch_id}_report.md",
]
report_path = next((c for c in candidates if c.is_file()), None)

try:
    if report_path is not None:
        convert_report_to_receipt(report_path, receipts_file=receipts_file)
    else:
        # No dispatch-named report yet — scan the dir (still hash-deduped).
        scan_and_convert([reports_dir], state_dir=state_dir)
except Exception:
    pass
sys.exit(0)
PY

exit 0
