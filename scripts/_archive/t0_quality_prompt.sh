#!/usr/bin/env bash
# T0 Quality Advisory Prompt
# Reads completion receipts with quality issues and prompts user for action

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"

# Source paths
# shellcheck source=lib/vnx_paths.sh
source "$LIB_DIR/vnx_paths.sh" 2>/dev/null || {
    echo "ERROR: Failed to load vnx_paths.sh"
    exit 1
}

RECEIPTS_FILE="${VNX_STATE_DIR}/t0_receipts.ndjson"
DISPATCHES_DIR="${VNX_ROOT}/dispatches/pending"

# Read the last completion receipt with quality advisory
get_last_completion_with_advisory() {
    if [[ ! -f "$RECEIPTS_FILE" ]]; then
        echo ""
        return
    fi

    # Get last 50 receipts and filter for completions with quality_advisory
    tail -50 "$RECEIPTS_FILE" | \
    grep -E '"event_type":"(task_complete|task_completed|completion|complete)"' | \
    grep '"quality_advisory"' | \
    tail -1
}

# Extract decision from receipt
get_decision() {
    local receipt="$1"
    echo "$receipt" | python3 -c "
import json, sys
receipt = json.load(sys.stdin)
advisory = receipt.get('quality_advisory', {})
decision = advisory.get('t0_recommendation', {}).get('decision', 'approve')
print(decision)
"
}

# Extract summary for prompt
get_summary() {
    local receipt="$1"
    echo "$receipt" | python3 -c "
import json, sys
receipt = json.load(sys.stdin)
advisory = receipt.get('quality_advisory', {})
summary = advisory.get('summary', {})
rec = advisory.get('t0_recommendation', {})

blocking = summary.get('blocking_count', 0)
warnings = summary.get('warning_count', 0)
risk = summary.get('risk_score', 0)
decision = rec.get('decision', 'approve')
reason = rec.get('reason', '')

print(f'{decision.upper()}: {blocking}🔴 blocking, {warnings}⚠️  warnings, risk={risk}/100 - {reason}')
"
}

# Extract suggested dispatches
get_suggested_dispatches() {
    local receipt="$1"
    echo "$receipt" | python3 -c "
import json, sys
receipt = json.load(sys.stdin)
advisory = receipt.get('quality_advisory', {})
dispatches = advisory.get('t0_recommendation', {}).get('suggested_dispatches', [])

for i, d in enumerate(dispatches, 1):
    print(f'  {i}. [{d.get(\"type\")}] {d.get(\"description\")}')
"
}

# Create dispatch files from suggestions
create_follow_up_dispatches() {
    local receipt="$1"
    local task_id
    task_id=$(echo "$receipt" | python3 -c "import json, sys; print(json.load(sys.stdin).get('task_id', 'unknown'))")

    echo "$receipt" | python3 -c "
import json, sys, time
from pathlib import Path

receipt = json.load(sys.stdin)
advisory = receipt.get('quality_advisory', {})
dispatches = advisory.get('t0_recommendation', {}).get('suggested_dispatches', [])
task_id = receipt.get('task_id', 'unknown')
dispatches_dir = Path('$DISPATCHES_DIR')
dispatches_dir.mkdir(parents=True, exist_ok=True)

for i, dispatch in enumerate(dispatches, 1):
    timestamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    filename = f'{timestamp}-T0-quality-followup-{task_id}-{i}.md'
    filepath = dispatches_dir / filename

    content = f'''# Quality Follow-Up Dispatch

**Generated**: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}
**Origin Task**: {task_id}
**Type**: {dispatch.get('type', 'unknown')}

## Task

{dispatch.get('description', 'No description')}

## Affected Files

'''
    for f in dispatch.get('files', []):
        content += f'- {f}\n'

    content += '''
## Context

This follow-up task was generated from quality advisory analysis of the completion receipt.

Original recommendation: See quality_advisory in receipt for full details.
'''

    filepath.write_text(content)
    print(f'Created: {filename}')
"
}

# Main workflow
main() {
    echo "🔍 Checking for completion receipts with quality issues..."

    receipt=$(get_last_completion_with_advisory)

    if [[ -z "$receipt" ]]; then
        echo "✅ No completion receipts with quality advisories found"
        exit 0
    fi

    decision=$(get_decision "$receipt")

    # Only prompt if there are issues (hold or approve_with_followup)
    if [[ "$decision" == "approve" ]]; then
        echo "✅ Last completion: APPROVE (no quality issues)"
        exit 0
    fi

    # Show summary
    echo ""
    echo "📋 Quality Issues Detected:"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    get_summary "$receipt"
    echo ""
    echo "Suggested Actions:"
    get_suggested_dispatches "$receipt"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # Prompt user
    read -r -p "Create follow-up dispatch tasks? [Y/n] " response

    case "$response" in
        [nN]|[nN][oO])
            echo "⏭️  Skipped - no follow-up tasks created"
            exit 0
            ;;
        *)
            echo ""
            echo "📝 Creating follow-up dispatch tasks..."
            create_follow_up_dispatches "$receipt"
            echo ""
            echo "✅ Follow-up tasks created in: $DISPATCHES_DIR"
            exit 0
            ;;
    esac
}

main "$@"
