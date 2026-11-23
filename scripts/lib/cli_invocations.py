"""CLI ledger B4b (ADR-075 decisions 1, 2, 7, 8, 10): one ``cli_invocation/1`` line per vnx call,
with the subcommand path, the flag names and placeholders, never an argument value."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actor_class
import permission_events as pe
import vnx_paths
from project_root import resolve_project_id

SCHEMA = "cli_invocation/1"
LEDGER = "cli_invocations.ndjson"
FIELDS = "schema ts invocation_id project_id vorm actor_class exit_code duration_ms".split()
#: bin/vnx main() case labels + vnx_cli/main.py subparsers (pinned by tests); any other word is a placeholder.
KNOWN_SUBCOMMANDS = tuple("""
activate add analyze-sessions approve attest backfill backfill-reason batch bootstrap-hooks bootstrap-skills
bootstrap-terminals check cleanup close config context-manifest cost-report costs daily-metrics decide-rule decide-steering
deliverable depend deps dismiss dispatch dispatch-agent dispatch-cleanup doctor done dream drift fabric-audit finish-worktree fireable gate
gate-check gate-profile gate-reanchor grounding-shadow guard handoff help history horizon impact inbox init init-db
insights install install-check install-git-hooks install-scheduler install-shell-helper install-validate
intelligence-export intelligence-import issue jump learning link-pr list list-projects main-window mandate mark-ready
memory merge-preflight migrate missing-reasons morning-round new new-worktree objective observe oi-close override package-check park
patch-agent-files pause plan-gate plan-scope pool pr-ready preflight promote providers ps publish queue queue-status
quiesce-check reblock reconcile reconcile-review reconcile-streak recover regen-settings regen-worker-permissions
register release remove-dependency reopen replay report restart restore resume retarget review revoke
rework-attribution roadmap role run seed set set-autonomous-promotion set-goal set-horizon set-lane-hint
set-plan-scope setup show sign skill-refine skill-review skills slot smoke snapshot staging-list start status steering
stop subsystems suggest sync tagger-ab track uninstall-git-hooks uninstall-scheduler undepend unlink-pr unmark-delivery unpark
unregister update verify verify-pr version withdraw worktree-refresh worktree-release worktree-start worktree-status
worktree-stop write""".split())
_KNOWN = frozenset(KNOWN_SUBCOMMANDS)
_PLACEHOLDERS = frozenset("<dlv> <dispatch> <pr> <sha> <n> <tmp> <scratchpad> <vnx-data>".split())
_FLAG_NAME = re.compile(r"--?[A-Za-z][A-Za-z0-9_-]{0,40}")


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
            path_open, form = False, pe.normalise(token)  # a decision 4 placeholder, else <arg>
            words.append(form if form in _PLACEHOLDERS else "<arg>")
    return " ".join(words + sorted(flags))


def _store(project_id: str, state_dir: Optional[Path] = None) -> Path:
    state = pe._state_dir(project_id, state_dir or Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"]))  # the call's
    if os.environ.get("PYTEST_CURRENT_TEST") and Path(tempfile.gettempdir()).resolve() not in state.resolve().parents:
        raise ValueError("a test run writes only a store under the temp dir, never the real store")
    return state


def note_failure(stage: str, exc: BaseException, state_dir: Optional[Path] = None) -> None:
    """A lost line leaves ``error`` in B4b (decision 8), with the class name only (decision 10); never raises."""
    try:
        project_id, reason = resolve_project_id(), {"record": "record failed: ", "launch": "launch failed: "}[stage]
        pe.write_coverage(project_id, "B4b", state="error", files_read=0, files_unreadable=1, window="all",
                          unreadable_reason=reason + type(exc).__name__, state_dir=_store(project_id, state_dir))
    except Exception:  # vnx-silent-except: last resort, a broken trace never changes the command (decision 7)
        pass


def record(argv: Sequence[str], exit_code: Optional[int], duration_ms: Optional[int],
           env: Optional[Mapping[str, str]] = None, isatty: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """Write one line; never raises (decision 7): on any failure the line stays away and B4b says error."""
    state = None
    try:
        project_id = resolve_project_id()
        line = {"schema": SCHEMA, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "invocation_id": uuid.uuid4().hex[:16], "project_id": project_id, "vorm": build_vorm(list(argv)),
                "actor_class": actor_class.derive(env, isatty), "exit_code": exit_code if isinstance(exit_code, int)
                else None, "duration_ms": duration_ms if isinstance(duration_ms, int) and duration_ms >= 0 else None}
        state = _store(project_id)
        with pe._locked(state):
            pe._append_all(state / LEDGER, (json.dumps(line, sort_keys=True) + "\n").encode("utf-8"))
        return line
    except Exception as exc:  # decision 7, the hook never changes the command; no line is written
        if state is not None:  # no project id or no store resolved: there is no place for the trace either
            note_failure("record", exc, state)
        return None


def _later(raw: str, since: Any) -> bool:
    try:
        return datetime.fromisoformat(json.loads(raw)["ts"]) > datetime.fromisoformat(since)
    except (ValueError, KeyError, TypeError):  # vnx-silent-except: no readable later ts is no proof, the error stays
        return False


def coverage(project_id: str, state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Coverage of B4b (decision 8): ``ok`` with the line count only for a readable ledger,
    ``unknown`` with a reason when it is missing or unreadable, never a 0."""
    state = pe._state_dir(project_id, state_dir)
    prior = pe._load_coverage(state / "permission_coverage.json")[0].get("B4b") or {}  # a record/launch error stays
    failed = prior.get("state") == "error" and str(prior.get("unreadable_reason"))[:14] in ("record failed:",
                                                                                           "launch failed:")
    try:
        lines = [raw for raw in (state / LEDGER).read_text(encoding="utf-8").splitlines() if raw.strip()]
    except (OSError, UnicodeDecodeError) as exc:
        if failed:  # until the ledger holds a line written after it
            return prior
        missing = isinstance(exc, FileNotFoundError)
        return pe.write_coverage(project_id, "B4b", state="unknown", files_read=0, window="all",
                                 files_unreadable=0 if missing else 1, state_dir=state_dir,
                                 unreadable_reason=f"{LEDGER} missing" if missing else type(exc).__name__)
    if failed and not any(_later(raw, prior.get("updated")) for raw in lines):
        return prior
    return pe.write_coverage(project_id, "B4b", state="ok", files_read=1, window="all", count=len(lines),
                             state_dir=state_dir)


def _cli(argv: List[str]) -> int:
    """``record <exit_code> <duration_ms|-> <tty 0|1> -- <argv...>``, started detached by the hooks."""
    if argv[:1] == ["record"] and argv[4:5] == ["--"]:
        record(argv[5:], int(argv[1]) if argv[1].lstrip("-").isdigit() else None,
               int(argv[2]) if argv[2].isdigit() else None, isatty=argv[3] == "1")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
