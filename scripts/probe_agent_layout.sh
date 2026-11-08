#!/usr/bin/env bash
# probe_agent_layout.sh - cells A, C, D, H1, H2, L and B of agents-als-map-fase-1 (points 3-5).
# Only the T0 runs the CLI cells (a builder cannot start `claude -p`, see
# scripts/hooks/pretooluse_block_raw_claude_spawn.sh): own account (~/.claude), cwd in a probe tree
# under $TMPDIR, throwaway store, sentinels only there. A transcript is read only as paths, types,
# lengths and hashes (scripts/lib/context_manifest.py). Cell L is static and runs in --self-check.
# One JSON line per cell; a CLI cell that cannot start claude is `niet_gemeten`, never `ja`.
# Usage: scripts/probe_agent_layout.sh [--cell A|C|D|H1|H2|L|B|all] [--out <file>] [--self-check]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CELLS="A C D H1 H2 L B"; CELL="all"; OUT=""; SELF_CHECK=0
CLAUDE_BIN="${PROBE_CLAUDE_BIN:-claude}"; TIMEOUT_SECONDS="${PROBE_TIMEOUT_SECONDS:-120}"
PROBE_DIR=""; VERSION=""; CELL_ENFORCE=0; CELL_DISPATCH_PATHS=""
die() { echo "probe_agent_layout: $*" >&2; exit 2; }
parse_args() {
  while [ "$#" -gt 0 ]; do case "$1" in
      --cell) [ "$#" -ge 2 ] || die "--cell needs a value"; CELL="$2"; shift ;;
      --out) [ "$#" -ge 2 ] || die "--out needs a path"; OUT="$2"; shift ;;
      --self-check) SELF_CHECK=1 ;;
      *) die "unknown argument: $1" ;;
    esac; shift; done
}
sha256_of() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }
json_quote() { python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.argv[1]))' "$1"; }
point_of() { case "$1" in A|C|D) printf 3 ;; H1|H2|L) printf 4 ;; B) printf 5 ;; esac; }
decisive_of() { if [ "$1" = C ]; then printf false; else printf true; fi; }
read_version() { "$CLAUDE_BIN" --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true; }
have_claude() { command -v "$CLAUDE_BIN" >/dev/null 2>&1; }
new_session() { python3 -c 'import uuid; print(uuid.uuid4())'; }
ensure_probe() {
  if [ -z "$PROBE_DIR" ]; then PROBE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/probe-agent-layout.XXXXXX")"
    trap cleanup EXIT; mkdir -p "$PROBE_DIR/raw" "$PROBE_DIR/data"; fi
}
cleanup() { case "$PROBE_DIR" in "${TMPDIR:-/tmp}"/*|/tmp/*|/var/folders/*) rm -rf -- "$PROBE_DIR" ;; esac; }
emit_cell() {
  local cell="$1" outcome="$2" reason="$3" command="$4" raw="${5:-}" sha="-" version="$VERSION" line
  if [ -n "$raw" ] && [ -s "$raw" ]; then sha="$(sha256_of "$raw")"; fi
  if [ "$cell" = L ]; then version="n/a"; fi
  line="$(printf '{"cell":%s,"point":%s,"decisive":%s,"outcome":%s,"reason":%s,"command":%s,"claude_version":%s,"sha256":%s}' \
    "$(json_quote "$cell")" "$(point_of "$cell")" "$(decisive_of "$cell")" "$(json_quote "$outcome")" \
    "$(json_quote "$reason")" "$(json_quote "$command")" "$(json_quote "$version")" "$(json_quote "$sha")")"
  printf '%s\n' "$line"
  if [ -n "$OUT" ]; then printf '%s\n' "$line" >>"$OUT"; fi
}
run_in_probe() {
  local cwd="$1" raw="$2"; shift 2
  ( cd "$cwd" || exit 125
    export VNX_DATA_DIR="$PROBE_DIR/data" VNX_DATA_DIR_EXPLICIT=1 VNX_ENFORCE_WORKER_PERMISSIONS="$CELL_ENFORCE"
    if [ -n "$CELL_DISPATCH_PATHS" ]; then export VNX_DISPATCH_PATHS="$CELL_DISPATCH_PATHS"; fi
    set +e
    perl -e 'alarm shift; exec @ARGV' "$TIMEOUT_SECONDS" "$CLAUDE_BIN" "$@" >"$raw" 2>&1
    echo "$?" )
}
capture_session() { PYTHONPATH="$REPO_ROOT/scripts/lib" python3 -c 'import json,sys; from context_manifest import capture; print(json.dumps(capture(sys.argv[1])))' "$1"; }
# inspect <capture.json> <instructions|auto_memory> <sentinel-path> -> "outcome<TAB>reason"
inspect() {
  python3 - "$@" <<'PY'
import json, sys
try: doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc: print("niet_gemeten\ttranscript scan unreadable: %s" % type(exc).__name__); raise SystemExit
if doc.get("status") not in ("ok", "partial"):
    print("niet_gemeten\t%s: %s" % (doc.get("status"), doc.get("reason", ""))); raise SystemExit
if sys.argv[2] == "auto_memory":
    m = doc.get("auto_memory") or {}
    print("ja\tauto_memory.path=%s" % m.get("path") if m.get("loaded") else "nee\tno AutoMem instruction loaded for this cwd")
    raise SystemExit
files = (doc.get("instructions") or {}).get("files") or []
hit = [f for f in files if str(f.get("path", "")).endswith(sys.argv[3]) and str(f.get("type", "")).lower() == "project"]
print("ja\tProject instruction at %s" % hit[0].get("path") if hit else
      "nee\tno Project instruction for the sentinel (seen types: %s)" % (",".join(sorted({str(f.get("type")) for f in files})) or "none"))
PY
}
classify_b() {
  local status="$1" out="$2"
  if [ "$status" = "0" ] && [ -s "$out" ]; then printf 'ja\tcall succeeded; OAuth works without --bare\n'
  elif [ "$status" = "142" ]; then printf 'niet_gemeten\ttimeout after %ss\n' "$TIMEOUT_SECONDS"
  elif [ "$status" != "0" ] && grep -Eiq 'ANTHROPIC_API_KEY|api key' "$out" 2>/dev/null; then printf 'nee\tcall demanded an API key\n'
  else printf 'niet_gemeten\texit %s without an API-key demand\n' "$status"; fi
}
# cell_call <cell> <extra-flag> <sentinel: yes|no>: A/C/D share one non-interactive call.
cell_call() {
  local cell="$1" extra="$2" sentinel="$3" root="$PROBE_DIR/$1" sid status raw verdict oc rc cmd
  mkdir -p "$root/klant"
  if [ "$sentinel" = yes ]; then printf 'VNX-AGENT-LAYOUT-SENTINEL-%s\n' "$cell" >"$root/CLAUDE.md"; fi
  if ! have_claude; then emit_cell "$cell" niet_gemeten "claude not on PATH ($CLAUDE_BIN)" "$CLAUDE_BIN -p ..." ""; return; fi
  sid="$(new_session)"; CELL_ENFORCE=0; CELL_DISPATCH_PATHS=""
  local call="$PROBE_DIR/raw/$cell.call"
  # shellcheck disable=SC2086 # $extra is an intentional flag list ("" or "--bare")
  status="$(run_in_probe "$root/klant" "$call" -p "Reply with OK." --output-format text --session-id "$sid" $extra)"
  cmd="cd $root/klant && VNX_DATA_DIR=$PROBE_DIR/data VNX_DATA_DIR_EXPLICIT=1 $CLAUDE_BIN -p 'Reply with OK.' --output-format text --session-id $sid $extra"
  if [ "$status" != 0 ]; then emit_cell "$cell" niet_gemeten "claude exited $status without a completed call" "$cmd" "$call"; return; fi
  raw="$PROBE_DIR/raw/$cell.txt"
  capture_session "$sid" >"$raw" 2>/dev/null || true
  if [ "$sentinel" = yes ]; then verdict="$(inspect "$raw" instructions "$root/CLAUDE.md")"; else verdict="$(inspect "$raw" auto_memory "")"; fi
  IFS=$'\t' read -r oc rc <<<"$verdict"
  emit_cell "$cell" "$oc" "$rc" "$cmd" "$raw"
}
cell_B() {
  local root="$PROBE_DIR/B" status raw verdict oc rc
  mkdir -p "$root/klant"
  if ! have_claude; then emit_cell B niet_gemeten "claude not on PATH ($CLAUDE_BIN)" "$CLAUDE_BIN -p ..." ""; return; fi
  CELL_ENFORCE=0; CELL_DISPATCH_PATHS=""
  raw="$PROBE_DIR/raw/B.txt"
  status="$(run_in_probe "$root/klant" "$raw" -p "Reply with the single word OK." --output-format text)"
  verdict="$(classify_b "$status" "$raw")"
  IFS=$'\t' read -r oc rc <<<"$verdict"
  emit_cell B "$oc" "$rc" "cd $root/klant && VNX_DATA_DIR=$PROBE_DIR/data VNX_DATA_DIR_EXPLICIT=1 $CLAUDE_BIN -p 'Reply with the single word OK.' --output-format text" "$raw"
}
write_probe_settings() {
  cat >"$1" <<JSON
{"hooks":{"PreToolUse":[{"matcher":"Write|Edit|MultiEdit|Bash","hooks":[{"type":"command","command":"$REPO_ROOT/scripts/hooks/pretooluse_worker_scope_enforce.sh","timeout":5}]}]}}
JSON
}
# cell_write <cell> <dispatch-paths> <allowed-rel> <denied-rel-or-abs>: H1/H2.
cell_write() {
  local cell="$1" paths="$2" allowed_rel="$3" denied="$4"
  local root="$PROBE_DIR/$cell" settings="$PROBE_DIR/$cell/probe-settings.json"
  mkdir -p "$root/docs" "$root/scripts"; write_probe_settings "$settings"
  case "$denied" in /*) ;; *) denied="$root/$denied" ;; esac
  local cmd="$CLAUDE_BIN -p 'Use the Write tool ...' --settings $settings --dangerously-skip-permissions"
  if ! have_claude; then emit_cell "$cell" niet_gemeten "claude not on PATH ($CLAUDE_BIN)" "$cmd" ""; return; fi
  CELL_ENFORCE=1; CELL_DISPATCH_PATHS="$paths"
  local r1="$PROBE_DIR/raw/$cell.allowed" r2="$PROBE_DIR/raw/$cell.denied" s1 s2
  s1="$(run_in_probe "$root" "$r1" --settings "$settings" --dangerously-skip-permissions -p "Use the Write tool to write the single word probe to $root/$allowed_rel" --output-format text)"
  s2="$(run_in_probe "$root" "$r2" --settings "$settings" --dangerously-skip-permissions -p "Use the Write tool to write the single word probe to $denied" --output-format text)"
  local raw="$PROBE_DIR/raw/$cell.txt" oc rc
  cat "$r1" "$r2" >"$raw" 2>/dev/null || true
  if [ -f "$denied" ]; then oc=nee; rc="out-of-scope write succeeded"
  elif [ -f "$root/$allowed_rel" ]; then oc=ja; rc="out-of-scope write denied; allowed write performed"
  else oc=niet_gemeten; rc="no positive-control write (exit $s1/$s2); the CLI did not reach the tool"; fi
  emit_cell "$cell" "$oc" "$rc" "$cmd" "$raw"
}
# Cell L is static: which lane starters in scripts/ start claude with --bare.
cell_L() {
  local raw="$PROBE_DIR/raw/L.txt" lane file anchor marker line hit bare summary=""
  : >"$raw"
  while IFS='|' read -r lane file anchor marker; do
    [ -n "$lane" ] || continue
    bare=no
    if line="$(grep -nF -m1 -- "$marker" "$REPO_ROOT/$file" 2>/dev/null)"; then bare=yes
    elif ! line="$(grep -nF -m1 -- "$anchor" "$REPO_ROOT/$file" 2>/dev/null)"; then
      printf 'lane=%s bare=unknown file=%s:?\n' "$lane" "$file" >>"$raw"; continue
    fi
    hit="${line%%:*}"
    printf 'lane=%s bare=%s file=%s:%s\n' "$lane" "$bare" "$file" "$hit" >>"$raw"
    if [ "$bare" = yes ]; then summary="${summary}${summary:+, }${lane} ${file}:${hit}"; fi
  done <<'TABLE'
claude_headless|scripts/lib/envelope_adapters_claude.py|spawn_claude(|__NO_BARE__
claude_subprocess|scripts/lib/provider_spawns/claude_spawn.py|def spawn_claude(|__NO_BARE__
claude_gate|scripts/lib/claude_gate_lane.py|spawn_claude(|__NO_BARE__
deepseek-harness|scripts/lib/provider_spawns/deepseek_harness_spawn.py|return [*bare_harness_cli_args|return [*bare_harness_cli_args
glm|scripts/lib/provider_spawns/glm_harness_spawn.py|return [*bare_harness_cli_args|return [*bare_harness_cli_args
deepseek-classifier|scripts/lib/classifier_providers/deepseek_provider.py|*bare_harness_cli_args()|*bare_harness_cli_args()
harness-shared|scripts/lib/provider_spawns/harness_config_dir.py|return [BARE_FLAG|return [BARE_FLAG
TABLE
  if [ -n "$summary" ]; then emit_cell L ja "bare lanes: $summary" "grep -nF over the lane starters in scripts/" "$raw"
  else emit_cell L nee "no lane starts claude with --bare" "grep -nF over the lane starters in scripts/" "$raw"; fi
}
run_cell() {
  local cell="$1"
  if [ "$SELF_CHECK" = 1 ] && [ "$cell" != L ]; then
    emit_cell "$cell" niet_gemeten "--self-check does not start claude" "$CLAUDE_BIN (not run)"; return
  fi
  case "$cell" in A) cell_call A "" yes ;; C) cell_call C "--bare" yes ;; D) cell_call D "" no ;;
    H1) cell_write H1 "" "allowed.txt" "$PROBE_DIR/H1-outside.txt" ;;
    H2) cell_write H2 '["docs/"]' "docs/x.md" "scripts/x.py" ;; L) cell_L ;; B) cell_B ;; esac
}
validate_cells() {
  python3 - "$1" <<'PY'
import json, sys
expected = {"A": (3, True), "C": (3, False), "D": (3, True), "H1": (4, True), "H2": (4, True), "L": (4, True), "B": (5, True)}
keys = {"cell", "point", "decisive", "outcome", "reason", "command", "claude_version", "sha256"}
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
seen = {row["cell"]: row for row in rows}
assert len(rows) == len(seen) == len(expected), "expected %d unique cells, got %r" % (len(expected), list(seen))
for row in rows:
    assert keys <= set(row) and row["outcome"] in ("ja", "nee", "niet_gemeten"), row
    assert isinstance(row["point"], int) and isinstance(row["decisive"], bool), row
    assert row["outcome"] != "niet_gemeten" or row["reason"], "cell %s without a reason" % row["cell"]
for cell, flags in expected.items():
    assert cell in seen and (seen[cell]["point"], seen[cell]["decisive"]) == flags, "cell %s wrong" % cell
assert seen["L"]["outcome"] == "ja" and ".py:" in seen["L"]["reason"], "cell L names no lane with file and line"
PY
}
main() {
  parse_args "$@"
  if [ "$SELF_CHECK" = 1 ]; then VERSION=""; else VERSION="$(read_version)"; fi
  local cells="$CELLS"
  if [ "$SELF_CHECK" != 1 ] && [ "$CELL" != all ]; then
    case " $CELLS " in *" $CELL "*) cells="$CELL" ;; *) die "unknown cell: $CELL" ;; esac
  fi
  ensure_probe
  if [ "$SELF_CHECK" = 1 ] && [ -z "$OUT" ]; then OUT="$PROBE_DIR/self-check.ndjson"; fi
  if [ -n "$OUT" ]; then : >"$OUT"; fi
  local cell
  for cell in $cells; do run_cell "$cell"; done
  if [ "$SELF_CHECK" = 1 ]; then validate_cells "$OUT"; fi
}
main "$@"
