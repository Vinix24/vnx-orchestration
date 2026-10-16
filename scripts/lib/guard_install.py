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

THE WRITE. Per file: a fresh read, a ``hooks`` tree of the documented shape (a malformed node
refuses the install and is named by its path), the new content validated as JSON that keeps
every top-level key, a timestamped backup, a ``tempfile.mkstemp`` scratch file in the same
directory, and a comparison (size, mtime, hash) just before the replace. A change starts the
command over once, and the backup of the abandoned attempt is removed; a second change refuses.

THE REPLACE is an atomic exchange of the scratch file and the target (``atomic_swap``):
afterwards the scratch name holds exactly the file that was at the target at that instant. It is
compared with the backup. Equal, it is removed. Different, a write landed after the backup: it
is kept as ``settings.json.vnx-displaced-<id>`` and reported with the top-level keys that
differ. Then the file is re-read and the three entries and every key of the backup are verified.
A difference is reported, never repaired. A platform or filesystem without the exchange refuses
the install: a plain replace would drop such a write without a copy.

WHAT IS NOT COVERED. Claude Code writes this file itself (permission grants) and holds no lock
VNX can join. A writer that read the file before the exchange and writes after it removes the
three entries again (nothing of its own is lost): the read-back reports that when it lands
first, ``vnx guard check`` afterwards. A writer that holds the old file open and writes into it
after the comparison writes into the moved-aside copy, which is gone when it still matched the
backup. ``CONCURRENT_WRITE_NOTICE`` says this and asks the operator to run the install while no
session answers a permission prompt.
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

import atomic_swap
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
STATE_DUPLICATE = "duplicate"
STATE_UNKNOWN = "unknown"
CURRENT_REL = Path(".vnx-system") / "current"

CONCURRENT_WRITE_NOTICE = (
    "Concurrent writes: Claude Code writes its settings file itself (permission grants) and "
    "holds no lock this command can join. The replace is an atomic exchange. The file at the "
    "path at that instant is moved aside and compared with the backup. When it differs it is "
    "kept as settings.json.vnx-displaced-<id> and reported, and the run exits 1. A write that "
    "landed before the replace is never lost. Not covered: a writer that read the file before "
    "the replace and writes after it removes the three entries again (the read-back or "
    "`vnx guard check` reports that), and a writer that holds the old file open and writes into "
    "it after the comparison. Run this while no session is answering a permission prompt."
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
    """STATE_OK when exactly one entry of ours sits in the event and it is the exact one in a
    group with the right matcher, STATE_DUPLICATE when the event holds more than one entry of
    ours (whatever their form), STATE_DIFFERS when the single entry is in another form,
    STATE_MISSING when there is none."""
    wanted = guard_hook()
    own = exact = 0
    for group in _groups(settings, event):
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if not is_own_hook(hook):
                continue
            own += 1
            exact += hook == wanted and (group.get("matcher") or "") == matcher
    if own == 0:
        return STATE_MISSING
    if own > 1:
        return STATE_DUPLICATE
    return STATE_OK if exact else STATE_DIFFERS


def entry_states(settings: Mapping) -> Dict[str, str]:
    return {event: event_state(settings, event, matcher) for event, matcher in GUARD_EVENTS}


def apply_entries(settings: Mapping) -> dict:
    """A copy of ``settings`` with exactly one guard entry per event. Other hooks stay; every
    entry of ours in an event that does not hold exactly the one correct entry is removed and
    the correct one added, so a second registration of ours never survives next to a correct one. Raises GuardInstallError for a ``hooks`` tree of the
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


def read_settings(path: Path) -> Tuple[Optional[bytes], dict]:
    """(raw bytes or None, parsed object). A missing file reads as an empty object. Invalid
    JSON, JSON that is not an object, or a ``hooks`` tree that is not the documented shape
    raises GuardInstallError naming the file and, for the tree, the path to the bad node."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise GuardInstallError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GuardInstallError(f"{path} is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise GuardInstallError(f"{path} is not a JSON object")
    problem = hooks_tree_problem(data)
    if problem:
        raise GuardInstallError(f"{path} has a malformed hooks tree: {problem}")
    return raw, data


def hooks_tree_problem(settings: Mapping) -> Optional[str]:
    """The path to the first node where the ``hooks`` tree departs from the documented shape,
    with what is wrong there (``hooks.Stop[0].hooks is not a list``); None when the tree is well
    formed or absent. The shape is the one ``hook_coverage`` loads; a test keeps them equal."""
    if "hooks" not in settings:
        return None
    hooks = settings["hooks"]
    if not isinstance(hooks, dict):
        return "hooks is not an object"
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            return f"hooks.{event} is not a list"
        for index, group in enumerate(groups):
            problem = _group_problem(group, f"hooks.{event}[{index}]")
            if problem:
                return problem
    return None


def _group_problem(group: object, where: str) -> Optional[str]:
    if not isinstance(group, dict):
        return f"{where} is not an object"
    if "matcher" in group and not isinstance(group["matcher"], (str, type(None))):
        return f"{where}.matcher is not a string"
    inner = group.get("hooks", [])
    if not isinstance(inner, list):
        return f"{where}.hooks is not a list"
    for index, hook in enumerate(inner):
        if not isinstance(hook, dict):
            return f"{where}.hooks[{index}] is not an object"
        if hook.get("type", "command") == "command" and not isinstance(hook.get("command", ""), str):
            return f"{where}.hooks[{index}].command is not a string"
    return None


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
    raw, old = read_settings(path)
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


def _discard(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _mode_of(target: Path) -> int:
    try:
        return target.stat().st_mode & 0o777
    except FileNotFoundError:
        return 0o600


@dataclass
class _Placed:
    """A pass that put the new content in place. ``replaced`` is the file the exchange moved
    out of the target (None when the target was new); ``raw`` is what the backup holds."""

    backup: Optional[Path]
    old: dict
    raw: Optional[bytes]
    replaced: Optional[Path]


_UNCHANGED = "unchanged"


def _put_in_place(target: Path, scratch: Path, existed: bool) -> bool:
    """Put ``scratch`` at ``target``. An existing target is exchanged atomically, so afterwards
    ``scratch`` holds the file that was at ``target`` at that instant. A new target is a hard
    link, which fails when a file appeared. False when the target appeared or vanished since the
    plan; GuardInstallError when no atomic exchange is available."""
    try:
        if existed:
            atomic_swap.exchange(scratch, target)
        else:
            os.link(scratch, target)
            _discard(scratch)
    except (FileExistsError, FileNotFoundError):
        return False
    except atomic_swap.ExchangeUnsupported as exc:
        raise GuardInstallError(f"{exc}; a plain replace would drop a concurrent write "
                                "without a copy") from exc
    return True


def _attempt(target: Path, on_compare: Optional[Callable[[], None]],
             before_replace: Optional[Callable[[], None]]):
    """One pass: read, build, back up, scratch, compare, exchange. Returns a _Placed,
    _UNCHANGED when the entries are already there, or None when the target changed meanwhile.
    A pass that does not put the content in place leaves neither its backup nor its scratch
    file behind."""
    snapshot = _fingerprint(target)
    raw, new_bytes, old = plan_file(target)
    if raw is not None and new_bytes == raw:
        return _UNCHANGED
    backup = scratch = None
    placed = False
    try:
        backup = _write_backup(target, raw)
        scratch = _scratch(target, new_bytes, _mode_of(target))
        if on_compare:
            on_compare()
        if _fingerprint(target) != snapshot:
            return None
        if before_replace:
            before_replace()
        placed = _put_in_place(target, scratch, raw is not None)
    finally:
        if not placed:
            _discard(scratch)
            _discard(backup)
    if not placed:
        return None
    return _Placed(backup, old, raw, scratch if raw is not None else None)


def _post_check(target: Path, placed: _Placed) -> List[str]:
    """Problems found after the replace; an empty list means the file is what was planned.
    The replaced file is settled first, so it is kept even when the target cannot be read."""
    problems: List[str] = []
    if placed.replaced is not None:
        problems.extend(_replaced_problems(target, placed))
    try:
        _, final = read_settings(target)
    except GuardInstallError as exc:
        return problems + [str(exc)]
    for event, state in entry_states(final).items():
        if state != STATE_OK:
            problems.append(f"after the write, {event} is {state}")
    lost = sorted(set(placed.old) - set(final))
    if lost:
        problems.append(f"after the write, top-level keys of the backup are missing: {lost}")
    return problems


def _replaced_problems(target: Path, placed: _Placed) -> List[str]:
    """Compare the file the exchange moved out with the backup. Equal: it is removed.
    Different: a write landed after the backup; it is kept under a displaced name."""
    replaced = placed.replaced
    try:
        content = replaced.read_bytes()
    except OSError as exc:
        return [f"cannot read the replaced file {replaced}: {exc}"]
    if content == placed.raw:
        _discard(replaced)
        return []
    kept = target.with_name(f"{target.name}.vnx-displaced-{uuid.uuid4().hex}")
    try:
        os.rename(replaced, kept)
    except OSError:
        kept = replaced
    try:
        current = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        current = None
    changed = _changed_keys(placed.old, current)
    return [f"a write landed after the backup and was overwritten; top-level keys that differ "
            f"from the backup: {changed}. The overwritten content is kept at {kept}, the "
            f"pre-state at {placed.backup}. Merge what you need and run the install again."]


def _changed_keys(old: Mapping, current: object) -> List[str]:
    if not isinstance(current, dict):
        return ["(the replaced file is not a JSON object)"]
    keys = set(old) | set(current)
    return sorted(k for k in keys if old.get(k, _MISSING) != current.get(k, _MISSING))


_MISSING = object()


def write_settings(path: Path, *, on_compare: Optional[Callable[[], None]] = None,
                   before_replace: Optional[Callable[[], None]] = None) -> FileResult:
    """Install the three entries in ``path``. ``on_compare`` runs right before the comparison
    and ``before_replace`` right before the exchange; both exist so a test can drive a
    concurrent write into exactly those places."""
    target = Path(os.path.realpath(path))
    outcome = None
    for _ in range(2):
        try:
            outcome = _attempt(target, on_compare, before_replace)
        except GuardInstallError as exc:
            return FileResult(path, "refused", f"{exc}; nothing was written")
        except OSError as exc:
            return FileResult(path, "failed", f"{type(exc).__name__}: {exc}")
        if outcome is not None:
            break
    if outcome is None:
        return FileResult(path, "refused",
                          f"{target} changed twice while it was being prepared; nothing written")
    if outcome is _UNCHANGED:
        return FileResult(path, "unchanged", "the three entries are already there")
    problems = _post_check(target, outcome)
    status = "failed" if problems else "written"
    return FileResult(path, status, "; ".join(problems), backup=outcome.backup, problems=problems)


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


def guard_installed_anywhere(home: Path) -> bool:
    """True when at least one account has all three entries in their exact form (the same
    discovery and entry check the install uses). An unreadable settings file counts as no."""
    for account in discover_accounts(home):
        try:
            _, settings = read_settings(settings_path(account))
        except GuardInstallError:
            continue
        if all(state == STATE_OK for state in entry_states(settings).values()):
            return True
    return False


def missing_guard_notice(home: Path) -> Optional[str]:
    """One line for `vnx init` and `vnx regen-settings`: the templates no longer register the
    T0 guard, so a machine where no account has the user-level one runs T0 without it. None
    when an account has it, or when there is no account to install it in."""
    if not discover_accounts(home) or guard_installed_anywhere(home):
        return None
    return ("T0 context guard: no Claude Code account on this machine has the user-level guard, "
            "and project settings no longer register it. Run `vnx guard install` (check with "
            "`vnx guard check`).")


def run_install(*, home: Path, config_dirs: List[str], dry_run: bool, out) -> int:
    """`vnx guard install`. Returns the exit code: 0 done, 1 refused or failed."""
    missing = preflight(home)
    if missing:
        for line in missing:
            print(f"vnx guard install: refused, {line}", file=out)
        print("Install a release that ships the shim and make it current first.", file=out)
        return 1
    no_exchange = atomic_swap.unsupported_reason()
    if no_exchange:
        print(f"vnx guard install: refused, {no_exchange}. Without an atomic exchange the "
              "replace would drop a write Claude Code makes at that instant without a copy, so "
              "this platform gets no install.", file=out)
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
    print(CONCURRENT_WRITE_NOTICE, file=out)
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
