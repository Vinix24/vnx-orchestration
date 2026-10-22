#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export VNX_HOME="$ROOT_DIR/.claude/vnx-system"
export VNX_DATA_DIR="$(mktemp -d /tmp/vnx-d3-test.XXXXXX)"

# shellcheck source=scripts/lib/ops_process_control.sh
source "$VNX_HOME/scripts/lib/ops_process_control.sh"

LOG_FILE="$VNX_DATA_DIR/process_control_safety_sweep.log"
mkdir -p "$VNX_PIDS_DIR" "$VNX_LOCKS_DIR" "$VNX_LOGS_DIR"

cleanup() {
  pkill -P $$ 2>/dev/null || true
  rm -rf "$VNX_DATA_DIR"
}
trap cleanup EXIT

echo "== process control safety sweep tests =="

TARGET_FILES=(
  "$ROOT_DIR/VNX_HYBRID_FINAL.sh"
  "$ROOT_DIR/vnx-dashboard"
  "$ROOT_DIR/scripts/run_e2e_supabase.sh"
)

echo "[1] no unsafe kill patterns in targeted operational scripts"
if rg -n 'pkill[[:space:]]+-f|kill[[:space:]]+-9|xargs[[:space:]].*kill[[:space:]]+-9' "${TARGET_FILES[@]}"; then
  echo "unsafe kill pattern detected"
  exit 1
fi

echo "[2] lifecycle helper wiring present in targeted scripts"
for file in "${TARGET_FILES[@]}"; do
  if ! grep -q "ops_process_control.sh" "$file"; then
    echo "missing ops_process_control helper wiring in $file"
    exit 1
  fi
done

echo "[3] fingerprint mismatch never terminates process"
sleep 30 &
mismatch_pid=$!
if vnx_stop_pid_if_matches "d3_mismatch" "$mismatch_pid" "$LOG_FILE" "mismatch_check" 1 "/tmp/definitely_not_matching"; then
  echo "expected mismatch stop to fail"
  exit 1
fi
if ! kill -0 "$mismatch_pid" 2>/dev/null; then
  echo "mismatch target unexpectedly terminated"
  exit 1
fi
kill -KILL "$mismatch_pid" 2>/dev/null || true

echo "[4] graceful stop path is used before forced kill"
graceful_script="$VNX_DATA_DIR/graceful_loop.sh"
cat > "$graceful_script" <<'EOF'
#!/bin/bash
while true; do sleep 1; done
EOF
chmod +x "$graceful_script"
"$graceful_script" &
graceful_pid=$!
sleep 0.5
vnx_stop_by_fingerprints "d3_graceful" "$LOG_FILE" "graceful_stop" 2 "$graceful_script"
sleep 0.5
if kill -0 "$graceful_pid" 2>/dev/null && vnx_proc_matches_fingerprint "$graceful_pid" "$graceful_script"; then
  echo "graceful process still running"
  exit 1
fi
if ! grep -q "process=d3_graceful event=stop pid=$graceful_pid reason=graceful_stop" "$LOG_FILE"; then
  echo "missing graceful stop lifecycle event"
  exit 1
fi
if grep -q "process=d3_graceful event=forced_kill pid=$graceful_pid" "$LOG_FILE"; then
  echo "graceful process was force-killed unexpectedly"
  exit 1
fi

echo "[5] forced kill fallback occurs after timeout"
stubborn_script="$VNX_DATA_DIR/stubborn_loop.py"
cat > "$stubborn_script" <<'EOF'
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)

while True:
    time.sleep(1)
EOF
python3 "$stubborn_script" &
stubborn_pid=$!
sleep 0.5
vnx_stop_by_fingerprints "d3_stubborn" "$LOG_FILE" "stubborn_stop" 1 "$stubborn_script"
sleep 0.5
if kill -0 "$stubborn_pid" 2>/dev/null && vnx_proc_matches_fingerprint "$stubborn_pid" "$stubborn_script"; then
  echo "stubborn process still running"
  exit 1
fi
if ! grep -q "process=d3_stubborn event=forced_kill pid=$stubborn_pid reason=stubborn_stop" "$LOG_FILE"; then
  echo "missing forced_kill lifecycle event for stubborn process"
  exit 1
fi

echo "[6] port listener stop validates fingerprint before termination"
PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
python3 -m http.server "$PORT" >/dev/null 2>&1 &
server_pid=$!
sleep 1
vnx_stop_listening_port_processes "d3_port" "$PORT" "$LOG_FILE" "port_restart" 2 "http.server $PORT"
sleep 0.5
if kill -0 "$server_pid" 2>/dev/null && vnx_proc_matches_fingerprint "$server_pid" "http.server $PORT"; then
  echo "port listener still running"
  exit 1
fi
if ! grep -q "process=d3_port event=stop pid=$server_pid reason=port_restart" "$LOG_FILE"; then
  echo "missing stop lifecycle event for port listener"
  exit 1
fi

echo "OK"
