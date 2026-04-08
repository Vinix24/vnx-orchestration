#!/usr/bin/env bash
# VNX Stop Hook — Auto-Report Pipeline Entry Point
#
# Claude Code calls this script when a worker session ends (Stop hook).
# Receives hook JSON on stdin, writes an extraction trigger file to
# .vnx-data/state/report_pipeline/ for the next pipeline stage.
#
# Gate: VNX_AUTO_REPORT=1 must be set; otherwise no-op.
# Non-blocking: must complete in < 5s.
# Exit 0 always (never block the session stop).

set -euo pipefail

# ── Env-var gate ─────────────────────────────────────────────────────────────
if [ "${VNX_AUTO_REPORT:-0}" != "1" ]; then
    # Disabled — emit skipped output and exit silently
    printf '{"skipped":true,"skip_reason":"VNX_AUTO_REPORT not set"}\n'
    exit 0
fi

# ── Read and parse stdin JSON ─────────────────────────────────────────────────
STDIN_JSON=$(cat)

if ! command -v jq &>/dev/null; then
    printf '{"skipped":true,"skip_reason":"jq not available"}\n' >&2
    exit 0
fi

SESSION_ID=$(echo "$STDIN_JSON" | jq -r '.session_id // ""')
TRANSCRIPT_PATH=$(echo "$STDIN_JSON" | jq -r '.transcript_path // ""')
CWD=$(echo "$STDIN_JSON" | jq -r '.cwd // ""')

if [ -z "$SESSION_ID" ] || [ -z "$CWD" ]; then
    printf '{"skipped":true,"skip_reason":"missing session_id or cwd in hook input"}\n' >&2
    exit 0
fi

# ── Detect terminal identity from cwd ────────────────────────────────────────
# Matches .claude/terminals/T1, T2, or T3 in the cwd path
TERMINAL=""
case "$CWD" in
    */.claude/terminals/T1*|*/terminals/T1)
        TERMINAL="T1" ;;
    */.claude/terminals/T2*|*/terminals/T2)
        TERMINAL="T2" ;;
    */.claude/terminals/T3*|*/terminals/T3)
        TERMINAL="T3" ;;
    *)
        # Not a worker terminal (T0 or non-VNX session) — skip silently
        printf '{"skipped":true,"skip_reason":"not a worker terminal","cwd":"%s"}\n' "$CWD"
        exit 0
        ;;
esac

# ── Resolve project root from cwd ────────────────────────────────────────────
# Walk up from CWD to find directory containing .vnx-data/
PROJECT_ROOT=""
SEARCH_DIR="$CWD"
for _ in 1 2 3 4 5 6; do
    if [ -d "$SEARCH_DIR/.vnx-data" ]; then
        PROJECT_ROOT="$SEARCH_DIR"
        break
    fi
    PARENT="$(dirname "$SEARCH_DIR")"
    if [ "$PARENT" = "$SEARCH_DIR" ]; then
        break
    fi
    SEARCH_DIR="$PARENT"
done

if [ -z "$PROJECT_ROOT" ]; then
    # Use VNX_DATA_DIR env var as fallback
    if [ -n "${VNX_DATA_DIR:-}" ]; then
        PROJECT_ROOT="$(dirname "$VNX_DATA_DIR")"
    else
        printf '{"skipped":true,"skip_reason":"could not resolve project root from cwd"}\n' >&2
        exit 0
    fi
fi

VNX_DATA="${VNX_DATA_DIR:-$PROJECT_ROOT/.vnx-data}"

# ── Detect active dispatch ────────────────────────────────────────────────────
ACTIVE_DIR="$VNX_DATA/dispatches/active"
DISPATCH_ID=""
DISPATCH_GATE=""
DISPATCH_TRACK=""
PR_ID=""

if [ -d "$ACTIVE_DIR" ]; then
    # Find dispatch file matching this terminal's track
    TRACK_SUFFIX=""
    case "$TERMINAL" in
        T1) TRACK_SUFFIX="-A" ;;
        T2) TRACK_SUFFIX="-B" ;;
        T3) TRACK_SUFFIX="-C" ;;
    esac

    # Match active dispatch files by track suffix
    for dispatch_file in "$ACTIVE_DIR"/*"${TRACK_SUFFIX}.md" "$ACTIVE_DIR"/*"${TRACK_SUFFIX}.json"; do
        if [ -f "$dispatch_file" ]; then
            BASENAME="$(basename "$dispatch_file")"
            # Extract dispatch ID from filename (strip extension)
            CANDIDATE="${BASENAME%.*}"
            # Read gate and PR from dispatch file header (Manager Block format)
            DISPATCH_GATE=$(grep -m1 '^Gate:' "$dispatch_file" 2>/dev/null | awk '{print $2}' || echo "")
            DISPATCH_TRACK=$(grep -m1 '^Track:' "$dispatch_file" 2>/dev/null | awk '{print $2}' || echo "")
            PR_ID=$(grep -m1 '^PR-ID:' "$dispatch_file" 2>/dev/null | awk '{print $2}' || echo "")
            DISPATCH_ID="$CANDIDATE"
            break
        fi
    done
fi

if [ -z "$DISPATCH_ID" ]; then
    # No active dispatch — write partial trigger (extraction can still attempt git-based extraction)
    DISPATCH_ID="unknown-${TERMINAL}-$(date +%s)"
fi

# ── Write extraction trigger file ─────────────────────────────────────────────
PIPELINE_DIR="$VNX_DATA/state/report_pipeline"
mkdir -p "$PIPELINE_DIR"

TRIGGER_FILE="$PIPELINE_DIR/${DISPATCH_ID}.trigger.json"
TRIGGER_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

jq -n \
    --arg trigger_time "$TRIGGER_TIME" \
    --arg dispatch_id "$DISPATCH_ID" \
    --arg terminal "$TERMINAL" \
    --arg track "${DISPATCH_TRACK:-}" \
    --arg gate "${DISPATCH_GATE:-}" \
    --arg pr_id "${PR_ID:-}" \
    --arg session_id "$SESSION_ID" \
    --arg transcript_path "$TRANSCRIPT_PATH" \
    --arg cwd "$CWD" \
    --arg project_root "$PROJECT_ROOT" \
    --arg source "stop_hook" \
    '{
        trigger_time: $trigger_time,
        dispatch_id: $dispatch_id,
        terminal: $terminal,
        track: $track,
        gate: $gate,
        pr_id: $pr_id,
        session_id: $session_id,
        transcript_path: $transcript_path,
        cwd: $cwd,
        project_root: $project_root,
        source: $source
    }' > "$TRIGGER_FILE"

# ── Invoke report assembler ───────────────────────────────────────────────────
# Runs report_assembler.py CLI which reads the trigger file, runs extraction,
# assembles AutoReport JSON + markdown, and writes both to .vnx-data/.
ASSEMBLER="$PROJECT_ROOT/scripts/lib/report_assembler.py"
ASSEMBLER_OUTPUT=""
ASSEMBLER_EXIT=0

if [ -f "$ASSEMBLER" ] && command -v python3 &>/dev/null; then
    mkdir -p "$VNX_DATA/logs"
    ASSEMBLER_OUTPUT=$(
        VNX_DATA_DIR="$VNX_DATA" \
        python3 "$ASSEMBLER" "$TRIGGER_FILE" 2>>"$VNX_DATA/logs/report_pipeline.log"
    ) || ASSEMBLER_EXIT=$?
fi

# ── Emit structured output ────────────────────────────────────────────────────
MD_PATH=""
JSON_PATH=""
if [ -n "$ASSEMBLER_OUTPUT" ] && echo "$ASSEMBLER_OUTPUT" | jq -e . &>/dev/null; then
    MD_PATH=$(echo "$ASSEMBLER_OUTPUT" | jq -r '.md_path // ""')
    JSON_PATH=$(echo "$ASSEMBLER_OUTPUT" | jq -r '.json_path // ""')
fi

jq -n \
    --arg dispatch_id "$DISPATCH_ID" \
    --arg terminal "$TERMINAL" \
    --arg trigger_file "$TRIGGER_FILE" \
    --arg md_path "$MD_PATH" \
    --arg json_path "$JSON_PATH" \
    --argjson assembler_exit "$ASSEMBLER_EXIT" \
    '{
        auto_report_path: $trigger_file,
        dispatch_id: $dispatch_id,
        terminal: $terminal,
        md_path: $md_path,
        json_path: $json_path,
        assembler_exit: $assembler_exit,
        skipped: false
    }'

exit 0
