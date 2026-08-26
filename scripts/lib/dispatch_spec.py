"""dispatch_spec.py — DispatchSpec: the typed input surface for the single-entry dispatch gate.

Pure types + one validate() function. No side effects beyond reading the instruction file.
Nothing imports this module in PR-1; it is wired in later PRs.

ADR-006: provider constraint enum enforces legal routing strings.
ADR-007: not triggered here — no new table, pure in-process types only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Optional

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Provider(str, Enum):
    """CLOSED set — the ONLY legal provider strings. Mirrors scripts/benchmark/models.yaml ids."""
    AUTO              = "auto"             # capability-seam fills provider+model before planning
    CLAUDE            = "claude"
    CODEX             = "codex"
    KIMI              = "kimi"             # CLI OAuth (kimi-via-cli-only)
    GEMINI            = "gemini"
    LITELLM_DEEPSEEK  = "litellm:deepseek"
    LITELLM_ZAI       = "litellm:zai"      # BENCHMARK-BASELINE ONLY (prod GLM uses Provider.GLM_HARNESS; glm-via-harness-only)
    LITELLM_MOONSHOT  = "litellm:moonshot"  # BENCHMARK-BASELINE ONLY (prod kimi uses Provider.KIMI)
    DEEPSEEK_HARNESS  = "deepseek-harness"
    GLM_HARNESS       = "glm-harness"      # GLM via claude-CLI harness → local :4141 litellm proxy → OpenRouter (prod GLM lane)
    LOCAL_GEMMA       = "local-gemma"


class Gate(str, Enum):
    """CLOSED set — the ONLY legal review-gate names (OI-845).

    Mirrors the gate names ``gate_recorder.py``/``gate_request_handler.py`` already
    dispatch on; this is the canonical enum they lacked. An empty string means
    "no gate assigned" and is handled separately by callers — it is not a member
    here so ``Gate("")`` fails the same as any other unknown value.

    KIMI_GATE and GLM_GATE (dlv45) extend the closed set with the same
    discipline as every other member: each has a ``gate_request_handler``
    dispatch branch and a ``closure_verifier._GATE_HANDLERS`` entry, pinned by
    ``test_closure_verifier_gate_enum_drift.py``. Extending the enum without
    both is what OI-1094 exists to catch — see that test module before adding
    another member.
    """
    GEMINI_REVIEW           = "gemini_review"
    CODEX_GATE              = "codex_gate"
    CLAUDE_GITHUB_OPTIONAL  = "claude_github_optional"
    CI_GATE                 = "ci_gate"
    WIRING_GATE             = "wiring_gate"
    KIMI_GATE               = "kimi_gate"
    GLM_GATE                = "glm_gate"


# Legacy lifecycle PHASE names that leak into the spec ``gate`` field but are not
# review gates. "planning" (dispatch_create.sh) and "implementation"
# (pr_queue_manager.py) are produced by real legacy code as no-gate markers; no
# gate runner has ever matched on either string. They are admitted as the same
# "no gate assigned" sentinel that an empty value carries, NOT as enum members —
# adding them to the closed ``Gate`` enum would legitimise gates no runner
# implements. Single source of truth: dispatch_bridge._canonical_gate imports this
# set so the bridge path and this module's validate() Rule 16 can never drift (OI-845).
LEGACY_GATE_SENTINELS = frozenset({"planning", "implementation"})


class Isolation(str, Enum):
    WORKTREE = "worktree"  # the ONLY legal value in 1.0 — every worker spawn is isolated, fail-loud


class PathAccess(str, Enum):
    READ       = "read"
    WRITE      = "write"
    READ_WRITE = "read_write"
    CREATE     = "create"


# OI-1196: which PathAccess values grant WRITE capability on a DispatchPath.
# WRITE, READ_WRITE, and CREATE all denote intent to mutate the path; READ is
# the one value that withholds it. This governs WRITE scope only — the fabric
# has no separate read-scope enforcement gate (Read/Grep are unrestricted for
# any worker with those tools allowed), so a READ-access path is honestly
# "excluded from write scope", not "reads are additionally restricted".
# Single source of truth: scripts/lib/worker_permissions.py imports this
# rather than redeclaring which access values mean "write".
WRITE_GRANTING_PATH_ACCESS = frozenset({PathAccess.WRITE, PathAccess.READ_WRITE, PathAccess.CREATE})


# ---------------------------------------------------------------------------
# Deadline bounds — single source of truth (deadline-passthrough)
# ---------------------------------------------------------------------------

# The consumer door (`vnx dispatch-agent --deadline-seconds N`) documents and
# enforces [300, 14400]. validate() Rule 11 and the bridge's trust boundary
# (dispatch_bridge.stage_spec_bundle + its --deadline-seconds CLI) use these
# constants so the ranges can never drift again — validate() previously allowed
# [60, 14400] while the door enforced [300, 14400], and two ranges without an
# explanation is the next drift source.
DEADLINE_SECONDS_MIN = 300
DEADLINE_SECONDS_MAX = 14400


# ---------------------------------------------------------------------------
# Path type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchPath:
    path: PurePosixPath
    access: PathAccess = PathAccess.READ_WRITE
    materialize_at_cwd: bool = False


def write_paths(paths: "tuple[DispatchPath, ...]") -> list[str]:
    """Return the POSIX path strings among *paths* whose access grants write.

    OI-1196: this is the first place ``DispatchPath.access`` is actually
    consulted for a decision. Before this change, ``validate()`` accepted
    and carried the field through into ``ValidatedSpec.normalized_paths``
    untouched, but nothing ever read it back — ``access`` was decoration
    that suggested a rights model with no enforcement behind it. A
    ``PathAccess.READ`` entry is excluded (see WRITE_GRANTING_PATH_ACCESS);
    everything else is included.

    This function's own caller is door-time classification, not wire
    encoding: ``dispatch_cli._spec_is_writing`` calls ``write_paths()`` to
    decide whether a dispatch is "writing" (and therefore needs a review
    gate) — it only needs to know whether the write-granting subset is
    non-empty, so the filtered, stripped-to-bare-string return shape is
    exactly what it wants.

    OI-1271 wired ``DispatchPath.access`` onto the tmux-lane
    ``--dispatch-paths`` CLI surface, but not by routing through this
    function. The sending side is ``dispatch_cli._dispatch_path_wire_entry``,
    called once per ``DispatchPath`` while building the ``dispatch_paths``
    kwarg for ``TmuxInteractiveDispatch.dispatch``; the receiving side is
    ``worker_permissions._parse_dispatch_path_entry``, which reads each wire
    entry back into a ``(path, access)`` pair for
    ``worker_permissions.resolve_dispatch_write_scope``. That bridge cannot
    call ``write_paths()``: the wire needs every declared path present, each
    carrying its own access, because two other consumers of the same list —
    ``benchmark_worker_isolation.materialize_benchmark_seed`` and
    ``TmuxInteractiveDispatch._scope_note`` — read every entry as a literal
    repo-relative path, and a pre-filtered, write-only subset would silently
    drop the read-only paths they still need to see.
    ``_dispatch_path_wire_entry`` instead re-derives write-intent per path
    against ``WRITE_GRANTING_PATH_ACCESS`` directly — the same frozenset
    this function already consults, and the same one ``worker_permissions``
    imports (as ``WRITE_GRANTING_ACCESS``) for the receiving side — so
    "which access values mean write" still has one source of truth even
    though door-time classification and wire encoding evaluate it through
    two independent call paths rather than one calling the other.
    """
    return [str(dp.path) for dp in paths if dp.access in WRITE_GRANTING_PATH_ACCESS]


# ---------------------------------------------------------------------------
# Core spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchSpec:
    """Immutable, typed dispatch input. Produced by callers, consumed by validate()."""
    schema_version: int
    project_id: str
    dispatch_id: str
    staging_id: str
    instruction_file: Path   # absolute path to the instruction file — NEVER inline text
    role: str
    target_slot: str         # "T0" | "T1" | "T2" | "T3"
    gate: str
    dispatch_paths: tuple[DispatchPath, ...]
    provider: Provider = Provider.AUTO
    model: Optional[str] = None
    skill: Optional[str] = None
    task_class: Optional[str] = None
    # Operator-declared irreversibility (plan-gate weight ladder, 2026-08-15):
    # a deletion/rename or a big architecture refactor cannot be walked back, so
    # it forces the strictest governance variant. Path-derivable irreversible
    # categories (schema migrations, fleet defaults, the receipt/ledger format)
    # need no flag; this field covers the two that are NOT path-derivable. It is
    # a declaration by the caller, never derived from instruction text or role.
    irreversible: bool = False
    pr_id: Optional[str] = None
    # OI-1137: explicit work-ref — the branch a fix-forward dispatch delivers onto, so the
    # phantom-guard can weigh the pushed branch diff when the own worktree reads empty.
    # Optional; None for a normal dispatch. A bare branch name (dispatch/<id>) or an
    # origin/-prefixed form is accepted; the resolver normalizes the prefix away.
    work_ref: Optional[str] = None
    track_id: Optional[str] = None  # structural link to a tracks-table row (TL-D1); validated at the door
    # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): the predecessor
    # this dispatch continues (retry / fix-forward / escalation), the tier
    # escalation signal (tier_from = parent's tier, tier_to = this tier), and
    # the smart_router task class. All advisory — carried onto the plan and the
    # receipt, never part of the permit fingerprint.
    parent_dispatch: Optional[str] = None
    tier_from: Optional[str] = None
    tier_to: Optional[str] = None
    deadline_seconds: int = 3600
    base_ref: str = "origin/main"
    isolation: Isolation = Isolation.WORKTREE
    requires_mcp: bool = False
    target_id_override: Optional[str] = None
    tags: tuple[str, ...] = ()
    instruction_sha256: Optional[str] = None  # P0-3: caller may pre-bind hash; validate() verifies
    # A2 (2026-08-26): claude_headless became the DEFAULT claude lane (see
    # dispatch_plan.resolve_claude_lane) — main 7f93f681 measured both governance
    # gaps the old default cited (isolation=worktree, report-before-receipt) as
    # closed, and the tmux lane duplicate-PR defect (OI-1115 skip_pr never wired
    # into tmux_interactive_dispatch.py) as still open. allow_headless=True is now
    # a REDUNDANT-with-default but still meaningful explicit statement (kept for
    # the pre-flip specs that already carry it + reason, and for any future caller
    # that wants the choice on the record rather than implied). It no longer gates
    # ACCESS to the lane — only whether a "HEADLESS lane opted-in" audit line is
    # emitted for a spec that asked for it by name.
    allow_headless: bool = False              # PR-5: explicit opt-in to the claude_headless lane
    headless_reason: Optional[str] = None    # PR-5: mandatory non-empty reason when allow_headless=True
    # A2 (2026-08-26): the mirror image of allow_headless — an explicit opt-OUT
    # back to the (now non-default) tmux lane. False (default) means "no opinion,
    # accept whatever the policy default resolves to" — same shape as
    # allow_headless=False always having meant "didn't ask", never "refused".
    # True requires force_tmux_reason (validate() Rule 12b) and is only valid for
    # provider=claude/auto, mirroring allow_headless's own Rule 12a exactly. Never
    # both True at once (Rule 12c) — that is a contradiction, not a choice.
    force_tmux: bool = False
    force_tmux_reason: Optional[str] = None
    # OI-1214: a post-merge-verification dispatch measures the CURRENT checkout
    # (proof that a just-merged PR is actually live), so it is only meaningful
    # when the local main checkout is current. This is a typed boolean declared by
    # the caller — the simplest form that fits the spec's existing declared fields
    # (mirrors `irreversible` / `allow_headless` / `requires_mcp`) and cannot
    # collide with the free-form `tags`/intelligence vocabulary the way a magic tag
    # string would. Default False means a normal build dispatch carries NO burden:
    # the door logs the lag number for every dispatch but only REFUSES when this
    # flag is set AND the checkout is behind — a door that blocked everything on
    # lag would be worse than the problem it fixes. Like `irreversible`, it is a
    # caller declaration, never derived from instruction text or role.
    post_merge_verification: bool = False
    # DERIVED-not-declared (deliberately absent): lane, billing, serialization_class
    # — compile_plan owns them. Do not add here.


# ---------------------------------------------------------------------------
# Validation result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Reject:
    code: str     # e.g. "ADR-006", "bad-provider", "instruction-unreadable"
    reason: str


@dataclass(frozen=True)
class ValidatedSpec:
    spec: DispatchSpec
    instruction_text: str                    # loaded from instruction_file during validate()
    normalized_paths: tuple[DispatchPath, ...]
    instruction_sha256: str                  # sha256 of instruction_text, computed in validate()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

# OI-1137: git-ref shape validation for an explicit work_ref. Branch names (which is what
# work_ref carries) disallow whitespace, "..", "@{", and a set of reserved chars; they may
# contain "/" (unlike _ID_RE) and may not start with "-", end with "/" or ".", or be "@".
_GIT_REF_INVALID = re.compile(r"[\s~^:?*\[\]\\]|\.\.|@\{")


def _validate_branch_ref(raw: str) -> "str | None":
    """Return an error string if raw is not a valid git branch name, else None."""
    if not raw or raw != raw.strip():
        return "empty or has leading/trailing whitespace"
    if len(raw) > 255:
        return "exceeds 255 chars"
    if raw == "@":
        return "reserved name '@'"
    if raw.startswith("-"):
        return "may not start with '-'"
    if raw.startswith("/") or raw.endswith("/") or raw.endswith("."):
        return "may not start with '/', end with '/', or end with '.'"
    if _GIT_REF_INVALID.search(raw):
        return "contains a character git forbids in a branch name"
    return None

_BLOCKED_FIRST_COMPONENTS = frozenset({".git", ".vnx-data"})

_VALID_TARGET_SLOTS = frozenset({"T0", "T1", "T2", "T3"})

# Cost-tier vocabulary — the escalation signal on receipts must come from this
# closed set. It spans the four classifier scope buckets (mirrors
# providers.smart_router.cost_tier) PLUS the three escalation-only rungs of the
# cost ladder (OI-1229): a model-named rung is reachable only by climbing, never
# by classification, but it is a legal tier_from/tier_to value on an escalation
# dispatch, so it must pass the door's Rule 14 tier check.
_VALID_TIERS = frozenset({
    "tier-zero",
    "tier-low",
    "tier-mid",
    "tier-high",
    "kimi-k3",
    "gpt-5.5",
    "fable-5",
})


def _validate_dispatch_path(dp: DispatchPath) -> Optional[str]:
    """Return an error string if the DispatchPath is invalid, else None."""
    raw = str(dp.path)
    if not raw or raw.strip() == "":
        return "empty path"

    p = PurePosixPath(raw)

    # Reject absolute paths
    if p.is_absolute():
        return f"absolute path not allowed: {raw}"

    parts = p.parts
    if not parts:
        return "empty path after normalization"

    # Reject .. components anywhere
    if ".." in parts:
        return f"'..' component not allowed: {raw}"

    # Reject blocked first-component names
    if parts[0] in _BLOCKED_FIRST_COMPONENTS:
        return f"path may not start with '{parts[0]}': {raw}"

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate(
    spec: DispatchSpec,
    *,
    project_id: str,
    repo_root: Path,
) -> ValidatedSpec | Reject:
    """Validate a DispatchSpec. Returns ValidatedSpec on success, Reject on first failure.

    Never raises — all errors are returned as typed Reject values.
    Existence-at-base_ref, registry validation for model, and skill presence
    are compile_plan rules, not validated here.
    """

    # Rule 1 — schema version
    if spec.schema_version != 1:
        return Reject("bad-schema", f"schema_version must be 1, got {spec.schema_version!r}")

    # Rule 2 — project_id must match the AUTHORITATIVE project_id the caller
    # (run_dispatch) derived from the PHYSICAL staged-bundle store — never from
    # ambient CWD/env. A bundle physically living in project X's store may not
    # declare a different project_id (ADR-007 anti-redirect). The old code re-
    # resolved from env with a hardcoded 'vnx-dev' default, which in a central
    # install (CWD = shared engine tree with a stray .vnx-project-id) rejected
    # every legitimate non-vnx-dev consumer dispatch as a "project-mismatch".
    if spec.project_id != project_id:
        return Reject(
            "project-mismatch",
            f"spec.project_id={spec.project_id!r} != authoritative project_id={project_id!r}; "
            "caller cannot redirect state to another project",
        )

    # Rule 3 — dispatch_id format
    if not _ID_RE.match(spec.dispatch_id):
        return Reject("bad-dispatch-id", f"dispatch_id {spec.dispatch_id!r} does not match id regex")

    # Rule 4 — staging_id format (presence + format only; promotion check is a plan rule)
    if not _ID_RE.match(spec.staging_id):
        return Reject("bad-staging-id", f"staging_id {spec.staging_id!r} does not match id regex")

    # Rule 5 — instruction_file must be absolute, regular, non-symlink, readable
    ifile = spec.instruction_file
    if not ifile.is_absolute():
        return Reject("instruction-unreadable", f"instruction_file must be absolute, got {ifile}")
    try:
        stat = ifile.stat()
    except OSError as exc:
        return Reject("instruction-unreadable", f"instruction_file not accessible: {exc}")
    import stat as stat_mod
    if not stat_mod.S_ISREG(stat.st_mode):
        return Reject("instruction-unreadable", f"instruction_file is not a regular file: {ifile}")
    if ifile.is_symlink():
        return Reject("instruction-unreadable", f"instruction_file must not be a symlink: {ifile}")
    try:
        instruction_text = ifile.read_text(encoding="utf-8")
    except OSError as exc:
        return Reject("instruction-unreadable", f"instruction_file not readable: {exc}")
    except UnicodeDecodeError as exc:
        # P1 (PR-4c): a non-UTF-8 instruction must Reject, not raise out of the door.
        # The "door never panics" invariant must cover validation, not just runtime.
        return Reject("instruction-unreadable", f"instruction_file is not valid UTF-8: {exc}")

    # P0-3: compute sha256 over instruction content; verify against DispatchSpec field if set
    computed_sha256 = hashlib.sha256(instruction_text.encode("utf-8")).hexdigest()
    if spec.instruction_sha256 is not None and spec.instruction_sha256 != computed_sha256:
        return Reject(
            "instruction-hash-mismatch",
            f"instruction_file sha256 mismatch: spec declared {spec.instruction_sha256[:12]}…, "
            f"computed {computed_sha256[:12]}…",
        )

    # Rule 6 — DO NOT scan instruction_text for spawn tokens (claude -p, codex exec, etc.).
    # The file-reference design already neutralizes prompt injection; a content scan would
    # falsely reject legitimate instructions that discuss CLI invocation patterns.

    # Rule 7 — role non-empty.
    # Tight role/skill validation (against the agents/ registry) is deferred to
    # compile_plan (OI-921: compile_plan enforces membership against
    # RuntimeSnapshot.valid_roles, discovered from the agents/ dir by the door's
    # build_runtime_snapshot). Here we only require non-empty — an unset role is
    # itself a Reject, so a dispatch MUST carry an explicit role.
    if not spec.role or not spec.role.strip():
        return Reject("bad-role", "role must be a non-empty string")

    # Rule 8 — target_slot
    if spec.target_slot not in _VALID_TARGET_SLOTS:
        return Reject("bad-target-slot", f"target_slot must be one of {sorted(_VALID_TARGET_SLOTS)}, got {spec.target_slot!r}")

    # Rule 9 — provider is valid by type (it's an enum member); model format if set
    if spec.model is not None and not spec.model.strip():
        return Reject("bad-model", "model must be a non-empty string when set")

    # Rule 10 — dispatch_paths structural validation
    normalized: list[DispatchPath] = []
    for dp in spec.dispatch_paths:
        err = _validate_dispatch_path(dp)
        if err is not None:
            return Reject("bad-path", f"invalid dispatch_path ({err}): {dp.path}")
        norm_p = PurePosixPath(str(dp.path))
        normalized.append(DispatchPath(norm_p, dp.access, dp.materialize_at_cwd))

    # Rule 11 — deadline bounds. [300, 14400] is the consumer-door contract
    # (vnx dispatch-agent); validate() enforces the SAME range the bridge's trust
    # boundary enforces at staging time, so the two gates can never disagree.
    if not (DEADLINE_SECONDS_MIN <= spec.deadline_seconds <= DEADLINE_SECONDS_MAX):
        return Reject(
            "bad-deadline",
            f"deadline_seconds must be in [{DEADLINE_SECONDS_MIN}, {DEADLINE_SECONDS_MAX}], "
            f"got {spec.deadline_seconds}",
        )

    # Rule 12a — explicit headless opt-in requires a non-empty reason (PR-5).
    # A2 (2026-08-26): headless is now the DEFAULT claude lane, so this no longer
    # gates ACCESS — it gates the audit-trail statement "I explicitly chose this"
    # for the pre-flip specs (and any future caller) that still set allow_headless
    # =True by name. Requiring a reason for silence (the default) would be
    # nonsense; requiring one for a stated choice is still the point.
    if spec.allow_headless:
        reason = (spec.headless_reason or "").strip()
        if not reason:
            return Reject(
                "headless-reason-required",
                "allow_headless=True requires a non-empty headless_reason explaining "
                "the headless-lane opt-in; set headless_reason to a human-readable justification",
            )
        # MED: headless is only valid for claude (or auto that could resolve to claude)
        if spec.provider not in (Provider.CLAUDE, Provider.AUTO):
            return Reject(
                "headless-claude-only",
                f"allow_headless is only valid for provider=claude, got provider={spec.provider.value!r}; "
                "headless is a claude-only lane",
            )

    # Rule 12b — explicit tmux opt-out requires a non-empty reason (A2). Mirrors
    # Rule 12a exactly: since claude_headless is now the silent default, CHOOSING
    # tmux instead is the deviation that must leave an audit trail, not the other
    # way around.
    if spec.force_tmux:
        reason = (spec.force_tmux_reason or "").strip()
        if not reason:
            return Reject(
                "force-tmux-reason-required",
                "force_tmux=True requires a non-empty force_tmux_reason explaining "
                "the explicit tmux-lane opt-out; set force_tmux_reason to a human-readable justification",
            )
        if spec.provider not in (Provider.CLAUDE, Provider.AUTO):
            return Reject(
                "force-tmux-claude-only",
                f"force_tmux is only valid for provider=claude, got provider={spec.provider.value!r}; "
                "force_tmux only overrides the claude lane's default",
            )

    # Rule 12c — the two explicit choices are mutually exclusive. A spec cannot
    # simultaneously declare "I want headless" and "I want tmux instead".
    if spec.allow_headless and spec.force_tmux:
        return Reject(
            "conflicting-lane-choice",
            "allow_headless and force_tmux cannot both be set — choose exactly one "
            "explicit lane, or leave both unset to accept the default",
        )

    # Rule 13 — track_id format (presence + format only; existence against the tracks
    # DB is deferred to dispatch_cli's door validation, which has DB access — mirrors
    # how role/skill existence is deferred to compile_plan per the module docstring)
    if spec.track_id is not None:
        _track_id = spec.track_id.strip()
        if not _track_id or not _ID_RE.match(_track_id):
            return Reject("bad-track-id", f"track_id {spec.track_id!r} does not match id regex")

    # Rule 14 — chain-link format (dispatch-20260802-model-ssot-en-ketenlink).
    # parent_dispatch must be a well-formed dispatch id when present; tier values
    # must come from the cost-tier vocabulary so the receipt's escalation signal
    # is joinable (never free-text).
    if spec.parent_dispatch is not None:
        _parent = spec.parent_dispatch.strip()
        if not _parent or not _ID_RE.match(_parent):
            return Reject(
                "bad-parent-dispatch",
                f"parent_dispatch {spec.parent_dispatch!r} does not match id regex",
            )
    for _tier_field, _tier_val in (("tier_from", spec.tier_from), ("tier_to", spec.tier_to)):
        if _tier_val is not None and _tier_val.strip() not in _VALID_TIERS:
            return Reject(
                "bad-tier-value",
                f"{_tier_field} {_tier_val!r} is not one of {sorted(_VALID_TIERS)}",
            )

    # Rule 15 — work_ref format (OI-1137). An explicit work-ref is a git branch name (may
    # contain "/"), validated with git's own ref rules. Strip a leading origin/ or refs/heads/
    # prefix before validating, since the resolver normalizes those away at use time.
    if spec.work_ref is not None:
        _work_ref = spec.work_ref.strip()
        for _prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
            if _work_ref.startswith(_prefix):
                _work_ref = _work_ref[len(_prefix):]
                break
        _err = _validate_branch_ref(_work_ref)
        if _err is not None:
            return Reject(
                "bad-work-ref",
                f"work_ref {spec.work_ref!r} is not a valid branch name ({_err})",
            )

    # Rule 16 — review gate must be a known Gate enum member (OI-845 / fix-1588
    # advisory). Empty means "no gate assigned" (the smart router derives one
    # later); any non-empty value outside the closed enum is a typo (e.g.
    # "codex_gat") and must be refused at the door, not discovered later as a
    # silently-unknown gate that passes on file presence alone. Legacy lifecycle
    # phase names (LEGACY_GATE_SENTINELS) are admitted as the no-gate sentinel,
    # mirroring dispatch_bridge._canonical_gate.
    _gate_name = (spec.gate or "").strip()
    if (
        _gate_name
        and _gate_name not in Gate._value2member_map_
        and _gate_name.lower() not in LEGACY_GATE_SENTINELS
    ):
        return Reject(
            "bad-gate",
            f"gate {spec.gate!r} is not a known review gate; "
            f"valid gates: {', '.join(sorted(Gate._value2member_map_))}",
        )

    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction_text,
        normalized_paths=tuple(normalized),
        instruction_sha256=computed_sha256,
    )
