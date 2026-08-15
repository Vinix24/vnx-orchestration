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
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Optional

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from atomic_io import atomic_write_json, atomic_write_text  # noqa: E402
from dispatch_flags import single_entry_enabled  # noqa: E402
from dispatch_spec import (  # noqa: E402
    _ID_RE,
    DEADLINE_SECONDS_MAX,
    DEADLINE_SECONDS_MIN,
    Gate,
    Provider,
)

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
    # OI-962: empty/None provider must resolve to AUTO so the smart router can
    # fill in provider+model BEFORE validation.  Previously this resolved to
    # "claude", which meant the router never saw the dispatch and kimi-pinned
    # workers rejected provider=claude + model=kimi-k3 on constraint violation.
    "": "auto",
}


def _canonical_provider(raw: Optional[str]) -> Provider:
    """Map a legacy provider/mode string to a Provider enum member.

    Raises ValueError on an unknown string (caught by the caller → clean reject).
    """
    key = (raw or "").strip().lower()
    canonical = _PROVIDER_ALIASES.get(key, key)
    return Provider(canonical)


# Legacy lifecycle PHASE names that leak into the gate field — not review gates.
# Each is produced by exactly one layer and consumed by none:
#   - "planning"       — dispatch_create.sh:365-367 (V8's _pdp_extract_dispatch_metadata)
#                         stamps this whenever no gate was specified ("V8: No gate
#                         specified, defaulting to 'planning'").
#   - "implementation" — pr_queue_manager.py:870/997/1638 uses this as the default
#                         `pr.get('gate', 'implementation')` / literal assignment when
#                         no gate was recorded on a PR.
# No gate runner (gate_runner.py, gate_recorder.py, gate_request_handler.py,
# review_gate_manager.py, codex_final_gate.py) has ever matched on either string —
# they are no-gate markers, not real gates, so neither belongs in the closed Gate
# enum: adding them would legitimise gates that no runner implements.
_LEGACY_PHASE_SENTINELS = frozenset({"planning", "implementation"})


def _canonical_gate(raw: Optional[str]) -> str:
    """Validate a gate name against the closed ``Gate`` enum (OI-845).

    An empty/blank value means "no gate assigned" and passes through as ``""`` —
    the same "unset" convention ``stage_spec_bundle`` already uses for ``gate``.
    Any non-empty value that is not a legal gate name raises ValueError naming
    the invalid value and listing the valid ones, instead of silently writing
    an unenforceable gate into the spec (a dispatch staged with ``gate="codex"``
    previously wrote that string through unchecked and the gate simply never ran).

    Members of ``_LEGACY_PHASE_SENTINELS`` are special-cased to the same
    empty-gate sentinel — see that constant's docstring for why they normalise
    instead of raising or joining the closed ``Gate`` enum.
    """
    key = (raw or "").strip()
    if not key or key.lower() in _LEGACY_PHASE_SENTINELS:
        return ""
    try:
        return Gate(key).value
    except ValueError:
        valid = ", ".join(sorted(g.value for g in Gate))
        raise ValueError(
            f"gate {key!r} is not a recognized gate name; valid gates are: {valid}"
        ) from None


def _data_dir(project_id: "Optional[str]" = None) -> Path:
    """Resolve the data root EXACTLY as the door does (no divergence).

    ``project_id`` (when given) is the authoritative tenant, so the bundle stages into
    THAT project's central store instead of the ambient ``vnx-dev`` default. This keeps
    the physical staging location coherent with the spec's declared ``project_id`` — the
    invariant the door's ADR-007 guard now validates against.
    """
    from dispatch_cli import _resolve_data_dir  # noqa: PLC0415
    return _resolve_data_dir(project_id)


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
    # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): the predecessor
    # this dispatch continues, the tier escalation, and the smart_router task
    # class. Carried verbatim onto the spec; the door passes them to the receipt.
    parent_dispatch: Optional[str] = None,
    task_class: Optional[str] = None,
    tier_from: Optional[str] = None,
    tier_to: Optional[str] = None,
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

    # 1a. deadline bounds validated at the trust boundary — this module is the
    # FIRST writer of a dispatch-spec.json bundle, so an out-of-range value must
    # fail loud at staging (bridge_dispatch surfaces it as a clean reject) rather
    # than drift silently downstream. The range is the consumer-door contract
    # [DEADLINE_SECONDS_MIN, DEADLINE_SECONDS_MAX] — the same range validate()
    # Rule 11 enforces, from the same constants (dispatch_spec).
    if not (DEADLINE_SECONDS_MIN <= int(deadline_seconds) <= DEADLINE_SECONDS_MAX):
        raise ValueError(
            f"deadline_seconds must be in [{DEADLINE_SECONDS_MIN}, {DEADLINE_SECONDS_MAX}], "
            f"got {deadline_seconds}"
        )

    # 1b. resolve the effective tenant ONCE, up front, so the physical staging store and
    # the spec's declared project_id are the SAME. Staging into the ambient _data_dir()
    # (vnx-dev in a central install) while stamping the spec with the real project_id is
    # exactly what caused the fleet-wide hard-reject: the door derives the tenant from the
    # bundle's physical location, so a bundle staged into vnx-dev but declaring
    # sales-copilot fails validation.
    effective_project_id = project_id or _project_id()

    # 2. resolve the data root the SAME way the door does — anchored on the tenant.
    root = (data_dir or _data_dir(effective_project_id)).resolve()
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
        "project_id": effective_project_id,
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction_file.resolve()),
        # OI-921: never silently fill the backend-developer sentinel. An empty role
        # is staged as an explicit "" so the door's validate() Rule 7 rejects it
        # loud (bad-role) — the caller MUST choose a real role. The distinction
        # between "no role chosen" (empty) and a conscious "backend-developer"
        # (a valid agents/ role) is preserved on the spec.
        "role": (role or "").strip(),
        "target_slot": target_slot,
        "gate": _canonical_gate(gate),
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
        # Chain-link fields (dispatch-20260802-model-ssot-en-ketenlink).
        "task_class": (task_class or "").strip() or None,
        "parent_dispatch": (parent_dispatch or "").strip() or None,
        "tier_from": (tier_from or "").strip() or None,
        "tier_to": (tier_to or "").strip() or None,
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
    rc = run_dispatch(spec_file, dry_run=dry_run)

    _settle_bundle(spec_file.parent, rc)

    return rc


def _settle_bundle(bundle_dir: Path, rc: int) -> None:
    """Move a door-processed bundle out of pending/ into completed/ or failed/.

    OI-1072: the bridge must MOVE the staged bundle directory after the door
    processes it — never delete it. Consumers read dispatch-spec.json AFTER the
    dispatch returns (receipt processing, operator forensics, the deadline
    passthrough assertions), so removal breaks read-after-completion while
    leaving the bundle in pending/ re-creates the unbounded growth this cleanup
    exists to solve (the daemon only picks up flat .md files, not directory
    bundles). The destination mirrors the door's own lifecycle dirs and the
    `dispatch-cleanup` transition: completed/ on rc == 0, failed/ otherwise.
    Best-effort: a failure here must not change the dispatch's exit code.
    """
    try:
        if not bundle_dir.exists():
            return
        # bundle_dir is <data_dir>/dispatches/pending/<id> — anchored inside the
        # data root by stage_spec_bundle before anything was written.
        dispatches_dir = bundle_dir.parent.parent
        if dispatches_dir.name != "dispatches" or bundle_dir.parent.name != "pending":
            return
        outcome_dir = dispatches_dir / ("completed" if rc == 0 else "failed")
        outcome_dir.mkdir(parents=True, exist_ok=True)
        dest = outcome_dir / bundle_dir.name
        if dest.exists():
            # Re-dispatch of the same id: never overwrite or nest into the prior
            # bundle — pick the first free suffixed name instead.
            suffix = 2
            while (outcome_dir / f"{bundle_dir.name}-{suffix}").exists():
                suffix += 1
            dest = outcome_dir / f"{bundle_dir.name}-{suffix}"
        shutil.move(str(bundle_dir), str(dest))
    except OSError as exc:
        print(
            f"[dispatch_bridge] WARN bundle settle failed for {bundle_dir}: {exc}",
            file=sys.stderr,
        )


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
    deadline_seconds: Optional[int] = None,
    allow_headless: bool = False,
    headless_reason: Optional[str] = None,
) -> bool:
    """Gated delivery for the in-process python callers (pool_worker_runner, claude_adapter,
    headless_dispatch_daemon). When ``VNX_SINGLE_ENTRY_DISPATCH=1`` route through the door
    (``bridge_dispatch``); otherwise run ``legacy`` — the caller's existing lane delivery,
    passed as a zero-arg callable. Returns True on success (normalizes the bridge's exit code
    and the legacy bool). OFF (default) = the legacy path, byte-for-byte unchanged.

    ``project_id`` may be None: the door resolves + validates it fail-closed (ADR-007). Pass it
    explicitly when the caller already knows it (preferred).

    ``deadline_seconds`` may be None: resolves to the unchanged 3600s default (matches
    ``stage_spec_bundle``'s own default) so an omitted value reproduces byte-identical
    prior behavior. Pass an explicit value (validated by the caller, e.g. dispatch-agent's
    300-14400 range) to override the lane's receipt-wait deadline. ``stage_spec_bundle``
    re-enforces the same [300, 14400] bounds at the trust boundary, so an out-of-range
    value fails loud at staging (bridge_dispatch returns 1) even if a caller skips its own
    validation.

    ``allow_headless`` / ``headless_reason`` (OI-1174): threaded through to
    ``bridge_dispatch`` so the pip-CLI door (``vnx dispatch-agent``) reaches the
    claude_headless lane the same way the bridge's own ``--allow-headless`` CLI
    does. The door's validate() re-enforces the reason-required + claude-only
    rules, so a caller that skips its own check still fails loud. Defaults False /
    None reproduce byte-identical prior behavior.

    Routing uses the single-source predicate (dispatch_flags.single_entry_enabled) so the default
    and the VNX_DISPATCH_LEGACY rollback are honored identically here and in the bash readers.
    """
    if single_entry_enabled():
        return bridge_dispatch(
            instruction_text=instruction_text,
            dispatch_id=dispatch_id,
            # OI-921: pass the caller's role through unchanged — never inject the
            # backend-developer sentinel here. stage_spec_bundle writes "" for an
            # unset role so the door rejects it loud instead of silently defaulting.
            role=role,
            target_slot=target_slot,
            provider=provider,
            model=model,
            gate=gate,
            pr_id=pr_id,
            project_id=project_id,
            deadline_seconds=deadline_seconds if deadline_seconds is not None else 3600,
            allow_headless=allow_headless,
            headless_reason=headless_reason,
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
    # OI-921: NO sentinel default — a caller that omits --role must fail loud at the
    # door (validate Rule 7) rather than silently dispatch as backend-developer.
    parser.add_argument("--role", default="")
    parser.add_argument("--provider", default="claude")
    parser.add_argument("--model", default=None)
    parser.add_argument("--gate", default="")
    parser.add_argument("--pr-id", default=None, dest="pr_id")
    parser.add_argument("--parent-dispatch", default=None, dest="parent_dispatch")
    parser.add_argument("--task-class", default=None, dest="task_class")
    parser.add_argument("--tier-from", default=None, dest="tier_from")
    parser.add_argument("--tier-to", default=None, dest="tier_to")
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

    # deadline bounds enforced on the CLI too (not just at stage_spec_bundle): the
    # bridge is the trust boundary for legacy callers, so an impossible
    # --deadline-seconds must fail loud here, not reach staging. Same constants as
    # stage_spec_bundle / dispatch_spec.validate (single source of truth).
    if not (DEADLINE_SECONDS_MIN <= args.deadline_seconds <= DEADLINE_SECONDS_MAX):
        print(
            f"[dispatch_bridge] REJECT [bad-deadline]: --deadline-seconds "
            f"{args.deadline_seconds} is out of range "
            f"[{DEADLINE_SECONDS_MIN}, {DEADLINE_SECONDS_MAX}]",
            file=sys.stderr,
        )
        return 2

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
        parent_dispatch=args.parent_dispatch,
        task_class=args.task_class,
        tier_from=args.tier_from,
        tier_to=args.tier_to,
        deadline_seconds=args.deadline_seconds,
        requires_mcp=args.requires_mcp,
        allow_headless=args.allow_headless,
        headless_reason=args.headless_reason,
        target_id_override=args.target_id_override,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
