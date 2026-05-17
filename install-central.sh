#!/usr/bin/env bash
set -euo pipefail

# install-central.sh — Centralized VNX install to ~/.vnx-system/versions/<version>/
# Separate from install.sh (embedded/open-source per-project installer).
#
# Layout created:
#   ~/.vnx-system/
#     versions/v1.0.0-rc2/     (immutable, content-addressed install)
#     current -> versions/...   (symlink, atomic switch)
#     bin/vnx                   (shim that reads .vnx-version from project root)

VERSION="v1.0.0-rc2"
TARGET_DIR="${HOME}/.vnx-system"
SOURCE_URL="https://github.com/Vinix24/vnx-orchestration"
DRY_RUN=false

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --target)
      TARGET_DIR="$2"; shift 2 ;;
    --target=*)
      TARGET_DIR="${1#*=}"; shift ;;
    --version)
      VERSION="$2"; shift 2 ;;
    --version=*)
      VERSION="${1#*=}"; shift ;;
    --source)
      SOURCE_URL="$2"; shift 2 ;;
    --source=*)
      SOURCE_URL="${1#*=}"; shift ;;
    --dry-run)
      DRY_RUN=true; shift ;;
    -h|--help)
      cat <<HELP
Usage: install-central.sh [OPTIONS]

Options:
  --target <dir>    Install root (default: ~/.vnx-system)
  --version <ver>   Version to install (default: v1.0.0-rc2)
  --source <url>    Git source URL (default: github.com/Vinix24/vnx-orchestration)
  --dry-run         Print steps without touching filesystem
  -h, --help        Show this help

Examples:
  bash install-central.sh
  bash install-central.sh --version v1.0.0-rc2 --dry-run
  bash install-central.sh --target /opt/vnx-system --version v1.0.0-rc2
HELP
      exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo "[install-central] $*"; }
success() { echo "[install-central] [ok] $*"; }
warn()    { echo "[install-central] [!] $*" >&2; }
die()     { echo "[install-central] [x] $*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = "true" ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# check_prereqs — fail-fast on missing dependencies
# ---------------------------------------------------------------------------
check_prereqs() {
  info "Checking prerequisites..."

  for cmd in git sqlite3 python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      die "Required command not found: $cmd"
    fi
  done

  # Python version check: >= 3.11 and < 3.14
  local py_ver
  py_ver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  local py_major py_minor
  py_major=$(echo "$py_ver" | cut -d. -f1)
  py_minor=$(echo "$py_ver" | cut -d. -f2)

  if [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 11 ]; }; then
    die "Python >= 3.11 required, found ${py_ver}"
  fi
  if [ "$py_major" -eq 3 ] && [ "$py_minor" -ge 14 ]; then
    die "Python < 3.14 required, found ${py_ver}"
  fi

  # Disk space: require >= 500 MB free in target parent
  local target_parent
  target_parent=$(dirname "$TARGET_DIR")
  if [ -d "$target_parent" ]; then
    local free_kb
    free_kb=$(df -k "$target_parent" 2>/dev/null | awk 'NR==2{print $4}')
    if [ -n "$free_kb" ] && [ "$free_kb" -lt 512000 ]; then
      die "Insufficient disk space: ${free_kb}KB free in ${target_parent}, need >= 500MB"
    fi
  fi

  success "Prerequisites OK (python ${py_ver}, git, sqlite3)"
}

# ---------------------------------------------------------------------------
# clone_version — idempotent git clone to versions/<version>/
# ---------------------------------------------------------------------------
clone_version() {
  local version_dir="${TARGET_DIR}/versions/${VERSION}"

  if [ -d "$version_dir" ]; then
    info "Version ${VERSION} already installed at ${version_dir} — skipping clone"
    return 0
  fi

  info "Cloning ${SOURCE_URL} @ ${VERSION} -> ${version_dir}..."
  run mkdir -p "$(dirname "$version_dir")"

  local clone_url="$SOURCE_URL"
  # Normalize: add https:// if not present
  if [[ "$clone_url" != https://* ]] && [[ "$clone_url" != git@* ]] && [[ "$clone_url" != http://* ]]; then
    clone_url="https://${clone_url}"
  fi

  if [ "$DRY_RUN" = "false" ]; then
    local tmp_dir="${version_dir}.tmp"
    git clone --depth 1 --branch "$VERSION" "$clone_url" "$tmp_dir" \
      || die "git clone failed for ${clone_url} @ ${VERSION}"
    mv "$tmp_dir" "$version_dir"
  else
    echo "  [dry-run] git clone --depth 1 --branch ${VERSION} ${clone_url} ${version_dir}"
  fi

  success "Cloned ${VERSION} to ${version_dir}"
}

# ---------------------------------------------------------------------------
# swap_symlink — atomic current -> versions/<version> switch
# ---------------------------------------------------------------------------
swap_symlink() {
  local version_dir="${TARGET_DIR}/versions/${VERSION}"
  local current_link="${TARGET_DIR}/current"

  info "Swapping symlink: ${current_link} -> ${version_dir}..."
  run mkdir -p "${TARGET_DIR}/versions"

  if [ "$DRY_RUN" = "false" ]; then
    local tmp_link="${TARGET_DIR}/current.tmp.$$"
    ln -s "$version_dir" "$tmp_link"
    # mv -T is Linux-specific; use portable approach for macOS compatibility
    if mv -T "$tmp_link" "$current_link" 2>/dev/null; then
      : # Linux mv -T succeeded
    else
      # macOS: ln -sfn is atomic enough for our needs
      rm -f "$tmp_link"
      ln -sfn "$version_dir" "$current_link"
    fi
  else
    echo "  [dry-run] ln -sfn ${version_dir} ${current_link}"
  fi

  success "Symlink updated: ${current_link} -> ${version_dir}"
}

# ---------------------------------------------------------------------------
# install_shim — ~/.vnx-system/bin/vnx project-pin shim
# ---------------------------------------------------------------------------
install_shim() {
  local shim_dir="${TARGET_DIR}/bin"
  local shim_path="${shim_dir}/vnx"

  info "Installing shim at ${shim_path}..."
  run mkdir -p "$shim_dir"

  local shim_content
  shim_content=$(cat <<'SHIM'
#!/usr/bin/env bash
# VNX project-pin shim — reads .vnx-version from project root (cwd traversal)
set -euo pipefail

VNX_SYSTEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Traverse up from cwd to find .vnx-version
find_version_pin() {
  local dir="$PWD"
  while [ "$dir" != "/" ]; do
    if [ -f "${dir}/.vnx-version" ]; then
      cat "${dir}/.vnx-version"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  echo ""
}

pin="$(find_version_pin)"

if [ -n "$pin" ]; then
  version_dir="${VNX_SYSTEM_DIR}/versions/${pin}"
  if [ ! -d "$version_dir" ]; then
    echo "[vnx-shim] [x] Pinned version ${pin} not installed at ${version_dir}" >&2
    echo "[vnx-shim] Run: bash ${VNX_SYSTEM_DIR}/../install-central.sh --version ${pin}" >&2
    exit 1
  fi
  export VNX_HOME="$version_dir"
else
  if [ ! -e "${VNX_SYSTEM_DIR}/current" ]; then
    echo "[vnx-shim] [x] No .vnx-version pin found and no current install at ${VNX_SYSTEM_DIR}/current" >&2
    exit 1
  fi
  export VNX_HOME="${VNX_SYSTEM_DIR}/current"
fi

exec "${VNX_HOME}/bin/vnx-cli" "$@"
SHIM
)

  if [ "$DRY_RUN" = "false" ]; then
    printf '%s\n' "$shim_content" > "$shim_path"
    chmod +x "$shim_path"
  else
    echo "  [dry-run] write shim to ${shim_path} (chmod +x)"
  fi

  success "Shim installed: ${shim_path}"
}

# ---------------------------------------------------------------------------
# verify_install — post-install sanity checks
# ---------------------------------------------------------------------------
verify_install() {
  local version_dir="${TARGET_DIR}/versions/${VERSION}"
  local current_link="${TARGET_DIR}/current"
  local shim_path="${TARGET_DIR}/bin/vnx"

  info "Verifying install..."

  if [ "$DRY_RUN" = "false" ]; then
    [ -d "$version_dir" ]  || die "version_dir missing: ${version_dir}"
    [ -L "$current_link" ] || die "current symlink missing: ${current_link}"
    [ -x "$shim_path" ]    || die "shim not executable: ${shim_path}"

    # Schema bootstrap check (idempotent)
    local db_init="${version_dir}/scripts/quality_db_init.py"
    if [ -f "$db_init" ]; then
      python3 "$db_init" --check-only 2>/dev/null \
        && success "Schema bootstrap check passed" \
        || warn "Schema bootstrap check returned non-zero — may need manual init"
    else
      info "quality_db_init.py not found at ${db_init} — skipping schema check"
    fi
  else
    echo "  [dry-run] verify: version_dir, current symlink, shim executable, schema bootstrap"
  fi

  success "Verification complete"
}

# ---------------------------------------------------------------------------
# Rollback helper — called on ERR when mid-flow symlink was swapped
# ---------------------------------------------------------------------------
_SYMLINK_SWAPPED=false
rollback_on_error() {
  if [ "$_SYMLINK_SWAPPED" = "true" ] && [ "$DRY_RUN" = "false" ]; then
    warn "Rolling back symlink due to installation error..."
    rm -f "${TARGET_DIR}/current"
  fi
}
trap rollback_on_error ERR

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  echo ""
  info "VNX Central Install"
  info "  version : ${VERSION}"
  info "  target  : ${TARGET_DIR}"
  info "  source  : ${SOURCE_URL}"
  if [ "$DRY_RUN" = "true" ]; then
    info "  mode    : DRY RUN (no filesystem changes)"
  fi
  echo ""

  check_prereqs
  clone_version
  swap_symlink
  _SYMLINK_SWAPPED=true
  install_shim
  verify_install

  echo ""
  success "VNX ${VERSION} installed successfully"
  echo ""
  echo "  Install root : ${TARGET_DIR}"
  echo "  Current      : ${TARGET_DIR}/current -> versions/${VERSION}"
  echo "  Shim         : ${TARGET_DIR}/bin/vnx"
  echo ""
  echo "Next steps:"
  echo "  1. Add ${TARGET_DIR}/bin to your PATH:"
  echo "       export PATH=\"\${PATH}:${TARGET_DIR}/bin\""
  echo "  2. Pin a project to this version:"
  echo "       echo '${VERSION}' > /path/to/project/.vnx-version"
  echo "  3. Run: vnx setup"
  echo ""
}

main "$@"
