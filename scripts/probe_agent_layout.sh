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
PROBE_DIR=""; VERSION=""; CELL_ENFORCE=0; CELL_DISPATCH_PATHS=""; CELL_SCRUB=0
CRED_VARS="ANTHROPIC_API_KEY ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX"
SCRUB_PREFIX="env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_USE_BEDROCK -u CLAUDE_CODE_USE_VERTEX"
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
# tmp_base: $TMPDIR without trailing slashes (macOS ends it in "/", which would put "//" in every probe path).
tmp_base() {
  local base="${TMPDIR:-/tmp}"
  while [ "${#base}" -gt 1 ] && [ "${base%/}" != "$base" ]; do base="${base%/}"; done
  if [ "$base" = / ]; then base=/tmp; fi
  printf '%s' "$base"
}
ensure_probe() {
  if [ -z "$PROBE_DIR" ]; then PROBE_DIR="$(mktemp -d "$(tmp_base)/probe-agent-layout.XXXXXX")"
    trap cleanup EXIT; mkdir -p "$PROBE_DIR/raw" "$PROBE_DIR/data"; fi
}
cleanup() { case "$PROBE_DIR" in "$(tmp_base)"/*|/tmp/*|/var/folders/*) rm -rf -- "$PROBE_DIR" ;; esac; }
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
    local -a launch=("$CLAUDE_BIN") var
    if [ "$CELL_SCRUB" = 1 ]; then launch=(env); for var in $CRED_VARS; do launch+=(-u "$var"); done; launch+=("$CLAUDE_BIN"); fi
    perl -e 'alarm shift; exec @ARGV' "$TIMEOUT_SECONDS" "${launch[@]}" "$@" >"$raw" 2>&1
    echo "$?" )
}
capture_session() { PYTHONPATH="$REPO_ROOT/scripts/lib" python3 -c 'import json,sys; from context_manifest import capture; print(json.dumps(capture(sys.argv[1])))' "$1"; }
# inspect <capture.json> <instructions|auto_memory> <sentinel-path> -> "outcome<TAB>reason"
inspect() {
  python3 - "$@" <<'PY'
import json, os, sys
try: doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc: print("niet_gemeten\ttranscript scan unreadable: %s" % type(exc).__name__); raise SystemExit
if doc.get("status") not in ("ok", "partial"):
    print("niet_gemeten\t%s: %s" % (doc.get("status"), doc.get("reason", ""))); raise SystemExit
if sys.argv[2] == "auto_memory":
    m = doc.get("auto_memory") or {}
    print("ja\tauto_memory.path=%s" % m.get("path") if m.get("loaded") else "nee\tno AutoMem instruction loaded for this cwd")
    raise SystemExit
files = (doc.get("instructions") or {}).get("files") or []
def same_file(reported, expected):
    # The CLI reports a normalised path; $TMPDIR may carry "//" or a symlinked prefix, so compare both normalised.
    if not reported or not expected: return False
    return os.path.realpath(reported) == os.path.realpath(expected) or os.path.normpath(reported).endswith(os.path.normpath(expected))
hit = [f for f in files if same_file(str(f.get("path", "")), sys.argv[3]) and str(f.get("type", "")).lower() == "project"]
print("ja\tProject instruction at %s" % hit[0].get("path") if hit else
      "nee\tno Project instruction for the sentinel (seen types: %s)" % (",".join(sorted({str(f.get("type")) for f in files})) or "none"))
PY
}
# stream_facts <raw> auth | stream_facts <raw> write <abs-path> -> one token, read from parsed stream-json events.
# auth: oauth (init apiKeySource == "none") | api_key (any other string) | unproven (no init event or no string field).
# write: denied (Write on the path, tool_result is_error true with the hook refusal) | written (tool_result for that tool_use id
# with is_error absent, null or false and no refusal text; 2.1.319 omits is_error on success) | tool_error (is_error true, no hook
# text) | unclear_result (any other result, e.g. the refusal text without is_error true) | no_tool_result | auth_failed | no_attempt.
stream_facts() {
  python3 - "$@" <<'PY'
import json, os, sys
raw, mode = sys.argv[1], sys.argv[2]
try: text = open(raw, encoding="utf-8", errors="replace").read()
except OSError: text = ""
events = []
for line in text.splitlines():
    try: ev = json.loads(line)
    except ValueError: continue
    if isinstance(ev, dict): events.append(ev)
if mode == "auth":
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    src = inits[0].get("apiKeySource") if inits else None
    print("unproven" if not isinstance(src, str) or not src else "oauth" if src == "none" else "api_key"); raise SystemExit
target = os.path.realpath(sys.argv[3])
def blocks():
    for ev in events:
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for blk in content if isinstance(content, list) else []:
            if isinstance(blk, dict): yield blk
def flat(content):
    if isinstance(content, str): return content
    return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict)) if isinstance(content, list) else ""
ids = set()
for blk in blocks():
    inp = blk.get("input")
    if blk.get("type") == "tool_use" and blk.get("name") == "Write" and isinstance(inp, dict) and isinstance(blk.get("id"), str) \
            and isinstance(inp.get("file_path"), str) and os.path.realpath(inp["file_path"]) == target:
        ids.add(blk["id"])
results = [b for b in blocks() if b.get("type") == "tool_result" and ids and b.get("tool_use_id") in ids]
def refusal(r):
    body = flat(r.get("content"))
    return "worker-scope: write target" in body and "is outside" in body
def clean_success(r):
    return ("is_error" not in r or r["is_error"] is None or r["is_error"] is False) and not refusal(r)
if not ids:
    low = text.lower()
    print("auth_failed" if any(m in low for m in ("authentication_error", "invalid api key", "please run /login", "oauth token has expired")) else "no_attempt")
elif any(r.get("is_error") is True and refusal(r) for r in results): print("denied")
elif not results: print("no_tool_result")
elif any(clean_success(r) for r in results): print("written")
elif any(r.get("is_error") is True for r in results): print("tool_error")
else: print("unclear_result")
PY
}
classify_b() {
  local status="$1" out="$2" auth
  auth="$(stream_facts "$out" auth)"
  if [ "$status" = "142" ]; then printf 'niet_gemeten\ttimeout after %ss\n' "$TIMEOUT_SECONDS"
  elif [ "$status" != "0" ] && grep -Eiq 'ANTHROPIC_API_KEY|api key' "$out" 2>/dev/null; then printf 'nee\tcall demanded an API key\n'
  elif [ "$status" != "0" ]; then printf 'niet_gemeten\tcli_exit_%s without an API-key demand\n' "$status"
  elif [ "$auth" = oauth ]; then printf 'ja\tcall succeeded and init apiKeySource=none with credential variables unset; OAuth works without --bare\n'
  elif [ "$auth" = api_key ]; then printf 'niet_gemeten\tauth_source_api_key: init event names an API key source\n'
  else printf 'niet_gemeten\tauth_source_unproven: no init event with an apiKeySource\n'; fi
}
# cli_error_class <raw>: a fixed class for a failed call, never the CLI's own text (it may carry secrets).
cli_error_class() {
  if grep -Eiq 'ANTHROPIC_API_KEY|api[ _-]key' "$1" 2>/dev/null; then printf requires_api_key
  elif grep -Eiq 'unknown (option|flag|argument)|unrecognized (option|flag|argument)|invalid (option|flag)' "$1" 2>/dev/null; then printf unknown_flag
  else printf other; fi
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
  if [ "$status" != 0 ]; then
    emit_cell "$cell" niet_gemeten "claude exited ${status:-unknown} without a completed call (stderr class: $(cli_error_class "$call"))" "$cmd" "$call"; return
  fi
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
  CELL_ENFORCE=0; CELL_DISPATCH_PATHS=""; CELL_SCRUB=1
  raw="$PROBE_DIR/raw/B.txt"
  status="$(run_in_probe "$root/klant" "$raw" -p "Reply with the single word OK." --output-format stream-json --verbose)"
  CELL_SCRUB=0
  verdict="$(classify_b "$status" "$raw")"
  IFS=$'\t' read -r oc rc <<<"$verdict"
  emit_cell B "$oc" "$rc" "cd $root/klant && VNX_DATA_DIR=$PROBE_DIR/data VNX_DATA_DIR_EXPLICIT=1 $SCRUB_PREFIX $CLAUDE_BIN -p 'Reply with the single word OK.' --output-format stream-json --verbose" "$raw"
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
  local cmd="$CLAUDE_BIN -p 'Use the Write tool ...' --settings $settings --dangerously-skip-permissions --output-format stream-json --verbose"
  if ! have_claude; then emit_cell "$cell" niet_gemeten "claude not on PATH ($CLAUDE_BIN)" "$cmd" ""; return; fi
  CELL_ENFORCE=1; CELL_DISPATCH_PATHS="$paths"
  local r1="$PROBE_DIR/raw/$cell.allowed" r2="$PROBE_DIR/raw/$cell.denied" s1 s2 allowed="$root/$allowed_rel"
  s1="$(run_in_probe "$root" "$r1" --settings "$settings" --dangerously-skip-permissions --output-format stream-json --verbose -p "Use the Write tool to write the single word probe to $allowed")"
  s2="$(run_in_probe "$root" "$r2" --settings "$settings" --dangerously-skip-permissions --output-format stream-json --verbose -p "Use the Write tool to write the single word probe to $denied")"
  local raw="$PROBE_DIR/raw/$cell.txt" oc rc
  cat "$r1" "$r2" >"$raw" 2>/dev/null || true
  IFS=$'\t' read -r oc rc <<<"$(judge_write "$r1" "$s1" "$allowed" "$r2" "$s2" "$denied")"
  emit_cell "$cell" "$oc" "$rc" "$cmd" "$raw"
}
# why_not <status> <token> <allowed|denied>: why a call gave no proof (timeout, auth, CLI exit, then what the stream shows).
# no_hook_denial is only ever the reason for the denied call.
why_not() {
  if [ "$1" = 142 ]; then printf timeout; return; fi
  if [ "$2" = auth_failed ]; then printf auth_failed; return; fi
  if [ "$1" != 0 ]; then printf 'cli_exit_%s' "${1:-unknown}"; return; fi
  case "$2:$3" in
    no_attempt:*) printf no_write_attempt ;;
    no_tool_result:*) printf no_tool_result ;;
    tool_error:allowed) printf write_error_without_hook_text ;;
    unclear_result:allowed) printf write_result_unclear ;;
    denied:allowed) printf hook_refused_the_allowed_write ;;
    written:allowed) printf allowed_file_not_as_expected ;;
    tool_error:denied) printf 'no_hook_denial: the Write failed without the hook text' ;;
    unclear_result:denied) printf 'no_hook_denial: the Write result is neither a clean success nor the hook refusal' ;;
    written:denied) printf 'no_hook_denial: the out-of-scope Write reported success' ;;
    *) printf unknown_write_state ;;
  esac
}
# judge_write <r1> <s1> <allowed> <r2> <s2> <denied> -> "outcome<TAB>reason": ja only on recorded evidence of both calls,
# and never when either call exited non-zero (checked first).
judge_write() {
  local r1="$1" s1="$2" allowed="$3" r2="$4" s2="$5" denied="$6" t1 t2
  t1="$(stream_facts "$r1" write "$allowed")"; t2="$(stream_facts "$r2" write "$denied")"
  if [ "$s1" != 0 ]; then printf 'niet_gemeten\tpositive control gave no proof: %s (exit %s)\n' "$(why_not "$s1" "$t1" allowed)" "${s1:-unknown}"; return; fi
  if [ "$s2" != 0 ]; then printf 'niet_gemeten\t%s (exit %s): no recorded hook denial of the out-of-scope Write\n' "$(why_not "$s2" "$t2" denied)" "${s2:-unknown}"; return; fi
  if [ -f "$denied" ]; then printf 'nee\tout-of-scope write succeeded\n'; return; fi
  if [ "$t1" != written ] || [ ! -f "$allowed" ] || [ "$(tr -d '[:space:]' <"$allowed")" != probe ]; then
    printf 'niet_gemeten\tpositive control gave no proof: %s (exit %s)\n' "$(why_not "$s1" "$t1" allowed)" "$s1"
  elif [ "$t2" != denied ]; then printf 'niet_gemeten\t%s (exit %s): no recorded hook denial of the out-of-scope Write\n' "$(why_not "$s2" "$t2" denied)" "$s2"
  else printf 'ja\tout-of-scope Write attempted and refused by the hook; allowed Write performed\n'; fi
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
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
