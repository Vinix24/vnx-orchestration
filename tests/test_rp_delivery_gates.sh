#!/usr/bin/env bash
# Tests for rp_delivery.sh (OI-654: submit-verify + dedupe + digest + push-switch)
#
# Coverage:
#   - Baseline: default env (push=1, unset) still pastes + verifies + processes (no regression)
#   - Submit-verify: empty input line after Enter -> verified -> pending file moves to processed
#   - Submit-verify: non-empty input line after Enter -> NOT verified -> stays pending, WARN logged
#   - Dedupe: 3 pending files, same dispatch_id -> exactly 1 paste-buffer call, all 3 processed
#   - Digest: pending dispatches over threshold -> exactly 1 digest paste, all processed
#   - Push-switch: VNX_RECEIPT_T0_PUSH=0 -> zero tmux calls, item still moves to processed (suppressed)
#
# Mirrors the function-override tmux mock pattern from tests/test_input_mode_guard.sh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Test harness ---
PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1 — $2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [ "$expected" = "$actual" ]; then pass "$msg"; else fail "$msg" "expected='$expected' actual='$actual'"; fi
}

assert_file_contains() {
    local file="$1" pattern="$2" msg="$3"
    if grep -q "$pattern" "$file" 2>/dev/null; then pass "$msg"; else fail "$msg" "pattern '$pattern' not found in $file"; fi
}

# --- Sandbox dirs ---
TMP_ROOT=$(mktemp -d)
STATE_DIR="$TMP_ROOT/state"
RECEIPTS_PENDING_DIR="$TMP_ROOT/receipts/pending"
RECEIPTS_PROCESSED_DIR="$TMP_ROOT/receipts/processed"
PROCESSING_LOG="$TMP_ROOT/processing.log"
mkdir -p "$STATE_DIR" "$RECEIPTS_PENDING_DIR" "$RECEIPTS_PROCESSED_DIR"
touch "$PROCESSING_LOG"

MOCK_CALL_LOG="$TMP_ROOT/mock_calls"
MOCK_LOADED_BUFFER="$TMP_ROOT/mock_loaded_buffer"
MOCK_PASTE_FAIL_FLAG="$TMP_ROOT/mock_paste_fail"
MOCK_LOADBUFFER_FAIL_FLAG="$TMP_ROOT/mock_loadbuffer_fail"
MOCK_CAPTURE_RESPONSE_FILE="$TMP_ROOT/mock_capture_response"
touch "$MOCK_CALL_LOG" "$MOCK_CAPTURE_RESPONSE_FILE"

reset_mocks() {
    rm -f "$MOCK_CALL_LOG" "$MOCK_LOADED_BUFFER" "$MOCK_PASTE_FAIL_FLAG" "$MOCK_LOADBUFFER_FAIL_FLAG"
    : > "$MOCK_CAPTURE_RESPONSE_FILE"
    touch "$MOCK_CALL_LOG"
    rm -rf "${RECEIPTS_PENDING_DIR:?}"/* "${RECEIPTS_PROCESSED_DIR:?}"/* 2>/dev/null
    unset VNX_RECEIPT_T0_PUSH VNX_RECEIPT_DIGEST_THRESHOLD VNX_RECEIPT_VERIFY_MAX_RETRIES
}

# capture-pane's "last line" response for the next verify call. Empty = submit
# verified (input line clear); non-empty = submit NOT verified (residual text).
set_capture_response() { printf '%s' "$1" > "$MOCK_CAPTURE_RESPONSE_FILE"; }

write_pending() {
    local filename="$1" dispatch_id="$2"
    cat > "$RECEIPTS_PENDING_DIR/$filename" <<JSON
{"dispatch_id":"$dispatch_id","terminal":"T1","status":"success","event_type":"task_complete","timestamp":"2026-07-16T10:00:00Z","report_path":"/tmp/r.md"}
JSON
    # Deterministic mtime ordering for "oldest" assertions (BSD+GNU touch -t compatible).
    touch -t "20260101000${filename: -6:1}" "$RECEIPTS_PENDING_DIR/$filename" 2>/dev/null || true
}

count_files() { find "$1" -type f 2>/dev/null | wc -l | tr -d ' '; }

# --- Minimal stubs required by rp_delivery.sh outside the full daemon ---
sleep()  { return 0; }
get_pane_id_smart() { echo "test:0.0"; }

# tmux mock: file-based state so changes survive $() subshells
tmux() {
    local subcmd="$1"
    echo "$subcmd" >> "$MOCK_CALL_LOG"
    case "$subcmd" in
        load-buffer)
            if [ -f "$MOCK_LOADBUFFER_FAIL_FLAG" ]; then
                cat > /dev/null
                return 1
            fi
            cat > "$MOCK_LOADED_BUFFER"
            return 0
            ;;
        paste-buffer)
            [ -f "$MOCK_PASTE_FAIL_FLAG" ] && return 1
            return 0
            ;;
        send-keys)
            return 0
            ;;
        capture-pane)
            cat "$MOCK_CAPTURE_RESPONSE_FILE" 2>/dev/null
            return 0
            ;;
        *)
            return 0
            ;;
    esac
}

# Source the real libraries under test
source "$PROJECT_ROOT/scripts/lib/receipt_processor/rp_logging.sh"
source "$PROJECT_ROOT/scripts/lib/receipt_processor/rp_extract.sh"
source "$PROJECT_ROOT/scripts/lib/receipt_processor/rp_delivery.sh"

# ===========================================================================
# Test 0: Baseline — default env (VNX_RECEIPT_T0_PUSH unset) still delivers
# ===========================================================================
reset_mocks
write_pending "d-000-1.json" "d-000"
set_capture_response ""   # empty input line -> verified
_retry_pending_receipts

assert_eq "1" "$(grep -c '^paste-buffer$' "$MOCK_CALL_LOG")" \
    "T0: baseline (push unset) sends exactly 1 paste-buffer"
assert_eq "1" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T0: baseline pending item moved to processed"
assert_eq "0" "$(count_files "$RECEIPTS_PENDING_DIR")" \
    "T0: baseline pending dir empty after delivery"

# ===========================================================================
# Test 1: Submit-verify success — empty input line -> processed
# ===========================================================================
reset_mocks
write_pending "d-101-1.json" "d-101"
set_capture_response ""
_retry_pending_receipts

assert_eq "1" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T1: verified-empty input line -> item moved to processed"
assert_eq "0" "$(count_files "$RECEIPTS_PENDING_DIR")" \
    "T1: verified-empty input line -> pending dir empty"

# ===========================================================================
# Test 2: Submit-verify failure — non-empty input line -> stays pending
# ===========================================================================
reset_mocks
write_pending "d-102-1.json" "d-102"
set_capture_response "Report: /tmp/r.md"   # residual receipt text still in input line
_retry_pending_receipts

assert_eq "0" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T2: unverified (non-empty input line) -> nothing moved to processed"
assert_eq "1" "$(count_files "$RECEIPTS_PENDING_DIR")" \
    "T2: unverified (non-empty input line) -> item stays pending"
assert_file_contains "$PROCESSING_LOG" "Submit-verify failed" \
    "T2: WARN logged for unverified submit"

# ===========================================================================
# Test 3: Dedupe — 3 pending files, same dispatch_id -> 1 delivery
# ===========================================================================
reset_mocks
write_pending "d-dup-1.json" "d-dup"
write_pending "d-dup-2.json" "d-dup"
write_pending "d-dup-3.json" "d-dup"
set_capture_response ""
_retry_pending_receipts

assert_eq "1" "$(grep -c '^paste-buffer$' "$MOCK_CALL_LOG")" \
    "T3: 3 pending items with the same dispatch_id -> exactly 1 paste-buffer"
assert_eq "3" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T3: all 3 duplicate pending files moved to processed together"
assert_eq "0" "$(count_files "$RECEIPTS_PENDING_DIR")" \
    "T3: pending dir empty after deduped delivery"
assert_file_contains "$PROCESSING_LOG" "Deduped 3 pending receipts for dispatch_id=d-dup" \
    "T3: dedupe logged"

# ===========================================================================
# Test 4: Digest — 6 distinct pending dispatches (> default threshold 5) -> 1 digest
# ===========================================================================
reset_mocks
write_pending "d-digest-1.json" "d-digest-1"
write_pending "d-digest-2.json" "d-digest-2"
write_pending "d-digest-3.json" "d-digest-3"
write_pending "d-digest-4.json" "d-digest-4"
write_pending "d-digest-5.json" "d-digest-5"
write_pending "d-digest-6.json" "d-digest-6"
set_capture_response ""
_retry_pending_receipts

assert_eq "1" "$(grep -c '^paste-buffer$' "$MOCK_CALL_LOG")" \
    "T4: 6 distinct pending dispatches -> exactly 1 digest paste"
assert_eq "6" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T4: all 6 pending files moved to processed after digest delivery"
assert_file_contains "$MOCK_LOADED_BUFFER" "RECEIPT-DIGEST: 6 receipts pending" \
    "T4: digest message states the correct count"
assert_file_contains "$MOCK_LOADED_BUFFER" "oudste: d-digest-1" \
    "T4: digest message names the oldest dispatch_id"

# ===========================================================================
# Test 4b: Digest threshold is configurable via VNX_RECEIPT_DIGEST_THRESHOLD
# ===========================================================================
reset_mocks
export VNX_RECEIPT_DIGEST_THRESHOLD=1
write_pending "d-cfg-1.json" "d-cfg-1"
write_pending "d-cfg-2.json" "d-cfg-2"
set_capture_response ""
_retry_pending_receipts

assert_file_contains "$MOCK_LOADED_BUFFER" "RECEIPT-DIGEST" \
    "T4b: lowered threshold (1) triggers digest for just 2 dispatches"
unset VNX_RECEIPT_DIGEST_THRESHOLD

# ===========================================================================
# Test 5: Push-switch — VNX_RECEIPT_T0_PUSH=0 -> zero tmux calls, still processed
# ===========================================================================
reset_mocks
export VNX_RECEIPT_T0_PUSH=0
receipt_json='{"dispatch_id":"d-push-off","terminal":"T2","status":"success","event_type":"task_complete","timestamp":"2026-07-16T10:00:00Z","report_path":"/tmp/r.md"}'
extract_receipt_fields "$receipt_json"
send_receipt_to_t0 "$receipt_json" "T2"

assert_eq "0" "$(wc -l < "$MOCK_CALL_LOG" | tr -d ' ')" \
    "T5: VNX_RECEIPT_T0_PUSH=0 makes zero tmux calls"
assert_eq "1" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T5: item still moves to processed when push is suppressed"
assert_eq "0" "$(count_files "$RECEIPTS_PENDING_DIR")" \
    "T5: pending dir empty after suppressed delivery"
assert_file_contains "$PROCESSING_LOG" "delivery_mode=suppressed dispatch_id=d-push-off" \
    "T5: suppressed delivery_mode logged"
unset VNX_RECEIPT_T0_PUSH

# ===========================================================================
# Test 6: Push-switch default (unset) behaves exactly like push=1 (no surprise)
# ===========================================================================
reset_mocks
receipt_json='{"dispatch_id":"d-push-default","terminal":"T2","status":"success","event_type":"task_complete","timestamp":"2026-07-16T10:00:00Z","report_path":"/tmp/r.md"}'
extract_receipt_fields "$receipt_json"
set_capture_response ""
send_receipt_to_t0 "$receipt_json" "T2"

assert_eq "1" "$(grep -c '^paste-buffer$' "$MOCK_CALL_LOG")" \
    "T6: default (VNX_RECEIPT_T0_PUSH unset) still pastes — no behavior change"
assert_eq "1" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T6: default push delivers and processes normally"

# ===========================================================================
# Test 7 (finding 4): verify-fail cap -> force-processed after N retries,
# no (N+1)th paste attempted
# ===========================================================================
reset_mocks
write_pending "d-cap-1.json" "d-cap"
set_capture_response "Report: /tmp/r.md"   # always unverified

_retry_pending_receipts
_retry_pending_receipts
_retry_pending_receipts
assert_eq "3" "$(grep -c '^paste-buffer$' "$MOCK_CALL_LOG")" \
    "T7: 3 failed sweeps -> exactly 3 paste-buffer attempts"
assert_eq "0" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T7: still pending after 3 failed sweeps (at cap, not yet over)"

_retry_pending_receipts   # 4th sweep: cap reached -> force-process, no paste
assert_eq "3" "$(grep -c '^paste-buffer$' "$MOCK_CALL_LOG")" \
    "T7: 4th sweep does not attempt another paste (cap already hit)"
assert_eq "1" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T7: 4th sweep force-processes the item"
assert_eq "0" "$(count_files "$RECEIPTS_PENDING_DIR")" \
    "T7: pending dir empty after force-process"
assert_file_contains "$PROCESSING_LOG" "unverified_after_3" \
    "T7: force-process WARN logged with retry count"

# ===========================================================================
# Test 8 (finding 5): jq absent on PATH -> fail loud, zero processed-moves
# ===========================================================================
reset_mocks
write_pending "d-nojq-1.json" "d-nojq"
set_capture_response ""
# PATH-stub containing only what log() needs (date, tee) so the fail-loud
# error path can still write to $PROCESSING_LOG — everything else PATH would
# normally resolve (jq included) is deliberately absent.
NOJQ_BIN_DIR="$TMP_ROOT/nojq_bin"
mkdir -p "$NOJQ_BIN_DIR"
ln -sf "$(command -v date)" "$NOJQ_BIN_DIR/date"
ln -sf "$(command -v tee)" "$NOJQ_BIN_DIR/tee"
ORIG_PATH="$PATH"
PATH="$NOJQ_BIN_DIR"
_retry_pending_receipts
RETRY_RC=$?
PATH="$ORIG_PATH"

assert_eq "1" "$RETRY_RC" \
    "T8: jq absent -> _retry_pending_receipts returns failure"
assert_eq "0" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T8: jq absent -> zero processed-moves"
assert_eq "1" "$(count_files "$RECEIPTS_PENDING_DIR")" \
    "T8: jq absent -> pending file untouched"
assert_file_contains "$PROCESSING_LOG" "jq not found" \
    "T8: jq-absence error logged"

# ===========================================================================
# Test 9 (finding 1a): load-buffer failure -> paste sequence aborts,
# item stays pending
# ===========================================================================
reset_mocks
write_pending "d-lb-1.json" "d-lb"
touch "$MOCK_LOADBUFFER_FAIL_FLAG"
set_capture_response ""
_retry_pending_receipts

assert_eq "0" "$(grep -c '^paste-buffer$' "$MOCK_CALL_LOG")" \
    "T9: load-buffer failure -> paste-buffer never attempted"
assert_eq "0" "$(count_files "$RECEIPTS_PROCESSED_DIR")" \
    "T9: load-buffer failure -> nothing moved to processed"
assert_eq "1" "$(count_files "$RECEIPTS_PENDING_DIR")" \
    "T9: load-buffer failure -> item stays pending"
assert_file_contains "$PROCESSING_LOG" "Failed to load-buffer" \
    "T9: load-buffer failure logged"
rm -f "$MOCK_LOADBUFFER_FAIL_FLAG"

# ===========================================================================
# Test 10 (finding 3): digest message includes the dispatch_id list
# ===========================================================================
reset_mocks
write_pending "d-idlist-1.json" "d-idlist-1"
write_pending "d-idlist-2.json" "d-idlist-2"
write_pending "d-idlist-3.json" "d-idlist-3"
write_pending "d-idlist-4.json" "d-idlist-4"
write_pending "d-idlist-5.json" "d-idlist-5"
write_pending "d-idlist-6.json" "d-idlist-6"
set_capture_response ""
_retry_pending_receipts

assert_file_contains "$MOCK_LOADED_BUFFER" "IDs:" \
    "T10: digest message has an IDs: section"
IDLIST_ALL_PRESENT=1
for n in 1 2 3 4 5 6; do
    grep -q "d-idlist-$n" "$MOCK_LOADED_BUFFER" || IDLIST_ALL_PRESENT=0
done
assert_eq "1" "$IDLIST_ALL_PRESENT" \
    "T10: digest message lists all 6 pending dispatch_ids (under the 10-id cap)"

# --- Cleanup ---
rm -rf "$TMP_ROOT"

# --- Summary ---
echo ""
echo "=== rp_delivery test results: $PASS_COUNT passed, $FAIL_COUNT failed ==="

[ "$FAIL_COUNT" -eq 0 ] || exit 1
