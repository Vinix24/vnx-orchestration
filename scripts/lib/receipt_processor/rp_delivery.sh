# shellcheck shell=bash
# rp_delivery.sh - Receipt delivery to T0 pane via tmux + outbox retry
# Sourced by scripts/receipt_processor.sh
# Requires: log() from rp_logging.sh, _build_state_line/_build_quality_line/
#           _drtp_get_next_action/_drtp_build_git_line from rp_extract.sh,
#           extract_receipt_fields() from rp_extract.sh,
#           get_pane_id_smart() from pane_manager,
#           _rf_* fields, $RECEIPTS_PENDING_DIR, $RECEIPTS_PROCESSED_DIR
# Env flags (OI-654): VNX_RECEIPT_T0_PUSH=1 (default) pushes to the T0 tmux
#           pane; 0 suppresses the push (ndjson append + outbox still happen).
#           VNX_RECEIPT_DIGEST_THRESHOLD=5 (default) collapses a pending stack
#           bigger than this into one digest paste instead of N individual ones.
#           VNX_RECEIPT_VERIFY_MAX_RETRIES=3 (default) caps how many failed
#           submit-verify sweeps a pending item/digest-group tolerates before
#           it is force-processed (delivery_mode=unverified_after_N) instead
#           of retried forever. Requires jq on PATH (fail-fast if absent).

# Verify a paste actually submitted rather than sitting in T0's input line
# (e.g. Enter landed while T0 was mid-turn or in a modal and got absorbed as
# a no-op). Captures the pane and checks whether the LAST line (the live
# input row) is still non-empty after the double-Enter sequence.
# Fails OPEN on a capture-pane error (empty dump): this is an additive
# vangnet, not a new way for delivery to block when tmux itself misbehaves.
# Returns 0 verified / 1 not-verified (caller must not mark processed).
_rpd_verify_submit() {
    local t0_pane="$1"
    local log_label="$2"

    local pane_dump last_line
    pane_dump=$(tmux capture-pane -t "$t0_pane" -p 2>/dev/null)
    last_line=$(printf '%s\n' "$pane_dump" | tail -n 1)
    # Trim leading/trailing whitespace so a blank-but-padded prompt line counts as empty.
    last_line="${last_line#"${last_line%%[![:space:]]*}"}"
    last_line="${last_line%"${last_line##*[![:space:]]}"}"

    if [ -n "$last_line" ]; then
        log "WARN" "Submit-verify failed for $log_label: input line not empty after Enter (pane: $t0_pane)"
        return 1
    fi
    return 0
}

# Shared paste + double-Enter + submit-verify sequence.
# Returns 0 if the paste was delivered AND verified submitted, 1 otherwise.
_rpd_paste_and_verify() {
    local t0_pane="$1"
    local message="$2"
    local log_label="$3"

    # finding 1a: check every tmux call's exit code. A failed load-buffer or
    # send-keys means the paste never happened (or Enter never landed) — the
    # item must stay pending, not fall through to submit-verify on a no-op.
    if ! echo "$message" | tmux load-buffer - 2>/dev/null; then
        log "ERROR" "Failed to load-buffer for T0 pane $t0_pane ($log_label)"
        return 1
    fi
    if ! tmux paste-buffer -t "$t0_pane" 2>/dev/null; then
        log "ERROR" "Failed to paste to T0 pane $t0_pane ($log_label)"
        return 1
    fi

    sleep 1
    if ! tmux send-keys -t "$t0_pane" Enter 2>/dev/null; then
        log "ERROR" "Failed to send Enter (1st) to T0 pane $t0_pane ($log_label)"
        return 1
    fi
    sleep 0.3
    if ! tmux send-keys -t "$t0_pane" Enter 2>/dev/null; then
        log "ERROR" "Failed to send Enter (2nd) to T0 pane $t0_pane ($log_label)"
        return 1
    fi
    sleep 0.3

    _rpd_verify_submit "$t0_pane" "$log_label"
}

# Section F (inner): Build enriched receipt message and paste to T0 tmux pane.
# Returns 0 on success, 1 if pane unreachable, paste failed, or submit-verify failed.
# Reads _rf_* variables set by extract_receipt_fields().
_deliver_receipt_to_t0_pane() {
    local receipt_json="$1"
    local terminal="$2"

    local dispatch_id="${_rf_dispatch_id:-no-id}"

    # Ghost-receipt filter: skip pastes for stop-hook triggers without real dispatch context.
    # Prevents flooding T0 pane when long-running sessions emit interim Stop events.
    # Pattern covers: bare 'unknown', 'unknown-*' variants, 'no-id', and empty string.
    # Primary fix is filename-based dispatch_id extraction in report_parser.py; this
    # is the vangnet for any residual cases where no real id can be derived at all.
    case "$dispatch_id" in
        unknown-*|unknown|no-id|"")
            log "INFO" "Skipping ghost receipt paste: dispatch_id=$dispatch_id"
            return 0
            ;;
    esac

    # Push-switch (OI-654): T0's polling on report-files instead of pane pushes
    # doesn't need the paste. Suppress it entirely — ndjson append already
    # happened upstream in append_and_track_receipt(), so the audit trail is
    # intact; only the tmux side-channel notification is skipped.
    if [ "${VNX_RECEIPT_T0_PUSH:-1}" = "0" ]; then
        log "INFO" "delivery_mode=suppressed dispatch_id=$dispatch_id (VNX_RECEIPT_T0_PUSH=0)"
        return 0
    fi

    local t0_pane
    t0_pane=$(get_pane_id_smart "T0" 2>/dev/null)
    if [ -z "$t0_pane" ]; then
        log "ERROR" "Could not find T0 pane - get_pane_id_smart returned empty"
        return 1
    fi

    local report_path="${_rf_report_path:-no-report}"
    local next_action
    next_action=$(_drtp_get_next_action "$_rf_status")
    local footer_status="$_rf_status"
    [ "$footer_status" = "success" ] && footer_status="done"

    local state_line quality_line git_line
    state_line=$(_build_state_line "$terminal")
    quality_line=$(_build_quality_line "$dispatch_id")
    git_line=$(_drtp_build_git_line "$receipt_json")

    local receipt_msg="/t0-orchestrator 📨 RECEIPT:${terminal}:${footer_status} | ID: ${dispatch_id} | Next: ${next_action}${quality_line}${state_line}${git_line}
Report: ${report_path}"

    if ! _rpd_paste_and_verify "$t0_pane" "$receipt_msg" "$dispatch_id"; then
        log "WARN" "Receipt not verified delivered, leaving pending: $dispatch_id"
        return 1
    fi

    log "INFO" "Receipt delivered to T0 (pane: $t0_pane)"
    return 0
}

# finding 3 (readability improvement, not an audit-trail change — see NOTE
# above the digest branch in _retry_pending_receipts): builds a comma-joined
# list of up to 10 dispatch_ids for the digest message, so T0 sees WHICH
# dispatches are behind the count instead of only a bare number + the oldest
# one. Caps at 10 + "…" so the message can't balloon for a very large stack —
# t0_receipts.ndjson remains the full inventory for anyone who needs it.
_rpd_build_id_list() {
    local max=10
    local n=$#
    local limit=$n
    [ "$limit" -gt "$max" ] && limit=$max

    local list="" i=0 item
    for item in "$@"; do
        i=$((i + 1))
        [ "$i" -gt "$limit" ] && break
        if [ -n "$list" ]; then
            list="${list},${item}"
        else
            list="$item"
        fi
    done
    [ "$n" -gt "$max" ] && list="${list}…"
    printf '%s' "$list"
}

# Deliver a digest paste covering a whole stack of pending receipts, instead of
# pasting each one individually. Same push-switch + submit-verify discipline
# as _deliver_receipt_to_t0_pane. Returns 0 verified-delivered / 1 otherwise.
_rpd_deliver_digest() {
    local count="$1"
    local oldest_dispatch_id="$2"
    local id_list="$3"

    if [ "${VNX_RECEIPT_T0_PUSH:-1}" = "0" ]; then
        log "INFO" "delivery_mode=suppressed digest count=$count (VNX_RECEIPT_T0_PUSH=0)"
        return 0
    fi

    local t0_pane
    t0_pane=$(get_pane_id_smart "T0" 2>/dev/null)
    if [ -z "$t0_pane" ]; then
        log "ERROR" "Could not find T0 pane for digest delivery"
        return 1
    fi

    local digest_msg="/t0-orchestrator 📨 RECEIPT-DIGEST: ${count} receipts pending, oudste: ${oldest_dispatch_id}, IDs: ${id_list}, zie t0_receipts.ndjson"

    if ! _rpd_paste_and_verify "$t0_pane" "$digest_msg" "digest:${oldest_dispatch_id}"; then
        log "WARN" "Digest not verified delivered, leaving $count receipt(s) pending"
        return 1
    fi

    log "INFO" "Digest delivered to T0 (pane: $t0_pane, count=$count)"
    return 0
}

# finding 4 vlucht-route: force-move a set of pending files straight to
# processed/ without another delivery attempt. t0_receipts.ndjson already has
# every one of these receipts (written upstream in append_and_track_receipt())
# — retrying a submit-verify that fails forever is more harmful than accepting
# one possibly-missed live paste. Shared by both the individual-group path and
# the digest path so the cap behaves identically in both (codex checklist:
# same fix to all handlers).
_rpd_force_process() {
    local max_retries="$1" label="$2" fail_count="$3"
    shift 3
    local f
    for f in "$@"; do
        mv "$f" "$RECEIPTS_PROCESSED_DIR/$(basename "$f")"
    done
    log "WARN" "delivery_mode=unverified_after_${max_retries} ${label} force-processed $# item(s) after ${fail_count} failed verify sweep(s)"
}

# finding 4: fail-count sidecar stored as a field inside the pending JSON
# receipt itself (rather than the filename) so it survives dedupe grouping
# and doesn't require renaming files mid-sweep. Defaults to 0 for files
# predating this fix or on any parse error.
_rpd_get_fail_count() {
    local count
    count=$(jq -r '._verify_fail_count // 0' "$1" 2>/dev/null)
    [ -z "$count" ] && count=0
    printf '%s' "$count"
}

# finding 4: atomic increment (codex checklist — never rewrite canonical state
# in place). Writes to a tmp file in the same dir, then renames over the
# original; a crash mid-write must not corrupt a pending receipt the next
# sweep still needs to read.
_rpd_increment_fail_count() {
    local file="$1"
    local tmp="${file}.tmp.$$"
    if jq '._verify_fail_count = ((._verify_fail_count // 0) + 1)' "$file" > "$tmp" 2>/dev/null; then
        mv "$tmp" "$file"
    else
        rm -f "$tmp"
        log "WARN" "Failed to increment verify-fail count for $(basename "$file")"
    fi
}

# Section F: Outbox wrapper — write-first, then deliver.
# Persists receipt to receipts/pending/ before attempting tmux delivery.
# On success: moves file to receipts/processed/.
# On failure: leaves file in receipts/pending/ for _retry_pending_receipts().
send_receipt_to_t0() {
    local receipt_json="$1"
    local terminal="$2"

    # Ensure outbox directories exist
    mkdir -p "$RECEIPTS_PENDING_DIR" "$RECEIPTS_PROCESSED_DIR"

    # Write-first: persist before any delivery attempt (guarantees no data loss)
    local pending_file="$RECEIPTS_PENDING_DIR/$(date +%s)-${terminal}-$RANDOM.json"
    printf '%s\n' "$receipt_json" > "$pending_file"

    if _deliver_receipt_to_t0_pane "$receipt_json" "$terminal"; then
        mv "$pending_file" "$RECEIPTS_PROCESSED_DIR/$(basename "$pending_file")"
        return 0
    else
        log "WARN" "Receipt queued for retry: $(basename "$pending_file")"
        return 1
    fi
}

# Retry poller: attempt delivery of all receipts still in pending/.
# Dedupes by dispatch_id (OI-654): a dispatch_id with multiple stacked pending
# files gets exactly ONE delivery attempt per sweep, and all its files move
# together on success/stay together on failure — a repeatedly-failing pane
# does not stack N identical notifications.
#
# NOTE (finding 2 — dedupe is a delivery-channel decision, not an audit-trail
# one): collapsing N pending files for the same dispatch_id into ONE tmux
# paste only reduces how many times T0's pane is interrupted. Every one of
# those N payloads was already durably persisted before dedupe ever runs
# (send_receipt_to_t0() writes to $RECEIPTS_PENDING_DIR before any delivery
# attempt), and a successful delivery here moves EVERY file in the group —
# not just the representative — to $RECEIPTS_PROCESSED_DIR. The full history
# of older same-id payloads and their individual details survives intact in
# t0_receipts.ndjson and receipts/processed/; this function only ever decides
# notification cadence, never what the audit trail retains.
#
# When the resulting number of distinct pending dispatches exceeds
# VNX_RECEIPT_DIGEST_THRESHOLD (default 5), one digest message replaces the
# individual pastes entirely (see NOTE on finding 3 at the digest branch).
#
# finding 4: a pending item/group whose submit-verify has failed
# VNX_RECEIPT_VERIFY_MAX_RETRIES (default 3) times in a row is force-processed
# instead of retried forever — see _rpd_force_process().
#
# finding 5: jq is required (dispatch_id lookup, dedupe key extraction,
# fail-count sidecar). Its absence must fail loud, not silently collapse
# every file to dispatch_id="no-id" or move files without delivery.
#
# Called periodically from _poll_new_reports() and once on startup.
_retry_pending_receipts() {
    if ! command -v jq >/dev/null 2>&1; then
        log "ERROR" "jq not found on PATH - cannot process pending receipts (dedupe/digest/retry-cap all require jq); leaving pending files untouched"
        return 1
    fi

    local digest_threshold="${VNX_RECEIPT_DIGEST_THRESHOLD:-5}"
    local max_retries="${VNX_RECEIPT_VERIFY_MAX_RETRIES:-3}"

    local pending_files=()
    while IFS= read -r -d '' f; do
        pending_files+=("$f")
    done < <(find "$RECEIPTS_PENDING_DIR" -name "*.json" -type f -print0 2>/dev/null)

    local total=${#pending_files[@]}
    [ "$total" -eq 0 ] && return 0

    # Per-file dispatch_id lookup (parallel array, same index as pending_files).
    local file_dispatch_ids=()
    local f dispatch_id
    for f in "${pending_files[@]}"; do
        dispatch_id=$(jq -r '.dispatch_id // empty' "$f" 2>/dev/null)
        [ -z "$dispatch_id" ] && dispatch_id="no-id"
        file_dispatch_ids+=("$dispatch_id")
    done

    # Unique dispatch_ids in first-seen order (dedupe key list). Deliberately
    # avoids associative arrays / negative array indices — bash 3.2 (macOS
    # default /bin/bash) supports neither.
    local unique_ids=()
    local id already u
    for id in "${file_dispatch_ids[@]}"; do
        already=0
        # bash 3.2 (macOS default) treats "${arr[@]}" as unbound under `set -u`
        # when arr has zero elements — the "${arr[@]+...}" guard avoids that.
        for u in "${unique_ids[@]+"${unique_ids[@]}"}"; do
            [ "$u" = "$id" ] && { already=1; break; }
        done
        [ "$already" -eq 0 ] && unique_ids+=("$id")
    done

    local group_count=${#unique_ids[@]}
    if [ "$group_count" -lt "$total" ]; then
        log "INFO" "Deduped $total pending receipt(s) into $group_count unique dispatch(es)"
    fi

    # Digest mode: too many distinct pending dispatches to paste individually.
    #
    # NOTE (finding 3 — digest is a delivery-channel decision, not an
    # audit-trail one): collapsing the whole pending stack into one digest
    # paste only affects the tmux-side notification. Every individual receipt
    # behind it is already in t0_receipts.ndjson and moves intact to
    # receipts/processed/ on delivery — the digest message now names up to
    # ~10 dispatch_ids (_rpd_build_id_list) so T0 gets more than a bare
    # count+oldest, but this is a cheap readability improvement, not a full
    # inventory; the ndjson stays the source of truth.
    if [ "$group_count" -gt "$digest_threshold" ]; then
        local oldest_idx=0 oldest_mtime cur_mtime i
        oldest_mtime=$(stat -f%m "${pending_files[0]}" 2>/dev/null || stat -c%Y "${pending_files[0]}" 2>/dev/null || echo 0)
        for ((i = 1; i < total; i++)); do
            cur_mtime=$(stat -f%m "${pending_files[$i]}" 2>/dev/null || stat -c%Y "${pending_files[$i]}" 2>/dev/null || echo 0)
            if [ "$cur_mtime" -lt "$oldest_mtime" ]; then
                oldest_mtime=$cur_mtime
                oldest_idx=$i
            fi
        done
        local oldest_dispatch_id="${file_dispatch_ids[$oldest_idx]}"

        # finding 4 vlucht-route (digest path): if this entire pending stack
        # has already failed verify max_retries times, force-process the lot
        # instead of attempting yet another digest paste.
        local digest_fail_count=0 df_idx df_fc
        for ((df_idx = 0; df_idx < total; df_idx++)); do
            df_fc=$(_rpd_get_fail_count "${pending_files[$df_idx]}")
            [ "$df_fc" -gt "$digest_fail_count" ] && digest_fail_count=$df_fc
        done
        if [ "$digest_fail_count" -ge "$max_retries" ]; then
            _rpd_force_process "$max_retries" "digest" "$digest_fail_count" "${pending_files[@]}"
            return 0
        fi

        local id_list
        id_list=$(_rpd_build_id_list "${unique_ids[@]}")

        log "INFO" "Pending stack ($group_count dispatches, $total files) exceeds digest threshold ($digest_threshold); sending 1 digest"
        if _rpd_deliver_digest "$total" "$oldest_dispatch_id" "$id_list"; then
            for f in "${pending_files[@]}"; do
                mv "$f" "$RECEIPTS_PROCESSED_DIR/$(basename "$f")"
            done
            log "INFO" "Digest delivered covering $total pending receipt(s)"
        else
            for f in "${pending_files[@]}"; do
                _rpd_increment_fail_count "$f"
            done
            log "WARN" "Digest delivery unverified; $total receipt(s) remain pending (fail count now $((digest_fail_count + 1))/$max_retries)"
        fi
        return 0
    fi

    log "INFO" "Retrying $total pending receipt(s) across $group_count dispatch(es)..."

    local gi
    for ((gi = 0; gi < group_count; gi++)); do
        id="${unique_ids[$gi]}"
        local group_indices=()
        local fidx
        for ((fidx = 0; fidx < total; fidx++)); do
            [ "${file_dispatch_ids[$fidx]}" = "$id" ] && group_indices+=("$fidx")
        done

        # Representative = most recently written file in the group.
        local rep_idx="${group_indices[0]}"
        local gj rep_mtime cur_mtime2
        rep_mtime=$(stat -f%m "${pending_files[$rep_idx]}" 2>/dev/null || stat -c%Y "${pending_files[$rep_idx]}" 2>/dev/null || echo 0)
        for gj in "${group_indices[@]}"; do
            cur_mtime2=$(stat -f%m "${pending_files[$gj]}" 2>/dev/null || stat -c%Y "${pending_files[$gj]}" 2>/dev/null || echo 0)
            if [ "$cur_mtime2" -ge "$rep_mtime" ]; then
                rep_mtime=$cur_mtime2
                rep_idx=$gj
            fi
        done

        # finding 4 vlucht-route: if this dispatch_id's group has already
        # failed submit-verify max_retries times, force-process it instead of
        # attempting another paste this sweep.
        local group_fail_count=0 gf_fc
        for gj in "${group_indices[@]}"; do
            gf_fc=$(_rpd_get_fail_count "${pending_files[$gj]}")
            [ "$gf_fc" -gt "$group_fail_count" ] && group_fail_count=$gf_fc
        done
        if [ "$group_fail_count" -ge "$max_retries" ]; then
            local group_files=()
            for gj in "${group_indices[@]}"; do
                group_files+=("${pending_files[$gj]}")
            done
            _rpd_force_process "$max_retries" "dispatch_id=$id" "$group_fail_count" "${group_files[@]}"
            continue
        fi

        local receipt_json terminal
        receipt_json=$(cat "${pending_files[$rep_idx]}")
        terminal=$(echo "$receipt_json" | jq -r '.terminal // "unknown"' 2>/dev/null)
        # Re-extract _rf_* fields so _deliver_receipt_to_t0_pane() has the right context
        extract_receipt_fields "$receipt_json" 2>/dev/null || true

        if [ "${#group_indices[@]}" -gt 1 ]; then
            log "INFO" "Deduped ${#group_indices[@]} pending receipts for dispatch_id=$id into 1 delivery"
        fi

        if _deliver_receipt_to_t0_pane "$receipt_json" "$terminal"; then
            for gj in "${group_indices[@]}"; do
                mv "${pending_files[$gj]}" "$RECEIPTS_PROCESSED_DIR/$(basename "${pending_files[$gj]}")"
            done
            log "INFO" "Pending receipt delivered: $(basename "${pending_files[$rep_idx]}")"
        else
            for gj in "${group_indices[@]}"; do
                _rpd_increment_fail_count "${pending_files[$gj]}"
            done
            log "WARN" "Verify-fail count incremented for dispatch_id=$id (now $((group_fail_count + 1))/$max_retries)"
        fi
    done
}
