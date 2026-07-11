#!/usr/bin/env python3
"""dispatch_sidedoor_audit.py — reproducible exhaustiveness gate for PR-12.

The single-entry flip (PR-11) is only safe if EVERY dispatch-delivery path routes through
the door. "We wired the N callers" is an assertion; this is the proof (review finding:
exhaustiveness must be verified, not asserted). It scans for files that invoke a lane script
as a DELIVERY path and reports any that are not on the audited allowlist — so a NEW direct
caller trips the gate before the flag is ever flipped.

It already earned its keep: the static scan caught `dispatch-agent.sh` and `dispatch.sh` as
delivery callers the bridge docstring's "4 callers" missed.

Classifier notes (why it is reproducible, not hand-waved):
  * lines inside docstrings/comments are skipped (triple-quote tracking) — a module that only
    *mentions* a lane in its docstring is not a caller (that was the over-flag in v1).
  * the door (`dispatch_cli.py`) and the lane scripts themselves are excluded — they reference
    the lanes legitimately; the bridge is the SANCTIONED path.
  * benchmarks + provider-spawn machinery are excluded — test harnesses / provider_dispatch's
    own internals, not production delivery side-doors.

Run standalone:  python3 scripts/lib/dispatch_sidedoor_audit.py   (exit 1 if a new caller appears)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Set

_LANES = ("subprocess_dispatch", "provider_dispatch", "tmux_interactive_dispatch")

# NOTE ON REACH: these patterns catch literal lane-script filename references in spawn/exec
# contexts and direct delivery-function calls. A FULLY dynamic construction (e.g. a lane name
# assembled from fragments, or an `importlib.import_module` / `__import__` caller) is out of
# static-regex reach by design. The runtime pretooluse spawn-guard hooks are the enforcing
# backstop for those; this static scan is the audit-surface ledger for the reachable shapes.
#
# Real DELIVERY invocations (not a docstring mention): the lane script named in a spawn/exec
# context, or a delivery-function CALL. Comment/docstring lines are skipped before matching.
_DELIVERY_PATTERNS = [
    re.compile(r"(subprocess_dispatch|provider_dispatch|tmux_interactive_dispatch)\.py"),
    re.compile(r"\bdeliver_with_recovery\s*\("),
    re.compile(r"\b[A-Za-z_]*(provider_dispatch|pd)\.main\s*\("),
]

# G5: the raw receipt-bypass primitive. A NON-INTERACTIVE claude spawn (`claude -p` /
# `claude --print`) delivers work WITHOUT a governance receipt. Interactive `claude` (the
# sanctioned subscription lane) is NOT matched, and provider_mix lists like ["claude","claude"]
# carry no -p/--print flag so they are NOT matched. The flag need NOT sit adjacent to `claude`:
# `["claude", "--model", m, "--print", p]` and `claude --model opus --print` are caught too
# (a trivially-reordered argv must not evade an exhaustiveness gate — codex G5-2). Patterns run on
# the file's code-only text (docstrings/comments stripped) so multi-line argv lists match.
# NOTE ON REACH: these patterns catch literal argv lists, shell command lines, and the split
# binary/args config idiom below. A FULLY dynamic construction (e.g. a binary name assembled from
# fragments, or flags read from an untyped external source) is out of static-regex reach — the
# runtime pretooluse hooks (`scripts/hooks/pretooluse_block_raw_claude_spawn.sh`) are the enforcing
# backstop for those; this static scan is the audit-surface ledger for the reachable shapes.
_RAW_CLAUDE_PATTERNS = [
    # Python argv list containing a "claude" element AND a standalone "-p"/"--print" element before
    # the list closes — claude need NOT be first (a `["timeout","3s","claude","-p"]` wrapper is caught
    # too — codex G5-8). `[^\]]*?` spans newlines but never crosses `]`, so both tokens belong to the
    # SAME list, and the flag must be a whole quoted token (so "--print-config" or a -p inside a string
    # value does NOT match, and a bare ["claude"] provider_mix with no flag does NOT match).
    re.compile(r"""\[[^\]]*?["']claude["'][^\]]*?["'](?:-p|--print)["']"""),
    # Shell: `claude` followed by a -p/--print flag anywhere later on the same command line.
    re.compile(r"\bclaude\b[^\n]*?\s(?:-p|--print)\b"),
    # Split binary/args construction: a CLI-config dict `"binary": "claude"` whose argv is assembled
    # elsewhere (headless_adapter builds `cmd = [binary] + args` with args carrying --print). A literal
    # argv/shell regex can't see the assembled command, so match the config idiom directly (codex G5-6).
    re.compile(r"""["']binary["']\s*:\s*["']claude["']"""),
]

# Audited NON-delivery raw-claude callers: haiku/deepseek classifiers, weekly digest, headless
# T0 orchestration, conversation analysis, benchmark/replay harnesses. A scanned file outside
# this set that spawns `claude -p` is a NEW receipt-bypass side door and fails the gate until
# audited here (or routed via a governed lane).
KNOWN_RAW_CLAUDE_CALLERS = frozenset({
    "scripts/conversation_analyzer/deep_analyzer.py",
    "scripts/f39/replay_harness/single_replay.py",
    "scripts/headless_trigger.py",
    "scripts/lib/classifier_providers/deepseek_provider.py",
    "scripts/lib/classifier_providers/haiku_provider.py",
    "scripts/lib/llm_decision_router.py",
    "scripts/lib/report_classifier.py",
    "scripts/lib/headless_adapter.py",             # the GOVERNED headless lane's spawner (binary=claude + --print args) — receipt-producing, NOT a bypass
    "scripts/lib/subprocess_adapter.py",           # the GOVERNED subprocess lane's spawner — emits receipts, NOT a bypass
    "scripts/lib/t0_decision_summarizer.py",       # haiku T0 decision-log summarizer (non-delivery)
    "scripts/llm_benchmark.py",
    "scripts/weekly_digest.py",
    # benchmark harnesses — spawn claude to BENCHMARK it, not to deliver governed work (codex G5-3):
    "scripts/benchmark/judge_quality.py",
    "scripts/benchmark/field-tests/runners/scorer.py",
    "scripts/benchmark/field-tests/runners/harvest_t6_reviews.py",
    # audited MENTION-only (the pattern trips on prose/keyword text, not an executable spawn) — kept
    # in the allowlist rather than line-suppressed, so a REAL spawn added here still matches (codex G5-8):
    "scripts/commands/dispatch.sh",                # burst-lane help text mentions "(claude -p)"; delivery already audited
    "scripts/lib/vnx_tag_vocabulary.py",           # "claude -p" appears as a keyword-taxonomy string literal
})

_EXCLUDE_SUBSTR = (
    "/subprocess_dispatch_internals/",  # the lane's own internals
    "/dispatch_bridge.py",              # the SANCTIONED door bridge
    "/dispatch_cli.py",                 # the door — calls the lanes legitimately
    "/dispatch_sidedoor_audit.py",      # this auditor
    "/__pycache__/",
    "/hooks/pretooluse_",               # guard hooks that BLOCK raw spawns (enforcement)
    "/lane_adapter.py",                 # benchmark adapter
    "/benchmark/", "/benchmarks/",      # benchmark harnesses test the lanes directly
    "/provider_spawns/", "/providers/", # provider_dispatch's own spawn machinery
)
_EXCLUDE_BASENAMES = {f"{n}.py" for n in _LANES}

# G5: the raw scan does NOT inherit the delivery `_EXCLUDE_SUBSTR` — that hides benchmark harnesses
# and lane machinery, but a raw `claude -p` spawn ANYWHERE is worth auditing for exhaustiveness
# (codex G5-3: silent /benchmark/ inheritance suppressed 3 real spawns). The raw scan excludes ONLY
# non-spawns: compiled caches, the guard hooks that DETECT the primitive (they hold the pattern but
# never spawn), and this auditor itself. Every real spawn — production auxiliary, benchmark harness,
# sanctioned lane spawner — is AUDITED in KNOWN_RAW_CLAUDE_CALLERS instead of hidden.
_RAW_SCAN_EXCLUDE_SUBSTR = (
    "/__pycache__/",
    "/hooks/pretooluse_",           # guards that DETECT raw spawns (hold the pattern, do not spawn)
    "/dispatch_sidedoor_audit.py",  # this auditor (holds the patterns as source)
    "/process_cleanup.py",          # process-hygiene scanner: holds claude -p/--print as DETECTION patterns to hunt forbidden spawns, never spawns (#1029)
)

# Audited delivery callers (PR-2). A scanned file outside this set is a NEW side door and
# fails the gate until it is audited (added here) and wired through dispatch_bridge.
KNOWN_DELIVERY_CALLERS = frozenset({
    "scripts/lib/dispatch_deliver.sh",
    "scripts/lib/pool_worker_runner.py",
    "scripts/lib/headless_dispatch_daemon.py",
    "scripts/lib/adapters/claude_adapter.py",
    "scripts/commands/dispatch-agent.sh",     # caught by the scan (not in the docstring's "4")
    "scripts/commands/dispatch.sh",           # caught by the scan (not in the docstring's "4")
    "scripts/lib/plan_gate_panel.py",         # interim side door PR-7 removes
    "vnx_cli/commands/dispatch_agent.py",     # packaged CLI; now routes through deliver_via_door (flip-PR F3)
})


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _code_lines(text: str):
    """Yield code lines, skipping comment lines and lines inside triple-quoted blocks."""
    in_doc = False
    quote = ""
    for line in text.splitlines():
        s = line.strip()
        if in_doc:
            if quote in line:
                in_doc = False
            continue
        if s.startswith("#"):
            continue
        # skip one-line docstrings as well as opening docstrings
        is_doc = False
        for q in ('"""', "'''"):
            if s.startswith(q):
                if line.count(q) == 1:
                    in_doc, quote = True, q
                is_doc = True
                break
        if is_doc:
            continue
        yield line


def scan_delivery_callers(root: Path | None = None) -> Set[str]:
    """Return repo-relative paths of files that invoke a lane script as a delivery path."""
    root = root or _repo_root()
    callers: Set[str] = set()
    for base in ("scripts", "bin", "vnx_cli"):  # vnx_cli: the packaged CLI ships dispatch entrypoints too (codex flip-PR F3)
        for path in (root / base).rglob("*"):
            if not path.is_file() or path.suffix not in (".py", ".sh"):
                continue
            rel = path.relative_to(root).as_posix()
            if any(s in f"/{rel}" for s in _EXCLUDE_SUBSTR) or path.name in _EXCLUDE_BASENAMES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in _code_lines(text):
                if any(p.search(line) for p in _DELIVERY_PATTERNS):
                    callers.add(rel)
                    break
    return callers


def _raw_scannable(path: Path) -> bool:
    """A file is scannable if it is a .py/.sh source OR an extensionless shebang script (e.g. bin/vnx
    — an entry point that can spawn claude too; codex G5-7 flagged the extensionless blind spot)."""
    if path.suffix in (".py", ".sh"):
        return True
    if path.suffix == "":
        try:
            with path.open("rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False
    return False


def scan_raw_claude_spawns(root: Path | None = None) -> Set[str]:
    """Return repo-relative paths of files that spawn a raw `claude -p`/`--print` (receipt bypass)."""
    root = root or _repo_root()
    callers: Set[str] = set()
    for base in ("scripts", "bin", "vnx_cli"):
        for path in (root / base).rglob("*"):
            if not path.is_file() or not _raw_scannable(path):
                continue
            rel = path.relative_to(root).as_posix()
            # NB: unlike the delivery scan, the raw scan does NOT skip lane-script basenames — a raw
            # `claude -p` added to a lane script must still be audited, not hidden (codex G5-4).
            if any(s in f"/{rel}" for s in _RAW_SCAN_EXCLUDE_SUBSTR):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Join code-only lines so a multi-line argv list is matchable as one blob; the argv
            # pattern is bounded by `]` and the shell pattern by `\n`, so joining is safe. Files that
            # only MENTION the primitive (a "claude -p" keyword literal, help text) still match — they
            # are AUDITED in KNOWN_RAW_CLAUDE_CALLERS, not line-suppressed: suppressing a quoted
            # "claude -p" would also hide a real `subprocess.run("claude -p", shell=True)` (codex G5-8).
            code_text = "\n".join(_code_lines(text))
            if any(p.search(code_text) for p in _RAW_CLAUDE_PATTERNS):
                callers.add(rel)
    return callers


def audit(root: Path | None = None) -> dict:
    """Return delivery-caller and raw-claude spawn scan results.

    `unaudited` or `raw_claude_unaudited` non-empty = a new side door = gate fails.
    """
    found = scan_delivery_callers(root)
    raw = scan_raw_claude_spawns(root)
    return {
        "known": set(KNOWN_DELIVERY_CALLERS),
        "found": found,
        "unaudited": found - KNOWN_DELIVERY_CALLERS,
        "raw_claude_known": set(KNOWN_RAW_CLAUDE_CALLERS),
        "raw_claude_found": raw,
        "raw_claude_unaudited": raw - KNOWN_RAW_CLAUDE_CALLERS,
    }


def main() -> int:
    result = audit()
    print(f"delivery callers found: {len(result['found'])}")
    for c in sorted(result["found"]):
        flag = "  [UNAUDITED — new side door]" if c in result["unaudited"] else ""
        print(f"  {c}{flag}")

    print(f"\nraw claude -p/--print spawns found: {len(result['raw_claude_found'])}")
    for c in sorted(result["raw_claude_found"]):
        flag = "  [UNAUDITED — new receipt-bypass side door]" if c in result["raw_claude_unaudited"] else ""
        print(f"  {c}{flag}")

    fail = False
    if result["unaudited"]:
        print(f"\nFAIL: {len(result['unaudited'])} unaudited delivery caller(s); audit + wire "
              "through dispatch_bridge before flipping VNX_SINGLE_ENTRY_DISPATCH.", file=sys.stderr)
        fail = True
    if result["raw_claude_unaudited"]:
        print(f"\nFAIL: {len(result['raw_claude_unaudited'])} unaudited raw claude -p/--print spawn(s); "
              "route via a governed lane (provider_dispatch) or audit here.", file=sys.stderr)
        fail = True
    if fail:
        return 1
    print("\nOK: no unaudited delivery callers and no unaudited raw claude spawns — exhaustiveness holds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
