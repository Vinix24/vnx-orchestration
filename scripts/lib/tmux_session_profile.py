#!/usr/bin/env python3
"""
VNX tmux Session Profile — Declarative session layout model.

Separates terminal *identity* from tmux *pane mechanics*. The profile is the
authoritative declaration of what a correct VNX session looks like, enabling:

  - Reproducible session creation and rebuild
  - Remap detection: pane IDs changed, terminal identity remains stable
  - Reheal: rediscover panes by working directory, not by positional index

Architecture rules honoured:
  A-R4 — pane IDs are derived state; terminal identity stays canonical
  A-R5 — profile recovery does not depend on hard-coded pane index assumptions
  G-R4 — tmux layout changes cannot redefine terminal identity

Profile storage: session_profile.json in the runtime state directory (VNX_STATE_DIR)

CLI usage:
  python3 tmux_session_profile.py save   --state-dir <path> --session <name>
  python3 tmux_session_profile.py verify --state-dir <path> --session <name>
  python3 tmux_session_profile.py remap  --state-dir <path> --terminal T2 --pane-id %7
  python3 tmux_session_profile.py reheal --state-dir <path> --session <name> --project-root <path>
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
PROFILE_FILENAME = "session_profile.json"

# Terminal IDs that constitute the home layout
HOME_TERMINALS = ("T0", "T1", "T2", "T3")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PaneProfile:
    """Declared identity and configuration for one tmux pane.

    pane_id is *derived state* only (A-R4). It is updated on remap but does
    not affect dispatch or lease state in runtime_coordination.db.
    """
    terminal_id: str        # Canonical: "T0", "T1", "T2", "T3"
    role: str               # "orchestrator", "worker", "deep"
    pane_id: str            # tmux pane ID (e.g. "%3") — derived, may drift
    provider: str = "claude_code"
    model: str = "default"
    track: Optional[str] = None   # "A", "B", "C" for workers; None for T0
    work_dir: str = ""      # Absolute path to terminal CWD (identity anchor)


@dataclass
class WindowProfile:
    """Declared configuration for one tmux window."""
    name: str               # tmux window name (e.g. "main", "ops", "recovery")
    window_type: str        # "home" | "ops" | "recovery" | "events"
    panes: List[PaneProfile] = field(default_factory=list)


@dataclass
class SessionProfile:
    """Full declared state for a VNX tmux session.

    home_window: the stable 2×2 T0/T1/T2/T3 layout.
    dynamic_windows: ops/recovery/events windows created as needed.
    """
    session_name: str
    home_window: WindowProfile
    dynamic_windows: List[WindowProfile] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""

    def get_pane(self, terminal_id: str) -> Optional[PaneProfile]:
        """Return PaneProfile for terminal_id from home or dynamic windows."""
        for pane in self.home_window.panes:
            if pane.terminal_id == terminal_id:
                return pane
        for win in self.dynamic_windows:
            for pane in win.panes:
                if pane.terminal_id == terminal_id:
                    return pane
        return None

    def all_panes(self) -> List[PaneProfile]:
        """Return all PaneProfiles across all windows."""
        result = list(self.home_window.panes)
        for win in self.dynamic_windows:
            result.extend(win.panes)
        return result


@dataclass
class ProfileDrift:
    """Result of verify_profile_integrity().

    correct:          terminal IDs whose pane_id is still live in tmux.
    stale:            terminal IDs whose pane_id is gone but workdir matched → can remap.
    missing:          terminal IDs whose workdir was not found in the live session.
    remap_candidates: terminal_id → new_pane_id for all stale entries.
    is_clean:         True only when stale and missing are both empty.
    """
    correct: List[str] = field(default_factory=list)
    stale: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    remap_candidates: Dict[str, str] = field(default_factory=dict)
    is_clean: bool = True

    def summary(self) -> str:
        parts = []
        if self.correct:
            parts.append(f"correct={self.correct}")
        if self.stale:
            parts.append(f"stale={self.stale}")
        if self.missing:
            parts.append(f"missing={self.missing}")
        return "ProfileDrift(" + ", ".join(parts) + ")"


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _profile_to_dict(profile: SessionProfile) -> Dict[str, Any]:
    """Convert SessionProfile to a JSON-serialisable dict."""
    def _pane_dict(p: PaneProfile) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "terminal_id": p.terminal_id,
            "role": p.role,
            "pane_id": p.pane_id,
            "provider": p.provider,
            "model": p.model,
            "work_dir": p.work_dir,
        }
        if p.track is not None:
            d["track"] = p.track
        return d

    def _window_dict(w: WindowProfile) -> Dict[str, Any]:
        return {
            "name": w.name,
            "window_type": w.window_type,
            "panes": [_pane_dict(p) for p in w.panes],
        }

    return {
        "schema_version": profile.schema_version,
        "session_name": profile.session_name,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "home_window": _window_dict(profile.home_window),
        "dynamic_windows": [_window_dict(w) for w in profile.dynamic_windows],
    }


def _pane_from_dict(d: Dict[str, Any]) -> PaneProfile:
    return PaneProfile(
        terminal_id=d["terminal_id"],
        role=d.get("role", "worker"),
        pane_id=d.get("pane_id", ""),
        provider=d.get("provider", "claude_code"),
        model=d.get("model", "default"),
        track=d.get("track"),
        work_dir=d.get("work_dir", ""),
    )


def _window_from_dict(d: Dict[str, Any]) -> WindowProfile:
    return WindowProfile(
        name=d["name"],
        window_type=d.get("window_type", "home"),
        panes=[_pane_from_dict(p) for p in d.get("panes", [])],
    )


def _profile_from_dict(d: Dict[str, Any]) -> SessionProfile:
    return SessionProfile(
        session_name=d["session_name"],
        home_window=_window_from_dict(d["home_window"]),
        dynamic_windows=[_window_from_dict(w) for w in d.get("dynamic_windows", [])],
        schema_version=d.get("schema_version", SCHEMA_VERSION),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
    )


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def save_session_profile(profile: SessionProfile, state_dir: Path) -> None:
    """Persist profile to session_profile.json in state_dir."""
    profile.updated_at = _now_iso()
    if not profile.created_at:
        profile.created_at = profile.updated_at
    out = state_dir / PROFILE_FILENAME
    out.write_text(json.dumps(_profile_to_dict(profile), indent=2), encoding="utf-8")


def load_session_profile(state_dir: Path) -> Optional[SessionProfile]:
    """Load session_profile.json from state_dir. Returns None if absent/corrupt."""
    path = state_dir / PROFILE_FILENAME
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return _profile_from_dict(d)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def generate_session_profile(
    session_name: str,
    panes_json: Dict[str, Any],
    project_root: str = "",
) -> SessionProfile:
    """Build a SessionProfile from an existing panes.json dict.

    panes.json is the tmux adapter's source of truth for pane IDs.
    This function lifts it into a declarative profile that also records
    work_dirs (the stable identity anchor for reheal).

    Args:
        session_name:  tmux session name (e.g. "vnx-project").
        panes_json:    Parsed panes.json content.
        project_root:  Project root path (used to derive work_dirs if not in panes_json).
    """
    terms_base = str(Path(project_root) / ".claude" / "terminals") if project_root else ""

    def _work_dir(tid: str, entry: Dict[str, Any]) -> str:
        if entry.get("work_dir"):
            return entry["work_dir"]
        if terms_base:
            return str(Path(terms_base) / tid)
        return ""

    home_panes: List[PaneProfile] = []

    for tid in HOME_TERMINALS:
        entry = panes_json.get(tid) or panes_json.get(tid.lower()) or {}
        if tid == "T0":
            role = "orchestrator"
            track = None
        elif tid == "T3":
            role = "deep"
            track = "C"
        else:
            role = "worker"
            track = entry.get("track")

        # Infer track from position if not explicitly set
        if track is None and tid in ("T1", "T2"):
            track = {"T1": "A", "T2": "B"}.get(tid)

        home_panes.append(PaneProfile(
            terminal_id=tid,
            role=role,
            pane_id=entry.get("pane_id", ""),
            provider=entry.get("provider", "claude_code"),
            model=entry.get("model", "default"),
            track=track,
            work_dir=_work_dir(tid, entry),
        ))

    home_window = WindowProfile(
        name="main",
        window_type="home",
        panes=home_panes,
    )
    return SessionProfile(
        session_name=session_name,
        home_window=home_window,
        created_at=_now_iso(),
    )


def profile_to_panes_json(profile: SessionProfile) -> Dict[str, Any]:
    """Convert a SessionProfile back to panes.json format.

    Useful for rebuilding panes.json from a saved profile (e.g. after remap).
    The returned dict is compatible with tmux_adapter.resolve_pane().
    """
    result: Dict[str, Any] = {"session": profile.session_name}

    for pane in profile.home_window.panes:
        entry: Dict[str, Any] = {
            "pane_id": pane.pane_id,
            "provider": pane.provider,
            "model": pane.model,
        }
        if pane.role == "orchestrator":
            entry["role"] = "orchestrator"
            entry["do_not_target"] = True
        elif pane.role == "deep":
            entry["role"] = "deep"
        if pane.track:
            entry["track"] = pane.track
        # Store work_dir so reheal can use it
        if pane.work_dir:
            entry["work_dir"] = pane.work_dir

        result[pane.terminal_id] = entry
        # Also write lowercase alias for t0
        if pane.terminal_id == "T0":
            result["t0"] = dict(entry)

    # Build tracks sub-object
    tracks: Dict[str, Any] = {}
    for pane in profile.home_window.panes:
        if pane.track and pane.terminal_id != "T0":
            tracks[pane.track] = {
                "pane_id": pane.pane_id,
                "track": pane.track,
                "model": pane.model,
                "provider": pane.provider,
            }
    if tracks:
        result["tracks"] = tracks

    return result


# ---------------------------------------------------------------------------
# Live tmux interaction
# ---------------------------------------------------------------------------

def _list_live_panes(session_name: str) -> Dict[str, str]:
    """Return {pane_id: work_dir} for all panes in session_name.

    Uses `tmux list-panes -s` which lists panes across all windows.
    Returns empty dict if tmux is unavailable or session doesn't exist.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-s", "-t", session_name,
             "-F", "#{pane_id} #{pane_current_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {}
        panes: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                panes[parts[0]] = parts[1]
        return panes
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}


def verify_profile_integrity(
    profile: SessionProfile,
    session_name: Optional[str] = None,
) -> ProfileDrift:
    """Compare profile's declared pane_ids against live tmux state.

    For each home terminal:
      - If pane_id still exists in live tmux → correct
      - If pane_id gone but work_dir matches a live pane → stale (can remap)
      - If pane_id gone and work_dir not found → missing

    Args:
        profile:      SessionProfile to verify.
        session_name: Override session name (defaults to profile.session_name).

    Returns:
        ProfileDrift with categorised results and remap_candidates.
    """
    sname = session_name or profile.session_name
    live = _list_live_panes(sname)  # pane_id -> work_dir

    # Invert: work_dir -> pane_id (for work_dir-based rediscovery)
    live_by_dir: Dict[str, str] = {}
    for pid, wdir in live.items():
        if wdir:
            live_by_dir[wdir] = pid

    drift = ProfileDrift()

    for pane in profile.home_window.panes:
        if pane.pane_id in live:
            drift.correct.append(pane.terminal_id)
        else:
            # pane_id is stale — try workdir match
            candidate_pid = live_by_dir.get(pane.work_dir, "")
            if candidate_pid:
                drift.stale.append(pane.terminal_id)
                drift.remap_candidates[pane.terminal_id] = candidate_pid
            else:
                drift.missing.append(pane.terminal_id)

    drift.is_clean = not drift.stale and not drift.missing
    return drift


def remap_pane_in_profile(
    profile: SessionProfile,
    terminal_id: str,
    new_pane_id: str,
) -> bool:
    """Update the pane_id for terminal_id in-place.

    This updates only the adapter-side mapping (A-R3, A-R4).
    Dispatch registry and lease state are not touched.

    Returns:
        True if the pane was found and updated; False if not found.
    """
    for pane in profile.home_window.panes:
        if pane.terminal_id == terminal_id:
            pane.pane_id = new_pane_id
            return True
    for win in profile.dynamic_windows:
        for pane in win.panes:
            if pane.terminal_id == terminal_id:
                pane.pane_id = new_pane_id
                return True
    return False


def add_dynamic_window(
    profile: SessionProfile,
    window_name: str,
    window_type: str,
) -> WindowProfile:
    """Add a dynamic window (ops/recovery/events) to the profile if not present.

    Returns the existing or newly created WindowProfile.
    """
    for win in profile.dynamic_windows:
        if win.name == window_name:
            return win
    win = WindowProfile(name=window_name, window_type=window_type)
    profile.dynamic_windows.append(win)
    return win


def remove_dynamic_window(profile: SessionProfile, window_name: str) -> bool:
    """Remove a dynamic window from the profile by name.

    Returns True if removed, False if not found.
    """
    before = len(profile.dynamic_windows)
    profile.dynamic_windows = [w for w in profile.dynamic_windows if w.name != window_name]
    return len(profile.dynamic_windows) < before


# ---------------------------------------------------------------------------
# High-level save-from-panes-json helper (called by start.sh)
# ---------------------------------------------------------------------------

def save_profile_from_panes_json(
    state_dir: Path,
    session_name: str,
    project_root: str = "",
) -> SessionProfile:
    """Read panes.json from state_dir and persist a session_profile.json.

    Idempotent: if a profile already exists, pane_ids and work_dirs are
    updated from the current panes.json while preserving dynamic_windows
    and timestamps.

    Returns the saved SessionProfile.
    """
    panes_path = state_dir / "panes.json"
    panes: Dict[str, Any] = {}
    if panes_path.exists():
        try:
            panes = json.loads(panes_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            panes = {}

    existing = load_session_profile(state_dir)

    profile = generate_session_profile(session_name, panes, project_root)
    if existing is not None:
        # Preserve dynamic windows and original creation timestamp
        profile.dynamic_windows = existing.dynamic_windows
        profile.created_at = existing.created_at

    save_session_profile(profile, state_dir)
    return profile


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_save(args: Any) -> int:
    state_dir = Path(args.state_dir)
    profile = save_profile_from_panes_json(
        state_dir=state_dir,
        session_name=args.session,
        project_root=args.project_root or "",
    )
    print(f"[session_profile] saved: {state_dir / PROFILE_FILENAME}")
    print(f"  session: {profile.session_name}")
    for p in profile.home_window.panes:
        print(f"  {p.terminal_id}: pane_id={p.pane_id or '(none)'} work_dir={p.work_dir or '(none)'}")
    return 0


def _cli_verify(args: Any) -> int:
    state_dir = Path(args.state_dir)
    profile = load_session_profile(state_dir)
    if profile is None:
        print("[session_profile] ERROR: no session_profile.json found", file=sys.stderr)
        return 1
    drift = verify_profile_integrity(profile, session_name=args.session or None)
    print(f"[session_profile] {drift.summary()}")
    if drift.correct:
        print(f"  correct:  {drift.correct}")
    if drift.stale:
        print(f"  stale:    {drift.stale} -> remap: {drift.remap_candidates}")
    if drift.missing:
        print(f"  missing:  {drift.missing}")
    return 0 if drift.is_clean else 2


def _cli_remap(args: Any) -> int:
    state_dir = Path(args.state_dir)
    profile = load_session_profile(state_dir)
    if profile is None:
        print("[session_profile] ERROR: no session_profile.json found", file=sys.stderr)
        return 1
    updated = remap_pane_in_profile(profile, args.terminal, args.pane_id)
    if not updated:
        print(f"[session_profile] ERROR: terminal {args.terminal!r} not found in profile",
              file=sys.stderr)
        return 1
    save_session_profile(profile, state_dir)
    # Also update panes.json so the adapter uses the new ID immediately
    panes_path = state_dir / "panes.json"
    if panes_path.exists():
        try:
            panes = json.loads(panes_path.read_text(encoding="utf-8"))
            for key in (args.terminal, args.terminal.lower()):
                if key in panes:
                    panes[key]["pane_id"] = args.pane_id
            # Also update tracks entry if present
            tracks = panes.get("tracks", {})
            for pane in profile.home_window.panes:
                if pane.terminal_id == args.terminal and pane.track:
                    if pane.track in tracks:
                        tracks[pane.track]["pane_id"] = args.pane_id
            panes_path.write_text(json.dumps(panes, indent=2), encoding="utf-8")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[session_profile] WARN: failed to update panes.json: {exc}", file=sys.stderr)
    print(f"[session_profile] remapped {args.terminal} -> pane_id={args.pane_id}")
    return 0


def _cli_reheal(args: Any) -> int:
    """Verify profile integrity and auto-remap stale pane IDs.

    Calls verify_profile_integrity(), then for each stale terminal calls
    the remap subcommand logic to update panes.json and session_profile.json.
    """
    state_dir = Path(args.state_dir)
    profile = load_session_profile(state_dir)
    if profile is None:
        print("[session_profile] ERROR: no session_profile.json found", file=sys.stderr)
        return 1

    session_name = args.session or profile.session_name
    drift = verify_profile_integrity(profile, session_name=session_name)

    if drift.is_clean:
        print(f"[session_profile] reheal: all pane IDs correct — nothing to do")
        return 0

    print(f"[session_profile] reheal: {drift.summary()}")

    remapped = 0
    for terminal_id, new_pane_id in drift.remap_candidates.items():
        ok = remap_pane_in_profile(profile, terminal_id, new_pane_id)
        if ok:
            print(f"  remapped {terminal_id}: -> {new_pane_id}")
            remapped += 1

    if remapped:
        save_session_profile(profile, state_dir)
        # Rebuild panes.json from updated profile
        updated_panes = profile_to_panes_json(profile)
        panes_path = state_dir / "panes.json"
        if panes_path.exists():
            try:
                # Preserve non-pane keys from existing panes.json (e.g. session name)
                existing_panes = json.loads(panes_path.read_text(encoding="utf-8"))
                existing_panes.update(updated_panes)
                panes_path.write_text(
                    json.dumps(existing_panes, indent=2), encoding="utf-8"
                )
            except (json.JSONDecodeError, OSError):
                panes_path.write_text(
                    json.dumps(updated_panes, indent=2), encoding="utf-8"
                )
        print(f"[session_profile] panes.json updated with {remapped} remap(s)")

    if drift.missing:
        print(f"[session_profile] WARN: {len(drift.missing)} terminal(s) not found in session: "
              f"{drift.missing}")
        print("  These require a full session restart or manual recovery.")

    return 0 if not drift.missing else 3


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="VNX tmux Session Profile management",
        prog="tmux_session_profile.py",
    )
    sub = parser.add_subparsers(dest="command")

    # save
    p_save = sub.add_parser("save", help="Save session profile from panes.json")
    p_save.add_argument("--state-dir", required=True)
    p_save.add_argument("--session", required=True)
    p_save.add_argument("--project-root", default="")

    # verify
    p_verify = sub.add_parser("verify", help="Verify profile pane IDs against live tmux")
    p_verify.add_argument("--state-dir", required=True)
    p_verify.add_argument("--session", default="")

    # remap
    p_remap = sub.add_parser("remap", help="Update pane_id for a terminal")
    p_remap.add_argument("--state-dir", required=True)
    p_remap.add_argument("--terminal", required=True, help="Terminal ID (T0-T3)")
    p_remap.add_argument("--pane-id", required=True, help="New tmux pane ID (e.g. %%7)")

    # reheal
    p_reheal = sub.add_parser("reheal", help="Auto-remap all stale pane IDs by workdir")
    p_reheal.add_argument("--state-dir", required=True)
    p_reheal.add_argument("--session", default="")
    p_reheal.add_argument("--project-root", default="")

    args = parser.parse_args(argv)

    if args.command == "save":
        return _cli_save(args)
    elif args.command == "verify":
        return _cli_verify(args)
    elif args.command == "remap":
        return _cli_remap(args)
    elif args.command == "reheal":
        return _cli_reheal(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
