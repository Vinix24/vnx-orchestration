#!/usr/bin/env bash
# probe_agent_skill_discovery.sh - the four discovery cells of agent-skill-discovery-binding (R7):
# C1 claude under `on`, C2 the same under --bare (not decisive), C3a codex placement (deterministic),
# C3b codex discovery. Only the T0 runs the CLI cells: a builder cannot start `claude -p` or `codex exec`
# (scripts/hooks/pretooluse_block_raw_claude_spawn.sh). cwd is a throwaway git worktree under $TMPDIR with
# the R2 ignore rule and a probe slug in no tracked catalog, removed at exit; the probe writes nothing
# outside $TMPDIR and reads the CLI's own transcript back as evidence. ja/nee only on recorded evidence;
# a timeout, a non-zero exit, a missing init/listing event, an auth failure or an unparseable stream is
# niet_gemeten with that reason. One JSON line per cell with the sha256 of the raw output judged.
# Usage: scripts/probe_agent_skill_discovery.sh --self-check  |  VNX_PROBE_OPERATOR=1 ... --run [--cell C1|C2|C3a|C3b|all] [--out <file>]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CELLS="C1 C2 C3a C3b"; CELL="all"; OUT=""; MODE=""; PROBE_DIR=""; SLUG=""
CLAUDE_BIN="${PROBE_CLAUDE_BIN:-claude}"; CODEX_BIN="${PROBE_CODEX_BIN:-codex}"
TIMEOUT_SECONDS="${PROBE_TIMEOUT_SECONDS:-120}"; CLAUDE_VERSION=""; CODEX_VERSION=""
ROLE="probe-skill-discovery"; NEGATIVE="vnx-probe-negative-control-not-in-tree"
CRED_VARS="ANTHROPIC_API_KEY ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX"
SCRUB_PREFIX="env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_USE_BEDROCK -u CLAUDE_CODE_USE_VERTEX"
die() { echo "probe_agent_skill_discovery: $*" >&2; exit 2; }
parse_args() {
  while [ "$#" -gt 0 ]; do case "$1" in
      --self-check) MODE="self-check" ;; --run) MODE="run" ;;
      --cell) [ "$#" -ge 2 ] || die "--cell needs a value"; CELL="$2"; shift ;;
      --out) [ "$#" -ge 2 ] || die "--out needs a path"; OUT="$2"; shift ;;
      *) die "unknown argument: $1" ;;
    esac; shift; done
  [ -n "$MODE" ] || die "one of --self-check or --run is required"
}
operator_gate() {
  if [ "${VNX_PROBE_OPERATOR:-}" != "1" ]; then
    echo "probe_agent_skill_discovery: --run needs VNX_PROBE_OPERATOR=1: a builder cannot start claude -p or codex exec, only the T0 runs the CLI cells" >&2
    exit 3
  fi
}
# $TMPDIR without trailing slashes, as a physical path (macOS symlinks /var to /private/var; the CLI reports the real cwd).
tmp_base() {
  local base="${TMPDIR:-/tmp}"
  while [ "${#base}" -gt 1 ] && [ "${base%/}" != "$base" ]; do base="${base%/}"; done
  if [ "$base" = / ]; then base=/tmp; fi
  if [ -d "$base" ]; then base="$(cd "$base" && pwd -P)"; fi
  printf '%s' "$base"
}
ensure_probe() {
  if [ -z "$PROBE_DIR" ]; then
    PROBE_DIR="$(mktemp -d "$(tmp_base)/probe-skill-discovery.XXXXXX")"; trap cleanup EXIT
    mkdir -p "$PROBE_DIR/raw" "$PROBE_DIR/data"
    SLUG="vnx-probe-skill-disc-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
    export PROBE_ROLE="$ROLE" PROBE_STATE="$PROBE_DIR/data" PROBE_DISPATCH="probe-$SLUG"
  fi
}
cleanup() { case "$PROBE_DIR" in "$(tmp_base)"/*|/tmp/*|/var/folders/*) rm -rf -- "$PROBE_DIR" ;; esac; }
sha256_of() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }
new_session() { python3 -c 'import uuid; print(uuid.uuid4())'; }
read_version() { "$1" --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true; }
decisive_of() { if [ "$1" = C2 ]; then printf false; else printf true; fi; }
emit_cell() {
  local cell="$1" outcome="$2" reason="$3" command="$4" raw="${5:-}" sha="-" line
  if [ -n "$raw" ] && [ -s "$raw" ]; then sha="$(sha256_of "$raw")"; fi
  line="$(python3 -c 'import json,sys; print(json.dumps({"cell":sys.argv[1],"decisive":sys.argv[2]=="true","outcome":sys.argv[3],"reason":sys.argv[4],"command":sys.argv[5],"claude_version":sys.argv[6],"codex_version":sys.argv[7],"sha256":sys.argv[8]},separators=(",",":")))' \
    "$cell" "$(decisive_of "$cell")" "$outcome" "$reason" "$command" "$CLAUDE_VERSION" "$CODEX_VERSION" "$sha")"
  printf '%s\n' "$line"; [ -z "$OUT" ] || printf '%s\n' "$line" >>"$OUT"
}
py() {
  PYTHONPATH="$REPO_ROOT/scripts/lib" python3 - "$@" <<'PY'
import json, os, re, sys
from pathlib import Path
cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "materialize":
    from agent_skill_materialize import materialize_skill_set
    result = materialize_skill_set(os.environ["PROBE_ROLE"], worktree=args[0], provider=args[1], mode="on",
                                   dispatch_id=os.environ["PROBE_DISPATCH"], state_dir=os.environ["PROBE_STATE"],
                                   project_id="probe-skill-discovery")
    print("%s\t%s" % (result.get("status"), result.get("reason") or "ok"))
elif cmd == "judge_claude":
    raw, session, root, slug, evidence = args
    try: raw_bytes = open(raw, "rb").read()
    except OSError: raw_bytes = b""
    text = raw_bytes.decode("utf-8", errors="replace")
    from token_harvest import locate_session_transcript
    transcript = locate_session_transcript(session)
    # The sha256 of the row covers everything judged: the CLI stream, then the transcript exactly as read.
    transcript_bytes = open(transcript, "rb").read() if transcript is not None else b""
    with open(evidence, "wb") as handle:
        handle.write(raw_bytes + b"--- transcript " + (str(transcript) if transcript is not None else "<none>").encode() + b" ---\n" + transcript_bytes)
    events = []
    for line in text.splitlines():
        try: event = json.loads(line)
        except ValueError: continue
        if isinstance(event, dict): events.append(event)
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    source = inits[0].get("apiKeySource") if inits else None
    if not isinstance(source, str) or not source:
        print("niet_gemeten\tauth_source_unproven: no init event with an apiKeySource"); raise SystemExit
    if source != "none":
        print("niet_gemeten\tauth_source_api_key: the init event names an API key source"); raise SystemExit
    from context_manifest import extract_skills
    if transcript is None:
        print("niet_gemeten\tno_transcript: session %s has no transcript" % session); raise SystemExit
    listings = []
    for line in transcript_bytes.splitlines():
        if b'"attachment"' not in line: continue
        try: entry = json.loads(line)
        except ValueError: continue
        attachment = entry.get("attachment") if isinstance(entry, dict) else None
        if isinstance(attachment, dict) and attachment.get("type") == "skill_listing": listings.append(attachment)
    initial = next((l for l in listings if l.get("isInitial") is True), None)
    if initial is None:
        print("niet_gemeten\tskill_listing_missing: no initial skill_listing in the transcript"); raise SystemExit
    names = initial.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        print("niet_gemeten\tskill_listing_malformed: the initial listing has no list of names"); raise SystemExit
    homes = sorted(p for p in Path.home().glob(".claude*") if p.is_dir())
    groups = extract_skills(listings, Path(root), homes).get("initial") or {}
    counts = ",".join("%s=%d" % (key, len(groups.get(key) or [])) for key in ("repo", "user", "synced", "namespaced", "other"))
    if slug in [n for group in groups.values() for n in group]:
        print("ja\tdeclared set is a subset of the initial listing (1/1 present); by origin: " + counts)
    else:
        print("nee\tthe initial skill_listing does not name %s; by origin: %s" % (slug, counts))
elif cmd == "judge_listing":
    raw, slug = args
    listed = [line.strip() for line in open(raw, encoding="utf-8", errors="replace") if line.strip()]
    extra, missing = sorted(set(listed) - {slug}), [slug] if slug not in listed else []
    if not extra and not missing and len(listed) == 1: print("ja\t.agents/skills holds exactly the declared set: " + slug)
    else: print("nee\t.agents/skills differs from the declared set: extra=%s missing=%s" % (extra or "none", missing or "none"))
elif cmd == "judge_codex":
    raw, slug, negative = args
    text = open(raw, encoding="utf-8", errors="replace").read()
    def names(value, line): return re.search(r"(?<![A-Za-z0-9_.-])%s(?![A-Za-z0-9_.-])" % re.escape(value), line) is not None
    def verdict(outcome, reason): print("%s\t%s" % (outcome, reason)); raise SystemExit
    if not text.strip(): verdict("niet_gemeten", "empty_output: codex printed nothing to judge")
    events = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip(): continue
        try: event = json.loads(line)
        except ValueError: event = None
        if not isinstance(event, dict): verdict("niet_gemeten", "stream_malformed: line %d is not a JSON object" % number)
        events.append(event)
    if not any(e.get("type") == "turn.completed" for e in events): verdict("niet_gemeten", "turn_incomplete: the stream has no turn.completed event")
    answer = None
    for event in events:
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            value = item.get("text")
            if not isinstance(value, str):
                value = item.get("content")
                if isinstance(value, list):
                    value = "\n".join(b["text"] for b in value if isinstance(b, dict) and isinstance(b.get("text"), str))
            answer = value if isinstance(value, str) else ""
    if answer is None or not answer.strip(): verdict("niet_gemeten", "no_agent_answer: no agent_message with text in the stream")
    lines = [l for l in (re.sub(r"^[\s\-*`]+|[\s`]+$", "", raw_line) for raw_line in answer.splitlines()) if l]
    if any(names(negative, l) for l in lines): verdict("nee", "the answer names the negative control %s, which is not in the tree" % negative)
    if any(names(slug, l) for l in lines): verdict("ja", "the answer names %s and not the negative control" % slug)
    verdict("nee", "the answer does not name %s (%d answer lines judged)" % (slug, len(lines)))
elif cmd == "validate":
    rows = [json.loads(line) for line in open(args[0], encoding="utf-8") if line.strip()]
    expected = {"C1": True, "C2": False, "C3a": True, "C3b": True}
    keys = {"cell", "decisive", "outcome", "reason", "command", "claude_version", "codex_version", "sha256"}
    assert len(rows) == len(expected) and {row["cell"] for row in rows} == set(expected), rows
    for row in rows:
        assert set(row) == keys, row
        assert row["decisive"] is expected[row["cell"]], row
        assert row["outcome"] in ("ja", "nee", "niet_gemeten"), row
        assert row["reason"], row
        assert row["sha256"] == "-" or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), row
PY
}
# A throwaway git worktree with the R2 ignore rule, one role and one probe skill that exists only as source.
write_tree() {
  local root="$1"
  mkdir -p "$root/agents/$ROLE" "$root/skills/$SLUG"
  printf '# probe role\n' >"$root/agents/$ROLE/CLAUDE.md"
  printf 'skills:\n  - %s\n' "$SLUG" >"$root/agents/$ROLE/config.yaml"
  printf -- '---\nname: %s\ndescription: Probe skill for the R7 agent-skill-discovery measurement; it exists only through materialisation.\n---\n\n# Probe skill\n' "$SLUG" >"$root/skills/$SLUG/SKILL.md"
  printf '.claude/skills/*/SKILL.md\n.agents/\n.vnx-agent-skills/\n' >"$root/.gitignore"
  git -C "$root" init -q
}
run_in_probe() {
  local cwd="$1" raw="$2" scrub="$3"; shift 3
  ( cd "$cwd" || exit 125
    export VNX_DATA_DIR="$PROBE_DIR/data" VNX_DATA_DIR_EXPLICIT=1 VNX_AGENT_LAYOUT=on
    set +e
    local -a launch=("$@") var
    if [ "$scrub" = yes ]; then
      launch=(env); for var in $CRED_VARS; do launch+=(-u "$var"); done; launch+=("$@")
    fi
    perl -e 'alarm shift; exec @ARGV' "$TIMEOUT_SECONDS" "${launch[@]}" >"$raw" 2>&1
    echo "$?" )
}
failed_call() {
  local cell="$1" status="$2" command="$3" raw="$4"
  if [ "$status" = 142 ]; then emit_cell "$cell" niet_gemeten "timeout after ${TIMEOUT_SECONDS}s" "$command" "$raw"
  else emit_cell "$cell" niet_gemeten "cli_exit_${status:-unknown}" "$command" "$raw"; fi
}
cell_claude() {
  local cell="$1" extra="$2" root="$PROBE_DIR/$1" status oc rc sid raw="$PROBE_DIR/raw/$1.txt"
  local cmd="$CLAUDE_BIN -p ... (not run)"
  if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    emit_cell "$cell" niet_gemeten "claude not on PATH ($CLAUDE_BIN)" "$cmd" ""; return
  fi
  write_tree "$root"
  IFS=$'\t' read -r oc rc <<<"$(py materialize "$root" claude)"
  if [ "$oc" != ok ]; then emit_cell "$cell" niet_gemeten "materialisation failed: $rc" "$cmd" ""; return; fi
  sid="$(new_session)"
  # shellcheck disable=SC2086 # $extra is an intentional flag list ("" or "--bare")
  status="$(run_in_probe "$root" "$raw" yes "$CLAUDE_BIN" -p "Reply with the single word OK." --output-format stream-json --verbose --session-id "$sid" $extra)"
  cmd="cd $root && VNX_AGENT_LAYOUT=on $SCRUB_PREFIX $CLAUDE_BIN -p 'Reply with the single word OK.' --output-format stream-json --verbose --session-id $sid $extra"
  if [ "$status" != 0 ]; then failed_call "$cell" "$status" "$cmd" "$raw"; return; fi
  IFS=$'\t' read -r oc rc <<<"$(py judge_claude "$raw" "$sid" "$root" "$SLUG" "$PROBE_DIR/raw/$cell.evidence")"
  emit_cell "$cell" "$oc" "$rc" "$cmd" "$PROBE_DIR/raw/$cell.evidence"
}
cell_c3a() {
  local root="$PROBE_DIR/C3a" raw="$PROBE_DIR/raw/C3a.txt" oc rc status
  local cmd="cd $root && PYTHONPATH=$REPO_ROOT/scripts/lib python3 -c \"from agent_skill_materialize import materialize_skill_set as m; m('$ROLE', worktree='$root', provider='codex', mode='on', dispatch_id='$PROBE_DISPATCH', state_dir='$PROBE_STATE', project_id='probe-skill-discovery')\" && ls -1 .agents/skills"
  write_tree "$root"
  IFS=$'\t' read -r oc rc <<<"$(py materialize "$root" codex)"
  if [ "$oc" != ok ]; then emit_cell C3a niet_gemeten "materialisation failed: $rc" "$cmd" ""; return; fi
  status="$( ( cd "$root" && set +e; ls -1 .agents/skills >"$raw" 2>&1; echo $? ) )"
  if [ "$status" != 0 ]; then emit_cell C3a niet_gemeten "ls of .agents/skills failed (exit $status)" "$cmd" "$raw"; return; fi
  IFS=$'\t' read -r oc rc <<<"$(py judge_listing "$raw" "$SLUG")"
  emit_cell C3a "$oc" "$rc" "$cmd" "$raw"
}
cell_c3b() {
  local root="$PROBE_DIR/C3b" raw="$PROBE_DIR/raw/C3b.txt" oc rc status
  local prompt="List the name of every skill available to you in this project, one per line, and nothing else."
  local cmd="cd $root && $CODEX_BIN exec --json --sandbox read-only '$prompt'"
  if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
    emit_cell C3b niet_gemeten "codex not on PATH ($CODEX_BIN)" "$cmd" ""; return
  fi
  write_tree "$root"
  IFS=$'\t' read -r oc rc <<<"$(py materialize "$root" codex)"
  if [ "$oc" != ok ]; then emit_cell C3b niet_gemeten "materialisation failed: $rc" "$cmd" ""; return; fi
  status="$(run_in_probe "$root" "$raw" no "$CODEX_BIN" exec --json --sandbox read-only "$prompt")"
  if [ "$status" != 0 ]; then failed_call C3b "$status" "$cmd" "$raw"; return; fi
  IFS=$'\t' read -r oc rc <<<"$(py judge_codex "$raw" "$SLUG" "$NEGATIVE")"
  emit_cell C3b "$oc" "$rc" "$cmd" "$raw"
}
run_cell() { case "$1" in C1) cell_claude C1 "" ;; C2) cell_claude C2 "--bare" ;; C3a) cell_c3a ;; C3b) cell_c3b ;; esac; }
main() {
  parse_args "$@"
  if [ "$CELL" != all ]; then case " $CELLS " in *" $CELL "*) ;; *) die "unknown cell: $CELL" ;; esac; fi
  if [ "$MODE" = run ]; then operator_gate; fi
  ensure_probe
  local cell cells="$CELLS"
  if [ -n "$OUT" ]; then : >"$OUT"; fi
  if [ "$MODE" = self-check ]; then
    [ -n "$OUT" ] || OUT="$PROBE_DIR/self-check.ndjson"; : >"$OUT"
    for cell in $CELLS; do emit_cell "$cell" niet_gemeten "--self-check does not start a CLI" "$CLAUDE_BIN/$CODEX_BIN (not run)" ""; done
    py validate "$OUT"; return 0
  fi
  if [ "$CELL" != all ]; then cells="$CELL"; fi
  CLAUDE_VERSION="$(read_version "$CLAUDE_BIN")"; CODEX_VERSION="$(read_version "$CODEX_BIN")"
  for cell in $cells; do run_cell "$cell"; done
}
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
