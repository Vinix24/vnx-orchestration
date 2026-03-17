#!/usr/bin/env bash
# Remote bootstrap for VNX orchestration.
# Usage: curl -sL <url>/install-remote.sh | bash
#    or: curl -sL <url>/install-remote.sh | bash -s -- --layout claude
#    or: bash install-remote.sh [--layout vnx|claude] [target-dir]
set -euo pipefail

VNX_REPO_URL="${VNX_REPO_URL:-https://github.com/Vinix24/vnx-orchestration-system.git}"
VNX_BRANCH="${VNX_BRANCH:-main}"
LAYOUT=""
TARGET_DIR=""

# Parse arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --layout)   LAYOUT="$2"; shift 2 ;;
    --layout=*) LAYOUT="${1#*=}"; shift ;;
    --branch)   VNX_BRANCH="$2"; shift 2 ;;
    --branch=*) VNX_BRANCH="${1#*=}"; shift ;;
    --repo)     VNX_REPO_URL="$2"; shift 2 ;;
    --repo=*)   VNX_REPO_URL="${1#*=}"; shift ;;
    -h|--help)
      echo "Usage: install-remote.sh [OPTIONS] [target-dir]"
      echo ""
      echo "Options:"
      echo "  --layout vnx|claude   Install layout (default: vnx)"
      echo "  --branch <branch>     Git branch/tag to install (default: main)"
      echo "  --repo <url>          Git repo URL"
      echo ""
      echo "Environment:"
      echo "  VNX_REPO_URL   Override default repo URL"
      echo "  VNX_BRANCH     Override default branch"
      exit 0
      ;;
    *)
      if [ -z "$TARGET_DIR" ]; then
        TARGET_DIR="$1"
      fi
      shift
      ;;
  esac
done

TARGET_DIR="${TARGET_DIR:-$PWD}"

echo "[vnx-install] Bootstrapping VNX orchestration..."
echo "[vnx-install] Repo: $VNX_REPO_URL (branch: $VNX_BRANCH)"
echo "[vnx-install] Target: $TARGET_DIR"

# Clone to temp directory
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

if ! git clone --depth 1 --branch "$VNX_BRANCH" "$VNX_REPO_URL" "$TMPDIR" 2>/dev/null; then
  echo "ERROR: Failed to clone $VNX_REPO_URL (branch: $VNX_BRANCH)" >&2
  exit 1
fi

# Run install.sh
INSTALL_ARGS=("$TARGET_DIR")
[ -n "$LAYOUT" ] && INSTALL_ARGS=("--layout" "$LAYOUT" "$TARGET_DIR")

bash "$TMPDIR/install.sh" "${INSTALL_ARGS[@]}"

# Determine VNX bin path based on layout
case "${LAYOUT:-vnx}" in
  claude) VNX_BIN="$TARGET_DIR/.claude/vnx-system/bin/vnx" ;;
  *)      VNX_BIN="$TARGET_DIR/.vnx/bin/vnx" ;;
esac

# Run init + doctor
if [ -f "$VNX_BIN" ]; then
  echo ""
  echo "[vnx-install] Running vnx init..."
  bash "$VNX_BIN" init

  echo ""
  echo "[vnx-install] Running vnx doctor..."
  bash "$VNX_BIN" doctor || true
fi

echo ""
echo "[vnx-install] Bootstrap complete."
echo "[vnx-install] Start with: $VNX_BIN start"
