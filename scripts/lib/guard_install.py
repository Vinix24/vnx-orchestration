#!/usr/bin/env python3
"""guard_install — `vnx guard install`: put the T0 context guard in the user's Claude settings.

Plan t0-guard-gebruikersniveau, § "User-level registration". The framework registers the guard
once per Claude Code account, at user scope, active only in a VNX project with a T0 folder
(the shim and the guard decide that). This module finds the accounts, builds the three entries
through ``hook_coverage.registered_command`` (no command is rendered by hand) and writes them
with a procedure that keeps the operator's own settings intact.

ACCOUNTS. ``~/.claude`` plus every ``~/.claude-*`` directory holding a ``settings.json`` or a
``.claude.json``. The code names no account.

THE ENTRIES. UserPromptSubmit, PreToolUse (matcher ``Bash|Skill|SlashCommand``) and Stop, each
``{"type": "command", "command": <registered_command>, "timeout": 10}`` (seconds). An entry is
ours when its command names the shim path, the guard entry and ``--scope user``. A second run
finds them and changes nothing.

THE WRITE. Per file: a timestamped backup, a fresh read, the new content validated as JSON that
keeps every top-level key, a ``tempfile.mkstemp`` scratch file in the same directory, and a
comparison (size, mtime, hash) just before the replace. A change starts the command over once;
a second change refuses. Just before ``os.replace`` the target is hard-linked to a
``*.vnx-displaced-*`` name, so the inode that is about to be replaced stays readable. After the
replace the file is re-read and the three entries and every key of the backup are verified, and
the displaced inode is compared with the backup. A difference is reported, never repaired.

THE RESIDUAL WINDOW. Claude Code writes this file itself (permission grants) and holds no lock
VNX can join, and a plain file has no conditional replace. A rename-style write that lands
between the hard link and ``os.replace`` is lost and cannot be seen. ``RESIDUAL_WINDOW_NOTICE``
names it and asks the operator to run the install while no session answers a permission prompt.
The one thing such a write carries is a permission choice, which Claude Code asks for again.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import hook_coverage as hc

USER_MATCHER = "Bash|Skill|SlashCommand"
USER_TOOLS: Tuple[str, ...] = tuple(USER_MATCHER.split("|"))
HOOK_TIMEOUT_SECONDS = 10
# (event, matcher of the group that carries the entry)
GUARD_EVENTS: Tuple[Tuple[str, str], ...] = (
    ("UserPromptSubmit", ""),
    ("PreToolUse", USER_MATCHER),
    ("Stop", ""),
)
STATE_OK = "ok"
STATE_DIFFERS = "differs"
STATE_MISSING = "missing"
CURRENT_REL = Path(".vnx-system") / "current"

RESIDUAL_WINDOW_NOTICE = (
    "Residual window: Claude Code writes its settings file itself (permission grants) and holds "
    "no lock this command can join. A write that lands between the last comparison and the "
    "replace is lost. The only thing such a write carries is a permission choice, and Claude "
    "Code asks for it again. Run this while no session is answering a permission prompt. After "
    "the write the file is read back and checked against its backup."
)


class GuardInstallError(Exception):
    """The install refused, or a file could not be written safely."""


@dataclass
class FileResult:
    """What happened to one settings file."""

    path: Path
    status: str  # "written", "unchanged", "dry-run", "refused", "failed"
    detail: str = ""
    backup: Optional[Path] = None
    diff: str = ""
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("written", "unchanged", "dry-run")


def home_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    mapping = os.environ if env is None else env
    return Path(mapping.get("HOME") or os.path.expanduser("~"))


def current_root(home: Path) -> Path:
    return home / CURRENT_REL


def preflight(home: Path) -> List[str]:
    """Reasons the install must refuse: the shim or the guard is missing in ``current``."""
    hooks = current_root(home) / "scripts" / "hooks"
    return [f"{name} is missing: {hooks / name}"
            for name in (hc.SHIM_NAME, hc.GUARD_ENTRY) if not (hooks / name).is_file()]


def discover_accounts(home: Path) -> List[Path]:
    """``~/.claude`` and every ``~/.claude-*`` holding ``settings.json`` or ``.claude.json``."""
    found: List[Path] = []
    base = home / ".claude"
    if base.is_dir():
        found.append(base)
    try:
        names = sorted(os.listdir(home))
    except OSError:
        return found
    for name in names:
        path = home / name
        if not name.startswith(".claude-") or not path.is_dir():
            continue
        if (path / "settings.json").is_file() or (path / ".claude.json").is_file():
            found.append(path)
    return found


# ── the entries ──────────────────────────────────────────────────────────────


def guard_hook() -> dict:
    return {"type": "command", "command": hc.registered_command(hc.GUARD_ENTRY, "user"),
            "timeout": HOOK_TIMEOUT_SECONDS}


def is_own_hook(hook: object) -> bool:
    """A hook entry this command wrote: it names the shim path, the guard and ``--scope user``."""
    command = hook.get("command") if isinstance(hook, dict) else None
    return (isinstance(command, str) and hc.SHIM_SUFFIX in command and hc.GUARD_ENTRY in command
            and "--scope user" in command)


def _groups(settings: Mapping, event: str) -> list:
    hooks = settings.get("hooks")
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    return groups if isinstance(groups, list) else []


def event_state(settings: Mapping, event: str, matcher: str) -> str:
    """STATE_OK when the exact entry sits in a group with the right matcher, STATE_DIFFERS when
    an entry of ours exists in another form, STATE_MISSING when there is none."""
    wanted = guard_hook()
    state = STATE_MISSING
    for group in _groups(settings, event):
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if not is_own_hook(hook):
                continue
            exact = hook == wanted and (group.get("matcher") or "") == matcher
            if exact:
                return STATE_OK
            state = STATE_DIFFERS
    return state


def entry_states(settings: Mapping) -> Dict[str, str]:
    return {event: event_state(settings, event, matcher) for event, matcher in GUARD_EVENTS}


def apply_entries(settings: Mapping) -> dict:
    """A copy of ``settings`` with exactly one guard entry per event. Other hooks stay; a
    wrong-form entry of ours is replaced. Raises GuardInstallError for a ``hooks`` tree of the
    wrong shape rather than guessing."""
    new = json.loads(json.dumps(settings))
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise GuardInstallError('"hooks" is not an object')
    for event, matcher in GUARD_EVENTS:
        if event_state(new, event, matcher) == STATE_OK:
            continue
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise GuardInstallError(f'"hooks.{event}" is not a list')
        hooks[event] = _without_own(groups)
        hooks[event].append({"matcher": matcher, "hooks": [guard_hook()]})
    return new


def _without_own(groups: list) -> list:
    kept = []
    for group in groups:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(inner, list) or not any(is_own_hook(h) for h in inner):
            kept.append(group)
            continue
        rest = [h for h in inner if not is_own_hook(h)]
        if rest:
            kept.append({**group, "hooks": rest})
    return kept


# ── reading and rendering ────────────────────────────────────────────────────


def _fingerprint(path: Path) -> Optional[Tuple[int, int, str]]:
    try:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GuardInstallError(f"cannot read {path}: {exc}") from exc
    return stat.st_size, stat.st_mtime_ns, digest


def _read_settings(path: Path) -> Tuple[Optional[bytes], dict]:
    """(raw bytes or None, parsed object). A missing file reads as an empty object; invalid
    JSON, or JSON that is not an object, raises."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise GuardInstallError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GuardInstallError(f"{path} is not valid JSON ({exc}); nothing was written") from exc
    if not isinstance(data, dict):
        raise GuardInstallError(f"{path} is not a JSON object; nothing was written")
    return raw, data


def _indent_of(raw: Optional[bytes]) -> int:
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line):
            return len(line) - len(stripped)
    return 2


def render(new: Mapping, raw: Optional[bytes]) -> bytes:
    text = json.dumps(new, indent=_indent_of(raw), ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def _validate_new(old: Mapping, new_bytes: bytes) -> dict:
    """The new content must parse and keep every top-level key of the old content."""
    try:
        parsed = json.loads(new_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GuardInstallError(f"the new content is not valid JSON ({exc})") from exc
    lost = sorted(set(old) - set(parsed))
    if lost:
        raise GuardInstallError(f"the new content would lose top-level keys: {lost}")
    return parsed


def plan_file(path: Path) -> Tuple[Optional[bytes], bytes, dict]:
    """(old bytes, new bytes, old object) for one settings file."""
    raw, old = _read_settings(path)
    new = apply_entries(old)
    new_bytes = raw if new == old and raw is not None else render(new, raw)
    _validate_new(old, new_bytes)
    return raw, new_bytes, old


def diff_text(path: Path, raw: Optional[bytes], new_bytes: bytes) -> str:
    before = (raw or b"").decode("utf-8", "replace").splitlines(keepends=True)
    after = new_bytes.decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(before, after, f"{path} (now)", f"{path} (after)"))


# ── the write ────────────────────────────────────────────────────────────────


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_backup(target: Path, raw: Optional[bytes]) -> Optional[Path]:
    """A backup under a name that does not exist yet (exclusive create); None when there was
    no file to back up."""
    if raw is None:
        return None
    for counter in range(100):
        suffix = "" if counter == 0 else f".{counter}"
        backup = target.with_name(f"{target.name}.vnx-backup-{_timestamp()}{suffix}")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return backup
    raise GuardInstallError(f"cannot find a free backup name next to {target}")


def _scratch(target: Path, new_bytes: bytes, mode: int) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".vnx-tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(new_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
    except BaseException:
        _discard(Path(name))
        raise
    return Path(name)


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _mode_of(target: Path) -> int:
    try:
        return target.stat().st_mode & 0o777
    except FileNotFoundError:
        return 0o600


def _swap(target: Path, scratch: Path, existed: bool) -> Optional[Path]:
    """Put ``scratch`` at ``target``. Returns the displaced hard link of the replaced inode.
    Raises FileExistsError when the file did not exist at the plan and appeared since."""
    if not existed:
        os.link(scratch, target)
        _discard(scratch)
        return None
    displaced = target.with_name(f"{target.name}.vnx-displaced-{uuid.uuid4().hex}")
    os.link(target, displaced)
    os.replace(scratch, target)
    return displaced


def _attempt(target: Path, on_compare: Optional[Callable[[], None]],
             before_replace: Optional[Callable[[], None]]):
    """One pass: read, back up, build, scratch, compare, swap. Returns
    (backup, old_obj, raw, displaced, snapshot) or None when the target changed meanwhile."""
    snapshot = _fingerprint(target)
    raw, new_bytes, old = plan_file(target)
    if raw is not None and new_bytes == raw:
        return target, None, old, raw, None, snapshot
    backup = _write_backup(target, raw)
    scratch = _scratch(target, new_bytes, _mode_of(target))
    try:
        if on_compare:
            on_compare()
        if _fingerprint(target) != snapshot:
            _discard(scratch)
            return None
        if before_replace:
            before_replace()
        try:
            displaced = _swap(target, scratch, raw is not None)
        except FileExistsError:
            _discard(scratch)
            return None
    except BaseException:
        _discard(scratch)
        raise
    return target, backup, old, raw, displaced, snapshot


def _post_check(target: Path, backup: Optional[Path], old: Mapping, displaced: Optional[Path],
                snapshot: Optional[Tuple[int, int, str]]) -> List[str]:
    """Problems found after the replace; an empty list means the file is what was planned."""
    problems: List[str] = []
    try:
        _, final = _read_settings(target)
    except GuardInstallError as exc:
        return [str(exc)]
    for event, state in entry_states(final).items():
        if state != STATE_OK:
            problems.append(f"after the write, {event} is {state}")
    lost = sorted(set(old) - set(final))
    if lost:
        problems.append(f"after the write, top-level keys of the backup are missing: {lost}")
    if displaced is not None:
        problems.extend(_displaced_problems(displaced, backup, old, snapshot))
    return problems


def _displaced_problems(displaced: Path, backup: Optional[Path], old: Mapping,
                        snapshot: Optional[Tuple[int, int, str]]) -> List[str]:
    try:
        replaced = hashlib.sha256(displaced.read_bytes()).hexdigest()
    except OSError as exc:
        return [f"cannot read the replaced file {displaced}: {exc}"]
    if snapshot is not None and replaced == snapshot[2]:
        _discard(displaced)
        return []
    try:
        current = json.loads(displaced.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    changed = _changed_keys(old, current)
    return [f"a write landed after the backup and was overwritten; top-level keys that differ "
            f"from the backup: {changed}. The replaced content is kept at {displaced}, the "
            f"pre-state at {backup}. Merge what you need and run the install again."]


def _changed_keys(old: Mapping, current: object) -> List[str]:
    if not isinstance(current, dict):
        return ["(the replaced file is not a JSON object)"]
    keys = set(old) | set(current)
    return sorted(k for k in keys if old.get(k, _MISSING) != current.get(k, _MISSING))


_MISSING = object()


def write_settings(path: Path, *, on_compare: Optional[Callable[[], None]] = None,
                   before_replace: Optional[Callable[[], None]] = None) -> FileResult:
    """Install the three entries in ``path``. ``on_compare`` runs right before the comparison
    and ``before_replace`` right before the swap; both exist so a test can drive a concurrent
    write into exactly those places."""
    target = Path(os.path.realpath(path))
    outcome = None
    for _ in range(2):
        try:
            outcome = _attempt(target, on_compare, before_replace)
        except GuardInstallError as exc:
            return FileResult(path, "refused", str(exc))
        except OSError as exc:
            return FileResult(path, "failed", f"{type(exc).__name__}: {exc}")
        if outcome is not None:
            break
    if outcome is None:
        return FileResult(path, "refused",
                          f"{target} changed twice while it was being prepared; nothing written")
    _, backup, old, raw, displaced, snapshot = outcome
    if backup is None and displaced is None and raw is not None:
        return FileResult(path, "unchanged", "the three entries are already there")
    problems = _post_check(target, backup, old, displaced, snapshot)
    status = "failed" if problems else "written"
    return FileResult(path, status, "; ".join(problems), backup=backup, problems=problems)


# ── the command ──────────────────────────────────────────────────────────────


def settings_path(config_dir: Path) -> Path:
    return config_dir / "settings.json"


def resolve_accounts(home: Path, config_dirs: List[str]) -> List[Path]:
    if not config_dirs:
        return discover_accounts(home)
    accounts = []
    for item in config_dirs:
        directory = Path(item).expanduser()
        if not directory.is_dir():
            raise GuardInstallError(f"--config-dir {item} is not a directory")
        accounts.append(directory)
    return accounts


def run_install(*, home: Path, config_dirs: List[str], dry_run: bool, out) -> int:
    """`vnx guard install`. Returns the exit code: 0 done, 1 refused or failed."""
    missing = preflight(home)
    if missing:
        for line in missing:
            print(f"vnx guard install: refused, {line}", file=out)
        print("Install a release that ships the shim and make it current first.", file=out)
        return 1
    try:
        accounts = resolve_accounts(home, config_dirs)
    except GuardInstallError as exc:
        print(f"vnx guard install: refused, {exc}", file=out)
        return 1
    if not accounts:
        print(f"vnx guard install: no Claude Code account found under {home}", file=out)
        return 1
    print("Accounts:", file=out)
    for account in accounts:
        print(f"  {account}", file=out)
    return _dry_run(accounts, out) if dry_run else _install(accounts, out)


def _dry_run(accounts: List[Path], out) -> int:
    code = 0
    for account in accounts:
        path = settings_path(account)
        try:
            raw, new_bytes, _ = plan_file(Path(os.path.realpath(path)))
        except GuardInstallError as exc:
            print(f"{path}: refused, {exc}", file=out)
            code = 1
            continue
        diff = diff_text(path, raw, new_bytes)
        print(diff if diff else f"{path}: unchanged, the three entries are already there",
              file=out, end="" if diff else "\n")
    print("Dry run: nothing was written.", file=out)
    return code


def _install(accounts: List[Path], out) -> int:
    print(RESIDUAL_WINDOW_NOTICE, file=out)
    code = 0
    for account in accounts:
        result = write_settings(settings_path(account))
        line = f"{result.path}: {result.status}"
        if result.detail:
            line += f", {result.detail}"
        print(line, file=out)
        if result.backup:
            print(f"  backup: {result.backup}", file=out)
        code = code or (0 if result.ok else 1)
    return code
