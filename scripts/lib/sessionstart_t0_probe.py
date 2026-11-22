#!/usr/bin/env python3
"""The two live reads of the T0 SessionStart digest that must run in the hook's own process.

``hooks/sessionstart.sh`` calls this once (one interpreter start) and prints what it returns:

* ``role_line``: empty for a session classified ``t0`` by ``actor_class.derive()``; a loud
  ``T0 ROLE MISSING`` line for any other class; a "could not be checked" line when the derive fails.
  The check belongs here and not in ``build_t0_state.py``: the builder runs detached (SS-2) and its
  environment is not the session's.
* ``inbox_text``: the operator-inbox lines from ``operator_inbox_summary.json`` through
  ``operator_queue.read_inbox_summary`` (current after every ledger write), never from the index.

Read-only. Runs no ``vnx inbox`` command and writes no ``presented`` line. Never raises: every
failure becomes a visible line, never an empty result.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_ROLE_FIX = (
    "Restart from the operator shell with CLAUDE_ROLE=orchestrator set "
    "(the rotation spawner and vnx start set it; a bare claude --resume does not)."
)


def role_line() -> str:
    try:
        import actor_class

        cls = actor_class.derive()
    except Exception as exc:  # vnx-silent-except: reported as a visible line, never as silence
        return (f"T0 ROLE UNCHECKED: the actor class of this session could not be determined "
                f"({type(exc).__name__}: {exc}); promote, grant and the worker skip may refuse it. {_ROLE_FIX}")[:500]
    if cls == actor_class.T0:
        return ""
    return (f"T0 ROLE MISSING: this session runs without CLAUDE_ROLE=orchestrator and is classified '{cls}'; "
            f"promote, grant and the worker skip will refuse it. {_ROLE_FIX}")


def _unknown(reason: str, extra: str = "") -> str:
    return f"operator inbox: could not be read ({reason}); NOT the same as no open questions{extra}"


def _age(seconds: int) -> str:
    # Same format as operator_queue_inbox._age; that module pulls in the whole scripts package, too heavy here.
    days, rest = divmod(max(seconds, 0), 86400)
    return f"{days}d {rest // 3600}h" if days else f"{rest // 3600}h {rest % 3600 // 60}m"


def inbox_text(state_dir: str) -> str:
    if not state_dir:
        return _unknown("state dir unresolved")
    try:
        import operator_queue as oq
        from vnx_paths import project_id_from_state_dir

        # Same derivation as build_t0_state.py: the state dir first, then the ambient id.
        project_id = project_id_from_state_dir(Path(state_dir)) or os.environ.get("VNX_PROJECT_ID", "").strip()
        if not project_id:
            return _unknown("project id not derivable from the state dir")
        got = oq.read_inbox_summary(project_id, state_dir=Path(state_dir))
        summary = got["summary"]
        if got["freshness"] != "current" or summary is None:
            return _unknown(f"{got['freshness']}: {got['reason'] or 'no reason given'}")
        if summary["status"] != "ok":
            return _unknown(f"unknown: {summary['reason'] or 'no reason given'}")
        if summary["open_total"] == 0:
            return ""
        now = datetime.now(timezone.utc)
        lines = [f"operator inbox: {summary['open_total']} open (+{summary['more']} more), as of {summary['generated_at']}"]
        for p in summary["points"]:
            age = _age(int((now - oq.parse_ts(p["asked_at"])).total_seconds()))
            lines.append(f"  [{age}, presented {p['presented']}x] {p['vraag']}")
            if p.get("kopieerblok"):
                lines.append(f"    {p['kopieerblok']}")
        return "\n".join(lines)
    except Exception as exc:  # vnx-silent-except: reported as a visible line, never as silence
        return _unknown(f"{type(exc).__name__}: {exc}")[:500]


def probe(state_dir: str) -> Dict[str, Any]:
    return {"role_line": role_line(), "inbox_text": inbox_text(state_dir)}


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: sessionstart_t0_probe.py <state-dir>", file=sys.stderr)
        return 2
    print(json.dumps(probe(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
