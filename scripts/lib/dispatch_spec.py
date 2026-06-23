"""dispatch_spec.py — DispatchSpec: the typed input surface for the single-entry dispatch gate.

Pure types + one validate() function. No side effects beyond reading the instruction file.
Nothing imports this module in PR-1; it is wired in later PRs.

ADR-006: provider constraint enum enforces legal routing strings.
ADR-007: not triggered here — no new table, pure in-process types only.
"""

from __future__ import annotations

import hashlib
import os
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


class Isolation(str, Enum):
    WORKTREE = "worktree"  # the ONLY legal value in 1.0 — every worker spawn is isolated, fail-loud


class PathAccess(str, Enum):
    READ       = "read"
    WRITE      = "write"
    READ_WRITE = "read_write"
    CREATE     = "create"


# ---------------------------------------------------------------------------
# Path type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchPath:
    path: PurePosixPath
    access: PathAccess = PathAccess.READ_WRITE
    materialize_at_cwd: bool = False


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
    pr_id: Optional[str] = None
    deadline_seconds: int = 3600
    base_ref: str = "origin/main"
    isolation: Isolation = Isolation.WORKTREE
    requires_mcp: bool = False
    target_id_override: Optional[str] = None
    tags: tuple[str, ...] = ()
    instruction_sha256: Optional[str] = None  # P0-3: caller may pre-bind hash; validate() verifies
    allow_headless: bool = False              # PR-5: explicit opt-in to api_metered headless lane
    headless_reason: Optional[str] = None    # PR-5: mandatory non-empty reason when allow_headless=True
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

_BLOCKED_FIRST_COMPONENTS = frozenset({".git", ".vnx-data"})

_VALID_TARGET_SLOTS = frozenset({"T0", "T1", "T2", "T3"})


def _resolve_project_id() -> str:
    return os.environ.get("VNX_PROJECT_ID", "vnx-dev")


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

    # Rule 2 — project_id must match the caller's resolved project_id
    resolved = _resolve_project_id()
    if spec.project_id != resolved:
        return Reject(
            "project-mismatch",
            f"spec.project_id={spec.project_id!r} != resolved project_id={resolved!r}; "
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
    # Tight role/skill validation (against the installed skill registry) is deferred to
    # compile_plan, which has access to runtime paths. Here we only require non-empty.
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

    # Rule 11 — deadline bounds
    if not (60 <= spec.deadline_seconds <= 14400):
        return Reject(
            "bad-deadline",
            f"deadline_seconds must be in [60, 14400], got {spec.deadline_seconds}",
        )

    # Rule 12 — headless opt-in requires a non-empty reason (PR-5)
    if spec.allow_headless:
        reason = (spec.headless_reason or "").strip()
        if not reason:
            return Reject(
                "headless-reason-required",
                "allow_headless=True requires a non-empty headless_reason explaining "
                "the API billing opt-in; set headless_reason to a human-readable justification",
            )
        # MED: headless is only valid for claude (or auto that could resolve to claude)
        if spec.provider not in (Provider.CLAUDE, Provider.AUTO):
            return Reject(
                "headless-claude-only",
                f"allow_headless is only valid for provider=claude, got provider={spec.provider.value!r}; "
                "headless api-metered billing is a claude-only lane",
            )

    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction_text,
        normalized_paths=tuple(normalized),
        instruction_sha256=computed_sha256,
    )
