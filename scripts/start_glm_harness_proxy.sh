#!/bin/bash
# start_glm_harness_proxy.sh — Start/stop/status the glm-harness local litellm proxy.
#
# OI-1147 punt 11: the glm-harness lane (provider `glm-harness`,
# scripts/lib/provider_spawns/glm_harness_spawn.py) talks to a local litellm
# proxy on :4141 that fronts OpenRouter GLM. Before this script the proxy was
# only ever started by hand against a config copied outside the repo — no
# startup artifact existed, so a restart silently killed the lane. This
# script IS that artifact: it starts the proxy from the repo-tracked config
# (scripts/lib/providers/glm_harness_litellm_proxy.yaml), the same file that
# is the SSOT for the model list and constraint-conformance notes.
#
# No credentials live in this script or in the repo config: the OpenRouter
# key is read from OPENROUTER_API_KEY at proxy-process start time (the config
# file references it as `os.environ/OPENROUTER_API_KEY`, litellm resolves
# it). The proxy's own local-only bearer (VNX_GLM_PROXY_KEY, default
# sk-glm-harness-local) is a shared secret between this machine's claude CLI
# and this machine's proxy — not an external credential — and already ships
# as a default in glm_harness_spawn.py and in the tracked config.
#
# Usage:
#   bash scripts/start_glm_harness_proxy.sh [start]   # start (no-op if already running)
#   bash scripts/start_glm_harness_proxy.sh status    # reachability check, exits non-zero if down
#   bash scripts/start_glm_harness_proxy.sh stop      # graceful stop

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
source "$SCRIPT_DIR/lib/process_lifecycle.sh"

PROXY_CONFIG="${VNX_GLM_PROXY_CONFIG:-$SCRIPT_DIR/lib/providers/glm_harness_litellm_proxy.yaml}"
PROXY_PORT="${VNX_GLM_PROXY_PORT:-4141}"
PROXY_HOST="${VNX_GLM_PROXY_HOST:-127.0.0.1}"
PROXY_URL="${VNX_GLM_PROXY_URL:-http://localhost:${PROXY_PORT}}"
PROXY_KEY="${VNX_GLM_PROXY_KEY:-sk-glm-harness-local}"

NAME="glm_harness_proxy"
PID_FILE="$VNX_PIDS_DIR/${NAME}.pid"
LOG_FILE="$VNX_LOGS_DIR/${NAME}.log"
FINGERPRINT="litellm --config ${PROXY_CONFIG} --port ${PROXY_PORT}"

mkdir -p "$VNX_PIDS_DIR" "$VNX_LOGS_DIR"

_check_url() {
  # HTTP-level probe mirroring dispatch_cli._probe_litellm_style_proxy (OI-1147
  # punt 11) — a bare TCP connect is not enough (spawn_glm_harness's own
  # _proxy_reachable() only checks the socket and misses an up-but-misconfigured
  # proxy). Prints "OK" + model count, or a labeled failure, to stdout.
  local models_url="${PROXY_URL%/}/v1/models"
  local probe_file http_code resp_body=""
  probe_file="$(mktemp "${TMPDIR:-/tmp}/glm_proxy_probe.XXXXXX")"
  http_code="$(curl -s -m 5 -o "$probe_file" -w '%{http_code}' \
    -H "Authorization: Bearer ${PROXY_KEY}" "$models_url" 2>/dev/null)" || true
  resp_body="$(cat "$probe_file" 2>/dev/null || true)"
  rm -f "$probe_file"
  if [ "$http_code" = "200" ]; then
    local model_count
    model_count="$(printf '%s' "$resp_body" | python3 -c 'import json,sys
try:
    print(len(json.load(sys.stdin).get("data", [])))
except Exception:
    print("?")' 2>/dev/null || echo "?")"
    echo "OK: ${model_count} models at ${models_url}"
    return 0
  fi
  if [ "$http_code" = "401" ] || [ "$http_code" = "403" ]; then
    echo "AUTH REJECTED (HTTP ${http_code}) at ${models_url} — VNX_GLM_PROXY_KEY does not match the proxy's general_settings.master_key"
    return 2
  fi
  if [ -n "$http_code" ] && [ "$http_code" != "000" ]; then
    echo "HTTP ${http_code} from ${models_url}"
    return 2
  fi
  echo "unreachable at ${models_url} (connection failed) — the proxy is not running"
  return 1
}

cmd_status() {
  local result rc
  result="$(_check_url)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "[glm_harness_proxy] [reachability] ${result}"
    return 0
  fi
  echo "[glm_harness_proxy] [ERROR] proxy NOT reachable — ${result}" >&2
  echo "[glm_harness_proxy] [ERROR] the glm-harness lane WILL fail before spending a single token if fired now — run 'bash scripts/start_glm_harness_proxy.sh start' first" >&2
  return 1
}

cmd_stop() {
  if vnx_proc_stop_pidfile "$NAME" "$PID_FILE" "$LOG_FILE" "operator_stop" 5; then
    echo "[glm_harness_proxy] stopped"
    return 0
  fi
  echo "[glm_harness_proxy] not running"
  return 0
}

cmd_start() {
  if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    echo "[glm_harness_proxy] [ERROR] OPENROUTER_API_KEY is not set in the environment." >&2
    echo "[glm_harness_proxy] [ERROR] the proxy config (${PROXY_CONFIG}) reads it as os.environ/OPENROUTER_API_KEY — refusing to start a proxy that will 401 on every request." >&2
    return 1
  fi

  if ! command -v litellm >/dev/null 2>&1; then
    echo "[glm_harness_proxy] [ERROR] litellm CLI not found on PATH. Install with: pip install 'litellm[proxy]'" >&2
    return 1
  fi

  if [ ! -f "$PROXY_CONFIG" ]; then
    echo "[glm_harness_proxy] [ERROR] proxy config not found at ${PROXY_CONFIG}" >&2
    return 1
  fi

  # Singleton: an already-running, matching process is a no-op success.
  local info pid
  info="$(vnx_proc_read_pidfile "$PID_FILE")"
  pid="${info%%|*}"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && vnx_proc_matches_fingerprint "$pid" "$FINGERPRINT"; then
    echo "[glm_harness_proxy] already running (PID ${pid})"
    cmd_status
    return $?
  fi

  echo "[glm_harness_proxy] starting: ${FINGERPRINT} (log: ${LOG_FILE})"
  nohup litellm --config "$PROXY_CONFIG" --port "$PROXY_PORT" --host "$PROXY_HOST" \
    >>"$LOG_FILE" 2>&1 &
  local new_pid=$!
  disown "$new_pid" 2>/dev/null || true
  vnx_proc_write_pidfile "$PID_FILE" "$new_pid" "$FINGERPRINT"

  # Poll for readiness instead of a fixed sleep — litellm's uvicorn startup
  # time varies. Bounded at 30s; a proxy that hasn't answered by then is
  # reported as a loud failure, never a silent background hang.
  local elapsed=0
  while [ "$elapsed" -lt 30 ]; do
    if ! kill -0 "$new_pid" 2>/dev/null; then
      echo "[glm_harness_proxy] [ERROR] proxy process (PID ${new_pid}) died during startup — see ${LOG_FILE}" >&2
      rm -f "$PID_FILE" "${PID_FILE}.fingerprint"
      return 1
    fi
    if _check_url >/dev/null 2>&1; then
      echo "[glm_harness_proxy] started (PID ${new_pid})"
      cmd_status
      return $?
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  echo "[glm_harness_proxy] [ERROR] proxy did not become reachable within 30s — see ${LOG_FILE}" >&2
  return 1
}

case "${1:-start}" in
  start) cmd_start ;;
  status) cmd_status ;;
  stop) cmd_stop ;;
  *)
    echo "usage: $0 {start|status|stop}" >&2
    exit 2
    ;;
esac
