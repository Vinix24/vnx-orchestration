#!/usr/bin/env python3
"""mcp_server_config.py: the global MCP server table, made safe to write to a file or argv.

One source, two consumers. The source is the ``mcpServers`` block of ``~/.claude.json``
(or the file named by ``VNX_GLOBAL_MCP_CONFIG_PATH``):

  * terminal ``.mcp.json`` generation, shared by ``vnx init``
    (``scripts/vnx_init.py``) and ``vnx bootstrap-terminals`` (``bin/vnx``);
  * the role-scoped ``--mcp-config`` handed to a headless worker
    (``worker_permissions.resolve_role_mcp_config``).

Neither consumer may put a credential into a file or into argv. The old generators copied
every entry with ``dict(cfg)``, ``env`` included, into a file that Claude Code ranks above
``~/.claude.json``: a key rotation then reached no terminal, and 21 files over five
projects held Perplexity keys (measured 2026-09-23).

What Claude Code does, measured 2026-09-23 on claude 2.1.280 (not documented)
-----------------------------------------------------------------------------
Method: isolated ``HOME`` + ``CLAUDE_CONFIG_DIR`` with a fake global server, a scratch
project directory, ``claude -p --output-format stream-json --verbose`` (the ``init``
message reports every server as ``{name, status, source}``) and ``claude mcp list``. The
scopes themselves are described at https://code.claude.com/docs/en/mcp; the four facts
below are not, and were measured:

  1. A project ``.mcp.json`` entry replaces a user-scope server of the same name
     (``source: project``). That is the only file-based way to switch a global server
     off for one directory. ``disabledMcpServers`` in ``.claude/settings.json`` or
     ``settings.local.json``, ``disabledMcpjsonServers`` and an empty ``mcpServers`` all
     left the user-scope server in the session (``source: user``).
  2. ``"disabled": true`` is not a field Claude Code reads. A working stdio server carrying
     it still reported ``connected``. The old generators therefore disabled nothing: they
     re-declared each global server, credentials included, one scope higher.
  3. An entry without ``command`` (stdio) is skipped as invalid
     (``command: Invalid input``), so ``{"disabled": true}`` alone does not work either.
  4. ``{"command": "true"}`` is the smallest entry Claude Code accepts. It replaces the
     global server by name, exits at once and leaves the server ``failed`` with no tools.
     That is :data:`MASK_ENTRY`. It works for a global http server too: the mask is keyed
     by name and Claude Code never compares the transport.

A ``.mcp.json`` can only mask names it knows. The file therefore lists the global names
and nothing else, and is derived from the global table again on every run: a removed
server disappears, a new one is masked. A server added to the global config AFTER the last
run stays unmasked until the next ``vnx init`` or ``vnx bootstrap-terminals``.

Worker ``--mcp-config``: why references and not values
------------------------------------------------------
``claude --mcp-config <json>`` takes an inline JSON string, so the definition is an argv
element and shows up in the process list (measured with ``ps -ww`` against a live process
launched with the exact argv). A file would only move the credential to disk. Three more
facts measured on 2.1.280 decide the shape:

  * ``${VAR}`` inside an ``env`` value of an inline ``--mcp-config`` is expanded from the
    spawning process's environment.
  * A stdio server inherits its parent's environment: a variable exported to the worker is
    visible to the server without any ``env`` entry.
  * An unset ``${VAR}`` is not an error. The server receives the literal string ``${VAR}``.

So a literal never has to travel. :func:`reference_only_definition` keeps an ``env`` or
``headers`` value only when it is a reference the runtime resolves (``${VAR}``, or an auth
scheme plus one: ``Bearer ${VAR}``). A literal is dropped and logged by server and key,
never by value. The variable of the same name still reaches the server through
inheritance when the fabric exports it. A literal is NOT rewritten into ``${KEY}``: with the
variable unset that would hand the server the placeholder string as its key.

Boundary: ``args`` and ``url`` are copied as written. They are positional, so no rule can
tell a credential from configuration there without guessing. Put a credential in ``env``
(or a header) as a ``${VAR}`` reference.

BILLING SAFETY: standard library only. No Anthropic SDK, no api.anthropic.com calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

# Smallest entry Claude Code accepts for a server (fact 4 above). It replaces the global
# server of the same name and starts nothing that can hold a credential.
MASK_ENTRY: Dict[str, str] = {"command": "true"}

# The pre-2026-09-23 generators wrote every entry with this flag. Claude Code ignores it
# (fact 2), so it never marks an entry an operator wrote on purpose: it marks generator
# output, which is how a legacy frozen entry is recognised and scrubbed.
_LEGACY_GENERATED_FLAG = "disabled"

# `${VAR}` alone, or an auth scheme and one reference (`Bearer ${VAR}`). A default
# (`${VAR:-x}`) and any other literal text can hide a credential, so neither matches.
_REFERENCE_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9-]* )?\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

# Where each field's literal can still reach the server once it is dropped.
_CREDENTIAL_FIELDS: Dict[str, str] = {
    "env": (
        "the server inherits the worker's environment, so the variable of the same name "
        "reaches it when the fabric exports it; or write it as a ${VAR} reference in the "
        "global config"
    ),
    "headers": "write it as a ${VAR} reference (for example 'Bearer ${TOKEN}') in the global config",
}


# ---------------------------------------------------------------------------
# The global table
# ---------------------------------------------------------------------------

def global_mcp_config_path() -> Path:
    """The ambient MCP server source: ``VNX_GLOBAL_MCP_CONFIG_PATH``, else ``~/.claude.json``."""
    override = os.environ.get("VNX_GLOBAL_MCP_CONFIG_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude.json"


def load_global_mcp_servers(config_path: Optional[Path] = None) -> Dict[str, dict]:
    """Read the ``mcpServers`` block. Returns ``{}`` when the file is absent or unreadable.

    An entry that is not a JSON object is dropped: Claude Code skips it as invalid, so it
    is neither a server to mask nor one a worker could be handed.
    """
    path = config_path if config_path is not None else global_mcp_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("mcp_server_config: failed to load %s: %s", path, exc)
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    valid: Dict[str, dict] = {}
    for name, definition in servers.items():
        if isinstance(definition, dict):
            valid[name] = definition
        else:
            logger.warning("mcp_server_config: server %r in %s is not an object, ignored", name, path)
    return valid


# ---------------------------------------------------------------------------
# Terminal .mcp.json
# ---------------------------------------------------------------------------

def _is_generated_entry(entry: object) -> bool:
    """True for an entry this generator wrote, in its current or its legacy shape."""
    if not isinstance(entry, dict):
        return False
    return entry == MASK_ENTRY or entry.get(_LEGACY_GENERATED_FLAG) is True


def build_terminal_mcp_config(
    global_servers: Mapping[str, object],
    existing_servers: Optional[Mapping[str, object]] = None,
) -> dict:
    """The ``.mcp.json`` content for one terminal.

    Every global server name gets a :data:`MASK_ENTRY`; no field of the global definition
    is copied. An entry already in the file that this generator did not write stays: it is
    a project server the operator added, and one that shares a global name keeps
    overriding it as the operator intended. Generator output, current or legacy, is
    rebuilt from the global table.
    """
    servers: Dict[str, object] = {
        name: entry
        for name, entry in (existing_servers or {}).items()
        if not _is_generated_entry(entry)
    }
    for name in global_servers:
        servers.setdefault(name, dict(MASK_ENTRY))
    return {"mcpServers": servers}


def _read_existing_servers(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("mcp_server_config: %s is unreadable (%s), rewriting it", path, exc)
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def write_terminal_mcp_configs(
    terminals_dir: Path,
    terminal_ids: Sequence[str],
    force: bool = False,
    global_config_path: Optional[Path] = None,
) -> List[Tuple[Path, bool]]:
    """Write ``<terminals_dir>/<id>/.mcp.json`` for every terminal.

    The file is derived from the global table on every run, so it never freezes a server
    table. An entry the operator added survives; ``force`` starts from a clean file and
    drops it. Returns ``(path, written)`` per terminal; ``written`` is False when the file
    already held exactly this content.
    """
    global_servers = load_global_mcp_servers(global_config_path)
    results: List[Tuple[Path, bool]] = []
    for tid in terminal_ids:
        target = terminals_dir / tid / ".mcp.json"
        existing = {} if force else _read_existing_servers(target)
        rendered = json.dumps(build_terminal_mcp_config(global_servers, existing), indent=2) + "\n"
        if target.exists() and target.read_text() == rendered:
            results.append((target, False))
            continue
        atomic_write_text(target, rendered)
        results.append((target, True))
    return results


# ---------------------------------------------------------------------------
# Worker --mcp-config
# ---------------------------------------------------------------------------

def reference_only_definition(server_name: str, definition: Mapping[str, object]) -> dict:
    """A copy of *definition* whose ``env`` and ``headers`` carry no literal value.

    A value survives only when it is a reference the runtime resolves (see the module
    docstring for why). A literal is dropped and logged by server, field and key; the value
    is never logged. A field left empty is removed. A field that is not an object is
    dropped as well: it cannot be checked, so it is not passed.
    """
    scoped = dict(definition)
    for field_name, hint in _CREDENTIAL_FIELDS.items():
        if field_name not in scoped:
            continue
        values = scoped.pop(field_name)
        if not isinstance(values, dict):
            logger.warning(
                "mcp_server_config: server %r %s is not an object, not passed to the worker",
                server_name, field_name,
            )
            continue
        kept: Dict[str, str] = {}
        for key, value in values.items():
            if isinstance(value, str) and _REFERENCE_RE.match(value):
                kept[key] = value
            else:
                logger.warning(
                    "mcp_server_config: server %r %s %r holds a literal value, not passed "
                    "to the worker (%s)",
                    server_name, field_name, key, hint,
                )
        if kept:
            scoped[field_name] = kept
    return scoped


# ---------------------------------------------------------------------------
# CLI, used by `vnx bootstrap-terminals`
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_server_config.py",
        description="Write the per-terminal .mcp.json files: every global MCP server "
        "masked by name, no definition copied.",
    )
    parser.add_argument("--terminals-dir", required=True, type=Path)
    parser.add_argument("--terminals", required=True, help='space-separated ids, e.g. "T0 T1 T2 T3"')
    parser.add_argument("--force", action="store_true", help="start from a clean file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="[bootstrap-terminals] %(message)s")
    for path, written in write_terminal_mcp_configs(
        args.terminals_dir, args.terminals.split(), args.force
    ):
        state = "Wrote" if written else "Up to date"
        print(f"[bootstrap-terminals] {state} .mcp.json (global servers masked by name): {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
