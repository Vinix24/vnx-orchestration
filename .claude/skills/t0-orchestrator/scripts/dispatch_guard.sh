#!/usr/bin/env bash
# dispatch_guard.sh
# Read-only pre-dispatch guard for T0.
# Direction B (OI-859): reads RUNTIME state via the vnx CLI (vnx status --json,
# vnx pool status --json) instead of the repo-local t0_brief.json presentation
# cache. The brief stays a presentation layer; no decision reads it.
#
# Examples:
#   bash skills/t0-orchestrator/scripts/dispatch_guard.sh
#   bash skills/t0-orchestrator/scripts/dispatch_guard.sh json
#
# Exit codes:
#   0 = GO (safe to dispatch)
#   2 = WAIT (no fresh live measurement, degraded, live work in the air,
#       pending queue, or conflicts)
#   1 = error (missing state)
#
# D5 (fabric-state-herstel): "is there work in the air" comes from
# .live_work (dispatches table + occupancy flock), not from .terminals or
# .queues.active_count, which the status snapshot no longer carries. Every
# live, starting, stale or unmeasured row is WAIT; a missing or unavailable
# live_work is Missing state, never GO.
#
# OI-2268: live_work and queues must be measured at the moment of the call
# (`vnx status --json --live`), not read from t0_state.json as it was last
# built: work that appeared after the build read as GO. GO requires the
# answer to prove it: live_measurement.mode "live" for both sections,
# live_measured_at at most _LIVE_MAX_AGE_SECONDS old, and no unmeasured read.
# A CLI older than the live measurement (it refuses or ignores --live) is
# WAIT, never GO: silence is not fresh.

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    echo "error: dispatch_guard.sh requires jq" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Robust root resolution: git top-level works from nested worktrees. The legacy
# four-parent walk overshoots by one level in a worktree topology
# (docs/operations/RUNTIME_LIVENESS.md §5).
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$SCRIPT_DIR/../../../.." && pwd)")"

# Reason string set by evaluate_state() when it returns non-zero.
# Consumed by print_human() and print_json() for operator-visible diagnostics.
_GUARD_REASON=""
_STATUS_JSON=""
# Set by main(): why the answer does not carry a fresh live measurement, or
# empty. Measured once per run, so every view reports the same age.
_LIVE_PROBLEM=""
# Set by _fetch_status_json() when the CLI refused `status --json --live`.
_LIVE_REFUSED=""

usage() {
    cat <<'USAGE'
Usage:
  dispatch_guard.sh         Human-readable go/no-go decision
  dispatch_guard.sh json    JSON go/no-go decision

Exit codes:
  0 = GO (safe to dispatch)
  2 = WAIT (no fresh live measurement, degraded, live work in the air,
      pending queue, or conflicts)
  1 = error (missing state)
USAGE
}

# Schema the guard's checks are written against. A CLI that answers anything
# else (the pip package's `active_dispatches`/`agents` shape) is refused.
_EXPECTED_SCHEMA="vnx_status/1.0"

# Oldest live measurement that still counts as taken at this call. The CLI
# stamps live_measured_at when it starts measuring and the guard reads it as
# soon as the CLI exits, so the age is the measurement plus interpreter exit:
# about 0.5 s on vnx-dev. Ten seconds leaves room for a loaded machine and
# stays far below the age of any build (a SessionStart snapshot is minutes to
# hours old).
_LIVE_MAX_AGE_SECONDS=10

# Resolve the vnx CLI to run, first executable wins: the repo's own bin/vnx, the
# install named by VNX_HOME, an explicit VNX_BIN, the project-pin shim
# ~/.vnx-system/bin/vnx (it honours .vnx-version, so a pinned consumer runs its own
# version), the central install ~/.vnx-system/current, then `vnx` on PATH. A consumer
# has no bin/vnx, and the `vnx` on its PATH may be the pip package, which does not
# speak vnx_status/1.0; the installs come before it so a consumer reads the schema the
# checks expect. intelligence.sh resolves in the same order.
_resolve_vnx() {
    local candidate
    for candidate in \
        "$REPO_ROOT/bin/vnx" \
        "${VNX_HOME:+$VNX_HOME/bin/vnx}" \
        "${VNX_BIN:-}" \
        "${HOME:+$HOME/.vnx-system/bin/vnx}" \
        "${HOME:+$HOME/.vnx-system/current/bin/vnx}"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    command -v vnx || true
}

# Run the resolved CLI bound to REPO_ROOT. `vnx status` has no --project-dir: it
# resolves the project from the cwd and from VNX_PROJECT_ROOT, so both are pinned.
# An unbound call answers for whatever directory (or inherited root) the caller has.
_vnx_in_repo() {
    local bin="$1"
    shift
    (cd "$REPO_ROOT" && VNX_PROJECT_ROOT="$REPO_ROOT" "$bin" "$@" 2>/dev/null)
}

# Fetch the runtime state into _STATUS_JSON, live_work and queues measured at
# this call (--live). A CLI older than the live measurement refuses --live (the
# 1.7.4 wrapper exits 1 on "Unknown option"): the plain call then tells such a
# CLI (it answers, without a live marker -> WAIT) from no state at all (no
# answer -> exit 1). Non-zero = the CLI produced no state.
_fetch_status_json() {
    local bin
    bin="$(_resolve_vnx)"
    [ -n "$bin" ] || return 1
    if _STATUS_JSON="$(_vnx_in_repo "$bin" status --json --live)"; then
        return 0
    fi
    _LIVE_REFUSED="$bin refused 'status --json --live'"
    _STATUS_JSON="$(_vnx_in_repo "$bin" status --json)"
}

# Best-effort pool info for the human view. Never gates the decision.
_fetch_pool_json() {
    local bin
    bin="$(_resolve_vnx)"
    [ -n "$bin" ] || return 1
    _vnx_in_repo "$bin" pool status --json --project-dir "$REPO_ROOT"
}

_LIVE_BUCKETS_JQ='[.live_work.live, .live_work.starting, .live_work.stale, .live_work.unmeasured]'

# jq: .live_measured_at as epoch seconds, null when absent or unparseable.
# fromdateiso8601 takes no fraction and no "+00:00", so both are normalised.
_LIVE_AT_DEF='def live_at: try (.live_measured_at | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601) catch null;'

# Empty output = live_work and queues in the answer were measured at this call.
# Otherwise why not: no live marker (a CLI older than the live measurement), a
# section that was not measured live, a stamp too old, in the future or
# unparseable, or a read that raised (live_measurement.unmeasured).
_live_freshness_problem() {
    jq -r --argjson max "$_LIVE_MAX_AGE_SECONDS" "${_LIVE_AT_DEF}"'
        (.live_measurement | if type == "object" then . else {} end) as $m
        | (["live_work", "queues"] - ($m.sections | if type == "array" then . else [] end)) as $missing
        | live_at as $at
        | if $m.mode != "live" or (.live_measured_at | type) != "string" then
            "cli gave no live measurement"
          elif ($missing | length) > 0 then
            "no live measurement of " + ($missing | join(", "))
          elif $at == null then
            "live_measured_at unparseable: " + (.live_measured_at | tostring)
          elif (now - $at) > $max then
            "live measurement is \((now - $at) | floor)s old (bound \($max)s)"
          elif ($at - now) > 1 then
            "live_measured_at lies \(($at - now) | floor)s in the future"
          elif ($m.unmeasured | type) != "array" then
            "live_measurement.unmeasured is not a list"
          elif ($m.unmeasured | length) > 0 then
            "measurement failed: " + ($m.unmeasured | map(tostring) | join("; "))
          else
            empty
          end
    ' <<<"$1"
}

# "live at <stamp> (age Ns, measured in Ds)" or "snapshot of t0_state.json (...)".
_measurement_line() {
    jq -r "${_LIVE_AT_DEF}"'
        live_at as $at
        | if (.live_measurement.mode? // "") == "live" and $at != null then
            "live at \(.live_measured_at) (age \((now - $at) | floor)s, measured in \(.live_measurement.duration_seconds // "?")s)"
          else
            "snapshot of t0_state.json (generated_at \(.state_freshness.generated_at? // "unknown"))"
          end
    ' <<<"$1"
}

# The queue count $1 (pending|conflict) of answer $2: a number, or
# "unmeasured: <reason>" when there is none. Not measured is not zero.
_queue_count() {
    jq -r --arg q "$1" '
        (.queues | if type == "object" then . else {} end) as $queues
        | if ($queues[$q + "_count"] | type) == "number" then
            $queues[$q + "_count"]
          else
            "unmeasured: " + ($queues[$q + "_unmeasured_reason"] // "no count given" | tostring)
          end
    ' <<<"$2"
}

# "live=N starting=N stale=N unmeasured=N", counted from the row lists so a
# stale .counts block can never under-report.
_live_counts() {
    jq -r '.live_work | "live=\(.live | length) starting=\(.starting | length) stale=\(.stale | length) unmeasured=\(.unmeasured | length)"' <<<"$1"
}

# Empty output = live_work usable. Otherwise the reason it is not.
_live_work_problem() {
    jq -r '
        if (.live_work | type) != "object" or (.live_work | length) == 0 then
            "live_work missing"
        elif .live_work.available != true then
            "live_work unavailable: " + (.live_work.reason // "no reason given" | tostring)
        elif ([.live_work.live, .live_work.starting, .live_work.stale, .live_work.unmeasured]
              | all(type == "array") | not) then
            "live_work lacks a live/starting/stale/unmeasured list"
        else
            empty
        end
    ' <<<"$1"
}

evaluate_state() {
    _GUARD_REASON=""
    local status_json="$1"

    # Check 0 (OI-2268): live_work and queues measured at this call, or WAIT.
    # main() judged the answer once into _LIVE_PROBLEM.
    if [ -n "$_LIVE_PROBLEM" ]; then
        _GUARD_REASON="live_unmeasured: ${_LIVE_PROBLEM}"
        return 2
    fi

    # Check 1 (postmortem §4.2): system_health.status == degraded → WAIT.
    # Ignoring this flag was part of the 2026-04-16 60h freeze.
    local sh_status
    sh_status=$(jq -r '.system_health.status // "healthy"' <<<"$status_json")
    if [ "$sh_status" = "degraded" ] || [ "$sh_status" = "failed" ]; then
        _GUARD_REASON="system_degraded: ${sh_status}"
        return 2
    fi

    # Check 2: any in-flight dispatch row → WAIT. unmeasured means the lock
    # probe raised: not measurable, so not GO. main() has already refused a
    # snapshot whose live_work is missing, unavailable or lacks a bucket.
    local live_total
    live_total=$(jq -r "$_LIVE_BUCKETS_JQ | map(length) | add" <<<"$status_json")
    if [ "$live_total" -gt 0 ]; then
        _GUARD_REASON="busy: live_work $(_live_counts "$status_json")"
        return 2
    fi

    # Check 3: pending dispatch queue → WAIT. A count the answer does not
    # carry (unmeasured, or absent) is WAIT, never read as zero.
    local pending_count
    pending_count=$(_queue_count pending "$status_json")
    case "$pending_count" in
        unmeasured*)
            _GUARD_REASON="queue: pending ${pending_count}"
            return 2
            ;;
    esac
    if [ "$pending_count" -gt 0 ]; then
        _GUARD_REASON="queue: pending=${pending_count}"
        return 2
    fi

    # Check 4: conflicts → WAIT, the same way.
    local conflict_count
    conflict_count=$(_queue_count conflict "$status_json")
    case "$conflict_count" in
        unmeasured*)
            _GUARD_REASON="conflicts ${conflict_count}"
            return 2
            ;;
    esac
    if [ "$conflict_count" -gt 0 ]; then
        _GUARD_REASON="conflicts: ${conflict_count}"
        return 2
    fi

    return 0
}

print_human() {
    if evaluate_state "$_STATUS_JSON"; then
        echo "GO: safe to dispatch"
    else
        echo "WAIT: ${_GUARD_REASON:-live work or queue are not idle}"
    fi

    echo "Measured: $(_measurement_line "$_STATUS_JSON")"
    echo "Queue: pending=$(_queue_count pending "$_STATUS_JSON") conflicts=$(_queue_count conflict "$_STATUS_JSON")"
    echo "Live work: $(_live_counts "$_STATUS_JSON")"
    jq -r '(.live_work | if type == "object" then . else {} end) as $w | ("live", "starting", "stale", "unmeasured") as $b | ($w[$b] // [])[] | "  \($b) \(.dispatch_id // "?") state=\(.state // "?") lock=\(.lock // "?") age=\(.age_seconds // 0)s"' <<<"$_STATUS_JSON"

    local pool
    pool="$(_fetch_pool_json)" || true
    if [ -n "$pool" ]; then
        echo "Pool: current=$(jq -r '.current // "n/a"' <<<"$pool") queue_depth=$(jq -r '.queue_depth // "n/a"' <<<"$pool")"
    fi
}

print_json() {
    local decision
    if evaluate_state "$_STATUS_JSON"; then
        decision="GO"
    else
        decision="WAIT"
    fi

    jq -n \
        --arg decision "$decision" \
        --arg reason "$_GUARD_REASON" \
        --argjson state "$(jq -c "${_LIVE_AT_DEF}"'{queues: .queues, live_work: .live_work, system_health: .system_health, live_measured_at: .live_measured_at, live_measurement: .live_measurement, live_age_seconds: (live_at as $at | if $at == null then null else ((now - $at) | floor) end)}' <<<"$_STATUS_JSON")" \
        '{decision: $decision, reason: $reason, queues: $state.queues, live_work: $state.live_work, system_health: $state.system_health, live_measured_at: $state.live_measured_at, live_measurement: $state.live_measurement, live_age_seconds: $state.live_age_seconds}'
}

main() {
    local mode="${1:-human}"

    if ! _fetch_status_json; then
        echo "Missing state: unable to read 'vnx status --json'" >&2
        exit 1
    fi

    # Fail closed on a CLI that speaks another schema, and say which CLI and which
    # schema: "live_work missing" would hide that the wrong CLI answered.
    local schema
    schema=$(jq -r '.schema? // "none"' <<<"$_STATUS_JSON" 2>/dev/null || echo "unparseable")
    if [ "$schema" != "$_EXPECTED_SCHEMA" ]; then
        echo "Missing state: $(_resolve_vnx) answered schema '${schema}', the guard needs '${_EXPECTED_SCHEMA}' (vnx status --json)" >&2
        exit 1
    fi

    # Fail closed when the runtime snapshot is not available or exposes no
    # system_health (we cannot confirm the fleet is healthy → error, not GO).
    local t0_avail sh_status
    t0_avail=$(jq -r '.t0_state_available // false' <<<"$_STATUS_JSON")
    if [ "$t0_avail" != "true" ]; then
        echo "Missing state: t0_state.json unavailable via 'vnx status --json'" >&2
        exit 1
    fi
    sh_status=$(jq -r '.system_health.status? // "unavailable"' <<<"$_STATUS_JSON")
    if [ "$sh_status" = "unavailable" ]; then
        echo "Missing state: system_health unavailable via 'vnx status --json'" >&2
        exit 1
    fi
    # Without a fresh live measurement the decision is WAIT on that alone
    # (Check 0), whatever shape the snapshot's live_work has. With one, a
    # live_work that is missing, unavailable or lacks a bucket is Missing state.
    _LIVE_PROBLEM="$(_live_freshness_problem "$_STATUS_JSON")"
    if [ -n "$_LIVE_PROBLEM" ] && [ -n "$_LIVE_REFUSED" ]; then
        _LIVE_PROBLEM="${_LIVE_PROBLEM} (${_LIVE_REFUSED})"
    fi
    if [ -z "$_LIVE_PROBLEM" ]; then
        local live_problem
        live_problem="$(_live_work_problem "$_STATUS_JSON")"
        if [ -n "$live_problem" ]; then
            echo "Missing state: ${live_problem} via 'vnx status --json --live'" >&2
            exit 1
        fi
    fi

    case "$mode" in
        human)
            print_human
            ;;
        json)
            print_json
            ;;
        help|--help|-h)
            usage
            ;;
        *)
            echo "Unknown mode: $mode" >&2
            usage
            exit 1
            ;;
    esac

    evaluate_state "$_STATUS_JSON"
}

main "$@"
