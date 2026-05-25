#!/usr/bin/env bash
# check_installer_no_template_leak.sh
#
# CI/CD gate: verifies that install.sh produces no developer-machine-specific
# paths in the installed .vnx directory.
#
# Usage:
#   bash scripts/check_installer_no_template_leak.sh [INSTALLED_VNX_DIR]
#
# If INSTALLED_VNX_DIR is not given, the script runs install.sh itself to a
# temp directory and checks the output.
#
# Exit code 0: clean (no leaks). Exit code 1: violations found.
#
# Dispatch-ID: 20260525-083038-wave2a-6-installer-template-leak

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Patterns that must NEVER appear in installed output ──────────────────────

# Hardcoded developer machine paths
FORBIDDEN_PATTERNS=(
    "/Users/vincentvandeth"
    "vincent-vd"
)

# Install-time placeholders that must be substituted
PLACEHOLDER_PATTERNS=(
    "{{USER_HOME}}"
    "{{VNX_PROJECT_ROOT}}"
    "{{VNX_HOME}}"
)

# File extensions to scan
SCAN_EXTENSIONS="-name '*.yml' -o -name '*.yaml' -o -name '*.json' -o -name '*.sh' -o -name '*.py' -o -name '*.md' -o -name '*.conf' -o -name '*.toml' -o -name '*.example'"

# ── Prepare install dir ──────────────────────────────────────────────────────

CLEANUP_TMPDIR=false
if [ $# -ge 1 ] && [ -d "$1" ]; then
    INSTALLED_VNX_DIR="$1"
else
    # Run install.sh to a fresh temp dir
    TMP_PROJ="$(mktemp -d /tmp/vnx-leak-check-XXXXXX)"
    CLEANUP_TMPDIR=true
    FAKE_HOME="$(mktemp -d /tmp/vnx-leak-check-home-XXXXXX)"

    echo "[check] Installing to $TMP_PROJ (HOME=$FAKE_HOME)..."
    HOME="$FAKE_HOME" bash "$REPO_ROOT/install.sh" "$TMP_PROJ" 2>/dev/null \
        || { echo "[check] ERROR: install.sh failed"; exit 1; }

    INSTALLED_VNX_DIR="$TMP_PROJ/.vnx"
fi

# ── Scan for violations ──────────────────────────────────────────────────────

VIOLATIONS=0

scan_for_pattern() {
    local dir="$1"
    local pattern="$2"
    local label="$3"

    while IFS= read -r -d '' file; do
        # Skip this script itself — it legitimately contains the pattern strings
        # as string constants for comparison purposes.
        [ "$(basename "$file")" = "check_installer_no_template_leak.sh" ] && continue

        if grep -qF "$pattern" "$file" 2>/dev/null; then
            rel="${file#$dir/}"
            echo "  VIOLATION [$label]: .vnx/$rel contains '$pattern'"
            VIOLATIONS=$((VIOLATIONS + 1))
        fi
    done < <(eval "find '$dir' -type f \( $SCAN_EXTENSIONS \) -print0 2>/dev/null")
}

echo ""
echo "VNX Installer Template-Leak Check"
echo "────────────────────────────────────"
echo "[check] Scanning: $INSTALLED_VNX_DIR"
echo ""

# Check forbidden developer paths
echo "── Checking forbidden developer paths ──"
for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
    scan_for_pattern "$INSTALLED_VNX_DIR" "$pattern" "developer-path"
done

# Check unsubstituted placeholders
echo "── Checking unsubstituted placeholders ──"
for ph in "${PLACEHOLDER_PATTERNS[@]}"; do
    scan_for_pattern "$INSTALLED_VNX_DIR" "$ph" "unsubstituted-placeholder"
done

# ── Linux-only: /Users/ prefix check (macOS is legitimate) ──────────────────
if [ "$(uname -s)" != "Darwin" ]; then
    echo "── Checking /Users/ prefix (Linux CI only) ──"
    scan_for_pattern "$INSTALLED_VNX_DIR" "/Users/" "macos-hardcoded-path"
fi

# ── Cleanup ──────────────────────────────────────────────────────────────────
if [ "$CLEANUP_TMPDIR" = true ]; then
    rm -rf "${TMP_PROJ:-}" "${FAKE_HOME:-}" 2>/dev/null || true
fi

# ── Result ───────────────────────────────────────────────────────────────────
echo ""
if [ "$VIOLATIONS" -gt 0 ]; then
    echo "FAILED — $VIOLATIONS violation(s) found in installed output."
    echo "Fix: replace hardcoded paths with {{USER_HOME}}, {{VNX_PROJECT_ROOT}}, or {{VNX_HOME}} placeholders."
    exit 1
fi

echo "PASSED — installer output is clean (no developer paths, no unsubstituted placeholders)."
exit 0
