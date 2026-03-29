#!/usr/bin/env python3
"""
load-dispatch — Worker-side dispatch bundle loader.

Usage
-----
  python load_dispatch.py --dispatch-id <id> [--state-dir <dir>] [--dispatch-dir <dir>]
  python load_dispatch.py --dispatch-id <id> --show-prompt
  python load_dispatch.py --dispatch-id <id> --show-bundle
  python load_dispatch.py --dispatch-id <id> --json

Worker terminals invoke this when they receive a `load-dispatch <id>`
control command. It reads the dispatch bundle from disk and prints the
skill activation line and full prompt so the worker can execute the task.

Exit codes:
  0   Bundle found and printed
  1   Bundle not found or parse error
  2   Bad arguments
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Defaults derived from environment (mirrors VNX runtime conventions)
# ---------------------------------------------------------------------------

def _default_state_dir() -> str:
    vnx_data = os.environ.get("VNX_DATA_DIR") or os.environ.get("VNX_STATE_DIR")
    if vnx_data:
        return str(Path(vnx_data) / "state")
    # Walk up from this file to find .vnx-data/state relative to VNX_HOME
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".vnx-data" / "state"
        if candidate.exists():
            return str(candidate)
    return ".vnx-data/state"


def _default_dispatch_dir() -> str:
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        return str(Path(vnx_data) / "dispatches")
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".vnx-data" / "dispatches"
        if candidate.exists():
            return str(candidate)
    return ".vnx-data/dispatches"


# ---------------------------------------------------------------------------
# Bundle loading
# ---------------------------------------------------------------------------

def load_bundle(dispatch_id: str, dispatch_dir: str | Path) -> dict:
    """Load and return parsed bundle.json for dispatch_id.

    Raises:
        FileNotFoundError: If bundle directory or bundle.json doesn't exist.
        ValueError:        If bundle.json is not valid JSON.
    """
    bundle_dir = Path(dispatch_dir) / dispatch_id
    bundle_path = bundle_dir / "bundle.json"
    prompt_path = bundle_dir / "prompt.txt"

    if not bundle_dir.exists():
        raise FileNotFoundError(
            f"Dispatch bundle directory not found: {bundle_dir}\n"
            f"Dispatch ID: {dispatch_id!r}"
        )

    if not bundle_path.exists():
        raise FileNotFoundError(
            f"bundle.json not found for dispatch {dispatch_id!r}: {bundle_path}"
        )

    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"bundle.json is not valid JSON for dispatch {dispatch_id!r}: {exc}") from exc

    # Attach prompt text if available
    if prompt_path.exists():
        bundle["_prompt"] = prompt_path.read_text(encoding="utf-8")
    else:
        bundle["_prompt"] = ""

    return bundle


def format_worker_output(bundle: dict) -> str:
    """Return the text a worker terminal should process.

    Format:
        /<skill-command>
        <prompt text>

    The skill_command is derived from the bundle's target_profile or
    track field. If no skill is specified the prompt is returned alone.
    """
    lines: list[str] = []

    # Determine skill command from bundle metadata
    target_profile = bundle.get("target_profile") or {}
    skill = target_profile.get("skill") or target_profile.get("skill_command")
    if not skill:
        # Derive from track: A->T1, B->T2, C->T3; skill embedded in bundle metadata
        metadata = bundle.get("metadata") or {}
        skill = metadata.get("skill") or metadata.get("skill_command")

    if skill and not skill.startswith("/"):
        skill = f"/{skill}"

    if skill:
        lines.append(skill)

    prompt = bundle.get("_prompt", "").strip()
    if prompt:
        lines.append(prompt)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="load-dispatch",
        description="Worker-side dispatch bundle loader for VNX terminals.",
    )
    p.add_argument(
        "--dispatch-id",
        required=True,
        metavar="ID",
        help="Dispatch ID to load (e.g. 20260329-112549-foo-B)",
    )
    p.add_argument(
        "--state-dir",
        default=None,
        metavar="DIR",
        help="Path to .vnx-data/state/ (default: auto-detected)",
    )
    p.add_argument(
        "--dispatch-dir",
        default=None,
        metavar="DIR",
        help="Path to .vnx-data/dispatches/ (default: auto-detected)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print only the raw prompt text (no skill line)",
    )
    mode.add_argument(
        "--show-bundle",
        action="store_true",
        help="Print bundle.json metadata (no prompt text)",
    )
    mode.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print full bundle + prompt as JSON",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    state_dir = args.state_dir or _default_state_dir()
    dispatch_dir = args.dispatch_dir or _default_dispatch_dir()

    try:
        bundle = load_bundle(args.dispatch_id, dispatch_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(bundle, indent=2))
        return 0

    if args.show_bundle:
        # Print metadata only (strip internal _prompt key)
        display = {k: v for k, v in bundle.items() if k != "_prompt"}
        print(json.dumps(display, indent=2))
        return 0

    if args.show_prompt:
        print(bundle.get("_prompt", "").strip())
        return 0

    # Default: print worker-ready output (skill line + prompt)
    output = format_worker_output(bundle)
    if output:
        print(output)
    else:
        print(f"# Dispatch bundle loaded: {args.dispatch_id}", file=sys.stderr)
        print(f"# Bundle dir: {Path(dispatch_dir) / args.dispatch_id}", file=sys.stderr)
        print("WARNING: Bundle has no prompt or skill command.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
