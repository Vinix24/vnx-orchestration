"""dispatch_bridge.py — legacy-caller → single-entry door bridge (PR-12).

Four legacy callers (dispatch_deliver.sh, pool_worker_runner.py,
headless_dispatch_daemon.py, claude_adapter.py) historically delivered work
WITHOUT going through the door (dispatch_cli.run_dispatch). This module is the
ONE shared bridge that turns each caller's legacy inputs into a genuinely-staged
door spec-bundle, then drives it through run_dispatch — so every dispatch path
funnels through the same validate → snapshot → compile_plan → permit → execute
gate.

SECURITY (the surface codex reviews hardest): `stage_spec_bundle` is the FIRST
writer of a `dispatch-spec.json` bundle, so it is the trust boundary. It is
non-forgeable by construction:
  * staging_id is DERIVED from dispatch_id (never caller free-text) and validated
    against dispatch_spec._ID_RE BEFORE any path join — kills path traversal.
  * the data root is resolved via the SAME helper the door uses
    (dispatch_cli._resolve_data_dir) — bridge and door never disagree on root.
  * the pending root is anchored (must resolve inside data_dir) BEFORE writing —
    a pre-planted symlinked pending/ is refused at write time, not just at read.
  * instruction + spec are written as fresh regular files (atomic_io) inside the
    bundle dir; instruction_file is the literal absolute child path (no symlink).
  * instruction_sha256 is pre-bound over the exact written bytes, so the door's
    TOCTOU re-read matches.
The door re-checks all of this (P0-2 containment) — this is defense-in-depth on
both write and read, not a loosening of any check.

BILLING SAFETY: this module spawns nothing itself; it calls run_dispatch, which
owns lane selection (claude→tmux unless allow_headless). No Anthropic SDK import.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path, PurePosixPath
from typing import Optional

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from atomic_io import atomic_write_json, atomic_write_text  # noqa: E402
from dispatch_flags import single_entry_enabled  # noqa: E402
from dispatch_spec import _ID_RE, Provider  # noqa: E402

# Legacy provider/mode strings → the closed Provider enum value. dispatch_deliver.sh
# emits tmux-mode strings (e.g. "codex_cli"); normalize them here so the door's
# Provider() construction never rejects a legitimate dispatch.
_PROVIDER_ALIASES = {
    "claude": "claude",
    "claude_cli": "claude",
    # get_terminal_provider() (dispatcher_minimal.sh) emits the tmux-domain string 'claude_code'
    # for claude terminals (and as the default). Without this alias the door-flip provider
    # propagation would canonicalize 'claude_code' -> Provider('claude_code') -> ValueError ->
    # bridge REJECT on every claude subprocess-routed worker. Map it to the closed enum value.
    "claude_code": "claude",
    "codex": "codex",
    "codex_cli": "codex",
    "kimi": "kimi",
    "kimi_cli": "kimi",
    "gemini": "gemini",
    "gemini_cli": "gemini",
    "litellm:deepseek": "litellm:deepseek",
    # glm-via-harness-only: GLM ALWAYS runs via the claude-CLI harness (glm-harness), never the
    # plain single-shot litellm runner. Any legacy caller emitting litellm:zai / zai / glm is
    # normalized to glm-harness AT THE DOOR. The benchmark does NOT go through this bridge (it
    # dispatches via provider_dispatch directly), so its litellm:zai baselines are untouched.
    "litellm:zai": "glm-harness",
    "zai": "glm-harness",
    "glm": "glm-harness",
    "glm-harness": "glm-harness",
    "litellm:moonshot": "litellm:moonshot",
    "deepseek-harness": "deepseek-harness",
    "local-gemma": "local-gemma",
    "auto": "auto",
    "": "claude",
}


def _canonical_provider(raw: Optional[str]) -> Provider:
    """Map a legacy provider/mode string to a Provider enum member.

    Raises ValueError on an unknown string (caught by the caller → clean reject).
    """
    key = (raw or "").strip().lower()
    canonical = _PROVIDER_ALIASES.get(key, key)
    return Provider(canonical)


def _data_dir() -> Path:
    """Resolve the data root EXACTLY as the door does (no divergence)."""
    from dispatch_cli import _resolve_data_dir  # noqa: PLC0415
    return _resolve_data_dir()


def _project_id() -> str:
    from dispatch_cli import _resolve_project_id  # noqa: PLC0415
    return _resolve_project_id()


def stage_spec_bundle(
    *,
    instruction_text: str,
    dispatch_id: str,
    role: str,
    target_slot: str,
    project_id: Optional[str] = None,
    provider: str = "claude",
    model: Optional[str] = None,
    gate: str = "",
    dispatch_paths: tuple[dict, ...] = (),
    deadline_seconds: int = 3600,
    base_ref: str = "origin/main",
    target_id_override: Optional[str] = None,
    requires_mcp: bool = False,
    allow_headless: bool = False,
    headless_reason: Optional[str] = None,
    pr_id: Optional[str] = None,
    tags: tuple[str, ...] = (),
    data_dir: Optional[Path] = None,
) -> Path:
    """Write a non-forgeable staged spec bundle; return the dispatch-spec.json path.

    Bundle layout: <data_dir>/dispatches/pending/<dispatch_id>/{instruction.md,
    dispatch-spec.json}. The bundle is genuinely promoted (physically under
    pending/), so the door's D11 staging gate is satisfied honestly.
    """
    # 1. staging_id is DERIVED from dispatch_id and validated BEFORE any path join.
    if not _ID_RE.match(dispatch_id or ""):
        raise ValueError(
            f"dispatch_id {dispatch_id!r} does not match the id regex; refusing to "
            "stage a bundle with an unsafe directory name"
        )
    staging_id = dispatch_id

    # 2. resolve the data root the SAME way the door does.
    root = (data_dir or _data_dir()).resolve()
    pending = root / "dispatches" / "pending"

    # 3. anchor the pending root BEFORE writing (defense-in-depth vs symlink escape).
    if pending.exists() and not pending.resolve().is_relative_to(root):
        raise ValueError(
            f"refusing to stage: pending root {pending} resolves outside data root "
            f"{root} (symlink escape) — possible forged-promotion attempt"
        )

    bundle_dir = pending / staging_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    # bundle_dir itself must resolve inside pending (catch a pre-planted symlinked id dir).
    if not bundle_dir.resolve().is_relative_to(pending.resolve()):
        raise ValueError(
            f"refusing to stage: bundle dir {bundle_dir} escapes pending root"
        )

    # 4. write the instruction as a fresh regular file inside the bundle.
    instruction_file = bundle_dir / "instruction.md"
    atomic_write_text(instruction_file, instruction_text)

    # 5. pre-bind the content hash over the exact written bytes (closes TOCTOU pre-door).
    instruction_sha256 = hashlib.sha256(instruction_text.encode("utf-8")).hexdigest()

    # 6. assemble + write the spec atomically. instruction_file is the literal
    #    absolute child path — a real regular file, no symlink.
    norm_reason = (headless_reason or "").strip() or None
    spec_payload = {
        "schema_version": 1,
        "project_id": project_id or _project_id(),
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction_file.resolve()),
        "role": role or "backend-developer",
        "target_slot": target_slot,
        "gate": gate or "",
        "dispatch_paths": [
            {
                "path": str(PurePosixPath(str(p["path"]))),
                "access": p.get("access", "read_write"),
                "materialize_at_cwd": p.get("materialize_at_cwd") is True,
            }
            for p in dispatch_paths
        ],
        "provider": _canonical_provider(provider).value,
        "model": model or None,
        "pr_id": pr_id or None,
        "deadline_seconds": int(deadline_seconds),
        "base_ref": base_ref or "origin/main",
        "isolation": "worktree",
        "requires_mcp": bool(requires_mcp),
        "target_id_override": target_id_override or None,
        "tags": list(tags),
        "instruction_sha256": instruction_sha256,
        "allow_headless": bool(allow_headless),
        "headless_reason": norm_reason,
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    atomic_write_json(spec_file, spec_payload)
    return spec_file


def bridge_dispatch(*, dry_run: bool = False, **stage_kwargs) -> int:
    """Stage a spec bundle, then drive it through the ONLY door (run_dispatch).

    Returns run_dispatch's exit code (0 success, 1 reject/failure). Any staging
    error (e.g. unsafe dispatch_id, symlink escape) is surfaced as a clean 1 so a
    caller never falls back to a side-door delivery.
    """
    if stage_kwargs.get("allow_headless"):
        from routing_policy import is_claude_headless_blocked, load_lane_safety  # noqa: PLC0415
        lane_safety = load_lane_safety()
        if is_claude_headless_blocked(lane_safety):
            override_env = (lane_safety.get("headless_block") or {}).get(
                "override_env", "VNX_OVERRIDE_CLAUDE_HEADLESS"
            )
            print(
                "[dispatch_bridge] REJECT [headless-blocked]: claude_headless lane blocked by default "
                f"(lane_safety.headless_block, routing_policy.yaml); set {override_env}=1 to opt in",
                file=sys.stderr,
            )
            return 1
    try:
        spec_file = stage_spec_bundle(**stage_kwargs)
    except (ValueError, OSError) as exc:
        print(f"[dispatch_bridge] REJECT [staging-error]: {exc}", file=sys.stderr)
        return 1
    from dispatch_cli import run_dispatch  # noqa: PLC0415
    return run_dispatch(spec_file, dry_run=dry_run)


def deliver_via_door(
    legacy,
    *,
    instruction_text: str,
    dispatch_id: str,
    target_slot: str,
    role: Optional[str] = None,
    provider: str = "claude",
    model: Optional[str] = None,
    gate: str = "",
    pr_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> bool:
    """Gated delivery for the in-process python callers (pool_worker_runner, claude_adapter,
    headless_dispatch_daemon). When ``VNX_SINGLE_ENTRY_DISPATCH=1`` route through the door
    (``bridge_dispatch``); otherwise run ``legacy`` — the caller's existing lane delivery,
    passed as a zero-arg callable. Returns True on success (normalizes the bridge's exit code
    and the legacy bool). OFF (default) = the legacy path, byte-for-byte unchanged.

    ``project_id`` may be None: the door resolves + validates it fail-closed (ADR-007). Pass it
    explicitly when the caller already knows it (preferred).

    Routing uses the single-source predicate (dispatch_flags.single_entry_enabled) so the default
    and the VNX_DISPATCH_LEGACY rollback are honored identically here and in the bash readers.
    """
    if single_entry_enabled():
        return bridge_dispatch(
            instruction_text=instruction_text,
            dispatch_id=dispatch_id,
            role=role or "backend-developer",
            target_slot=target_slot,
            provider=provider,
            model=model,
            gate=gate,
            pr_id=pr_id,
            project_id=project_id,
        ) == 0
    return bool(legacy())


# ---------------------------------------------------------------------------
# Thin CLI — for the bash caller (dispatch_deliver.sh) which shells in like it
# already shells into dispatch_cli.py. Instruction text arrives on stdin to avoid
# argv length/escaping limits.
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="VNX legacy→door dispatch bridge (PR-12)")
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--terminal", required=True, dest="target_slot")
    parser.add_argument("--role", default="backend-developer")
    parser.add_argument("--provider", default="claude")
    parser.add_argument("--model", default=None)
    parser.add_argument("--gate", default="")
    parser.add_argument("--pr-id", default=None, dest="pr_id")
    parser.add_argument("--deadline-seconds", type=int, default=3600, dest="deadline_seconds")
    parser.add_argument("--requires-mcp", action="store_true", dest="requires_mcp")
    parser.add_argument("--allow-headless", action="store_true", dest="allow_headless")
    parser.add_argument("--headless-reason", default=None, dest="headless_reason")
    parser.add_argument("--target-id-override", default=None, dest="target_id_override")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument(
        "--instruction-stdin", action="store_true", dest="instruction_stdin",
        help="Read the instruction text from stdin (preferred — avoids argv limits).",
    )
    parser.add_argument("--instruction", default=None, help="Inline instruction text (fallback).")
    args = parser.parse_args(argv)

    if args.instruction_stdin:
        instruction_text = sys.stdin.read()
    elif args.instruction is not None:
        instruction_text = args.instruction
    else:
        print("[dispatch_bridge] no instruction provided (--instruction-stdin or --instruction)", file=sys.stderr)
        return 2

    if not instruction_text.strip():
        print("[dispatch_bridge] empty instruction", file=sys.stderr)
        return 2

    return bridge_dispatch(
        instruction_text=instruction_text,
        dispatch_id=args.dispatch_id,
        role=args.role,
        target_slot=args.target_slot,
        provider=args.provider,
        model=args.model,
        gate=args.gate,
        pr_id=args.pr_id,
        deadline_seconds=args.deadline_seconds,
        requires_mcp=args.requires_mcp,
        allow_headless=args.allow_headless,
        headless_reason=args.headless_reason,
        target_id_override=args.target_id_override,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
