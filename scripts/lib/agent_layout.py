"""VNX_AGENT_LAYOUT: the mode switch for the agent-as-folder skill layout.

Phase 1 of track ``agents-als-map-fase-1`` knows two modes:

* ``off`` (default): nothing changes.
* ``shadow``: a non-discovered copy of an agent's skills is written under
  ``.vnx-agent-skills/`` (built by later PRs of the track).

``on`` is phase 2. In phase 1 it reads as ``off`` and logs one WARNING, so the
discovered copy cannot be switched on early. An unknown value is handled the
same way. The flag is independent of ``VNX_AGENT_FOLDERS``
(``agent_resolver.agent_folders_enabled``), which this module never touches.
"""

from __future__ import annotations

import logging

import config_registry

logger = logging.getLogger(__name__)

AGENT_LAYOUT_ENV = "VNX_AGENT_LAYOUT"
MODE_OFF = "off"
MODE_SHADOW = "shadow"


def layout_mode() -> str:
    """Return ``off`` or ``shadow``; ``on`` and unknown values return ``off``.

    Reads through the config registry precedence chain. Each call that meets
    ``on`` or an unknown value logs exactly one WARNING.
    """
    raw = config_registry.get(AGENT_LAYOUT_ENV)
    value = (raw if raw is not None else MODE_OFF).strip().lower()
    if value == MODE_SHADOW:
        return MODE_SHADOW
    if value in (MODE_OFF, ""):
        return MODE_OFF
    if value == "on":
        logger.warning("VNX_AGENT_LAYOUT=on is fase 2; nu off")
    else:
        logger.warning("VNX_AGENT_LAYOUT=%r is onbekend; nu off", raw)
    return MODE_OFF


def is_shadow() -> bool:
    """True only when the layout mode resolves to ``shadow``."""
    return layout_mode() == MODE_SHADOW
