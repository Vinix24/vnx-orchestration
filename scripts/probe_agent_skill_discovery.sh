#!/usr/bin/env bash
# probe_agent_skill_discovery.sh - the four discovery cells of agent-skill-discovery-binding (R7):
# C1 claude under `on`, C2 the same under --bare (not decisive), C3a codex placement (deterministic),
# C3b codex discovery. Only the T0 runs the CLI cells: a builder cannot start `claude -p` or `codex exec`
# (scripts/hooks/pretooluse_block_raw_claude_spawn.sh). cwd is a throwaway git worktree under $TMPDIR with
# the R2 ignore rule and a probe slug in no tracked catalog, removed at exit; the probe writes nothing
# outside $TMPDIR and reads the CLI's own transcript back as evidence. ja/nee only on recorded evidence;
# a timeout, a non-zero exit, a missing init/listing event, an auth failure or an unparseable stream is
# niet_gemeten with that reason, and so is a judge or materialiser that crashes or prints anything but one
# verdict line. One JSON line per cell with the sha256 of the raw output judged; every run ends by validating its rows.
# Usage: scripts/probe_agent_skill_discovery.sh --self-check  |  VNX_PROBE_OPERATOR=1 ... --run [--cell C1|C2|C3a|C3b|all] [--out <file>]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CELLS="C1 C2 C3a C3b"; CELL="all"; OUT=""; MODE=""; PROBE_DIR=""; SLUG=""
CLAUDE_BIN="${PROBE_CLAUDE_BIN:-claude}"; CODEX_BIN="${PROBE_CODEX_BIN:-codex}"
TIMEOUT_SECONDS="${PROBE_TIMEOUT_SECONDS:-120}"; CLAUDE_VERSION=""; CODEX_VERSION=""
ROLE="probe-skill-discovery"; NEGATIVE="vnx-probe-negative-control-not-in-tree"
VERDICTS="ja nee niet_gemeten"; MATERIALIZED="ok skipped error"; PY_OC=""; PY_RC=""
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
def json_objects(data):
    # Every non-empty line is evidence and must parse as a JSON object. Returns (objects, number of the first bad line or 0).
    objects = []
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip(): continue
        try: value = json.loads(line)
        except ValueError: value = None
        if not isinstance(value, dict): return objects, number
        objects.append(value)
    return objects, 0
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
    events, bad = json_objects(text)
    if bad:
        print("niet_gemeten\tstream_malformed: line %d is not a JSON object" % bad); raise SystemExit
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    source = inits[0].get("apiKeySource") if inits else None
    if not isinstance(source, str) or not source:
        print("niet_gemeten\tauth_source_unproven: no init event with an apiKeySource"); raise SystemExit
    if source != "none":
        print("niet_gemeten\tauth_source_api_key: the init event names an API key source"); raise SystemExit
    from context_manifest import extract_skills
    if transcript is None:
        print("niet_gemeten\tno_transcript: session %s has no transcript" % session); raise SystemExit
    entries, bad = json_objects(transcript_bytes)
    if bad:
        print("niet_gemeten\ttranscript_malformed: line %d is not a JSON object" % bad); raise SystemExit
    listings = []
    for entry in entries:
        # Other entry types are valid transcript content; only skill_listing attachments are read.
        attachment = entry.get("attachment")
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
    def verdict(outcome, reason): print("%s\t%s" % (outcome, reason)); raise SystemExit
    if not text.strip(): verdict("niet_gemeten", "empty_output: codex printed nothing to judge")
    events, bad = json_objects(text)
    if bad: verdict("niet_gemeten", "stream_malformed: line %d is not a JSON object" % bad)
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
    # The prompt asks for bare names, one per line: the answer counts only as that list. A line that is anything
    # else (a denial, a header, a sentence that mentions the slug) means there is no listing to judge.
    entries = []
    for number, answer_line in enumerate(answer.splitlines(), 1):
        entry = re.sub(r"^[\s\-*`]+|[\s`]+$", "", answer_line)
        if not entry: continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", entry):
            verdict("niet_gemeten", "answer_not_a_list: line %d is not a bare skill name" % number)
        entries.append(entry)
    if negative in entries: verdict("nee", "the answer lists the negative control %s, which is not in the tree" % negative)
    if slug in entries: verdict("ja", "the answer lists %s as an entry and not the negative control" % slug)
    verdict("nee", "the answer does not list %s (%d entries judged)" % (slug, len(entries)))
elif cmd == "validate":
    # One row per cell in args[1:] (all four when none is named). Explicit checks, not assert: python -O drops an assert.
    path, expected = args[0], {"C1": True, "C2": False, "C3a": True, "C3b": True}
    cells = args[1:] or sorted(expected)
    keys = {"cell", "decisive", "outcome", "reason", "command", "claude_version", "codex_version", "sha256"}
    def refuse(why, value): raise SystemExit("validate: %s: %r" % (why, value))
    rows, bad = json_objects(open(path, encoding="utf-8").read())
    if bad: refuse("line %d is not a JSON object" % bad, path)
    if len(rows) != len(cells) or sorted(str(row.get("cell")) for row in rows) != sorted(cells):
        refuse("not one row per cell %s" % " ".join(cells), rows)
    for row in rows:
        if set(row) != keys: refuse("keys differ from the schema", row)
        if row["decisive"] is not expected.get(row["cell"]): refuse("decisive is wrong for the cell", row)
        if row["outcome"] not in ("ja", "nee", "niet_gemeten"): refuse("outcome is not ja, nee or niet_gemeten", row)
        if not isinstance(row["reason"], str) or not row["reason"].strip(): refuse("reason is empty", row)
        sha = row["sha256"]
        if sha != "-" and not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)): refuse("sha256 is not - or a digest", row)
PY
}
# py_line <outcomes> <subcommand> [args...]: one `py` call with stdout, stderr and exit status kept apart, because
# `read <<<"$(py ...)"` returns 0 when py crashed. Sets PY_OC/PY_RC from its single `<outcome>\t<reason>` line. A
# non-zero exit, no line, more than one, no tab, an outcome outside <outcomes> or an empty reason is PY_OC=niet_gemeten
# with a PY_RC that names the subcommand and why, e.g. "judge_failed: judge_claude exited 1: <last stderr line>".
py_line() {
  local outcomes="$1" sub="$2" err="$PROBE_DIR/raw/py-$2.stderr" out status line known=no o why=""; shift
  if out="$(code=0; py "$@" 2>"$err" || code=$?; printf .; exit "$code")"; then status=0; else status=$?; fi
  out="${out%.}"; line="${out%$'\n'}"; PY_OC="${line%%$'\t'*}"; PY_RC="${line#*$'\t'}"
  for o in $outcomes; do if [ "$PY_OC" = "$o" ]; then known=yes; fi; done
  if [ "$status" != 0 ]; then
    why="exited $status: $(awk 'NF { last = $0 } END { print (last == "" ? "no stderr" : last) }' "$err" 2>/dev/null || echo "stderr unreadable")"
  elif [ -z "$line" ]; then why="printed no line"
  elif [ "$line" != "${line%%$'\n'*}" ]; then why="printed more than one line"
  elif [ "$PY_OC" = "$line" ]; then why="printed a line without a tab"
  elif [ "$known" = no ]; then why="printed the outcome '$PY_OC', not one of: $outcomes"
  elif [ -z "$PY_RC" ]; then why="printed an empty reason"; fi
  if [ -n "$why" ]; then PY_OC=niet_gemeten; PY_RC="${sub%%_*}_failed: $sub $why"; fi
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
  local cell="$1" extra="$2" root="$PROBE_DIR/$1" status sid raw="$PROBE_DIR/raw/$1.txt"
  local cmd="$CLAUDE_BIN -p ... (not run)"
  if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    emit_cell "$cell" niet_gemeten "claude not on PATH ($CLAUDE_BIN)" "$cmd" ""; return
  fi
  write_tree "$root"
  py_line "$MATERIALIZED" materialize "$root" claude
  if [ "$PY_OC" != ok ]; then emit_cell "$cell" niet_gemeten "materialisation failed: $PY_RC" "$cmd" ""; return; fi
  sid="$(new_session)"
  # shellcheck disable=SC2086 # $extra is an intentional flag list ("" or "--bare")
  status="$(run_in_probe "$root" "$raw" yes "$CLAUDE_BIN" -p "Reply with the single word OK." --output-format stream-json --verbose --session-id "$sid" $extra)"
  cmd="cd $root && VNX_AGENT_LAYOUT=on $SCRUB_PREFIX $CLAUDE_BIN -p 'Reply with the single word OK.' --output-format stream-json --verbose --session-id $sid $extra"
  if [ "$status" != 0 ]; then failed_call "$cell" "$status" "$cmd" "$raw"; return; fi
  py_line "$VERDICTS" judge_claude "$raw" "$sid" "$root" "$SLUG" "$PROBE_DIR/raw/$cell.evidence"
  emit_cell "$cell" "$PY_OC" "$PY_RC" "$cmd" "$PROBE_DIR/raw/$cell.evidence"
}
cell_c3a() {
  local root="$PROBE_DIR/C3a" raw="$PROBE_DIR/raw/C3a.txt" status
  local cmd="cd $root && PYTHONPATH=$REPO_ROOT/scripts/lib python3 -c \"from agent_skill_materialize import materialize_skill_set as m; m('$ROLE', worktree='$root', provider='codex', mode='on', dispatch_id='$PROBE_DISPATCH', state_dir='$PROBE_STATE', project_id='probe-skill-discovery')\" && ls -1 .agents/skills"
  write_tree "$root"
  py_line "$MATERIALIZED" materialize "$root" codex
  if [ "$PY_OC" != ok ]; then emit_cell C3a niet_gemeten "materialisation failed: $PY_RC" "$cmd" ""; return; fi
  status="$( ( cd "$root" || exit 125; set +e; ls -1 .agents/skills >"$raw" 2>&1; echo $? ) )"
  if [ "$status" != 0 ]; then emit_cell C3a niet_gemeten "ls of .agents/skills failed (exit $status)" "$cmd" "$raw"; return; fi
  py_line "$VERDICTS" judge_listing "$raw" "$SLUG"
  emit_cell C3a "$PY_OC" "$PY_RC" "$cmd" "$raw"
}
cell_c3b() {
  local root="$PROBE_DIR/C3b" raw="$PROBE_DIR/raw/C3b.txt" status
  local prompt="List the name of every skill available to you in this project, one per line, and nothing else."
  local cmd="cd $root && $CODEX_BIN exec --json --sandbox read-only '$prompt'"
  if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
    emit_cell C3b niet_gemeten "codex not on PATH ($CODEX_BIN)" "$cmd" ""; return
  fi
  write_tree "$root"
  py_line "$MATERIALIZED" materialize "$root" codex
  if [ "$PY_OC" != ok ]; then emit_cell C3b niet_gemeten "materialisation failed: $PY_RC" "$cmd" ""; return; fi
  status="$(run_in_probe "$root" "$raw" no "$CODEX_BIN" exec --json --sandbox read-only "$prompt")"
  if [ "$status" != 0 ]; then failed_call C3b "$status" "$cmd" "$raw"; return; fi
  py_line "$VERDICTS" judge_codex "$raw" "$SLUG" "$NEGATIVE"
  emit_cell C3b "$PY_OC" "$PY_RC" "$cmd" "$raw"
}
run_cell() { case "$1" in C1) cell_claude C1 "" ;; C2) cell_claude C2 "--bare" ;; C3a) cell_c3a ;; C3b) cell_c3b ;; esac; }
main() {
  parse_args "$@"
  if [ "$CELL" != all ]; then case " $CELLS " in *" $CELL "*) ;; *) die "unknown cell: $CELL" ;; esac; fi
  if [ "$MODE" = run ]; then operator_gate; fi
  ensure_probe
  local cell cells="$CELLS"
  [ -n "$OUT" ] || OUT="$PROBE_DIR/$MODE.ndjson"; : >"$OUT"
  if [ "$MODE" = self-check ]; then
    for cell in $CELLS; do emit_cell "$cell" niet_gemeten "--self-check does not start a CLI" "$CLAUDE_BIN/$CODEX_BIN (not run)" ""; done
  else
    if [ "$CELL" != all ]; then cells="$CELL"; fi
    CLAUDE_VERSION="$(read_version "$CLAUDE_BIN")"; CODEX_VERSION="$(read_version "$CODEX_BIN")"
    for cell in $cells; do run_cell "$cell"; done
  fi
  # A row outside the schema makes the run exit non-zero instead of leaving a file that looks like a measurement.
  # shellcheck disable=SC2086 # $cells is the intended word list of cells that ran
  py validate "$OUT" $cells
}
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
