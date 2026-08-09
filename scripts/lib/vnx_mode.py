#!/usr/bin/env python3
"""VNX Mode — detection, storage, and command gating.

Manages the two VNX execution modes (starter, operator) as defined
in the productization contract (PR-0). Mode is persisted in
``.vnx-data/mode.json`` and checked at command dispatch time.
(Demo mode was retired 2026-06-27, audit #9 — the `vnx demo` command was removed.)

Contracts:
  G-R2: Receipts and runtime state in all modes.
  A-R1: Starter and operator share the same canonical runtime model.
  Productization §2.4: mode.json is source of truth for current mode.
  Productization §7.5: No silent degradation — unavailable commands fail explicitly.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional


# ---------------------------------------------------------------------------
# Mode enum
# ---------------------------------------------------------------------------

class VNXMode(str, Enum):
    STARTER = "starter"
    OPERATOR = "operator"

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# Command tiers (productization contract §3.2)
# ---------------------------------------------------------------------------

TIER_UNIVERSAL: FrozenSet[str] = frozenset({
    "init", "migrate", "doctor", "status", "recover", "help", "update",
    "setup", "install-check", "install-validate",
})

TIER_STARTER_OPERATOR: FrozenSet[str] = frozenset({
    "staging-list", "promote", "queue-status", "gate-check", "suggest",
    "cost-report", "analyze-sessions", "intelligence-export",
    "intelligence-import", "init-feature", "bootstrap-skills",
    "bootstrap-terminals", "bootstrap-hooks", "regen-settings", "regen-worker-permissions",
    "skills", "role",
    "patch-agent-files", "register", "list-projects", "unregister",
    "roadmap", "insights", "objective", "horizon", "deliverable",
    "install-git-hooks", "uninstall-git-hooks", "install-shell-helper",
    "init-db",
})

TIER_OPERATOR_ONLY: FrozenSet[str] = frozenset({
    "start", "stop", "restart", "jump", "ps", "cleanup",
    "new-worktree", "finish-worktree", "worktree-start", "worktree-stop",
    "worktree-refresh", "worktree-status", "merge-preflight",
    "smoke", "package-check", "fabric-audit", "subsystems",
    "dispatch", "gate", "dream",
    "snapshot", "restore", "quiesce-check", "pause", "resume",
    "pool", "permission",
})

# Mode -> allowed command sets
MODE_COMMANDS: Dict[VNXMode, FrozenSet[str]] = {
    VNXMode.STARTER: TIER_UNIVERSAL | TIER_STARTER_OPERATOR,
    VNXMode.OPERATOR: TIER_UNIVERSAL | TIER_STARTER_OPERATOR | TIER_OPERATOR_ONLY,
}


# ---------------------------------------------------------------------------
# Mode file I/O
# ---------------------------------------------------------------------------

MODE_FILENAME = "mode.json"


def _mode_file_path(data_dir: Optional[str] = None) -> Path:
    """Return the path to mode.json, resolving the data dir like the rest of the fabric.

    Resolution order (the same two-key contract as ``project_root.resolve_data_dir``
    and ``vnx_paths.resolve_paths``):

      1. A caller-supplied ``data_dir`` wins (callers like ``vnx_setup`` /
         ``vnx_starter`` pass the already-resolved ``VNX_DATA_DIR``).
      2. ``VNX_DATA_DIR`` — honored ONLY when ``VNX_DATA_DIR_EXPLICIT=1`` is also
         set. A bare inherited ``VNX_DATA_DIR`` is pollution, not config.
      3. The fabric-controlled store for the active project:
         ``vnx_paths.resolve_paths()["VNX_DATA_DIR"]`` — the SAME resolver the rest
         of the fabric uses (``vnx_init``, ``vnx_setup``, ``vnx_starter``), which
         applies the two-key contract and the data-dir/project-id guard.

    Previously this read ``os.environ["VNX_DATA_DIR"]`` raw — the fourth data-dir
    resolver in the fabric and the only one without any protection. A suite-wide
    test run that pinned a scratch pad with ``VNX_DATA_DIR_EXPLICIT=1`` lost the
    flag in a cleaned-env subprocess and the write silently fell back to
    ``~/.vnx-data/<project_id>`` (OI-911).
    """
    if data_dir:
        return Path(data_dir).expanduser().resolve() / MODE_FILENAME

    explicit_val = os.environ.get("VNX_DATA_DIR", "").strip()
    if explicit_val and os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1":
        return Path(explicit_val).expanduser().resolve() / MODE_FILENAME

    # Deferred import: vnx_paths pulls in data_dir_guard / project_root, which
    # must not be loaded at vnx_mode import time (vnx_mode is imported by the
    # bash CLI from minimal PYTHONPATHs). resolve_paths itself warns about a bare
    # VNX_DATA_DIR and runs the project-id guard on the resolved store.
    from vnx_paths import resolve_paths
    resolved = Path(resolve_paths()["VNX_DATA_DIR"]).expanduser().resolve()
    return resolved / MODE_FILENAME


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON atomically via temp-file-then-rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def read_mode(data_dir: Optional[str] = None) -> Optional[VNXMode]:
    """Read current mode from mode.json. Returns None if not initialized."""
    try:
        path = _mode_file_path(data_dir)
    except RuntimeError:
        return None
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return VNXMode(data["mode"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _guard_mode_write_target(target_dir: Path) -> None:
    """Fail loud when a mode.json write would land outside the active project store.

    Three checks (OI-911 + w19c/OI-934):

    1. Divergence guard. When ``VNX_DATA_DIR`` is set WITHOUT
       ``VNX_DATA_DIR_EXPLICIT=1``, the two-key contract treats it as inherited
       pollution and the fabric resolver falls back to the resolved store. If
       that fallback diverges from the env value, the process was configured for
       a different data dir and writing mode.json to the fallback store is
       exactly the OI-911 test-run incident. Refuse.
    2. Test-isolation guard (w19c/OI-934). Under pytest, refuse a write into
       the REAL central store outright — even with NO env override at all
       (nothing to "diverge" from), even when the resolved project_id happens
       to match (the gap the divergence check above cannot see: a completely
       clean test env resolves to this repo's own real ``~/.vnx-data/vnx-dev``
       "correctly", and correctly is still wrong for a test — this is what let
       a suite-wide pytest run flip the live mode.json from operator to
       starter with no divergence to catch), and even when project_id is NOT
       resolvable at all (w22/PR#1333: a subprocess with a cleaned env has no
       ``VNX_PROJECT_ID``, no reachable ``.vnx-project-id`` marker, and often
       no git remote either — ``resolve_project_id()`` raising is exactly the
       scenario this guard exists for, not a reason to skip it).
    3. Cross-project guard. A write target under ``~/.vnx-data/<other>`` while
       the resolved project_id is not ``<other>`` is a cross-project write.

    When no project_id is resolvable the cross-project half cannot verify a
    mismatch and stays silent about THAT specifically (same contract as
    ``data_dir_guard``) — but the test-isolation half does not need a
    project_id at all, so it still runs and still refuses a central-store
    write under pytest even when project_id resolution fails or returns
    empty. The divergence half is likewise independent of project_id and
    always runs.
    """
    try:
        target = Path(target_dir).expanduser().resolve()
    except OSError:
        return

    env_val = os.environ.get("VNX_DATA_DIR", "").strip()
    explicit = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    if env_val and not explicit:
        env_path = Path(env_val).expanduser().resolve()
        if env_path != target:
            raise RuntimeError(
                f"mode.json write target {target} diverges from the inherited "
                f"VNX_DATA_DIR={env_val} without VNX_DATA_DIR_EXPLICIT=1. A bare "
                "VNX_DATA_DIR is pollution, not config: refusing to write mode.json "
                "to a store the environment did not pin (OI-911). Set "
                "VNX_DATA_DIR_EXPLICIT=1 to opt in explicitly."
            )

    home_vnx = Path.home() / ".vnx-data"
    try:
        rel = target.relative_to(home_vnx)
    except ValueError:
        return  # repo-local / scratch / XDG — not a central-store path

    from project_root import resolve_project_id
    from vnx_paths import refuse_real_central_store_write_under_pytest
    try:
        pid = resolve_project_id()
    except RuntimeError:
        # Cannot verify the cross-project half without a project_id, but the
        # test-isolation half (w19c/OI-934) does not depend on one: a
        # subprocess with a cleaned env is exactly the scenario where
        # resolve_project_id() fails, and exactly the scenario this guard
        # exists for (w22/PR#1333). Run it before allowing the write.
        refuse_real_central_store_write_under_pytest(target)
        return
    pid = pid.strip()
    if not pid:
        refuse_real_central_store_write_under_pytest(target)
        return
    expected = home_vnx / pid
    if target == expected or str(target).startswith(str(expected) + os.sep):
        # No divergence, no cross-project mismatch — by OI-911's own checks
        # this write is "correct". Still refuse it under pytest (w19c/OI-934):
        # a completely clean test env resolving "correctly" to this repo's
        # real ~/.vnx-data/vnx-dev is exactly the gap those two checks can't
        # see, since there is neither a divergence nor a project mismatch to
        # catch.
        refuse_real_central_store_write_under_pytest(target)
        return
    raise RuntimeError(
        f"mode.json write target {target} is {rel.parts[0]!r}'s central store, "
        f"but the active project resolves to {pid!r}. Refusing to write mode.json "
        "into another project's store (OI-911). Set VNX_PROJECT_ID or run from "
        "the correct project."
    )


def write_mode(mode: VNXMode, data_dir: Optional[str] = None) -> Path:
    """Write mode to mode.json atomically. Returns the path written.

    Refuses (raises) when the write target is not the active project's store:
    a write that diverges from a bare (non-explicit) ``VNX_DATA_DIR``, or that
    lands in another project's ``~/.vnx-data/<other>``, is rejected loudly
    (see :func:`_guard_mode_write_target`).
    """
    path = _mode_file_path(data_dir)
    _guard_mode_write_target(path.parent)
    payload = {
        "mode": str(mode),
        "set_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
    }
    _atomic_write_json(path, payload)
    return path


def read_mode_raw(data_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read the full mode.json document (for status display)."""
    try:
        path = _mode_file_path(data_dir)
    except RuntimeError:
        return None
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Command gating
# ---------------------------------------------------------------------------

# Cross-entrance name drift. The two VNX entrances (`bin/vnx` in the fabric
# repo and the pip-installed `vnx_cli`) expose the same surface under
# different spellings. A refused top-level name that exists as an alias or a
# sub-verb of a working command should point the user at the working form
# instead of only saying "requires a different mode" (OI-1084 / OI-1060).
#
# - ALIASES: ``vnx <name>`` is a working command in one entrance but only an
#   alias of ``ALIASES[name]`` in the other. ``dispatch`` works in ``bin/vnx``
#   but is ``dispatch-agent`` in the pip CLI (the pip CLI's ``dispatch`` is
#   reserved for a future direct-SDK lane that is not wired yet).
# - SUB_VERBS: ``vnx <name>`` is not a top-level command at all in either
#   entrance, but it is a sub-verb of ``SUB_VERBS[name]`` (e.g.
#   ``plan-gate`` lives under ``vnx horizon plan-gate``).
ALIASES: Dict[str, str] = {
    "dispatch": "dispatch-agent",
    "dispatch-agent": "dispatch",
}

SUB_VERBS: Dict[str, str] = {
    "plan-gate": "horizon",
}


def _suggest_working_form(command: str) -> str:
    """Return a human-readable hint naming the working form of a refused
    command, or ``""`` when no known alias/sub-verb matches.

    The suggestion is entrance-agnostic on purpose: it names both spellings so
    the user can pick whichever entrance they are in, instead of assuming a
    specific CLI. ``plan-gate`` -> ``vnx horizon plan-gate``; ``dispatch`` ->
    note that the pip CLI spells it ``dispatch-agent``.
    """
    if command in SUB_VERBS:
        parent = SUB_VERBS[command]
        return (f"'{command}' is a sub-verb of 'vnx {parent}' "
                f"(try: vnx {parent} {command}).")
    if command in ALIASES:
        return (f"'{command}' is spelled '{ALIASES[command]}' in the other "
                "VNX entrance (the fabric `bin/vnx` vs the pip `vnx`).")
    return ""


class ModeGateError(Exception):
    """Raised when a command is not available in the current mode."""

    def __init__(self, command: str, current_mode: VNXMode):
        self.command = command
        self.current_mode = current_mode
        if current_mode == VNXMode.STARTER:
            upgrade = "Run 'vnx init --operator' to upgrade."
        else:
            upgrade = ""
        suggestion = _suggest_working_form(command)
        suffix = f" {suggestion}" if suggestion else ""
        super().__init__(
            f"'{command}' requires a different mode (current: {current_mode}).{suffix} {upgrade}".strip()
        )


def check_command_allowed(command: str, mode: Optional[VNXMode] = None,
                          data_dir: Optional[str] = None) -> None:
    """Check if command is allowed in current mode. Raises ModeGateError if not.

    If mode is None, reads from mode.json. If mode.json doesn't exist,
    all commands are allowed (pre-init backward compatibility).
    """
    if mode is None:
        mode = read_mode(data_dir)
    if mode is None:
        # Not initialized yet — allow everything (backward compat)
        return
    allowed = MODE_COMMANDS.get(mode, frozenset())
    if command not in allowed:
        raise ModeGateError(command, mode)


def get_available_commands(mode: Optional[VNXMode] = None,
                           data_dir: Optional[str] = None) -> FrozenSet[str]:
    """Return the set of commands available in the current or given mode."""
    if mode is None:
        mode = read_mode(data_dir)
    if mode is None:
        # Pre-init: all commands
        return TIER_UNIVERSAL | TIER_STARTER_OPERATOR | TIER_OPERATOR_ONLY
    return MODE_COMMANDS.get(mode, frozenset())


def get_mode_description(mode: VNXMode) -> str:
    """Return a human-readable description of the mode."""
    descriptions = {
        VNXMode.STARTER: "Single terminal, one AI provider, sequential dispatch. No tmux required.",
        VNXMode.OPERATOR: "Full multi-agent orchestration with tmux grid, multiple providers, and all governance controls.",
    }
    return descriptions.get(mode, "Unknown mode")


# ---------------------------------------------------------------------------
# Feature flags for rollback control
# ---------------------------------------------------------------------------

FEATURE_FLAGS = {
    "VNX_STARTER_MODE_ENABLED": ("1", "Enable starter mode (set '0' to disable)"),
    "VNX_MODE_GATING_ENABLED": ("1", "Enable command gating by mode (set '0' for backward compat)"),
}


def is_feature_enabled(flag_name: str) -> bool:
    """Check if a feature flag is enabled. Defaults from FEATURE_FLAGS."""
    default, _ = FEATURE_FLAGS.get(flag_name, ("0", ""))
    return os.environ.get(flag_name, default) == "1"


def check_mode_feature_enabled(mode: VNXMode) -> bool:
    """Check if the given mode is enabled via feature flags."""
    flag_map = {
        VNXMode.STARTER: "VNX_STARTER_MODE_ENABLED",
    }
    flag = flag_map.get(mode)
    if flag is None:
        return True  # Operator mode always enabled
    return is_feature_enabled(flag)


# ---------------------------------------------------------------------------
# CLI entrypoint (for direct testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    from vnx_paths import ensure_env
    ensure_env()

    mode = read_mode()
    if mode:
        print(f"Current mode: {mode}")
        print(f"Description: {get_mode_description(mode)}")
        raw = read_mode_raw()
        if raw:
            print(f"Set at: {raw.get('set_at', 'unknown')}")
        print(f"Available commands: {len(get_available_commands(mode))}")
    else:
        print("No mode set (pre-init state)")
    sys.exit(0)
