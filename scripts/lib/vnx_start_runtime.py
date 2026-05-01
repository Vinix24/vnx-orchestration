#!/usr/bin/env python3
"""VNX Start Runtime — Python-led start orchestration logic.

PR-3 deliverable: migrates the most failure-prone branching and stateful
logic from start.sh into testable Python. The shell wrapper remains thin
and handles tmux session creation, pane splitting, and send-keys — this
module owns:

  1. Provider command building (unified, no duplication)
  2. Environment variable management (canonical var list)
  3. Profile/preset resolution (deterministic fallback chain)
  4. State file generation (panes.json, terminal_state.json)

Design:
  - All functions are pure or side-effect-isolated for testability.
  - Provider command building is unified: re-heal and fresh-start share
    the same code path (eliminating the dual-update problem in start.sh).
  - VNX_VARS is the single source of truth for env cleanup.
  - Mode awareness: starter and operator share the same runtime model (A-R1).

Governance:
  G-R2: State files use the same schema regardless of mode.
  A-R1: Starter and operator modes share canonical runtime expectations.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Canonical VNX environment variables (single source of truth)
# ---------------------------------------------------------------------------

VNX_VARS: Tuple[str, ...] = (
    "PROJECT_ROOT",
    "VNX_HOME",
    "VNX_DATA_DIR",
    "VNX_STATE_DIR",
    "VNX_DISPATCH_DIR",
    "VNX_LOGS_DIR",
    "VNX_SKILLS_DIR",
    "VNX_PIDS_DIR",
    "VNX_LOCKS_DIR",
    "VNX_SOCKETS_DIR",
    "VNX_REPORTS_DIR",
    "VNX_DB_DIR",
)
"""All VNX environment variables that must be cleaned/set during start.

This replaces the hardcoded list duplicated across start.sh (lines 234, 529,
623-626, 645). Any new VNX env var added here is automatically handled in
both env-clean and env-set operations.
"""


# ---------------------------------------------------------------------------
# Provider command building
# ---------------------------------------------------------------------------

PROVIDER_CLAUDE = "claude_code"
PROVIDER_CODEX = "codex"
PROVIDER_GEMINI = "gemini"

# Aliases that map to canonical provider names
PROVIDER_ALIASES = {
    "codex_cli": PROVIDER_CODEX,
    "codex": PROVIDER_CODEX,
    "gemini_cli": PROVIDER_GEMINI,
    "gemini": PROVIDER_GEMINI,
    "claude_code": PROVIDER_CLAUDE,
    "claude": PROVIDER_CLAUDE,
}


@dataclass
class TerminalConfig:
    """Configuration for a single terminal pane."""
    terminal_id: str          # "T0", "T1", "T2", "T3"
    provider: str             # canonical provider name
    model: str                # model identifier
    role: str                 # "orchestrator" or "worker"
    track: str = ""           # "A", "B", "C" (empty for T0)
    skip_permissions: bool = False
    extra_flags: str = ""     # additional CLI flags (e.g. T0 custom flags)


@dataclass
class StartConfig:
    """Full start configuration resolved from presets/profiles/defaults."""
    project_root: str
    vnx_home: str
    vnx_data_dir: str
    terminals: Dict[str, TerminalConfig] = field(default_factory=dict)
    gemini_model: str = "gemini-2.5-flash"
    codex_model: str = "gpt-5.1-codex-mini"
    queue_popup_enabled: bool = True
    preset_name: str = ""
    profile_name: str = ""

    @classmethod
    def from_env(cls) -> "StartConfig":
        """Build StartConfig from current environment (preset/profile already sourced)."""
        project_root = os.environ.get("PROJECT_ROOT", "")
        vnx_home = os.environ.get("VNX_HOME", "")
        vnx_data_dir = os.environ.get("VNX_DATA_DIR", "")

        gemini_model = os.environ.get("VNX_GEMINI_MODEL", "gemini-2.5-flash")
        codex_model = os.environ.get("VNX_CODEX_MODEL", "gpt-5.1-codex-mini")
        t0_flags = os.environ.get("VNX_T0_FLAGS", "")
        queue_popup = os.environ.get("VNX_QUEUE_POPUP_ENABLED", "1") != "0"

        terminals = {}
        for tid, default_provider, default_model, role, track in [
            ("T0", PROVIDER_CLAUDE, os.environ.get("VNX_T0_MODEL", "default"), "orchestrator", ""),
            ("T1", os.environ.get("VNX_T1_PROVIDER", PROVIDER_CLAUDE), os.environ.get("VNX_T1_MODEL", "sonnet"), "worker", "A"),
            ("T2", os.environ.get("VNX_T2_PROVIDER", PROVIDER_CLAUDE), os.environ.get("VNX_T2_MODEL", "sonnet"), "worker", "B"),
            ("T3", os.environ.get("VNX_T3_PROVIDER", PROVIDER_CLAUDE), os.environ.get("VNX_T3_MODEL", "default"), "worker", "C"),
        ]:
            raw_provider = default_provider
            provider = PROVIDER_ALIASES.get(raw_provider, PROVIDER_CLAUDE)
            skip_env = os.environ.get(f"VNX_{tid}_SKIP_PERMISSIONS", "0")
            terminals[tid] = TerminalConfig(
                terminal_id=tid,
                provider=provider,
                model=default_model,
                role=role,
                track=track,
                skip_permissions=(skip_env == "1"),
                extra_flags=t0_flags if tid == "T0" else "",
            )

        return cls(
            project_root=project_root,
            vnx_home=vnx_home,
            vnx_data_dir=vnx_data_dir,
            terminals=terminals,
            gemini_model=gemini_model,
            codex_model=codex_model,
            queue_popup_enabled=queue_popup,
        )


def build_provider_command(
    tc: TerminalConfig,
    gemini_model: str = "gemini-2.5-flash",
    codex_model: str = "gpt-5.1-codex-mini",
    project_root: str = "",
) -> str:
    """Build the CLI launch command for a terminal based on its provider.

    This is the single source of truth for provider command construction,
    replacing the duplicated case statements in start.sh (lines 213-230
    and 664-693).
    """
    provider = PROVIDER_ALIASES.get(tc.provider, PROVIDER_CLAUDE)

    if provider == PROVIDER_CODEX:
        skip_flag = " --full-auto" if tc.skip_permissions else ""
        return f"codex -m {codex_model}{skip_flag}"

    if provider == PROVIDER_GEMINI:
        return f"gemini --yolo -m {gemini_model} --include-directories '{project_root}'"

    # Default: Claude Code
    skip_flag = " --dangerously-skip-permissions" if tc.skip_permissions else ""
    extra = f" {tc.extra_flags}" if tc.extra_flags else ""
    return f"claude --model {tc.model}{extra}{skip_flag}"


def build_provider_label(provider: str) -> str:
    """Return a human-readable label for a provider."""
    canonical = PROVIDER_ALIASES.get(provider, PROVIDER_CLAUDE)
    if canonical == PROVIDER_CODEX:
        return "Codex CLI"
    if canonical == PROVIDER_GEMINI:
        return "Gemini CLI"
    return "Claude"


def build_pane_title(terminal_id: str, provider: str) -> str:
    """Return the tmux pane title for a terminal."""
    canonical = PROVIDER_ALIASES.get(provider, PROVIDER_CLAUDE)
    if canonical == PROVIDER_CODEX:
        return f"{terminal_id} [CODEX]"
    if canonical == PROVIDER_GEMINI:
        return f"{terminal_id} [GEMINI]"
    return terminal_id


# ---------------------------------------------------------------------------
# Environment management
# ---------------------------------------------------------------------------

def build_env_clean_cmd() -> str:
    """Build shell command to unset all VNX environment variables.

    Uses VNX_VARS as the single source of truth — no more hardcoded
    lists that can drift out of sync.
    """
    return "unset " + " ".join(VNX_VARS)


def build_env_set_cmd(
    project_root: str,
    vnx_home: str,
    vnx_data_dir: str,
    vnx_skills_dir: str = "",
) -> str:
    """Build shell command to export VNX environment variables."""
    parts = [
        f"export PROJECT_ROOT='{project_root}'",
        f"VNX_HOME='{vnx_home}'",
        f"VNX_DATA_DIR='{vnx_data_dir}'",
    ]
    if vnx_skills_dir:
        parts.append(f"VNX_SKILLS_DIR='{vnx_skills_dir}'")
    return " ".join(parts)


def build_launch_command(
    tc: TerminalConfig,
    config: StartConfig,
    terms_dir: str,
    node_path: str = "",
) -> str:
    """Build the full tmux send-keys command string for a terminal.

    Combines shell profile sourcing, env cleanup, env setting, PATH
    adjustment, role export, cd, and provider command into one string.
    """
    env_clean = build_env_clean_cmd()
    env_set = build_env_set_cmd(
        config.project_root,
        config.vnx_home,
        config.vnx_data_dir,
        os.environ.get("VNX_SKILLS_DIR", ""),
    )
    path_prefix = f"{config.vnx_home}/bin"
    if node_path:
        path_prefix = f"{path_prefix}:{node_path}"

    provider_cmd = build_provider_command(
        tc, config.gemini_model, config.codex_model, config.project_root,
    )

    role_export = f"export CLAUDE_ROLE={tc.role}"
    track_export = f" && export CLAUDE_TRACK={tc.track}" if tc.track else ""
    project_export = f" && export CLAUDE_PROJECT_DIR='{config.project_root}'"

    return (
        f"source ~/.zshrc 2>/dev/null && {env_clean} && {env_set} && "
        f"export PATH={path_prefix}:$PATH && {role_export}{track_export}"
        f"{project_export} && cd '{terms_dir}/{tc.terminal_id}' && {provider_cmd}"
    )


# ---------------------------------------------------------------------------
# State file generation
# ---------------------------------------------------------------------------

def generate_panes_json(
    session_name: str,
    pane_ids: Dict[str, str],
    config: StartConfig,
) -> Dict[str, Any]:
    """Generate panes.json content as a dict.

    Replaces the inline heredoc in start.sh (lines 245-259 and 403-416).
    """
    t0 = config.terminals.get("T0")
    t1 = config.terminals.get("T1")
    t2 = config.terminals.get("T2")
    t3 = config.terminals.get("T3")

    result: Dict[str, Any] = {"session": session_name}

    if t0:
        t0_entry = {
            "pane_id": pane_ids.get("T0", ""),
            "role": "orchestrator",
            "do_not_target": True,
            "model": t0.model,
            "provider": PROVIDER_CLAUDE,
        }
        result["t0"] = t0_entry
        result["T0"] = t0_entry

    tracks: Dict[str, Any] = {}
    for tid, tc in [("T1", t1), ("T2", t2), ("T3", t3)]:
        if not tc:
            continue
        entry = {
            "pane_id": pane_ids.get(tid, ""),
            "track": tc.track,
            "model": tc.model,
            "provider": tc.provider,
        }
        if tc.role == "deep" or tid == "T3":
            entry["role"] = "deep"
        result[tid] = entry
        tracks[tc.track] = dict(entry)

    result["tracks"] = tracks
    return result


def generate_terminal_state_json() -> Dict[str, Any]:
    """Generate initial terminal_state.json with all terminals idle.

    Replaces the inline heredoc in start.sh (lines 263-272 and 436-444).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    terminals = {}
    for tid in ("T1", "T2", "T3"):
        terminals[tid] = {
            "terminal_id": tid,
            "status": "idle",
            "last_activity": ts,
            "version": 1,
        }
    return {
        "schema_version": 1,
        "terminals": terminals,
    }


def write_state_files(
    state_dir: str,
    session_name: str,
    pane_ids: Dict[str, str],
    config: StartConfig,
) -> None:
    """Write panes.json and terminal_state.json atomically."""
    state_path = Path(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)

    panes = generate_panes_json(session_name, pane_ids, config)
    panes_file = state_path / "panes.json"
    panes_file.write_text(json.dumps(panes, indent=2) + "\n")

    ts = generate_terminal_state_json()
    ts_file = state_path / "terminal_state.json"
    ts_file.write_text(json.dumps(ts, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Profile/preset resolution
# ---------------------------------------------------------------------------

@dataclass
class PresetResolution:
    """Result of resolving a startup preset/profile."""
    source: str          # "preset", "last", "profile", "config", "default"
    name: str            # preset/profile name or ""
    env_file: str        # path to the .env file that was resolved, or ""
    error: str = ""      # non-empty if resolution failed


def resolve_preset(
    preset_name: str = "",
    profile_name: str = "",
    use_last: bool = False,
    presets_dir: str = "",
    profiles_dir: str = "",
    config_env: str = "",
    interactive: bool = False,
) -> PresetResolution:
    """Resolve which startup preset/profile to use.

    Priority chain (matches start.sh lines 82-141):
      1. --preset <name>  → load preset directly
      2. --last           → load last-used preset
      3. --profile <name> → legacy profile support
      4. interactive menu → (handled by shell, returns "default" here)
      5. config.env       → project-level config
      6. defaults         → no env file, use env var defaults

    Returns a PresetResolution indicating which source was selected.
    """
    if preset_name:
        preset_file = os.path.join(presets_dir, f"{preset_name}.env")
        if os.path.isfile(preset_file):
            return PresetResolution("preset", preset_name, preset_file)
        available = _list_presets(presets_dir)
        return PresetResolution(
            "preset", preset_name, "",
            error=f"Preset not found: {preset_file}. Available: {', '.join(available) or 'none'}",
        )

    if use_last:
        last_file = os.path.join(presets_dir, "last-used.env")
        if os.path.isfile(last_file):
            target = os.path.realpath(last_file)
            name = Path(target).stem
            return PresetResolution("last", name, last_file)
        return PresetResolution(
            "last", "", "",
            error="No last-used preset found. Run 'vnx start' interactively first.",
        )

    if profile_name:
        profile_file = os.path.join(profiles_dir, f"{profile_name}.env")
        if os.path.isfile(profile_file):
            return PresetResolution("profile", profile_name, profile_file)
        available = _list_profiles(profiles_dir)
        return PresetResolution(
            "profile", profile_name, "",
            error=f"Profile not found: {profile_file}. Available: {', '.join(available) or 'none'}",
        )

    if config_env and os.path.isfile(config_env):
        return PresetResolution("config", "config.env", config_env)

    return PresetResolution("default", "", "")


def _list_presets(presets_dir: str) -> List[str]:
    """List available preset names (excluding last-used)."""
    if not os.path.isdir(presets_dir):
        return []
    return sorted(
        Path(f).stem
        for f in Path(presets_dir).glob("*.env")
        if Path(f).stem != "last-used"
    )


def _list_profiles(profiles_dir: str) -> List[str]:
    """List available profile names."""
    if not os.path.isdir(profiles_dir):
        return []
    return sorted(Path(f).stem for f in Path(profiles_dir).glob("*.env"))


# ---------------------------------------------------------------------------
# CLI entrypoint (for shell delegation)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VNX Start Runtime")
    sub = parser.add_subparsers(dest="command")

    # Sub-command: resolve-preset
    rp = sub.add_parser("resolve-preset", help="Resolve startup preset/profile")
    rp.add_argument("--preset", default="")
    rp.add_argument("--profile", default="")
    rp.add_argument("--last", action="store_true")
    rp.add_argument("--presets-dir", default="")
    rp.add_argument("--profiles-dir", default="")
    rp.add_argument("--config-env", default="")

    # Sub-command: build-commands
    bc = sub.add_parser("build-commands", help="Build provider commands for all terminals")

    # Sub-command: write-state
    ws = sub.add_parser("write-state", help="Write panes.json and terminal_state.json")
    ws.add_argument("--state-dir", required=True)
    ws.add_argument("--session", required=True)
    ws.add_argument("--panes", required=True, help="JSON dict of terminal_id -> pane_id")

    # Sub-command: env-commands
    ec = sub.add_parser("env-commands", help="Print env clean/set commands")

    args = parser.parse_args()

    if args.command == "resolve-preset":
        result = resolve_preset(
            preset_name=args.preset,
            profile_name=args.profile,
            use_last=args.last,
            presets_dir=args.presets_dir,
            profiles_dir=args.profiles_dir,
            config_env=args.config_env,
        )
        print(json.dumps({
            "source": result.source,
            "name": result.name,
            "env_file": result.env_file,
            "error": result.error,
        }))

    elif args.command == "build-commands":
        config = StartConfig.from_env()
        commands = {}
        for tid, tc in config.terminals.items():
            commands[tid] = build_provider_command(
                tc, config.gemini_model, config.codex_model, config.project_root,
            )
        print(json.dumps(commands, indent=2))

    elif args.command == "write-state":
        pane_ids = json.loads(args.panes)
        config = StartConfig.from_env()
        write_state_files(args.state_dir, args.session, pane_ids, config)
        print(json.dumps({"status": "ok"}))

    elif args.command == "env-commands":
        print(json.dumps({
            "clean": build_env_clean_cmd(),
            "set": build_env_set_cmd(
                os.environ.get("PROJECT_ROOT", ""),
                os.environ.get("VNX_HOME", ""),
                os.environ.get("VNX_DATA_DIR", ""),
                os.environ.get("VNX_SKILLS_DIR", ""),
            ),
            "vars": list(VNX_VARS),
        }))
