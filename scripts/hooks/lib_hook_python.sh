#!/usr/bin/env bash
# Sourced helper: resolve the interpreter a hook core runs on (OI-2819).
# The bare `python3` can be relinked to a build without PyYAML; the permission
# policy then refuses with "yaml-unavailable". Same precedence as bin/vnx:
# VNX_PYTHON (env, executable) > <repo>/.venv/bin/python > pinned homebrew
# 3.12 > bare python3. The pinned path is VNX_HOOK_PINNED_PYTHON when set (tests,
# non-homebrew machines), default /opt/homebrew/opt/python@3.12/bin/python3.12.
# Usage: py="$(vnx_hook_python "<repo root>")"

vnx_hook_python() {
  local root="${1:-}"
  local pinned="${VNX_HOOK_PINNED_PYTHON:-/opt/homebrew/opt/python@3.12/bin/python3.12}"
  if [[ -n "${VNX_PYTHON:-}" && -x "${VNX_PYTHON}" && ! -d "${VNX_PYTHON}" ]]; then
    printf '%s\n' "${VNX_PYTHON}"
  elif [[ -n "$root" && -x "$root/.venv/bin/python" ]]; then
    printf '%s\n' "$root/.venv/bin/python"
  elif [[ -x "$pinned" && ! -d "$pinned" ]]; then
    printf '%s\n' "$pinned"
  else
    printf '%s\n' "python3"
  fi
}
