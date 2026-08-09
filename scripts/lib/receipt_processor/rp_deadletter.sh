# shellcheck shell=bash
# rp_deadletter.sh - Deterministic-rejection tracking + dead-letter quarantine
# Sourced by scripts/receipt_processor.sh (after rp_logging.sh / rp_dedup.sh).
# Requires: log() and log_structured_failure() from rp_logging.sh,
#           _sha256() from main, $STATE_DIR, $PROCESSED_HASHES
#
# OI-1085 / OI-1086: a report that append_receipt.py refuses on a
# deterministic validation code (e.g. missing_model) can never succeed on
# retry — the file content does not change between poll cycles. Retrying it
# every cycle is exactly what produced the 2026-08-03..07 incident: 109
# reports refused eleven times per 50k log lines for four days straight,
# 386 CPU-minutes and 4.3 GB of log, ending in ENOSPC. This module counts
# per-(report-hash, code) refusals and, after DEADLETTER_THRESHOLD identical
# refusals, moves the report out of the scanned directory into
# $DEADLETTER_DIR and records its hash as processed, so neither the Bash
# lane nor the Python converter lane ever retries it.

# Why N=3: validation verdicts are deterministic on content, so the SECOND
# identical refusal already proves permanence. One extra attempt absorbs the
# one transient look-alike — a report still being written while the first
# append attempt runs can surface as invalid_json/empty_input and self-heal
# on the next cycle. N=3 quarantines within ~15s at the default 5s poll
# interval while tolerating that single mid-write race. Override via
# VNX_RECEIPT_DEADLETTER_THRESHOLD.
DEADLETTER_THRESHOLD="${VNX_RECEIPT_DEADLETTER_THRESHOLD:-3}"
DEADLETTER_DIR="${VNX_RECEIPT_DEADLETTER_DIR:-$STATE_DIR/receipt_deadletter}"
REJECTIONS_FILE="${VNX_RECEIPT_REJECTIONS_FILE:-$STATE_DIR/receipt_rejections.txt}"

# Validation codes whose verdict is a pure function of the report bytes:
# retrying the same content can never produce a different outcome. Anything
# else (I/O failure, crash, unexpected_error, internal_null_result) is
# treated as transient and is NOT counted — those keep the legacy
# retry-next-cycle behaviour so a real transient fault is never quarantined.
_is_deterministic_rejection_code() {
    case "$1" in
        missing_model|missing_required_key|invalid_json|invalid_receipt_type|empty_input)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# Record one deterministic refusal of $report_path with $code.
# Returns 0 when the report was dead-lettered by this call, 1 otherwise
# (transient code, below threshold, or a failed quarantine move).
record_rejection_and_maybe_deadletter() {
    local report_path="$1"
    local report_name="$2"
    local code="${3:-}"

    _is_deterministic_rejection_code "$code" || return 1

    local report_hash
    report_hash=$(_sha256 "$report_path")
    if [ -z "$report_hash" ]; then
        log "WARN" "Dead-letter: cannot hash $report_name — refusal not counted"
        return 1
    fi

    # Current count for (hash, code); default 0. Keyed on content hash so an
    # edited report starts a fresh count instead of inheriting a stale one.
    local count
    count=$(awk -v h="$report_hash" -v c="$code" '$1==h && $2==c {n=$3} END {print n+0}' "$REJECTIONS_FILE" 2>/dev/null)
    count=$(( ${count:-0} + 1 ))

    # Persist the incremented count atomically (tmp + mv, matching the
    # watermark-write pattern in receipt_processor.sh). A failed persist
    # must be loud: silently losing the count is exactly how the OI-1085
    # infinite retry loop stays possible.
    local tmp_rejections="${REJECTIONS_FILE}.tmp.$$"
    if ! {
        [ -f "$REJECTIONS_FILE" ] && grep -v -F "$report_hash $code " "$REJECTIONS_FILE" 2>/dev/null
        printf '%s %s %s\n' "$report_hash" "$code" "$count"
    } > "$tmp_rejections" || ! mv "$tmp_rejections" "$REJECTIONS_FILE" 2>/dev/null; then
        rm -f "$tmp_rejections"
        log "ERROR" "Dead-letter: cannot persist rejection count to $REJECTIONS_FILE — refusal NOT counted"
        return 1
    fi

    if [ "$count" -lt "$DEADLETTER_THRESHOLD" ]; then
        log "WARN" "Deterministic rejection ${count}/${DEADLETTER_THRESHOLD} for $report_name (code=$code) — dead-letter at ${DEADLETTER_THRESHOLD}"
        return 1
    fi

    # Threshold reached: quarantine so no lane retries this report again.
    if ! mkdir -p "$DEADLETTER_DIR" 2>/dev/null; then
        log "ERROR" "Dead-letter: cannot create $DEADLETTER_DIR — $report_name NOT quarantined"
        return 1
    fi
    local target="$DEADLETTER_DIR/$report_name"
    if [ -e "$target" ]; then
        target="$DEADLETTER_DIR/${report_name%.md}.${report_hash:0:8}.md"
    fi
    if mv "$report_path" "$target" 2>/dev/null; then
        # Belt-and-braces: record the hash as processed so even a restored
        # copy is skipped by should_process_report's hash check.
        if ! grep -q "^${report_hash}$" "$PROCESSED_HASHES" 2>/dev/null; then
            echo "$report_hash" >> "$PROCESSED_HASHES"
        fi
        printf '%s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$report_hash" "$code" "$report_name" >> "$DEADLETTER_DIR/INDEX.txt"
        log_structured_failure "receipt_deadlettered" "Report quarantined after $count identical rejections" "report=$report_name code=$code target=$target"
        log "ERROR" "DEAD-LETTERED: $report_name (code=$code, $count identical rejections) -> $target"
        return 0
    fi
    log "ERROR" "Dead-letter: failed to move $report_name to $DEADLETTER_DIR (non-fatal; retried next cycle)"
    return 1
}
