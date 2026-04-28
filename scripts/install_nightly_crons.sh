#!/usr/bin/env bash
# install_nightly_crons.sh — idempotently installs the compact_state nightly cron.
# Safe to run multiple times; checks for the entry before adding.
set -euo pipefail

CRON_ENTRY='30 2 * * * cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt && python3 scripts/compact_state.py --mode all >> .vnx-data/logs/compact_state.log 2>&1'

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
