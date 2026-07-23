#!/usr/bin/env bash
set -eEuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# install-central.sh — Centralized VNX install to ~/.vnx-system/versions/<version>/
# Separate from install.sh (embedded/open-source per-project installer).
#
# Layout created:
#   ~/.vnx-system/
#     versions/v1.0.0/     (immutable, content-addressed install)
#     current -> versions/...   (symlink, atomic switch)
#     bin/vnx                   (shim that reads .vnx-version from project root)

VERSION="v1.0.0"
TARGET_DIR="${HOME}/.vnx-system"
SOURCE_URL="https://github.com/Vinix24/vnx-orchestration"
DRY_RUN=false
MATERIALIZE_ONLY=false

# Defined early so arg parsing can call it before helpers are defined.
validate_version() {
  local v="$1"
  if [ -z "$v" ]; then
    echo "[install-central] [x] version must not be empty" >&2; exit 78
  fi
  if ! [[ "$v" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "[install-central] [x] invalid version '${v}' (must match [A-Za-z0-9._-]+)" >&2
    exit 78  # EX_CONFIG
  fi
}

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
    --materialize-only)
      # Produce versions/<version>/ only: skip the current-symlink swap, shim
      # install, and post-install verification. Used by `vnx release publish`,
      # which owns the (optional, --set-current-gated) cutover itself.
      MATERIALIZE_ONLY=true; shift ;;
    -h|--help)
      cat <<HELP
Usage: install-central.sh [OPTIONS]

Options:
  --target <dir>    Install root (default: ~/.vnx-system)
  --version <ver>   Version to install (default: v1.0.0)
  --source <url>    Git source URL (default: github.com/Vinix24/vnx-orchestration)
  --dry-run         Print steps without touching filesystem
  --materialize-only  Only materialize versions/<version>/ (no current flip, no shim)
  -h, --help        Show this help

Examples:
  bash install-central.sh
  bash install-central.sh --version v1.0.0 --dry-run
  bash install-central.sh --target /opt/vnx-system --version v1.0.0
HELP
      exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

validate_version "$VERSION"

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

# Marker written into each central-install version dir. scripts/lib/vnx_paths.{sh,py}
# and the bin/vnx inline fallback read it to distinguish a central install
# (PROJECT_ROOT = the operator's project) from a standalone vnx-orchestration dev
# checkout (PROJECT_ROOT = VNX_HOME). Without it the resolver treats a central
# install as a dev checkout and collapses runtime state into the immutable code tree.
INSTALL_MODE_MARKER=".vnx-install-mode"
INSTALL_MODE_VALUE="central"

write_install_marker() {
  local version_dir="$1"
  local marker="${version_dir}/${INSTALL_MODE_MARKER}"
  if [ "$DRY_RUN" = "true" ]; then
    echo "  [dry-run] write ${INSTALL_MODE_MARKER} (${INSTALL_MODE_VALUE}) -> ${marker}"
    return 0
  fi
  # Atomic write: never leave a half-written marker the resolver could misread.
  local tmp="${marker}.tmp.$$"
  printf '%s\n' "$INSTALL_MODE_VALUE" > "$tmp"
  mv -f "$tmp" "$marker"
}

# strip_tenant_marker — the installed engine tree must be TENANT-NEUTRAL. The repo
# tracks its own `.vnx-project-id = vnx-dev`, which the clone drags into the shared
# version dir. In central-install mode the door's CWD is this tree; a stray marker
# there makes CWD-based project_id resolution return `vnx-dev` for EVERY consumer
# (the fleet-wide misroute/hard-reject class). Remove it after clone.
strip_tenant_marker() {
  local version_dir="$1"
  local marker="${version_dir}/.vnx-project-id"
  if [ "$DRY_RUN" = "true" ]; then
    echo "  [dry-run] strip stray tenant marker -> ${marker}"
    return 0
  fi
  [ -f "$marker" ] && rm -f "$marker" && info "Stripped stray tenant marker: ${marker}"
  return 0
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
    # Idempotent: ensure the marker exists even on a pre-existing version dir
    # (e.g. cloned before this installer learned to write it).
    write_install_marker "$version_dir"
    strip_tenant_marker "$version_dir"
    return 0
  fi

  info "Cloning ${SOURCE_URL} @ ${VERSION} -> ${version_dir}..."
  run mkdir -p "$(dirname "$version_dir")"

  local clone_url="$SOURCE_URL"
  # Normalize: add https:// if not present. Local sources (existing path or
  # file:// URL) are used as-is — `vnx release publish` passes a temp checkout
  # dir as --source, and prepending https:// would break the clone.
  if [ -e "$clone_url" ] || [[ "$clone_url" == file://* ]]; then
    : # local source — no URL normalization
  elif [[ "$clone_url" != https://* ]] && [[ "$clone_url" != git@* ]] && [[ "$clone_url" != http://* ]]; then
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

  write_install_marker "$version_dir"
  strip_tenant_marker "$version_dir"
  success "Cloned ${VERSION} to ${version_dir}"
}

# ---------------------------------------------------------------------------
# swap_symlink current_link target — atomic symlink replacement
#   Never removes $current_link before successful replacement.
#   Both Linux (GNU mv -T) and macOS (BSD mv -f via rename(2)) paths are safe
#   because $current_link is always a symlink or nonexistent, never a real dir.
#   Returns 75 (EX_TEMPFAIL) on failure; caller handles rollback.
# ---------------------------------------------------------------------------
swap_symlink() {
  local current_link="$1"
  local target="$2"
  local tmp_link="${current_link}.swap.$$"

  info "Swapping symlink: ${current_link} -> ${target}..."

  if [ "$DRY_RUN" = "true" ]; then
    echo "  [dry-run] ln -sfn ${target} ${current_link}"
    success "Symlink updated: ${current_link} -> ${target}"
    return 0
  fi

  mkdir -p "$(dirname "$current_link")"

  ln -sn "$target" "$tmp_link" || {
    rm -f "$tmp_link"
    echo "ERROR: failed to create temp symlink" >&2
    return 75
  }

  # Try GNU mv -T first (Linux). macOS BSD mv without -T follows symlinks to
  # directories, so mv -f is not safe there. Python os.replace() maps directly
  # to rename(2) on all POSIX platforms — atomic, no directory-following.
  if mv -fT "$tmp_link" "$current_link" 2>/dev/null; then
    : # GNU mv -T succeeded (Linux)
  elif python3 -c "import os,sys; os.replace(sys.argv[1],sys.argv[2])" \
         "$tmp_link" "$current_link" 2>/dev/null; then
    : # python os.replace() => rename(2), atomic on macOS
  else
    rm -f "$tmp_link"
    echo "ERROR: failed to rename temp symlink" >&2
    return 75
  fi

  success "Symlink updated: ${current_link} -> ${target}"
  return 0
}

# ---------------------------------------------------------------------------
# install_shim — ~/.vnx-system/bin/vnx project-pin shim
# ---------------------------------------------------------------------------
install_shim() {
  local shim_dir="${TARGET_DIR}/bin"
  local shim_path="${shim_dir}/vnx"
  local tpl_path="${SCRIPT_DIR}/scripts/templates/vnx_shim.sh.tpl"

  info "Installing shim at ${shim_path}..."
  run mkdir -p "$shim_dir"

  [ -f "$tpl_path" ] || die "Shim template not found: ${tpl_path}"

  if [ "$DRY_RUN" = "false" ]; then
    local shim_tmp
    shim_tmp=$(mktemp "${shim_path}.tmp.XXXXXX")
    cat "$tpl_path" > "$shim_tmp"
    chmod +x "$shim_tmp"
    mv -f "$shim_tmp" "$shim_path"
  else
    echo "  [dry-run] write shim to ${shim_path} (chmod +x) from $(basename "${tpl_path}")"
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

    # Install-mode marker must exist and read "central". The path resolver keys
    # on this marker to keep PROJECT_ROOT (and all runtime state) out of the
    # immutable code tree, so a missing/invalid marker is a hard install failure.
    local marker="${version_dir}/${INSTALL_MODE_MARKER}"
    [ -f "$marker" ] || die "install-mode marker missing: ${marker}"
    local marker_value
    marker_value="$(tr -d '[:space:]' < "$marker" 2>/dev/null || true)"
    [ "$marker_value" = "$INSTALL_MODE_VALUE" ] \
      || die "install-mode marker invalid: expected '${INSTALL_MODE_VALUE}', got '${marker_value}'"
    success "Install-mode marker present (${INSTALL_MODE_VALUE})"

    # Schema validation against a throwaway temp dir. quality_db_init.py always
    # writes to its resolved VNX_STATE_DIR, so pin VNX_HOME + VNX_DATA_DIR/
    # VNX_STATE_DIR at a temp location — the check then never writes runtime
    # state into the version dir (or any real project that happens to be CWD).
    local db_init="${version_dir}/scripts/quality_db_init.py"
    if [ -f "$db_init" ]; then
      local tmp_db
      tmp_db="$(mktemp -d)"
      if VNX_HOME="$version_dir" VNX_DATA_DIR="$tmp_db" VNX_STATE_DIR="$tmp_db/state" \
           python3 "$db_init" >/dev/null 2>&1; then
        success "Schema validation passed (temp dir)"
      else
        warn "Schema validation returned non-zero — may need manual 'vnx init-db'"
      fi
      rm -rf "$tmp_db"
    else
      local schema_file="${version_dir}/schemas/quality_intelligence.sql"
      if [ -f "$schema_file" ]; then
        success "Schema file present (${schema_file})"
      else
        warn "Schema file not found at ${schema_file}"
      fi
    fi
  else
    echo "  [dry-run] verify: version_dir, current symlink, shim executable, install-mode marker, schema (temp dir)"
  fi

  success "Verification complete"
}

# ---------------------------------------------------------------------------
# Rollback helper — called on ERR; restores previous symlink or exits 70
# ---------------------------------------------------------------------------
_PREVIOUS_TARGET=""

cleanup_on_failure() {
  if [ -n "${_PREVIOUS_TARGET:-}" ]; then
    if ! swap_symlink "${TARGET_DIR}/current" "$_PREVIOUS_TARGET"; then
      echo "FATAL: rollback failed — manual recovery required: previous=$_PREVIOUS_TARGET" >&2
      exit 70  # EX_SOFTWARE
    fi
    echo "ROLLBACK: restored previous symlink target $_PREVIOUS_TARGET"
  fi
}
trap cleanup_on_failure ERR

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

  if [ "$MATERIALIZE_ONLY" = "true" ]; then
    success "Materialized ${VERSION} at ${TARGET_DIR}/versions/${VERSION} (no current flip, no shim)"
    return 0
  fi

  if [ -L "${TARGET_DIR}/current" ]; then
    _PREVIOUS_TARGET=$(readlink "${TARGET_DIR}/current")
  fi
  swap_symlink "${TARGET_DIR}/current" "${TARGET_DIR}/versions/${VERSION}"
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
