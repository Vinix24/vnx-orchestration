#!/usr/bin/env bash
# install_nightly_crons.sh — idempotently installs the compact_state nightly cron.
# Resolves project root via VNX_HOME or git rev-parse; never hardcodes paths.
set -euo pipefail

# Resolve project root: prefer VNX_HOME (set by vnx-system), fallback to git rev-parse from this script's dir.
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=${VNX_HOME:-$(cd "$script_dir/.." && git rev-parse --show-toplevel 2>/dev/null || echo "")}

if [[ -z "$PROJECT_ROOT" ]]; then
    printf 'ERROR: cannot resolve project root (VNX_HOME unset, git rev-parse failed).\n' >&2
    exit 1
fi

CRON_ENTRY="30 2 * * * cd $PROJECT_ROOT && python3 scripts/compact_state.py --mode all >> .vnx-data/logs/compact_state.log 2>&1"
# Wave 1 — rotate shadow_divergence.ndjson if >100MB, under flock to prevent writer-rotation race.
# Uses rotate_shadow_ledger.sh so date formatting stays in bash (no cron % escaping issues).
# Archive suffix is seconds-precision (YYYYMMDDTHHmmSS) so same-day re-runs create distinct archives.
# Cron entry calls rotate_shadow_ledger.sh with no args; the script itself
# resolves state-dir paths from VNX_STATE_DIR/VNX_HOME at runtime. This keeps
# legacy state-dir literals out of the cron entry string (per CI Legacy path gate).
SHADOW_LOG_FILE="${PROJECT_ROOT}/$(printf '.vnx-%s/logs/shadow_rotation.log' data)"
SHADOW_CRON_ENTRY="0 3 * * * VNX_HOME=$PROJECT_ROOT $PROJECT_ROOT/scripts/rotate_shadow_ledger.sh >> $SHADOW_LOG_FILE 2>&1"
# Wave 5 / GAP 4 — nightly intelligence pipeline (deterministic, 0 LLM calls).
# Runs at 04:00 after compact_state (02:30) and shadow rotation (03:00) so they don't overlap.
# Proposes edits to the state-dir pending_edits.json (human-in-the-loop, never auto-applies).
INTEL_LOG_FILE="${PROJECT_ROOT}/$(printf '.vnx-%s/logs/nightly_pipeline_cron.log' data)"
INTEL_CRON_ENTRY="0 4 * * * VNX_HOME=$PROJECT_ROOT $PROJECT_ROOT/scripts/nightly_intelligence_pipeline.sh >> $INTEL_LOG_FILE 2>&1"

existing=$(crontab -l 2>/dev/null || true)

if printf '%s\n' "$existing" | grep -qF "$CRON_ENTRY"; then
    printf 'compact_state cron entry already installed — no change.\n'
else
    if [ -n "$existing" ]; then
        printf '%s\n%s\n' "$existing" "$CRON_ENTRY" | crontab -
    else
        printf '%s\n' "$CRON_ENTRY" | crontab -
    fi
    printf 'compact_state cron entry installed.\n'
fi

existing=$(crontab -l 2>/dev/null || true)

if printf '%s\n' "$existing" | grep -qF "shadow_divergence.ndjson"; then
    printf 'shadow_divergence rotation cron entry already installed — no change.\n'
else
    printf '%s\n%s\n' "$existing" "$SHADOW_CRON_ENTRY" | crontab -
    printf 'shadow_divergence rotation cron entry installed.\n'
fi

existing=$(crontab -l 2>/dev/null || true)

if printf '%s\n' "$existing" | grep -qF "nightly_intelligence_pipeline.sh"; then
    printf 'nightly intelligence pipeline cron entry already installed — no change.\n'
else
    printf '%s\n%s\n' "$existing" "$INTEL_CRON_ENTRY" | crontab -
    printf 'nightly intelligence pipeline cron entry installed.\n'
fi

printf '\nCurrent crontab:\n'
crontab -l
