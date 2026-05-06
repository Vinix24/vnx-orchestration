#!/usr/bin/env python3
"""vnx status CLI dashboard — W-UX-3.

Reads .vnx-data/strategy/current_state.md (strategic) +
.vnx-data/state/t0_state.json (live terminals/queues).

Outputs a 1-screen human-readable dashboard or --json for scripting.
Read-only: no writes to .vnx-data/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from project_root import resolve_data_dir  # noqa: E402


# ── Color helpers ──────────────────────────────────────────────────────

_CODES: dict[str, str] = {
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


def _use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR", "")


def _c(code: str, text: str) -> str:
    if not _use_color():
        return text
    return f"{_CODES.get(code, '')}{text}{_CODES['reset']}"


def _header(title: str) -> None:
    print(_c("bold", _c("cyan", f"\n── {title} ")))


# ── Parsing current_state.md ──────────────────────────────────────────

def _parse_current_state(strategy_dir: Path) -> dict:
    """Parse sections from current_state.md into structured data."""
    cs_file = strategy_dir / "current_state.md"
    if not cs_file.exists():
        return {}

    text = cs_file.read_text()
    result: dict = {
        "focus": "",
        "waves": [],
        "prs": [],
        "decisions": [],
    }

    m = re.search(r"^\*\*Focus\*\*:\s*(.+)$", text, re.MULTILINE)
    if m:
        result["focus"] = m.group(1).strip()

    current_section: str | None = None
    for line in text.splitlines():
        if line.startswith("## Roadmap Waves"):
            current_section = "waves"
        elif line.startswith("## Open Pull Requests"):
            current_section = "prs"
        elif line.startswith("## Recent Decisions"):
            current_section = "decisions"
        elif line.startswith("## "):
            current_section = None
        elif current_section == "waves" and line.startswith("- ["):
            m2 = re.match(r"^- \[(.)\] `([^`]+)`:\s*(.+)$", line)
            if m2:
                badge, wid, title = m2.groups()
                result["waves"].append({
                    "badge": badge,
                    "id": wid,
                    "title": title.strip(),
                })
        elif current_section == "prs" and line.startswith("- PR #"):
            result["prs"].append(line[2:].strip())
        elif current_section == "decisions" and line.startswith("- "):
            result["decisions"].append(line[2:].strip())

    return result


# ── Loading t0_state.json ─────────────────────────────────────────────

def _load_t0_state(state_dir: Path) -> dict:
    f = state_dir / "t0_state.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


# ── Dashboard sections ────────────────────────────────────────────────

def _active_waves(waves: list[dict], n: int = 3) -> list[dict]:
    """Return up to n in-progress/blocked waves, falling back to first n."""
    active = [w for w in waves if w["badge"] in ("~", "!")]
    return (active or waves)[:n]


def _print_focus(cs: dict) -> None:
    _header("Current Focus")
    focus = cs.get("focus", "")
    print(f"  {_c('bold', focus) if focus else '(no focus data)'}")


def _print_waves(cs: dict) -> None:
    _header("Active Waves  (top 3)")
    waves = _active_waves(cs.get("waves", []))
    if not waves:
        print("  (no wave data)")
        return
    for w in waves:
        b = w["badge"]
        color = "yellow" if b == "~" else "red" if b == "!" else "dim"
        print(f"  [{_c(color, b)}] {_c('bold', w['id'])}: {w['title']}")


def _print_prs(cs: dict) -> None:
    _header("Open PRs  (top 3)")
    prs = cs.get("prs", [])[:3]
    if not prs:
        print("  (no open PRs found in current_state.md)")
        return
    for pr in prs:
        print(f"  {_c('dim', chr(8226))} {pr}")


def _print_terminals(t0: dict) -> None:
    _header("Terminal Status")
    terminals = t0.get("terminals", {})
    if not terminals:
        print("  (no terminal data — run vnx start first)")
        return
    for tid in sorted(terminals):
        t = terminals[tid]
        status = t.get("status", "unknown")
        lease = t.get("lease_state", "idle")
        track = t.get("track", "?")
        dispatch = t.get("current_dispatch") or "—"
        color = "green" if status == "idle" else "yellow" if status == "busy" else "dim"
        print(
            f"  {_c('bold', tid)} [{track}] "
            f"{_c(color, status)}/{lease}  "
            f"{_c('dim', str(dispatch)[:45])}"
        )


def _print_decisions(cs: dict) -> None:
    _header("Recent Decisions  (last 3)")
    decisions = cs.get("decisions", [])[:3]
    if not decisions:
        print("  (no decisions found in current_state.md)")
        return
    for d in decisions:
        print(f"  {_c('dim', chr(8226))} {d}")


# ── JSON output ───────────────────────────────────────────────────────

def _build_json_output(cs: dict, t0: dict) -> dict:
    return {
        "schema": "vnx_status/1.0",
        "focus": cs.get("focus", ""),
        "active_waves": _active_waves(cs.get("waves", [])),
        "open_prs": cs.get("prs", [])[:3],
        "terminals": t0.get("terminals", {}),
        "recent_decisions": cs.get("decisions", [])[:3],
        "queues": t0.get("queues", {}),
        "strategy_available": bool(cs),
        "t0_state_available": bool(t0),
    }


# ── Entry point ───────────────────────────────────────────────────────

def main(argv: list[str] | None = None, data_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vnx status",
        description="VNX operator status dashboard (W-UX-3)",
    )
    parser.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit parseable JSON (stable schema: vnx_status/1.0)",
    )
    args, _extra = parser.parse_known_args(argv)

    if data_dir is None:
        data_dir = resolve_data_dir(__file__)

    strategy_dir = data_dir / "strategy"
    state_dir = data_dir / "state"

    strategy_ok = strategy_dir.exists()
    t0_ok = (state_dir / "t0_state.json").exists()

    if not strategy_ok and not t0_ok:
        if args.emit_json:
            print(json.dumps({
                "schema": "vnx_status/1.0",
                "error": "not_initialised",
                "strategy_available": False,
                "t0_state_available": False,
            }))
        else:
            print("vnx status: not initialised — run 'vnx init' to set up .vnx-data/")
        return 0

    cs = _parse_current_state(strategy_dir) if strategy_ok else {}
    t0 = _load_t0_state(state_dir) if t0_ok else {}

    if args.emit_json:
        print(json.dumps(_build_json_output(cs, t0), indent=2))
        return 0

    if not strategy_ok:
        print(
            f"{_c('yellow', 'warn')} strategy/ missing — "
            "run: python3 scripts/build_current_state.py"
        )
    if not t0_ok:
        print(
            f"{_c('yellow', 'warn')} t0_state.json missing — "
            "run: python3 scripts/build_t0_state.py"
        )

    _print_focus(cs)
    _print_waves(cs)
    _print_prs(cs)
    _print_terminals(t0)
    _print_decisions(cs)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
