#!/usr/bin/env bash
set -euo pipefail

SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse arguments
LAYOUT="vnx"  # default layout
TARGET_PROJECT_DIR=""
DO_CHECK=false
while [ $# -gt 0 ]; do
  case "$1" in
    --layout)
      LAYOUT="$2"; shift 2
      ;;
    --layout=*)
      LAYOUT="${1#*=}"; shift
      ;;
    --check)
      DO_CHECK=true; shift
      ;;
    -h|--help)
      cat <<HELP
Usage: install.sh [TARGET_DIR] [--layout vnx|claude] [--check]

Arguments:
  TARGET_DIR        Project directory to install into (default: current dir)

Options:
  --layout <type>   Installation layout: 'vnx' (default) or 'claude'
  --check           Check prerequisites only (dry-run, no files written)
  -h, --help        Show this help text

Examples:
  bash install.sh                          # Install to current directory
  bash install.sh /path/to/project         # Install to specific project
  bash install.sh --check                  # Check prerequisites only
  bash install.sh --layout claude ./proj   # Hidden layout install

Post-install:
  .vnx/bin/vnx setup              # One-command setup (recommended)
  .vnx/bin/vnx setup --starter    # Starter mode (no tmux needed)
  .vnx/bin/vnx setup --operator   # Operator mode (full tmux grid)
HELP
      exit 0
      ;;
    *)
      if [ -z "$TARGET_PROJECT_DIR" ]; then
        TARGET_PROJECT_DIR="$1"
      fi
      shift
      ;;
  esac
done

TARGET_PROJECT_DIR="${TARGET_PROJECT_DIR:-$PWD}"
TARGET_PROJECT_DIR="$(cd "$TARGET_PROJECT_DIR" && pwd)"

# ── Prerequisite validation ──────────────────────────────────────────────
# Run Python install validator if available, fall back to inline checks.
_check_prereqs() {
  local validator="$SRC_ROOT/scripts/vnx_install.py"
  if command -v python3 >/dev/null 2>&1 && [ -f "$validator" ]; then
    python3 "$validator" --check
    return $?
  fi

  # Inline fallback: check minimum required tools
  local fails=0
  echo ""
  echo "VNX Install — Prerequisite Check"
  echo "─────────────────────────────────"

  if command -v python3 >/dev/null 2>&1; then
    echo "  [PASS] python3: $(which python3)"
  else
    echo "  [FAIL] python3: not found (required)"
    fails=$((fails + 1))
  fi

  if command -v bash >/dev/null 2>&1; then
    echo "  [PASS] bash: $(which bash)"
  else
    echo "  [FAIL] bash: not found (required)"
    fails=$((fails + 1))
  fi

  if command -v git >/dev/null 2>&1; then
    echo "  [PASS] git: $(which git)"
  else
    echo "  [FAIL] git: not found (required)"
    fails=$((fails + 1))
  fi

  echo ""
  if [ "$fails" -gt 0 ]; then
    echo "FAILED — $fails prerequisite(s) missing"
    return 1
  fi
  echo "READY — all prerequisites met"
  return 0
}

if [ "$DO_CHECK" = true ]; then
  _check_prereqs
  exit $?
fi

# Run prereq check before install (non-blocking for backward compat)
if ! _check_prereqs; then
  echo ""
  echo "WARNING: Prerequisites not fully met. Install may produce a broken setup."
  echo "Run 'install.sh --check' for details, or press Enter to continue anyway."
  read -r _continue || true
fi

# Layout determines install directory
case "$LAYOUT" in
  vnx)
    TARGET_VNX_DIR="$TARGET_PROJECT_DIR/.vnx"
    ;;
  claude)
    TARGET_VNX_DIR="$TARGET_PROJECT_DIR/.claude/vnx-system"
    ;;
  *)
    echo "ERROR: Unknown layout '$LAYOUT'. Use 'vnx' (default) or 'claude'." >&2
    exit 1
    ;;
esac

# Directories/files to install via simple copy (non-docs).
SHIP_PATHS=(
  "bin"
  "skills"
  "scripts"
  "templates"
  "schemas"
  "configs"
  "hooks"
  "README.md"
  "LICENSE"
  "CONTRIBUTING.md"
  "SECURITY.md"
)

# Docs subdirectories that belong in a client install.
# Everything else (archive/, roadmap/, internal/, architecture/, etc.) is excluded.
DOCS_SHIP_DIRS=(
  "core"
  "intelligence"
  "operations"
  "orchestration"
  "testing"
)

BASIC_SKILLS=(
  "planner"
  "architect"
  "backend-developer"
  "api-developer"
  "frontend-developer"
  "test-engineer"
  "reviewer"
  "debugger"
)

log() {
  printf '%s\n' "$*"
}

copy_item() {
  local src="$1"
  local dst="$2"

  if [ -d "$src" ]; then
    mkdir -p "$dst"
    if command -v rsync >/dev/null 2>&1; then
      rsync -a "$src/" "$dst/"
    else
      cp -R "$src/." "$dst/"
    fi
  else
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
  fi
}

copy_if_missing() {
  local src="$1"
  local dst="$2"
  if [ -e "$dst" ]; then
    return 0
  fi
  copy_item "$src" "$dst"
}

# Install docs selectively: only root .md files + whitelisted subdirectories.
# This prevents archive/, roadmap/, internal/, architecture/ from leaking into
# client installs (those can have 300+ files that don't belong in a target project).
install_docs() {
  local src_docs="$SRC_ROOT/docs"
  local dst_docs="$TARGET_VNX_DIR/docs"

  if [ ! -d "$src_docs" ]; then
    log "[install] Skipping missing path: docs"
    return 0
  fi

  mkdir -p "$dst_docs"

  # 1. Copy root-level docs files only (not directories).
  for f in "$src_docs"/*.md; do
    [ -f "$f" ] || continue
    cp "$f" "$dst_docs/"
  done

  # 2. Copy whitelisted subdirectories.
  local subdir
  for subdir in "${DOCS_SHIP_DIRS[@]}"; do
    if [ -d "$src_docs/$subdir" ]; then
      # If inside a git repo, only copy tracked files to avoid untracked leakage.
      if git -C "$SRC_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        local files
        files=$(git -C "$SRC_ROOT" ls-files "docs/$subdir" 2>/dev/null || true)
        if [ -n "$files" ]; then
          while IFS= read -r relpath; do
            local file_src="$SRC_ROOT/$relpath"
            local file_dst="$TARGET_VNX_DIR/$relpath"
            [ -f "$file_src" ] || continue
            mkdir -p "$(dirname "$file_dst")"
            cp "$file_src" "$file_dst"
          done <<< "$files"
        else
          # Fallback: no tracked files, copy directory as-is.
          copy_item "$src_docs/$subdir" "$dst_docs/$subdir"
        fi
      else
        # Not a git repo (e.g. downloaded tarball): copy directory as-is.
        copy_item "$src_docs/$subdir" "$dst_docs/$subdir"
      fi
    fi
  done

  log "[install] Installed: .vnx/docs ($(find "$dst_docs" -type f | wc -l | tr -d ' ') files)"
}

bootstrap_provider_skills() {
  # Install skills to project-local directories for each CLI provider.
  # We deliberately avoid writing to global home directories (~/.codex/skills,
  # ~/.gemini/skills) — the user may have their own global skills and we should
  # not pollute them. Project-local paths are sufficient for VNX dispatches.
  #
  # Discovery paths (project-scoped):
  #   Codex CLI  → .agents/skills/   (scanned from cwd up to repo root)
  #   Gemini CLI → .gemini/skills/   (workspace-scoped)
  #   Claude     → .claude/skills/   (handled by vnx bootstrap-skills)

  local skills_src=""
  if [ -d "$TARGET_VNX_DIR/skills" ]; then
    skills_src="$TARGET_VNX_DIR/skills"
  elif [ -d "$SRC_ROOT/skills" ]; then
    skills_src="$SRC_ROOT/skills"
  fi

  [ -n "$skills_src" ] || return 0

  # Codex — project-local .agents/skills/
  if command -v codex >/dev/null 2>&1; then
    local agents_skills_dir="$TARGET_PROJECT_DIR/.agents/skills"
    mkdir -p "$agents_skills_dir"
    local copied_count=0
    local skill
    for skill in "${BASIC_SKILLS[@]}"; do
      if [ -d "$skills_src/$skill" ]; then
        copy_if_missing "$skills_src/$skill" "$agents_skills_dir/$skill"
        copied_count=$((copied_count + 1))
      fi
    done
    copy_if_missing "$skills_src/README.md" "$agents_skills_dir/README.md"
    copy_if_missing "$skills_src/skills.yaml" "$agents_skills_dir/skills.yaml"
    log "[install] Codex skills (project-local): $copied_count skills in $agents_skills_dir"
  fi

  # Gemini — project-local .gemini/skills/
  if command -v gemini >/dev/null 2>&1; then
    local gemini_skills_dir="$TARGET_PROJECT_DIR/.gemini/skills"
    mkdir -p "$gemini_skills_dir"
    local copied_count=0
    local skill
    for skill in "${BASIC_SKILLS[@]}"; do
      if [ -d "$skills_src/$skill" ]; then
        copy_if_missing "$skills_src/$skill" "$gemini_skills_dir/$skill"
        copied_count=$((copied_count + 1))
      fi
    done
    copy_if_missing "$skills_src/README.md" "$gemini_skills_dir/README.md"
    copy_if_missing "$skills_src/skills.yaml" "$gemini_skills_dir/skills.yaml"
    log "[install] Gemini skills (project-local): $copied_count skills in $gemini_skills_dir"
  fi
}

# ── Main ─────────────────────────────────────────────────────────────

if [ ! -f "$SRC_ROOT/bin/vnx" ]; then
  echo "ERROR: install.sh must be run from a VNX repository root." >&2
  exit 1
fi

mkdir -p "$TARGET_VNX_DIR"

# Install non-docs paths.
for rel in "${SHIP_PATHS[@]}"; do
  src="$SRC_ROOT/$rel"
  if [ ! -e "$src" ]; then
    log "[install] Skipping missing path: $rel"
    continue
  fi

  dst="$TARGET_VNX_DIR/$rel"
  copy_item "$src" "$dst"
  log "[install] Installed: .vnx/$rel"
done

# Install docs (selective, git-aware).
install_docs

chmod +x "$TARGET_VNX_DIR/bin/vnx"
bootstrap_provider_skills

# Persist origin URL for vnx update
if git -C "$SRC_ROOT" remote get-url origin >/dev/null 2>&1; then
  git -C "$SRC_ROOT" remote get-url origin > "$TARGET_VNX_DIR/.vnx-origin"
  log "[install] Saved origin: $(cat "$TARGET_VNX_DIR/.vnx-origin")"
fi

# Persist layout choice for vnx doctor auto-detection
echo "$LAYOUT" > "$TARGET_VNX_DIR/.layout"
log "[install] Layout: $LAYOUT (saved to $TARGET_VNX_DIR/.layout)"

log "[install] Completed without root."
log "[install] Next steps:"
log ""
log "  Quick start (recommended):"
log "    $TARGET_VNX_DIR/bin/vnx setup              # One-command setup"
log "    $TARGET_VNX_DIR/bin/vnx setup --starter    # Starter mode (no tmux)"
log "    $TARGET_VNX_DIR/bin/vnx setup --operator   # Operator mode (full grid)"
log ""
log "  Manual steps (if you prefer):"
log "    $TARGET_VNX_DIR/bin/vnx init"
log "    $TARGET_VNX_DIR/bin/vnx doctor"
log "    $TARGET_VNX_DIR/bin/vnx register"
log "    $TARGET_VNX_DIR/bin/vnx install-shell-helper"
