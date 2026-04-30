#!/usr/bin/env bash
# Smoke test: no unannotated hardcoded user paths in shell scripts.
# Scans scripts/**/*.sh for /Users/<name> or /home/<name> literal paths.
# Exempt a line with: # vnx-allow-hardcoded: <rationale>
#
# Usage: bash tests/smoke/smoke_no_hardcoded_user_paths.sh
# Exit 0: clean. Exit 1: unannotated hardcoded user path found.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"

FAIL=0
VIOLATIONS=""

while IFS= read -r match; do
    line_content="$(printf '%s' "$match" | cut -d: -f3-)"
    case "$line_content" in
        *'# vnx-allow-hardcoded:'*) continue ;;
    esac
    VIOLATIONS="${VIOLATIONS}  ${match}
"
    FAIL=1
done < <(grep -rn -E '/Users/[a-zA-Z]|/home/[a-zA-Z]' "$SCRIPTS_DIR" --include='*.sh' 2>/dev/null || true)

if [ "$FAIL" -eq 1 ]; then
    echo "[FAIL] Unannotated hardcoded user paths found in shell scripts:"
    printf '%s' "$VIOLATIONS"
    echo "  → Add '# vnx-allow-hardcoded: <rationale>' to the offending line to exempt it"
    exit 1
fi

echo "[PASS] No unannotated hardcoded user paths in scripts/**/*.sh"
exit 0
