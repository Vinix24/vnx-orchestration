"""provider_switch — the operator's one place to turn model providers on and off.

One file, ``~/.vnx/providers.json`` (``VNX_PROVIDER_SWITCH_FILE`` moves it, and with it the
audit log, the lock and every quarantined copy), holds a switch per provider FAMILY, globally
and per project. A family names the model maker, so one switch covers every route to it:
``deepseek`` covers deepseek-harness, ``deepseek_gate``, the classifier provider and DeepSeek
via OpenRouter.

Every launch function (a function that actually starts a non-claude provider process or
request) calls :func:`require` first; entry points turn a refusal into fabric behaviour before
that. Until every launch function does, the write verbs refuse: see ``PENDING_*`` below.

Read fresh on every decision: the file is a few hundred bytes and a change must take effect on
the next call in every process, launchd jobs included. No cache.

The switch governs calls, never evidence. A refusal is not a reachability fact: it is never
booked as auth/credit/quota, and where it is recorded its class and reason are
``provider_switched_off``.
"""

from __future__ import annotations

import copy
import fcntl
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import urlsplit

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from atomic_io import atomic_write_text  # noqa: E402
from vnx_ids import PROJECT_ID_RE  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Families and classes
# ---------------------------------------------------------------------------

DEEPSEEK = "deepseek"
GLM = "glm"
KIMI = "kimi"
GEMINI = "gemini"
LOCAL = "local"
OPENROUTER = "openrouter"
CLAUDE = "claude"
CODEX = "codex"
TOOLING = "tooling"
UNRESOLVED = "unresolved"

SWITCHABLE_FAMILIES: Tuple[str, ...] = ("deepseek", "glm", "kimi", "gemini", "local", "openrouter")

# Never refused by the switch. claude and codex by operator decision (2b); tooling is the
# gh-based gates; unresolved is ``auto``, which the router resolves to a lane that is checked.
NON_SWITCHABLE_CLASSES: Dict[str, str] = {
    CLAUDE: "claude is never switchable (operator decision 2b)",
    CODEX: "codex is never switchable (operator decision 2b)",
    TOOLING: "tooling is the gh-based gates, which call no model",
    UNRESOLVED: "auto is resolved by the router to a lane, and that lane is checked",
}

UNKNOWN_FAMILY = "unknown"

# Exact identifiers, matched case-insensitively after strip. Family names are identifiers too,
# so ``require(family_of_host(url), ...)`` decides on the family the host maps to.
_EXACT: Dict[str, str] = {
    "deepseek": DEEPSEEK, "deepseek-harness": DEEPSEEK, "deepseek_harness": DEEPSEEK,
    "deepseek_gate": DEEPSEEK,
    "glm": GLM, "glm-harness": GLM, "glm_harness": GLM, "glm_gate": GLM, "zai": GLM,
    "kimi": KIMI, "kimi_cli": KIMI, "kimi_gate": KIMI,
    "gemini": GEMINI, "gemini_review": GEMINI, "google": GEMINI,
    "local": LOCAL, "ollama": LOCAL, "local-gemma": LOCAL, "local_gemma": LOCAL,
    "openrouter": OPENROUTER,
    "claude": CLAUDE, "claude_headless": CLAUDE, "anthropic": CLAUDE,
    "codex": CODEX, "codex_gate": CODEX, "openai": CODEX,
    "gh": TOOLING, "ci_gate": TOOLING, "wiring_gate": TOOLING, "claude_github_optional": TOOLING,
    "auto": UNRESOLVED,
}

# The maker segment of an OpenRouter model id (``openrouter/<maker>/<model>``). A maker not
# named here is the ``openrouter`` family.
_OPENROUTER_MAKERS: Dict[str, str] = {
    "deepseek": DEEPSEEK, "z-ai": GLM, "moonshotai": KIMI, "google": GEMINI,
}

# Ordered prefix rules, applied after the exact table and the OpenRouter rule.
_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("litellm:deepseek", DEEPSEEK),
    ("litellm:zai", GLM),
    ("litellm:moonshot", KIMI),
    ("litellm:anthropic/", CLAUDE),
    ("litellm:openai/", CODEX),
    ("litellm:google/", GEMINI),
    ("litellm:gemini/", GEMINI),
    ("litellm:ollama/", LOCAL),
    ("deepseek-", DEEPSEEK),
    ("deepseek/", DEEPSEEK),
    ("glm-", GLM),
    ("z-ai/", GLM),
    ("kimi-", KIMI),
    ("moonshot", KIMI),
    ("gemini-", GEMINI),
    ("ollama:", LOCAL),
    ("claude-", CLAUDE),
    ("opus", CLAUDE),
    ("sonnet", CLAUDE),
    ("haiku", CLAUDE),
    ("fable", CLAUDE),
    ("gpt-", CODEX),
)

_LOCAL_GEMMA_RE = re.compile(r"^gemma-.+-local$")

# Hosts for the host-keyed check (an ANTHROPIC_BASE_URL or a request URL).
_HOSTS: Dict[str, str] = {
    "api.deepseek.com": DEEPSEEK,
    "openrouter.ai": OPENROUTER,
    "api.z.ai": GLM,
    "open.bigmodel.cn": GLM,
    "aiplatform.googleapis.com": GEMINI,
    "generativelanguage.googleapis.com": GEMINI,
    "api.anthropic.com": CLAUDE,
    "api.openai.com": CODEX,
}
_PORTS: Dict[int, str] = {
    11434: LOCAL,  # ollama
    4141: GLM,     # the local litellm proxy that fronts GLM
}


class UnknownProviderError(ValueError):
    """An identifier or host the family map does not know."""


def family_of(identifier: str) -> str:
    """Return the family (or non-switchable class) of a provider identifier.

    Explicit table plus prefix rules, case-insensitive. Raises :class:`UnknownProviderError`
    for an identifier nothing maps: the caller decides, and :func:`decide` refuses it.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise UnknownProviderError(f"provider identifier {identifier!r} is empty or not a string")
    key = identifier.strip().lower()
    if key in _EXACT:
        return _EXACT[key]
    for openrouter_prefix in ("litellm:openrouter/", "openrouter/"):
        if key.startswith(openrouter_prefix):
            maker = key[len(openrouter_prefix):].split("/", 1)[0]
            return _OPENROUTER_MAKERS.get(maker, OPENROUTER)
    for prefix, family in _PREFIXES:
        if key.startswith(prefix):
            return family
    if _LOCAL_GEMMA_RE.match(key):
        return LOCAL
    raise UnknownProviderError(
        f"provider identifier {identifier!r} is not in the provider_switch family map; "
        "add it to scripts/lib/provider_switch.py before routing on it"
    )


def family_of_host(url_or_host: str, *, model: Optional[str] = None) -> str:
    """Return the family behind a base URL or host (the host-keyed check).

    ``openrouter.ai`` maps to the maker of ``model`` when the model is known, else to
    ``openrouter``. Port 11434 is ollama (``local``); port 4141 is the litellm proxy that fronts
    GLM. Raises :class:`UnknownProviderError` for a host the map does not know.
    """
    if not isinstance(url_or_host, str) or not url_or_host.strip():
        raise UnknownProviderError(f"host {url_or_host!r} is empty or not a string")
    raw = url_or_host.strip()
    try:
        parts = urlsplit(raw if "://" in raw else f"//{raw}")
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise UnknownProviderError(f"host {url_or_host!r} cannot be parsed: {exc}") from exc
    family = _HOSTS.get(host)
    if family is None:
        if host.endswith("-aiplatform.googleapis.com"):
            family = GEMINI
        elif host.startswith("api.moonshot."):
            family = KIMI
        elif port in _PORTS:
            family = _PORTS[port]
    if family is None:
        raise UnknownProviderError(
            f"host {host or url_or_host!r} is not in the provider_switch host map"
        )
    if family == OPENROUTER and isinstance(model, str) and model.strip():
        name = model.strip().lower().removeprefix("litellm:")
        if "/" in name and not name.startswith("openrouter/"):
            name = f"openrouter/{name}"
        try:
            maker_family = family_of(name)
        except UnknownProviderError:
            return OPENROUTER
        if maker_family in SWITCHABLE_FAMILIES:
            return maker_family
    return family


# ---------------------------------------------------------------------------
# File locations
# ---------------------------------------------------------------------------

SWITCH_FILE_ENV = "VNX_PROVIDER_SWITCH_FILE"
SCHEMA_VERSION = 1
AUDIT_FILE_NAME = "providers_audit.ndjson"


def switch_file_path() -> Path:
    """The switch file: ``$VNX_PROVIDER_SWITCH_FILE`` or ``~/.vnx/providers.json``."""
    override = os.environ.get(SWITCH_FILE_ENV)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return Path.home() / ".vnx" / "providers.json"


def audit_log_path(path: Optional[Path] = None) -> Path:
    """The append-only audit log, in the directory of the switch file."""
    return (path or switch_file_path()).parent / AUDIT_FILE_NAME


def lock_file_path(path: Optional[Path] = None) -> Path:
    """The sidecar lock every writer holds: ``providers.json.lock`` next to the switch file."""
    p = path or switch_file_path()
    return p.with_name(p.name + ".lock")


def quarantine_dir(path: Optional[Path] = None) -> Path:
    """Where ``repair`` keeps an unreadable file: the directory of the switch file."""
    return (path or switch_file_path()).parent


# ---------------------------------------------------------------------------
# Reading (version policy)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SwitchEntry:
    enabled: bool
    changed_at: Optional[str] = None
    changed_by: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class SwitchState:
    """A parsed switch file, or an explicit unreadable one. ``error`` is None when readable."""

    path: Path
    exists: bool
    error: Optional[str]
    version: Optional[int]
    global_switches: Dict[str, SwitchEntry] = field(default_factory=dict)
    project_switches: Dict[str, Dict[str, SwitchEntry]] = field(default_factory=dict)
    document: Optional[Dict[str, Any]] = None

    @property
    def readable(self) -> bool:
        return self.error is None


_LOGGED_IGNORED_FAMILIES: set = set()


def _log_ignored_family_once(name: str, where: str) -> None:
    if name in _LOGGED_IGNORED_FAMILIES:
        return
    _LOGGED_IGNORED_FAMILIES.add(name)
    if name in NON_SWITCHABLE_CLASSES:
        logger.info("provider_switch: %s.%s ignored: %s", where, name, NON_SWITCHABLE_CLASSES[name])
    else:
        logger.info("provider_switch: unknown family %r in %s ignored (a newer CLI wrote it)", name, where)


def _optional_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _parse_section(section: Any, where: str) -> Tuple[Dict[str, SwitchEntry], Optional[str]]:
    if not isinstance(section, dict):
        return {}, f"section {where!r} is not an object"
    entries: Dict[str, SwitchEntry] = {}
    for name, entry in section.items():
        if name not in SWITCHABLE_FAMILIES:
            _log_ignored_family_once(name, where)
            continue
        enabled = entry.get("enabled") if isinstance(entry, dict) else None
        if not isinstance(enabled, bool):
            return {}, f"{where}.{name}.enabled is not a boolean"
        entries[name] = SwitchEntry(
            enabled=enabled,
            changed_at=_optional_str(entry.get("changed_at")),
            changed_by=_optional_str(entry.get("changed_by")),
            reason=_optional_str(entry.get("reason")),
        )
    return entries, None


def parse_document(document: Any) -> Tuple[Dict[str, SwitchEntry], Dict[str, Dict[str, SwitchEntry]], Optional[int], Optional[str]]:
    """Validate a parsed switch document against the version policy.

    Returns ``(global, projects, version, error)``. Only these make it unreadable: a missing or
    non-integer ``version``, a higher major (or one below 1, which never existed), a section
    that is not an object, and ``enabled`` that is not a boolean on a known family. Unknown
    families and unknown fields are ignored.
    """
    if not isinstance(document, dict):
        return {}, {}, None, "the top level is not an object"
    version = document.get("version")
    if "version" not in document:
        return {}, {}, None, "version is missing"
    if isinstance(version, bool) or not isinstance(version, int):
        return {}, {}, None, f"version {version!r} is not an integer"
    if version > SCHEMA_VERSION:
        return {}, {}, version, (
            f"version {version} is newer than this reader (major {SCHEMA_VERSION}); "
            "update the engine before reading it"
        )
    if version < 1:
        return {}, {}, version, f"version {version} is not a valid major (majors start at 1)"
    global_entries, error = _parse_section(document.get("global", {}), "global")
    if error:
        return {}, {}, version, error
    projects_raw = document.get("projects", {})
    if not isinstance(projects_raw, dict):
        return {}, {}, version, "section 'projects' is not an object"
    projects: Dict[str, Dict[str, SwitchEntry]] = {}
    for pid, section in projects_raw.items():
        entries, error = _parse_section(section, f"projects.{pid}")
        if error:
            return {}, {}, version, error
        projects[pid] = entries
    return global_entries, projects, version, None


def load_state(path: Optional[Path] = None) -> SwitchState:
    """Read and validate the switch file. Never raises.

    A missing file is a readable state with no switches (everything on). Anything else that
    cannot be read or parsed is an explicit unreadable state carrying the parse error.
    """
    p = Path(path) if path is not None else switch_file_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SwitchState(path=p, exists=False, error=None, version=None)
    except (OSError, UnicodeDecodeError) as exc:
        return SwitchState(path=p, exists=True, error=f"cannot read {p}: {exc}", version=None)
    try:
        document = json.loads(text)
    except ValueError as exc:
        return SwitchState(path=p, exists=True, error=f"invalid JSON in {p}: {exc}", version=None)
    global_entries, projects, version, error = parse_document(document)
    if error:
        return SwitchState(path=p, exists=True, error=f"{p}: {error}", version=version)
    return SwitchState(
        path=p, exists=True, error=None, version=version,
        global_switches=global_entries, project_switches=projects, document=document,
    )


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

SOURCE_DEFAULT = "default"
SOURCE_GLOBAL = "global"
SOURCE_PROJECT = "project"
SOURCE_OVERRIDE = "override"
SOURCE_OVERRIDE_INVALID = "override-invalid"
SOURCE_UNREADABLE = "unreadable"
SOURCE_UNKNOWN_IDENTIFIER = "unknown-identifier"
SOURCE_NOT_SWITCHABLE = "not-switchable"

SCOPE_GLOBAL = "global"

REASON_SWITCHED_OFF = "provider_switched_off"
# The exit code a refused launch exits with: provider_dispatch on a refusal (B), mapped by
# gate_runner (C), and `vnx providers check` for a refused identifier.
EXIT_PROVIDER_SWITCHED_OFF = 4

OVERRIDE_ENV_PREFIX = "VNX_OVERRIDE_PROVIDER_"
OVERRIDE_OFF = "off"
OVERRIDE_IGNORED = "ignored"
OVERRIDE_INVALID = "invalid"
_OVERRIDE_OFF_TOKENS = frozenset({"off", "0", "false", "no"})
_OVERRIDE_ON_TOKENS = frozenset({"on", "1", "true", "yes"})


def interpret_override(value: str) -> str:
    """``off`` (switches the family off), ``ignored`` (an on-token: only the CLI re-enables) or
    ``invalid`` (any other value refuses the family). Case-insensitive, trimmed."""
    token = (value or "").strip().lower()
    if token in _OVERRIDE_OFF_TOKENS:
        return OVERRIDE_OFF
    if token in _OVERRIDE_ON_TOKENS:
        return OVERRIDE_IGNORED
    return OVERRIDE_INVALID


@dataclass(frozen=True)
class SwitchDecision:
    identifier: str
    allowed: bool
    family: str
    source: str
    scope: str
    consumer: str
    changed_at: Optional[str] = None
    changed_by: Optional[str] = None
    reason: Optional[str] = None
    error: Optional[str] = None

    def detail(self) -> str:
        """One line for logs and refusals."""
        verdict = "allowed" if self.allowed else REASON_SWITCHED_OFF
        parts = [
            f"{verdict}: {self.identifier} family={self.family} source={self.source} "
            f"scope={self.scope} consumer={self.consumer}"
        ]
        if self.changed_at:
            parts.append(f"changed_at={self.changed_at}")
        if self.changed_by:
            parts.append(f"changed_by={self.changed_by}")
        if self.reason:
            parts.append(f"reason={self.reason!r}")
        if self.error:
            parts.append(f"error={self.error!r}")
        return " ".join(parts)


class ProviderDisabledError(RuntimeError):
    """Raised by :func:`require` when a provider may not be called. Carries the decision."""

    def __init__(self, decision: SwitchDecision) -> None:
        self.decision = decision
        super().__init__(decision.detail())


def resolve_project(project_id: Optional[str] = None) -> Optional[str]:
    """The project a decision is for, or None (the decision then uses the global section only).

    Order: the explicit argument, ``VNX_PROJECT_ID``, the state-dir resolver of the process
    (``vnx_paths``), none.
    """
    if isinstance(project_id, str) and project_id.strip():
        return project_id.strip()
    env_pid = os.environ.get("VNX_PROJECT_ID", "").strip()
    if env_pid:
        return env_pid
    try:
        import vnx_paths  # noqa: PLC0415  (deferred: pulls in data_dir_guard / project_root)
        state_dir = vnx_paths.resolve_paths().get("VNX_STATE_DIR")
        derived = vnx_paths.project_id_from_state_dir(Path(state_dir)) if state_dir else ""
    except Exception as exc:  # the resolver may refuse; the decision then falls back to global
        logger.warning("provider_switch: the state-dir resolver failed (%s); deciding with scope=global", exc)
        return None
    return derived or None


def decide(identifier: str, *, consumer: str, project_id: Optional[str] = None) -> SwitchDecision:
    """Decide whether ``identifier`` may be called now. Never raises.

    Precedence per family: a ``VNX_OVERRIDE_PROVIDER_<FAMILY>`` override (off only), the
    project section, the global section, the default ``on``. Fail-closed: an unreadable file
    refuses every switchable family, an unknown identifier is refused. Non-switchable classes
    are always allowed.
    """
    ident = identifier if isinstance(identifier, str) else repr(identifier)
    who = str(consumer or "unknown-consumer")
    pid = resolve_project(project_id)
    scope = pid or SCOPE_GLOBAL

    def _decision(allowed: bool, family: str, source: str, **extra: Any) -> SwitchDecision:
        return SwitchDecision(identifier=ident, allowed=allowed, family=family, source=source,
                              scope=scope, consumer=who, **extra)

    try:
        family = family_of(identifier)
    except UnknownProviderError as exc:
        logger.error("provider_switch: refusing %r for consumer %s: %s", ident, who, exc)
        return _decision(False, UNKNOWN_FAMILY, SOURCE_UNKNOWN_IDENTIFIER, error=str(exc))
    if family not in SWITCHABLE_FAMILIES:
        return _decision(True, family, SOURCE_NOT_SWITCHABLE)

    env_key = OVERRIDE_ENV_PREFIX + family.upper()
    raw = os.environ.get(env_key)
    if raw is not None:
        meaning = interpret_override(raw)
        if meaning == OVERRIDE_OFF:
            logger.warning("provider_switch: %s=%r switches %s off for consumer %s (pid %d)",
                           env_key, raw, family, who, os.getpid())
            return _decision(False, family, SOURCE_OVERRIDE, reason=f"{env_key}={raw.strip()}")
        if meaning == OVERRIDE_IGNORED:
            logger.warning(
                "provider_switch: %s=%r ignored for consumer %s (pid %d): an override can only "
                "switch a family off; re-enable with `vnx providers on %s`",
                env_key, raw, who, os.getpid(), family,
            )
        else:
            logger.warning("provider_switch: %s=%r is not a valid token; refusing %s for consumer %s (pid %d)",
                           env_key, raw, family, who, os.getpid())
            return _decision(False, family, SOURCE_OVERRIDE_INVALID,
                             error=f"{env_key}={raw!r}: use off/0/false/no")

    state = load_state()
    if not state.readable:
        return _decision(False, family, SOURCE_UNREADABLE, error=state.error)
    entry = state.project_switches.get(pid, {}).get(family) if pid else None
    source = SOURCE_PROJECT
    if entry is None:
        entry = state.global_switches.get(family)
        source = SOURCE_GLOBAL
    if entry is None:
        return _decision(True, family, SOURCE_DEFAULT)
    return _decision(entry.enabled, family, source, changed_at=entry.changed_at,
                     changed_by=entry.changed_by, reason=entry.reason)


def require(identifier: str, *, consumer: str, project_id: Optional[str] = None) -> SwitchDecision:
    """Return the decision when allowed; raise :class:`ProviderDisabledError` when not."""
    decision = decide(identifier, consumer=consumer, project_id=project_id)
    if not decision.allowed:
        raise ProviderDisabledError(decision)
    return decision


# ---------------------------------------------------------------------------
# Write protection
# ---------------------------------------------------------------------------

DISPATCH_MARKER_ENV: Tuple[str, ...] = (
    "VNX_CURRENT_DISPATCH_ID", "VNX_DISPATCH_PATHS", "VNX_WORKER_DISPATCH_ID",
)
_WORKTREE_GITDIR_RE = re.compile(r"(^|[\\/])worktrees[\\/]")


def linked_worktree_root(start: Path) -> Optional[Path]:
    """The linked git worktree that contains ``start``, or None.

    Walks up to the first ``.git``: a directory is a main checkout; a file whose ``gitdir``
    points into ``/worktrees/`` is a linked worktree. A ``.git`` file that cannot be read is
    treated as a linked worktree, so an unreadable marker refuses instead of allowing.
    """
    for directory in [start, *start.parents]:
        marker = directory / ".git"
        if marker.is_dir():
            return None
        if marker.is_file():
            try:
                text = marker.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return directory
            for line in text.splitlines():
                if line.startswith("gitdir:") and _WORKTREE_GITDIR_RE.search(line.split(":", 1)[1].strip()):
                    return directory
            return None
    return None


def pending_launch_functions() -> Dict[str, Tuple[str, ...]]:
    """Launch functions that do not call :func:`require` yet, per deliverable."""
    return {"B": tuple(PENDING_B), "C": tuple(PENDING_C), "E": tuple(PENDING_E)}


def write_refusals() -> List[str]:
    """Every reason the current process may not change a switch. Empty means it may.

    Follows the caller: its working directory and its environment, not this module's location.
    There is no variable, flag or config key that bypasses any of these.
    """
    reasons: List[str] = []
    try:
        here = Path(os.getcwd()).resolve()
    except OSError as exc:
        reasons.append(f"the working directory cannot be determined ({exc})")
    else:
        worktree = linked_worktree_root(here)
        if worktree is not None:
            reasons.append(
                f"the working directory lies in the linked git worktree {worktree}; build workers "
                "run in linked worktrees, so switches are changed from the main checkout or "
                "outside any repo"
            )
    markers = [name for name in DISPATCH_MARKER_ENV if name in os.environ]
    if markers:
        reasons.append(
            f"the environment carries a dispatch marker ({', '.join(markers)}); a dispatch may "
            "not change provider switches"
        )
    pending = pending_launch_functions()
    if any(pending.values()):
        counts = ", ".join(f"{k}: {len(v)}" for k, v in pending.items() if v)
        reasons.append(
            f"not enforced yet: {sum(len(v) for v in pending.values())} launch function(s) do "
            f"not call provider_switch.require ({counts}); the write verbs go live when they all do"
        )
    return reasons


class SwitchWriteRefused(PermissionError):
    def __init__(self, reasons: List[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


class SwitchFileUnreadable(RuntimeError):
    def __init__(self, state: SwitchState) -> None:
        self.state = state
        super().__init__(state.error or "switch file unreadable")


class AuditLogUnreadable(RuntimeError):
    pass


class RepairNotNeeded(RuntimeError):
    pass


class NotSwitchableError(ValueError):
    pass


def check_switchable(family: str) -> str:
    """Validate a family name for a write. Raises :class:`NotSwitchableError` with the reason."""
    name = (family or "").strip().lower()
    if name in SWITCHABLE_FAMILIES:
        return name
    if name in NON_SWITCHABLE_CLASSES:
        raise NotSwitchableError(f"{name} cannot be switched: {NON_SWITCHABLE_CLASSES[name]}")
    raise NotSwitchableError(
        f"unknown family {family!r}; switchable families: {', '.join(SWITCHABLE_FAMILIES)}"
    )


def check_project_id(project_id: str) -> str:
    pid = (project_id or "").strip()
    if not PROJECT_ID_RE.match(pid) or pid == SCOPE_GLOBAL:
        raise ValueError(f"invalid project id {project_id!r}: must match {PROJECT_ID_RE.pattern} and not be 'global'")
    return pid


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WriteResult:
    changed: bool
    scope: str
    family: Optional[str]
    old: Any
    new: Any
    path: Path
    quarantined_to: Optional[Path] = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_actor() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return f"uid-{os.getuid()}"


def _parent_command(ppid: int) -> str:
    try:
        result = subprocess.run(["ps", "-o", "command=", "-p", str(ppid)],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<unavailable: {exc}>"
    return result.stdout.strip()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock = lock_file_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _write_document(path: Path, document: Dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")


OP_ABORT = "abort"


def _audit_header(op: str, actor: str) -> Dict[str, Any]:
    """The fields every audit record carries, starting with its unique ``id``."""
    ppid = os.getppid()
    try:
        cwd = os.getcwd()
    except OSError as exc:
        cwd = f"<unavailable: {exc}>"
    return {
        "id": uuid.uuid4().hex, "ts": _now(), "op": op, "actor": actor, "cwd": cwd,
        "pid": os.getpid(), "ppid": ppid, "parent_cmd": _parent_command(ppid), "argv": list(sys.argv),
    }


def _append_audit_record(path: Path, record: Dict[str, Any]) -> None:
    audit = audit_log_path(path)
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _append_audit(path: Path, *, op: str, scope: str, family: Optional[str], old: Any, new: Any,
                  reason: str, actor: str) -> str:
    """Append one change record and return its id."""
    record = {**_audit_header(op, actor),
              "scope": scope, "family": family, "old": old, "new": new, "reason": reason}
    _append_audit_record(path, record)
    return record["id"]


def _append_abort(path: Path, ref: str, error: BaseException, actor: str) -> Optional[str]:
    """Append the ``abort`` record for the change record ``ref``.

    Returns None when it landed, else why it could not be appended.
    """
    try:
        _append_audit_record(path, {**_audit_header(OP_ABORT, actor), "ref": ref,
                                    "error": f"{type(error).__name__}: {error}"})
    except Exception as exc:  # the caller re-raises the original error and names the unconfirmed line
        logger.error("provider_switch: cannot append the abort for audit record %s: %s", ref, exc)
        return f"{type(exc).__name__}: {exc}"
    return None


def _replace_after_audit(path: Path, document: Dict[str, Any], audit_id: str, actor: str) -> None:
    """Replace the switch file with the change the audit record ``audit_id`` already holds.

    The log stays append-only and ledger-first. When the replace raises, an ``abort`` record for
    ``audit_id`` follows it, so :func:`replay_audit` never replays a change that did not land, and
    the original error is re-raised with ``aborted_audit_id`` set. When the abort cannot be
    appended either, the original error is re-raised with ``unconfirmed_audit_id`` set (and
    ``abort_error``): the log may then hold that one unconfirmed line.
    """
    try:
        _write_document(path, document)
    except BaseException as exc:
        abort_error = _append_abort(path, audit_id, exc, actor)
        if abort_error is None:
            exc.aborted_audit_id = audit_id
        else:
            exc.unconfirmed_audit_id = audit_id
            exc.abort_error = abort_error
        raise


def _assert_write_allowed() -> None:
    reasons = write_refusals()
    if reasons:
        raise SwitchWriteRefused(reasons)


def _empty_document() -> Dict[str, Any]:
    return {"version": SCHEMA_VERSION, "global": {}, "projects": {}}


def _section(document: Dict[str, Any], scope: str, *, create: bool) -> Optional[Dict[str, Any]]:
    if scope == SCOPE_GLOBAL:
        return document.setdefault("global", {}) if create else document.get("global")
    projects = document.setdefault("projects", {}) if create else document.get("projects", {})
    return projects.setdefault(scope, {}) if create else projects.get(scope)


def set_switch(family: str, enabled: bool, *, scope: str, actor: str, reason: str) -> WriteResult:
    """Switch ``family`` on or off, globally (``scope="global"``) or for one project.

    Locks the sidecar lock, re-reads the file (refusing an unreadable one), preserves unknown
    families and fields verbatim, appends the audit line, then replaces the file atomically. A
    failed replace is marked in the log: see :func:`_replace_after_audit`.
    """
    name = check_switchable(family)
    target = SCOPE_GLOBAL if scope == SCOPE_GLOBAL else check_project_id(scope)
    if not isinstance(enabled, bool):
        raise TypeError(f"enabled must be a bool, got {enabled!r}")
    _assert_write_allowed()
    path = switch_file_path()
    with _locked(path):
        state = load_state(path)
        if not state.readable:
            raise SwitchFileUnreadable(state)
        document = copy.deepcopy(state.document) if state.exists else _empty_document()
        document["version"] = SCHEMA_VERSION
        section = _section(document, target, create=True)
        old = copy.deepcopy(section.get(name))
        new = dict(old) if isinstance(old, dict) else {}
        new.update({"enabled": enabled, "changed_at": _now(), "changed_by": actor, "reason": reason})
        section[name] = new
        audit_id = _append_audit(path, op="set", scope=target, family=name, old=old, new=new,
                                 reason=reason, actor=actor)
        _replace_after_audit(path, document, audit_id, actor)
    return WriteResult(changed=True, scope=target, family=name, old=old, new=new, path=path)


def clear_project_override(family: str, project_id: str, *, actor: str, reason: str) -> WriteResult:
    """Remove a project's override for ``family``; the project then follows the global section."""
    name = check_switchable(family)
    pid = check_project_id(project_id)
    _assert_write_allowed()
    path = switch_file_path()
    with _locked(path):
        state = load_state(path)
        if not state.readable:
            raise SwitchFileUnreadable(state)
        document = copy.deepcopy(state.document) if state.exists else _empty_document()
        section = _section(document, pid, create=False)
        if not isinstance(section, dict) or name not in section:
            return WriteResult(changed=False, scope=pid, family=name, old=None, new=None, path=path)
        old = copy.deepcopy(section.pop(name))
        if not section:
            document["projects"].pop(pid, None)
        audit_id = _append_audit(path, op="clear", scope=pid, family=name, old=old, new=None,
                                 reason=reason, actor=actor)
        _replace_after_audit(path, document, audit_id, actor)
    return WriteResult(changed=True, scope=pid, family=name, old=old, new=None, path=path)


def _read_audit_records(log_path: Path) -> List[Tuple[int, Dict[str, Any]]]:
    """Every non-blank line of the audit log as ``(lineno, record)``."""
    try:
        text = log_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AuditLogUnreadable(f"the audit log {log_path} does not exist") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise AuditLogUnreadable(f"cannot read the audit log {log_path}: {exc}") from exc
    records: List[Tuple[int, Dict[str, Any]]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise AuditLogUnreadable(f"{log_path} line {lineno} is not valid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise AuditLogUnreadable(f"{log_path} line {lineno} is not an object")
        records.append((lineno, record))
    return records


def _aborted_ids(log_path: Path, records: List[Tuple[int, Dict[str, Any]]]) -> Set[str]:
    """The ids of the change records a later ``abort`` cancels.

    A record without ``id`` (written before ids existed) is accepted. An ``id`` that is not a
    string or repeats an earlier one, and an ``abort`` whose ``ref`` names no earlier change
    record, make the log unreadable.
    """
    seen: Set[str] = set()
    changes: Set[str] = set()
    aborted: Set[str] = set()
    for lineno, record in records:
        rid = record.get("id")
        if rid is not None:
            if not isinstance(rid, str) or rid in seen:
                raise AuditLogUnreadable(f"{log_path} line {lineno}: id {rid!r} is not a unique string")
            seen.add(rid)
        if record.get("op") != OP_ABORT:
            if rid is not None:
                changes.add(rid)
            continue
        ref = record.get("ref")
        if not isinstance(ref, str) or ref not in changes:
            raise AuditLogUnreadable(
                f"{log_path} line {lineno}: unrecognised record (abort of ref={ref!r}, which no earlier record has)"
            )
        aborted.add(ref)
    return aborted


def _apply_audit_record(document: Dict[str, Any], record: Dict[str, Any], log_path: Path,
                        lineno: int) -> Dict[str, Any]:
    op, scope, name, new = record.get("op"), record.get("scope"), record.get("family"), record.get("new")
    if op == "set" and isinstance(scope, str) and isinstance(name, str) and isinstance(new, dict):
        _section(document, scope, create=True)[name] = copy.deepcopy(new)
    elif op == "clear" and isinstance(scope, str) and isinstance(name, str):
        section = _section(document, scope, create=False)
        if isinstance(section, dict):
            section.pop(name, None)
            if not section:
                document["projects"].pop(scope, None)
    elif op == "repair" and isinstance(new, dict):
        document = copy.deepcopy(new)
    else:
        raise AuditLogUnreadable(f"{log_path} line {lineno}: unrecognised record (op={op!r})")
    return document


def replay_audit(audit: Optional[Path] = None) -> Dict[str, Any]:
    """Rebuild the switch document by replaying the audit log in order.

    Skips every change record that a later ``abort`` references (a write that never landed).
    Raises :class:`AuditLogUnreadable` when the log is missing, a line is not a JSON object,
    an op is unknown, an ``abort`` references no earlier change record, an id repeats, or the
    result does not pass the version policy.
    """
    log_path = audit or audit_log_path()
    records = _read_audit_records(log_path)
    aborted = _aborted_ids(log_path, records)
    document = _empty_document()
    for lineno, record in records:
        if record.get("op") == OP_ABORT or record.get("id") in aborted:
            continue
        document = _apply_audit_record(document, record, log_path, lineno)
    _, _, _, error = parse_document(document)
    if error:
        raise AuditLogUnreadable(f"replaying {log_path} gives an unreadable state: {error}")
    return document


def _quarantine_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.corrupt-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")
        counter += 1
    return candidate


def repair(*, actor: str, empty: bool = False) -> WriteResult:
    """Repair an unreadable switch file.

    Keeps the unreadable file as ``<name>.corrupt-<UTC timestamp>``, rebuilds the state by
    replaying the audit log (or writes an empty version 1 file with ``empty=True``), records the
    repair in the audit log and replaces the switch file atomically, so the path never stops
    holding a fail-closed file. Refuses a readable or missing file.
    """
    _assert_write_allowed()
    path = switch_file_path()
    with _locked(path):
        state = load_state(path)
        if state.readable:
            raise RepairNotNeeded(
                f"{path} is {'readable' if state.exists else 'missing (no switches)'}; repair only handles an unreadable file"
            )
        rebuilt = _empty_document() if empty else replay_audit(audit_log_path(path))
        quarantined = _quarantine_path(path)
        shutil.copy2(path, quarantined)
        audit_id = _append_audit(path, op="repair", scope="*", family=None,
                                 old={"quarantined_to": quarantined.name, "error": state.error},
                                 new=rebuilt, reason="empty" if empty else "replayed from the audit log",
                                 actor=actor)
        _replace_after_audit(path, rebuilt, audit_id, actor)
    return WriteResult(changed=True, scope="*", family=None, old=state.error, new=rebuilt,
                       path=path, quarantined_to=quarantined)


# ---------------------------------------------------------------------------
# Census: every launch function, and what is still pending
# ---------------------------------------------------------------------------
# Entries are ``<repo-relative path>:<qualname>``. The coverage guard
# (tests/test_provider_switch_coverage.py) asserts the census against its AST detector and the
# measured floor: a detected launch function must be here or on EXEMPT, every entry here must
# call require() before its first launch call or be pending, and an EXEMPT entry must still
# match something.

# Launch functions the detector cannot see, each with the reason.
UNDETECTED_LAUNCH_FUNCTIONS: Dict[str, str] = {
    "scripts/lib/provider_spawns/claude_spawn.py:spawn_claude":
        "host-keyed check: a claude launch whose received env carries a non-Anthropic "
        "ANTHROPIC_BASE_URL maps that host to its family; the launch itself goes through "
        "SubprocessAdapter, so this body holds no process call",
    "scripts/lib/provider_spawns/deepseek_harness_spawn.py:spawn_deepseek_harness":
        "delegates to spawn_claude with the DeepSeek ANTHROPIC_BASE_URL; no process call in "
        "its own body",
    "scripts/lib/provider_spawns/glm_harness_spawn.py:spawn_glm_harness":
        "delegates to spawn_claude with the :4141 proxy ANTHROPIC_BASE_URL; no process call "
        "in its own body",
    "scripts/lib/provider_spawns/kimi_spawn.py:_start_kimi_subprocess":
        "Popen of an argv that arrives as a parameter; the literal 'kimi' head sits in "
        "_build_kimi_cmd, called by spawn_kimi",
    "scripts/lib/provider_spawns/gemini_spawn.py:_start_gemini_subprocess":
        "Popen of an argv that arrives as a parameter; the literal 'gemini' head sits in "
        "_build_gemini_cmd, called by _spawn_streaming and _spawn_legacy",
    "scripts/lib/provider_spawns/litellm_spawn.py:_start_litellm_subprocess":
        "starts the litellm runner through sys.executable; the model sits in the stdin payload",
    "scripts/lib/provider_spawns/litellm_spawn.py:spawn_litellm_agentic":
        "starts the agentic litellm runner through sys.executable; the model sits in the "
        "stdin payload",
    "scripts/lib/envelope_adapters_provider.py:ProviderAdapter._run_kimi":
        "the envelope's kimi branch; it reaches the CLI through spawn_kimi",
    "scripts/lib/llm_decision_router.py:_call_ollama_adapter":
        "reaches ollama through OllamaAdapter.execute; the host constant lives in this module "
        "but the HTTP call in the adapter",
    "scripts/lib/providers/local_gemma/spawn.py:spawn_local_gemma":
        "runs local-gemma in-process through MLX (runtime_mlx.run_mlx): no process or request "
        "for the detector to see",
}

PENDING_C: Tuple[str, ...] = (
    "scripts/gate_runner.py:GateRunner._run_with_stall_detection",
)

PENDING_E: Tuple[str, ...] = (
    "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._try_ollama",
)

PENDING_B: Tuple[str, ...] = (
    "scripts/benchmark/field-tests/runners/scorer.py:_judge_kimi",
    "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._try_deepseek_harness",
    "scripts/lib/adapters/_litellm_agentic_runner.py:main",
    "scripts/lib/adapters/_litellm_runner.py:main",
    "scripts/lib/adapters/gemini_adapter.py:GeminiAdapter._execute_legacy",
    "scripts/lib/adapters/gemini_adapter.py:GeminiAdapter.stream_events",
    "scripts/lib/adapters/ollama_adapter.py:OllamaAdapter._drain_http_stream",
    "scripts/lib/classifier_providers/deepseek_provider.py:DeepSeekProvider.classify",
    "scripts/lib/classifier_providers/gemini_provider.py:GeminiProvider.classify",
    "scripts/lib/classifier_providers/ollama_provider.py:OllamaProvider.classify",
    "scripts/lib/gemini_wrapper.py:gemini_exec",
    "scripts/lib/kimi_wrapper.py:kimi_exec",
    "scripts/lib/providers/local_gemma/spawn.py:_run_ollama_fallback",
    "scripts/lib/vertex_ai_runner.py:run_vertex_ai",
    "scripts/llm_benchmark.py:_ollama_api",
    "scripts/llm_benchmark.py:_ollama_stream",
    *UNDETECTED_LAUNCH_FUNCTIONS,
)

LAUNCH_FUNCTIONS: Tuple[str, ...] = tuple(sorted({*PENDING_C, *PENDING_E, *PENDING_B}))

# Flagged by the detector or named by the plan, and deliberately outside layer 1. An entry is a
# function (``path:qualname``), a file or a directory (trailing slash).
EXEMPT: Dict[str, str] = {
    "scripts/lib/provider_spawns/claude_spawn.py":
        "claude launches are never switchable (decision 2b); spawn_claude is in the census "
        "only for its host-keyed check",
    "scripts/lib/provider_spawns/codex_spawn.py": "codex launch (decision 2b)",
    "scripts/lib/providers/provider_lanes/codex.py": "re-export of the codex lane (decision 2b)",
    "scripts/lib/classifier_providers/haiku_provider.py": "claude (haiku) launch (decision 2b)",
    "scripts/lib/classifier_providers/codex_provider.py": "codex launch (decision 2b)",
    "scripts/lib/adapters/codex_adapter.py:CodexAdapter.stream_events": "codex launch (decision 2b)",
    "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._try_claude_max":
        "claude launch on the subscription (decision 2b)",
    "scripts/lib/dispatch_cli.py:_check_reachability":
        "reachability probe (GET /v1/models): no model call",
    "scripts/lib/adapters/ollama_adapter.py:OllamaAdapter.reachability":
        "reachability probe (GET /api/tags): no model call",
    "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._probe_ollama":
        "reachability probe (GET /api/tags): no model call",
    "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._fetch_deepseek_balance":
        "balance probe (GET /user/balance): no model call",
    "scripts/benchmark/field-tests/runners/check_openrouter_credits.py:check":
        "credit probe (GET /api/v1/auth/key): no model call",
    "scripts/lib/adapters/litellm_adapter.py:LiteLLMAdapter.health_check":
        "`litellm --health-check`: no model call",
    "scripts/lib/provider_spawns/kimi_spawn.py:_harvest_session_token_usage":
        "`kimi export` reads a finished session's archive for its token accounting: no model call",
    "scripts/llm_benchmark.py:pull_ollama_model": "`ollama pull` downloads weights: no model call",
    "scripts/conversation_analyzer_nightly.sh": "daemon start-up (`ollama serve`): no model call",
    "scripts/spikes/": "throwaway spikes; no runtime module imports from them",
}
