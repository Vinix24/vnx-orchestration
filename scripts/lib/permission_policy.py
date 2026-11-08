"""One PermissionPolicy with one policy_digest (track provider-agnostic-permissions, P2).

Two sources set the boundary: ``role_profile`` (the role's entry in
``.vnx/worker_permissions.yaml``, read only) and ``agent_isolation`` (the optional
``isolation`` block of ``agents/<role>/config.yaml``). ``governance_profile`` is
recorded in ``sources`` as ``excluded_source`` and never feeds the digest.

The digest is a sha256 over a canonical JSON of the boundary fields plus
``version``. The role name is not in it: equal boundaries share a digest.

Evidence is fail-closed: an unreadable or broken source, an unknown key or a
malformed value raises :class:`PermissionPolicyRefused` with a named ``reason``.
Degradation is fail-open and visible in ``sources``: ``is_fallback``,
``isolation: {}`` and an absent isolation block. This module must not import
``worker_permissions`` (which imports it).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

POLICY_VERSION = 1
PROFILE_KEYS = frozenset({
    "allowed_tools", "denied_tools", "bash_allow_patterns",
    "bash_deny_patterns", "file_write_scope", "mcp_servers",
})
ISOLATION_KEYS = frozenset({"scope_type", "allowed_paths", "denied_paths"})
_NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})
_ROLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PermissionPolicyRefused(ValueError):
    """The policy cannot be proven. ``reason`` is a stable slug to match on."""

    reason = "refused"

    def __init__(self, message: str, reason: Optional[str] = None) -> None:
        super().__init__(message)
        if reason:
            self.reason = reason


def _norm(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class FilesystemScope:
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    scope_type: str = ""


@dataclass(frozen=True)
class ToolScope:
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True)
class PermissionPolicy:
    filesystem: FilesystemScope
    tools: ToolScope
    mcp: ToolScope
    network: str
    # Degradation record, not boundary: excluded from equality and the digest.
    sources: Mapping[str, Any] = field(default_factory=dict, compare=False)
    version: int = POLICY_VERSION

    def boundary(self) -> dict:
        fs = self.filesystem
        return {
            "filesystem": {"read": list(fs.read), "write": list(fs.write),
                           "deny": list(fs.deny), "scope_type": fs.scope_type},
            "tools": {"allow": list(self.tools.allow), "deny": list(self.tools.deny)},
            "mcp": {"allow": list(self.mcp.allow), "deny": list(self.mcp.deny)},
            "network": self.network,
            "version": self.version,
        }

    @property
    def policy_digest(self) -> str:
        try:
            canonical = json.dumps(self.boundary(), sort_keys=True,
                                   separators=(",", ":"), ensure_ascii=False)
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except (TypeError, ValueError) as exc:
            raise PermissionPolicyRefused(
                f"policy digest could not be computed: {exc}", "digest-failed"
            ) from exc


def _read_yaml(path: Path, label: str) -> Any:
    """Parse *path* as YAML. Any read or parse failure refuses, never defaults."""
    try:
        import yaml
    except ImportError as exc:
        raise PermissionPolicyRefused(
            f"{label} cannot be parsed: PyYAML is not importable", "yaml-unavailable"
        ) from exc
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PermissionPolicyRefused(f"{label} is missing", "source-missing") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise PermissionPolicyRefused(
            f"{label} is unreadable: {exc}", "source-unreadable") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PermissionPolicyRefused(
            f"{label} is not valid YAML: {exc}", "source-broken-yaml") from exc


def _malformed(message: str) -> PermissionPolicyRefused:
    return PermissionPolicyRefused(message, "source-malformed")


def load_role_profiles(path: Path) -> Mapping[str, Any]:
    """The ``profiles`` map of worker_permissions.yaml, read strictly.

    An empty file, a non-mapping document or a non-mapping ``profiles`` refuses.
    A role merely absent from the map does not: the caller falls back.
    """
    data = _read_yaml(path, "worker_permissions.yaml")
    if data is None:
        raise PermissionPolicyRefused("worker_permissions.yaml is empty", "source-empty")
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise _malformed("worker_permissions.yaml has no 'profiles' mapping")
    return data["profiles"]


def _check_keys(where: str, block: Any, known: frozenset, lists: Iterable[str]) -> None:
    if not isinstance(block, dict):
        raise _malformed(f"{where} is not a mapping")
    unknown = sorted(set(block) - known)
    if unknown:
        raise PermissionPolicyRefused(
            f"{where} has unknown key(s): {unknown}", "unknown-source-key")
    for key in lists:
        value = block.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _malformed(f"{where} key {key!r} must be a list of strings")


def check_profile_entry(role: str, raw: Any) -> None:
    """Refuse a role entry with an unknown key or a non-list boundary value."""
    if raw is not None:
        _check_keys(f"profile for role {role!r}", raw, PROFILE_KEYS, PROFILE_KEYS)


def load_isolation(role: str, agents_dir: Optional[Path]) -> Optional[dict]:
    """The ``isolation`` block of ``agents/<role>/config.yaml``; None when absent.

    ``{}`` is present-but-empty, not missing. A role that is not a plain directory
    name, a broken file, a non-mapping block or an unknown key refuses.
    """
    if agents_dir is None:
        return None
    if not _ROLE_NAME.match(role):
        raise PermissionPolicyRefused(
            f"role {role!r} is not a valid agent directory name", "role-name-invalid")
    config = Path(agents_dir) / role / "config.yaml"
    if not config.is_file():
        return None
    data = _read_yaml(config, f"agents/{role}/config.yaml")
    if not isinstance(data, dict) or "isolation" not in data:
        return None
    where = f"isolation of role {role!r}"
    block = data["isolation"]
    _check_keys(where, block, ISOLATION_KEYS, ("allowed_paths", "denied_paths"))
    if not isinstance(block.get("scope_type", ""), str):
        raise _malformed(f"{where} key 'scope_type' must be a string")
    return block


def _network_tools(tools: Iterable[str]) -> set:
    return {t.split("(", 1)[0] for t in tools} & _NETWORK_TOOLS


def _network(allow: Iterable[str], deny: Iterable[str]) -> str:
    return "allow" if _network_tools(allow) - _network_tools(deny) else "deny"


def compose_policy(
    fields: Mapping[str, Any],
    isolation: Optional[Mapping[str, Any]],
    *,
    is_fallback: bool,
    implicit_tool_deny: Iterable[str] = (),
    mcp_namespace_deny: str = "",
) -> PermissionPolicy:
    """Compose the policy from the resolved role fields and the isolation block.

    ``implicit_tool_deny`` is denied to every worker whatever the role says;
    ``mcp_namespace_deny`` is denied when the role names no MCP server.
    """
    unknown = sorted(set(fields) - PROFILE_KEYS)
    if unknown:
        raise PermissionPolicyRefused(
            f"unknown role field(s): {unknown}", "unknown-source-key")
    iso = isolation or {}
    allow = list(fields.get("allowed_tools", []))
    allow += [f"bash_allow:{p}" for p in fields.get("bash_allow_patterns", [])]
    deny = list(fields.get("denied_tools", [])) + list(implicit_tool_deny)
    deny += [f"bash_deny:{p}" for p in fields.get("bash_deny_patterns", [])]
    servers = list(fields.get("mcp_servers", []))
    iso_state = "absent" if isolation is None else ("present" if isolation else "present-empty")
    return PermissionPolicy(
        filesystem=FilesystemScope(
            read=_norm(iso.get("allowed_paths", [])),
            write=_norm(fields.get("file_write_scope", [])),
            deny=_norm(iso.get("denied_paths", [])),
            scope_type=iso.get("scope_type", ""),
        ),
        tools=ToolScope(allow=_norm(allow), deny=_norm(deny)),
        mcp=ToolScope(allow=_norm(servers),
                      deny=_norm([mcp_namespace_deny] if mcp_namespace_deny and not servers else [])),
        network=_network(allow, deny),
        sources={
            "role_profile": {"state": "fallback" if is_fallback else "profile"},
            "agent_isolation": {"state": iso_state},
            "governance_profile": {"state": "excluded_source"},
        },
    )
