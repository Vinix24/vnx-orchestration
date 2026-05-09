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
# Wave 1 — rotate shadow_divergence.ndjson if >100MB or older than 30 days
SHADOW_CRON_ENTRY="0 3 * * * find $PROJECT_ROOT/.vnx-data/state -name \"shadow_divergence.ndjson\" -size +100M -exec mv {} {}.archive-\$(date +%Y%m%d) \; -exec touch {} \;"

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

printf '\nCurrent crontab:\n'
crontab -l
