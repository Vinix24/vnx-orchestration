#!/usr/bin/env bash
# Install ripgrep (`rg`) on a CI runner and print its version.
#
# One script for every workflow step that needs rg (vnx-ci.yml x2,
# profile-a-prelude, public-ci.yml), so the install logic cannot drift.
#
# Path 1: pinned release + sha256-verified static binary (OI-1362). ripgrep
# ships on NO ubuntu runner image, and apt-get against azure.archive.ubuntu.com
# stalls intermittently and hangs the job even with the timeout+retry from
# #1608 — measured 2026-08-18/19: run 32294142912 (PR #1609) and run
# 32291338049 (main) both died in apt with exit 124. x86_64-unknown-linux-musl
# is statically linked, so it runs unchanged across runner base-image drift.
#
# Measured 2026-11-08 from 08:45Z for over an hour: the release download
# returned HTTP 504 on the runners' edge (while a Mac got 200), and
# `curl --retry 3` gave up within ~7 s, turning every VNX CI run red. Path 1
# now retries patiently: 8 retries, 10 s apart, at most ~3 minutes in total.
#
# Path 2 (only when path 1 fails): bounded apt-get, each step under `timeout`,
# so a stalled mirror cannot hang the job again. The flaky microsoft apt source
# is stripped first (NOSPLIT/hash-sum mismatch on the runner image).
set -euo pipefail

if command -v rg >/dev/null 2>&1; then
  echo "ripgrep already present on the runner image, skipping install"
  rg --version | head -1
  exit 0
fi

RG_VERSION="15.2.0"
RG_SHA256="33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c"
RG_ARCHIVE="ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl.tar.gz"
RG_URL="https://github.com/BurntSushi/ripgrep/releases/download/${RG_VERSION}/${RG_ARCHIVE}"
RG_DIR="${RUNNER_TEMP:-/tmp}/ripgrep-install"

install_from_release() {
  mkdir -p "$RG_DIR/bin"
  curl -sS -L --fail --retry 8 --retry-delay 10 --retry-all-errors \
    --max-time 60 --connect-timeout 15 -o "$RG_DIR/$RG_ARCHIVE" "$RG_URL" || return 1
  echo "${RG_SHA256}  ${RG_DIR}/${RG_ARCHIVE}" | sha256sum -c - || return 2
  tar -xzf "$RG_DIR/$RG_ARCHIVE" -C "$RG_DIR" || return 3
  install -m 755 "$RG_DIR/ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl/rg" "$RG_DIR/bin/rg" || return 4
}

install_from_apt() {
  sudo rm -f /etc/apt/sources.list.d/microsoft*.list /etc/apt/sources.list.d/*microsoft*.sources 2>/dev/null || true
  timeout 120 sudo apt-get update || return 1
  timeout 240 sudo apt-get install -y ripgrep || return 2
}

release_rc=0
install_from_release || release_rc=$?
if [ "$release_rc" -eq 0 ]; then
  echo "ripgrep installed from the pinned release ${RG_VERSION}"
  if [ -n "${GITHUB_PATH:-}" ]; then
    echo "$RG_DIR/bin" >> "$GITHUB_PATH"
  fi
  "$RG_DIR/bin/rg" --version | head -1
  exit 0
fi
echo "pinned release failed (step code ${release_rc}); falling back to apt" >&2

apt_rc=0
install_from_apt || apt_rc=$?
if [ "$apt_rc" -eq 0 ]; then
  echo "ripgrep installed from apt"
  rg --version | head -1
  exit 0
fi

echo "ripgrep install failed: release path step code ${release_rc} (${RG_URL}), apt path step code ${apt_rc}" >&2
exit 1
