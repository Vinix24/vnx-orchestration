"""CLI ledger B4b (ADR-075 decisions 1, 2, 7, 8, 10): one ``cli_invocation/1`` line per vnx call,
with the subcommand path, the flag names and placeholders, never an argument value; and the
pairing of an ``asked`` event (B4a) with the operator call that followed it (``executed``)."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actor_class
import permission_events as pe
from project_root import resolve_project_id

SCHEMA = "cli_invocation/1"
LEDGER = "cli_invocations.ndjson"
FIELDS = "schema ts invocation_id project_id vorm actor_class exit_code duration_ms".split()
#: Subcommand names of the ``main()`` case labels in bin/vnx and the vnx_cli/main.py subparsers;
#: pinned by tests/test_cli_invocations.py. Any other word is a positional and becomes a placeholder.
KNOWN_SUBCOMMANDS = tuple("""
activate add analyze-sessions approve attest backfill backfill-reason batch bootstrap-hooks bootstrap-skills
bootstrap-terminals check cleanup close context-manifest cost-report costs daily-metrics decide-rule
decide-steering deliverable dismiss dispatch dispatch-agent dispatch-cleanup doctor done dream drift
fabric-audit finish-worktree gate gate-check gate-profile gate-reanchor grounding-shadow guard handoff help
history horizon impact inbox init init-db insights install install-check install-git-hooks install-scheduler
install-shell-helper install-validate intelligence-export intelligence-import issue jump learning link-pr list
list-projects main-window mandate mark-ready memory merge-preflight migrate missing-reasons new new-worktree
objective observe oi-close override package-check park patch-agent-files pause plan-gate pool pr-ready
preflight promote providers ps publish queue queue-status quiesce-check reblock reconcile reconcile-review
reconcile-streak recover regen-settings regen-worker-permissions register release remove-dependency reopen
replay report restart restore resume retarget review revoke rework-attribution roadmap role run seed set
set-autonomous-promotion set-goal set-horizon set-lane-hint set-plan-scope setup show sign skill-refine
skill-review skills slot smoke snapshot staging-list start status steering stop subsystems suggest sync
tagger-ab track uninstall-git-hooks uninstall-scheduler unlink-pr unmark-delivery unpark unregister update
verify verify-pr version withdraw worktree-refresh worktree-release worktree-start worktree-status worktree-stop
write""".split())
_KNOWN = frozenset(KNOWN_SUBCOMMANDS)
_PLACEHOLDERS = frozenset("<dlv> <dispatch> <pr> <sha> <n> <tmp> <scratchpad> <vnx-data>".split())
_FLAG_NAME = re.compile(r"--?[A-Za-z][A-Za-z0-9_-]{0,40}")


def _placeholder(token: str) -> str:
    """Decision 4 placeholder when the whole token is one, otherwise ``<arg>``."""
    form = pe.normalise(token)
    return form if form in _PLACEHOLDERS else "<arg>"


def build_vorm(argv: Sequence[str]) -> str:
    """``vnx`` + known subcommand path + placeholders for positionals + sorted flag names.
    A word right after a flag without ``=`` is read as that flag's value and dropped."""
    words, flags, path_open, after_flag = ["vnx"], set(), True, False
    for token in argv:
        if token == "--":
            path_open, after_flag = False, False
        elif token.startswith("-") and len(token) > 1:
            name = token.split("=", 1)[0]
            flags.add(name if _FLAG_NAME.fullmatch(name) else "<flag>")
            after_flag = "=" not in token
        elif path_open and token in _KNOWN:
            words.append(token)
            after_flag = False
        elif after_flag:
            after_flag = False
        else:
            path_open = False
            words.append(_placeholder(token))
    return " ".join(words + sorted(flags))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(argv: Sequence[str], exit_code: Optional[int], duration_ms: Optional[int],
           env: Optional[Mapping[str, str]] = None, isatty: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """Write one line; never raises (decision 7): on any failure the line stays away."""
    try:
        project_id = resolve_project_id()
        line = {
            "schema": SCHEMA, "ts": _now(), "invocation_id": uuid.uuid4().hex[:16], "project_id": project_id,
            "vorm": build_vorm(list(argv)), "actor_class": actor_class.derive(env, isatty),
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "duration_ms": duration_ms if isinstance(duration_ms, int) and duration_ms >= 0 else None,
        }
        state = pe._state_dir(project_id, None)
        scratch = Path(tempfile.gettempdir()).resolve()
        if os.environ.get("PYTEST_CURRENT_TEST") and scratch not in state.resolve().parents:
            return None  # a test run never writes the real store, only a store under the temp dir
        with pe._locked(state):
            pe._append_all(state / LEDGER, (json.dumps(line, sort_keys=True) + "\n").encode("utf-8"))
        return line
    except Exception:  # vnx-silent-except: decision 7, the hook never changes the command; no line is written
        return None


def _ts(value: Any) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return when if when.tzinfo else None  # a naive ts cannot be placed in the window
    except (AttributeError, TypeError, ValueError):  # vnx-silent-except: an unreadable ts never pairs
        return None


def command_key(vorm: Any) -> Optional[tuple]:
    """Subcommand path plus leading placeholders, without the binary and the flags; None if not vnx."""
    words = vorm.split() if isinstance(vorm, str) else []
    if not words or not (os.path.basename(words[0]) == "vnx" or words[0] == "~/..."):
        return None
    key: List[str] = []
    for word in words[1:]:
        if word.startswith("-"):
            break
        key.append(word if word in _KNOWN or word in _PLACEHOLDERS or not key else "<arg>")
    return tuple(key) if key and key[0] in _KNOWN else None


def _read_ndjson(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:  # vnx-silent-except: a damaged line is skipped, never paired
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def pair(project_id: str, state_dir: Optional[Path] = None, window_minutes: int = 30) -> Optional[int]:
    """Write one ``executed`` (B4b) per ``asked`` (B4a) with an operator call of the same command
    within the window; returns the pairs found, None when the CLI ledger is missing or unreadable."""
    state = pe._state_dir(project_id, state_dir)
    window = f"{window_minutes}m"
    try:
        calls = _read_ndjson(state / LEDGER)
    except (OSError, UnicodeDecodeError) as exc:
        missing = isinstance(exc, FileNotFoundError)
        pe.write_coverage(project_id, "B4b", state="unknown", files_read=0, window=window,
                          files_unreadable=0 if missing else 1,
                          unreadable_reason=f"{LEDGER} missing" if missing else type(exc).__name__,
                          state_dir=state_dir)
        return None
    events_path = state / "permission_events.ndjson"
    asked = [e for e in (_read_ndjson(events_path) if events_path.exists() else [])
             if e.get("bron") == "B4a" and e.get("besluit") == "asked" and _ts(e.get("ts"))]
    calls = [c for c in calls if c.get("project_id") == project_id and c.get("actor_class") == "operator"
             and _ts(c.get("ts")) and command_key(c.get("vorm"))]
    used: set = set()
    out = []
    for ask in sorted(asked, key=lambda e: _ts(e["ts"])):
        start, key = _ts(ask["ts"]), command_key(ask.get("vorm"))
        for call in sorted(calls, key=lambda c: _ts(c["ts"])):
            when = _ts(call["ts"])
            if (key and call.get("invocation_id") not in used and command_key(call["vorm"]) == key
                    and start <= when <= start + timedelta(minutes=window_minutes)):
                used.add(call.get("invocation_id"))
                out.append(pe.build_event(
                    project_id=project_id, session_id=ask.get("session_id") or "", ts=call["ts"],
                    account="unknown", origin="operator", bron="B4b", besluit="executed", command=call["vorm"],
                    reason_code=f"asked:{ask.get('event_id')}", dispatch_id=ask.get("dispatch_id") or None))
                break
    pe.write_events(out, project_id, state_dir)
    pe.write_coverage(project_id, "B4b", state="ok", files_read=1, window=window, count=len(out),
                      state_dir=state_dir)
    return len(out)


def _cli(argv: List[str]) -> int:
    """``record <exit_code> <duration_ms|-> <tty 0|1> -- <argv...>``, started detached by the hooks."""
    if argv[:1] == ["record"] and argv[4:5] == ["--"]:
        code = int(argv[1]) if argv[1].lstrip("-").isdigit() else None
        ms = int(argv[2]) if argv[2].isdigit() else None
        record(argv[5:], code, ms, isatty=argv[3] == "1")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
