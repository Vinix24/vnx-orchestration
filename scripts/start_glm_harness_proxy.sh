#!/usr/bin/env bash
# start_glm_harness_proxy.sh — starts the local litellm proxy the glm-harness
# dispatch lane depends on (OI-1147 pt.11).
#
# The glm-harness lane runs GLM through the full `claude` CLI harness by
# redirecting ANTHROPIC_BASE_URL at a LOCAL litellm proxy on :4141 that fronts
# OpenRouter (scripts/lib/provider_spawns/glm_harness_spawn.py). Before this
# script, that proxy existed only as a manually-started process with no
# artifact anywhere in the repo — a restart silently killed the lane, and the
# failure surfaced later as an opaque `returncode=1 (no error captured)`
# instead of a named cause.
#
# Config artifact: scripts/lib/providers/glm_harness_litellm_proxy.yaml
#   (model_list -> openrouter/z-ai/glm-5.2; api_key read from
#   os.environ/OPENROUTER_API_KEY — never hardcoded)
#
# Constraint-conformance (scripts/lib/providers/provider_constraints.yaml):
#   zai-via-openrouter-only — this proxy only ever reaches OpenRouter, never
#   the direct Zhipu API, so the harness lane stays on the allowed route.
#
# Usage:
#   bash scripts/start_glm_harness_proxy.sh          # start (idempotent)
#   bash scripts/start_glm_harness_proxy.sh status   # check + reachability probe
#   bash scripts/start_glm_harness_proxy.sh stop     # stop the proxy this script started

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_resolve_root.sh"
vnx_resolve_project_root "${BASH_SOURCE[0]}"
vnx_resolve_data_dir

PROXY_CONFIG="$VNX_PROJECT_ROOT/scripts/lib/providers/glm_harness_litellm_proxy.yaml"
PROXY_PORT="${VNX_GLM_PROXY_PORT:-4141}"
PROXY_URL="${VNX_GLM_PROXY_URL:-http://localhost:$PROXY_PORT}"
PROXY_KEY="${VNX_GLM_PROXY_KEY:-sk-glm-harness-local}"
LOG_DIR="$VNX_DATA_DIR/logs"
PID_DIR="$VNX_DATA_DIR/pids"
LOG_FILE="$LOG_DIR/glm_harness_proxy.log"
PID_FILE="$PID_DIR/glm_harness_proxy.pid"

mkdir -p "$LOG_DIR" "$PID_DIR"

# Portable TCP-listening check (bash builtin — no lsof/python dependency).
_is_listening() {
    (exec 3<>"/dev/tcp/127.0.0.1/$PROXY_PORT") 2>/dev/null
}

_reachability_probe() {
    local models_url="$PROXY_URL/v1/models"
    local http_status
    http_status="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PROXY_KEY" "$models_url" 2>/dev/null || echo "000")"
    if [ "$http_status" = "200" ]; then
        echo "[glm-harness-proxy] [reachability] $models_url OK (HTTP 200)"
        return 0
    fi
    echo "[glm-harness-proxy] WARNING: $models_url returned HTTP $http_status — proxy is listening but /v1/messages may not be healthy. Check $LOG_FILE" >&2
    return 1
}

cmd="${1:-start}"

case "$cmd" in
    status)
        if _is_listening; then
            echo "[glm-harness-proxy] listening on port $PROXY_PORT"
            _reachability_probe
            exit $?
        fi
        echo "[glm-harness-proxy] NOT listening on port $PROXY_PORT" >&2
        exit 1
        ;;
    stop)
        if [ -f "$PID_FILE" ]; then
            pid="$(cat "$PID_FILE" 2>/dev/null || echo "")"
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                kill "$pid"
                echo "[glm-harness-proxy] stopped (PID $pid)"
            else
                echo "[glm-harness-proxy] PID file present but process not running — nothing to kill"
            fi
            rm -f "$PID_FILE"
        else
            echo "[glm-harness-proxy] no PID file at $PID_FILE — nothing to stop"
        fi
        exit 0
        ;;
    start)
        ;;
    *)
        echo "Usage: $0 {start|status|stop}" >&2
        exit 2
        ;;
esac

if _is_listening; then
    echo "[glm-harness-proxy] already listening on port $PROXY_PORT — not starting a second instance"
    _reachability_probe || true
    exit 0
fi

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    echo "[glm-harness-proxy] ERROR: OPENROUTER_API_KEY is not set in the environment. The proxy config reads the key from os.environ/OPENROUTER_API_KEY ($PROXY_CONFIG) — refusing to start without it." >&2
    exit 1
fi

if ! command -v litellm >/dev/null 2>&1; then
    echo "[glm-harness-proxy] ERROR: litellm CLI not found on PATH. Install with: pip install 'litellm[proxy]'" >&2
    exit 1
fi

if [ ! -f "$PROXY_CONFIG" ]; then
    echo "[glm-harness-proxy] ERROR: proxy config not found at $PROXY_CONFIG" >&2
    exit 1
fi

echo "[glm-harness-proxy] starting: litellm --config $PROXY_CONFIG --port $PROXY_PORT (log: $LOG_FILE)"
nohup litellm --config "$PROXY_CONFIG" --port "$PROXY_PORT" >> "$LOG_FILE" 2>&1 &
proxy_pid=$!
echo "$proxy_pid" > "$PID_FILE"
disown "$proxy_pid" 2>/dev/null || true

echo "[glm-harness-proxy] waiting for readiness (PID $proxy_pid)..."
attempts=0
max_attempts=30
while [ "$attempts" -lt "$max_attempts" ]; do
    if ! kill -0 "$proxy_pid" 2>/dev/null; then
        echo "[glm-harness-proxy] ERROR: process died during startup — see $LOG_FILE" >&2
        rm -f "$PID_FILE"
        exit 1
    fi
    if _is_listening; then
        break
    fi
    attempts=$((attempts + 1))
    sleep 1
done

if ! _is_listening; then
    echo "[glm-harness-proxy] ERROR: did not become reachable on port $PROXY_PORT within ${max_attempts}s — see $LOG_FILE" >&2
    exit 1
fi

echo "[glm-harness-proxy] listening on port $PROXY_PORT (PID $proxy_pid)"
_reachability_probe
