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

printf '\nCurrent crontab:\n'
crontab -l
