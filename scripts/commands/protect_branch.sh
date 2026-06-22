#!/usr/bin/env bash
# VNX: apply the standard worker-repo branch-protection policy to a repo's main.
#
# This is the server-side, un-evadable slot for OI-098 (worker->main push). The
# local pre-push hook (hooks/git/pre-push) is bypassable with --no-verify; this
# branch protection is not. POLICY (general — applies to ANY repo where governed
# VNX workers run, not just one named repo):
#   - PR required, required_approving_review_count=0 (solo maintainer self-merges)
#   - enforce_admins=true  (binds even owner credentials; a worker pushing with
#     the owner's creds is still blocked — the actual threat)
#   - no force-pushes, no deletions
#
# Usage:
#   protect_branch.sh <owner/repo> [branch]      # apply (branch default: main)
#   protect_branch.sh --show <owner/repo> [branch]
#   VNX_PROTECT_DRY_RUN=1 protect_branch.sh <owner/repo> [branch]   # print, don't apply
#
# vnx-orchestration's main already carries this exact policy. Rollout targets:
# SEOCRAWLER_V2 (after its P0 work merges) and mission-control (at the B cutover).

set -euo pipefail

usage() {
    echo "usage: protect_branch.sh [--show] <owner/repo> [branch]  (default branch: main)"
}

SHOW=0
if [ "${1:-}" = "--show" ]; then
    SHOW=1
    shift
fi

REPO="${1:-}"
BRANCH="${2:-main}"
if [ -z "$REPO" ]; then
    usage >&2
    exit 2
fi

API_PATH="repos/$REPO/branches/$BRANCH/protection"

if [ "$SHOW" -eq 1 ]; then
    gh api "$API_PATH"
    exit 0
fi

read -r -d '' BODY <<JSON || true
{
  "required_status_checks": null,
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 0, "dismiss_stale_reviews": true},
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

if [ "${VNX_PROTECT_DRY_RUN:-}" = "1" ]; then
    echo "DRY-RUN: gh api -X PUT $API_PATH --input -"
    echo "$BODY"
    exit 0
fi

echo "Applying branch protection to ${REPO}@${BRANCH} ..."
printf '%s' "$BODY" | gh api -X PUT "$API_PATH" --input - >/dev/null
echo "Protected: ${REPO}@${BRANCH} (enforce_admins, PR-required, no force-push/delete)."
