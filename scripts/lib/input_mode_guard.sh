#!/bin/bash
# Input-Mode Guard Library
# PR-1: Dispatcher Input-Mode Detection And Recovery
# Contract: docs/core/110_INPUT_READY_TERMINAL_CONTRACT.md
#
# Implements IMR-1 (dispatcher MUST NOT send-keys to pane_in_mode=1) and
# IMR-2 (fail-closed when recovery cannot prove input-readiness).
#
# Rate-limiting (PR-0 retry-loop-ux-protection):
#   Per-terminal cooldown prevents repeated copy-mode cancellation during retry storms.
#   Default cooldown: 30s (VNX_INPUT_MODE_COOLDOWN override).
#   During cooldown: probe pane_in_mode but skip recovery (cancel/escape).
#   If blocked during cooldown: defer dispatch (requeueable), preserve scrollback.
#   First attempt after cooldown expires: full recovery as normal.
#
# Audit reasons emitted:
#   recovered_before_dispatch   — pane was blocked; recovery restored normal mode
#   blocked_input_mode          — delivery blocked; recovery exhausted (or probe failed)
#   recovery_failed             — both recovery attempts failed
#   recovery_cooldown_deferred  — blocked during cooldown; dispatch deferred (scrollback preserved)

# Emit a structured NDJSON event to the VNX coordination audit log.
# Usage: _emit_input_mode_event <event_type> <terminal_id> <pane_target>
#        <pane_in_mode> <pane_dead> <pane_mode> <dispatch_id> [<extra_kv>]
# extra_kv: space-separated key=value pairs, e.g. "action=programmatic_cancel mode_before=copy-mode"
_emit_input_mode_event() {
    local event_type="$1"
    local terminal_id="$2"
    local pane_target="$3"
    local pane_in_mode="$4"
    local pane_dead="$5"
    local pane_mode="$6"
    local dispatch_id="$7"
    local extra="${8:-}"

    local audit_file="${STATE_DIR:-${VNX_STATE_DIR:-/tmp}}/blocked_dispatch_audit.ndjson"
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    python3 - "$event_type" "$terminal_id" "$pane_target" \
        "$pane_in_mode" "$pane_dead" "$pane_mode" \
        "$dispatch_id" "$ts" "$audit_file" "$extra" <<'PY'
import json, sys, os
(event_type, terminal_id, pane_target, pane_in_mode, pane_dead,
 pane_mode, dispatch_id, ts, audit_file, extra) = sys.argv[1:]
event = {
    "event_type": event_type,
    "terminal_id": terminal_id,
    "pane_target": pane_target,
    "pane_in_mode": int(pane_in_mode) if pane_in_mode.isdigit() else pane_in_mode,
    "pane_dead": int(pane_dead) if pane_dead.isdigit() else pane_dead,
    "pane_mode": pane_mode,
    "dispatch_id": dispatch_id,
    "timestamp": ts,
}
for kv in extra.split():
    if "=" in kv:
        k, v = kv.split("=", 1)
        try:
            event[k] = int(v)
        except ValueError:
            event[k] = v
os.makedirs(os.path.dirname(os.path.abspath(audit_file)), exist_ok=True)
with open(audit_file, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(event, separators=(",", ":")) + "\n")
PY
}

# Execute a single pane input-mode probe.
# Usage: _input_mode_probe <target_pane>
# Outputs: "<pane_in_mode>:<pane_dead>:<pane_mode>" triple (e.g. "0:0:" or "1:0:copy-mode")
# Returns: 0 on success, 1 if tmux query fails (pane unreachable, session lost)
_input_mode_probe() {
    local target_pane="$1"
    local result
    if ! result=$(tmux display-message -p -t "$target_pane" '#{pane_in_mode}:#{pane_dead}:#{pane_mode}' 2>/dev/null); then
        return 1
    fi
    printf '%s' "$result"
}

# ---------------------------------------------------------------------------
# Rate-limiting: per-terminal recovery cooldown
# ---------------------------------------------------------------------------

# Default cooldown period in seconds (override with VNX_INPUT_MODE_COOLDOWN)
_INPUT_MODE_COOLDOWN="${VNX_INPUT_MODE_COOLDOWN:-30}"

# Return the cooldown file path for a terminal.
_cooldown_file() {
    local terminal_id="$1"
    local cooldown_dir="${STATE_DIR:-${VNX_STATE_DIR:-/tmp}}/input_mode_cooldown"
    mkdir -p "$cooldown_dir" 2>/dev/null
    printf '%s/%s' "$cooldown_dir" "$terminal_id"
}

# Check if recovery is within cooldown window for a terminal.
# Returns 0 if in cooldown (should skip recovery), 1 if cooldown expired.
_recovery_in_cooldown() {
    local terminal_id="$1"
    local cf
    cf="$(_cooldown_file "$terminal_id")"
    if [ ! -f "$cf" ]; then
        return 1
    fi
    local last_recovery now elapsed
    last_recovery=$(cat "$cf" 2>/dev/null) || return 1
    now=$(date +%s)
    elapsed=$((now - last_recovery))
    if [ "$elapsed" -lt "$_INPUT_MODE_COOLDOWN" ]; then
        return 0
    fi
    return 1
}

# Record that a recovery attempt was made for a terminal (starts cooldown).
_record_recovery_timestamp() {
    local terminal_id="$1"
    local cf
    cf="$(_cooldown_file "$terminal_id")"
    date +%s > "$cf"
}

# Probe pane input mode, attempt recovery if blocked, fail closed if recovery fails.
#
# Implements the canonical recovery sequence from contract Section 5.3:
#   Attempt 1 — programmatic cancel: tmux copy-mode -q
#   Attempt 2 — escape fallback:     tmux send-keys Escape
#
# Usage: check_pane_input_ready <target_pane> <terminal_id> <dispatch_id> [<provider>]
#
# Args:
#   target_pane   tmux pane target (e.g. "T2:0.1")
#   terminal_id   logical terminal ID (e.g. "T2") — for audit events
#   dispatch_id   dispatch being delivered — for audit linkage
#   provider      (optional) provider name; providers matching "headless*" are exempt
#
# Returns:
#   0 — pane is input-ready (native or recovered); audit: recovered_before_dispatch / none
#   1 — pane is blocked and recovery failed; audit: blocked_input_mode
#   1 — probe failed (pane unreachable or dead); audit: blocked_input_mode
#
# Audit events written to $STATE_DIR/blocked_dispatch_audit.ndjson (NDJSON):
#   input_mode_probed               — every probe execution
#   input_mode_recovery_started     — each recovery attempt
#   input_mode_recovery_succeeded   — recovery restored normal mode
#   input_mode_recovery_failed      — all attempts exhausted without success
#   input_mode_delivery_blocked     — delivery blocked due to unrecoverable mode
check_pane_input_ready() {
    local target_pane="$1"
    local terminal_id="$2"
    local dispatch_id="$3"
    local provider="${4:-}"

    # Headless exemption: headless targets invoke the CLI as a subprocess without
    # tmux send-keys — probing tmux pane mode would produce false failures.
    # (contract Section 8.3)
    if [[ "$provider" == headless* ]]; then
        return 0
    fi

    # --- Initial probe (contract Section 4.1) ---
    local probe_result
    if ! probe_result=$(_input_mode_probe "$target_pane"); then
        log "V8 INPUT_MODE: probe_failed terminal=$terminal_id pane=$target_pane dispatch=$dispatch_id"
        _emit_input_mode_event "input_mode_probed" "$terminal_id" "$target_pane" \
            "probe_failed" "0" "" "$dispatch_id"
        _emit_input_mode_event "input_mode_delivery_blocked" "$terminal_id" "$target_pane" \
            "probe_failed" "0" "" "$dispatch_id" \
            "reason=probe_failed"
        return 1
    fi

    local pane_in_mode pane_dead pane_mode
    IFS=: read -r pane_in_mode pane_dead pane_mode <<< "$probe_result"

    # Emit probe event (Section 7.1)
    _emit_input_mode_event "input_mode_probed" "$terminal_id" "$target_pane" \
        "${pane_in_mode:-probe_failed}" "${pane_dead:-0}" "${pane_mode:-}" "$dispatch_id"

    # Dead pane — abort (contract Section 4.2)
    if [[ "$pane_dead" == "1" ]]; then
        log "V8 INPUT_MODE: pane_dead terminal=$terminal_id pane=$target_pane dispatch=$dispatch_id"
        _emit_input_mode_event "input_mode_delivery_blocked" "$terminal_id" "$target_pane" \
            "${pane_in_mode:-0}" "1" "${pane_mode:-}" "$dispatch_id" \
            "reason=pane_dead"
        return 1
    fi

    # Empty or malformed probe result — fail closed
    if [[ -z "$pane_in_mode" ]]; then
        log "V8 INPUT_MODE: probe_empty terminal=$terminal_id pane=$target_pane dispatch=$dispatch_id"
        _emit_input_mode_event "input_mode_delivery_blocked" "$terminal_id" "$target_pane" \
            "probe_empty" "0" "" "$dispatch_id" \
            "reason=probe_empty"
        return 1
    fi

    # Input-ready — proceed immediately (no recovery needed)
    if [[ "$pane_in_mode" == "0" ]]; then
        log "V8 INPUT_MODE: input_ready terminal=$terminal_id dispatch=$dispatch_id"
        return 0
    fi

    # Input-blocked — check cooldown before attempting recovery
    local mode_before="${pane_mode:-copy-mode}"

    # Rate-limiting: if within cooldown window, defer dispatch without recovery
    # to preserve operator scrollback during retry storms.
    if _recovery_in_cooldown "$terminal_id"; then
        log "V8 INPUT_MODE: recovery_cooldown mode=$mode_before terminal=$terminal_id dispatch=$dispatch_id — deferring (scrollback preserved)"
        _emit_input_mode_event "input_mode_recovery_cooldown" "$terminal_id" "$target_pane" \
            "$pane_in_mode" "${pane_dead:-0}" "$mode_before" "$dispatch_id" \
            "reason=recovery_cooldown_deferred cooldown=${_INPUT_MODE_COOLDOWN}s"
        _emit_input_mode_event "input_mode_delivery_blocked" "$terminal_id" "$target_pane" \
            "$pane_in_mode" "${pane_dead:-0}" "$mode_before" "$dispatch_id" \
            "reason=recovery_cooldown_deferred"
        return 1
    fi

    # Begin bounded recovery (contract Section 5)
    log "V8 INPUT_MODE: input_blocked mode=$mode_before terminal=$terminal_id dispatch=$dispatch_id — attempting recovery"

    # Record recovery timestamp to start cooldown window
    _record_recovery_timestamp "$terminal_id"

    # --- Recovery attempt 1: programmatic cancel (preferred, Section 5.2) ---
    _emit_input_mode_event "input_mode_recovery_started" "$terminal_id" "$target_pane" \
        "$pane_in_mode" "${pane_dead:-0}" "$mode_before" "$dispatch_id" \
        "action=programmatic_cancel"

    tmux copy-mode -q -t "$target_pane" 2>/dev/null || true
    sleep 0.2

    if ! probe_result=$(_input_mode_probe "$target_pane"); then
        probe_result="1:0:"
    fi
    IFS=: read -r pane_in_mode pane_dead pane_mode <<< "$probe_result"

    if [[ "$pane_in_mode" == "0" ]]; then
        log "V8 INPUT_MODE: recovery_succeeded action=programmatic_cancel mode_before=$mode_before terminal=$terminal_id dispatch=$dispatch_id"
        _emit_input_mode_event "input_mode_recovery_succeeded" "$terminal_id" "$target_pane" \
            "0" "${pane_dead:-0}" "${pane_mode:-}" "$dispatch_id" \
            "action=programmatic_cancel mode_before=$mode_before"
        return 0
    fi

    # --- Recovery attempt 2: Escape fallback (Section 5.3, step 7) ---
    _emit_input_mode_event "input_mode_recovery_started" "$terminal_id" "$target_pane" \
        "$pane_in_mode" "${pane_dead:-0}" "${pane_mode:-$mode_before}" "$dispatch_id" \
        "action=escape_fallback"

    tmux send-keys -t "$target_pane" Escape 2>/dev/null || true
    sleep 0.2

    if ! probe_result=$(_input_mode_probe "$target_pane"); then
        probe_result="1:0:"
    fi
    IFS=: read -r pane_in_mode pane_dead pane_mode <<< "$probe_result"

    if [[ "$pane_in_mode" == "0" ]]; then
        log "V8 INPUT_MODE: recovery_succeeded action=escape mode_before=$mode_before terminal=$terminal_id dispatch=$dispatch_id"
        _emit_input_mode_event "input_mode_recovery_succeeded" "$terminal_id" "$target_pane" \
            "0" "${pane_dead:-0}" "${pane_mode:-}" "$dispatch_id" \
            "action=escape_fallback mode_before=$mode_before"
        return 0
    fi

    # --- Recovery exhausted — fail closed (IMR-2, Section 6) ---
    local final_mode="${pane_mode:-$mode_before}"
    log "V8 INPUT_MODE: recovery_failed mode=$final_mode mode_before=$mode_before terminal=$terminal_id dispatch=$dispatch_id attempts=2"
    _emit_input_mode_event "input_mode_recovery_failed" "$terminal_id" "$target_pane" \
        "$pane_in_mode" "${pane_dead:-0}" "$final_mode" "$dispatch_id" \
        "mode_before=$mode_before attempts=2"
    _emit_input_mode_event "input_mode_delivery_blocked" "$terminal_id" "$target_pane" \
        "$pane_in_mode" "${pane_dead:-0}" "$final_mode" "$dispatch_id" \
        "reason=recovery_failed mode_before=$mode_before"
    return 1
}
