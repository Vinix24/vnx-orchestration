"""VNX_AGENT_LAYOUT: the mode switch for the agent-as-folder skill layout.

Three modes:

* ``off`` (default, no fleet default: ADR-050 section 1): nothing changes.
* ``shadow``: a non-discovered copy of an agent's skills is written under
  ``.vnx-agent-skills/``.
* ``on``: the skills are materialized at the provider's native, discovered
  destination and a record per attempt feeds the receipt (ADR-059). The operator
  sets it per project.

An unknown value reads as ``off`` and logs one WARNING. The flag is independent of
``VNX_AGENT_FOLDERS`` (``agent_resolver.agent_folders_enabled``), which this module
never touches.
"""

from __future__ import annotations

import logging

import config_registry

logger = logging.getLogger(__name__)

AGENT_LAYOUT_ENV = "VNX_AGENT_LAYOUT"
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"


def layout_mode() -> str:
    """Return ``off``, ``shadow`` or ``on``; an unknown value returns ``off``.

    Reads through the config registry precedence chain. Each call that meets
    an unknown value logs exactly one WARNING.
    """
    raw = config_registry.get(AGENT_LAYOUT_ENV)
    value = (raw if raw is not None else MODE_OFF).strip().lower()
    if value in (MODE_SHADOW, MODE_ON):
        return value
    if value in (MODE_OFF, ""):
        return MODE_OFF
    logger.warning("VNX_AGENT_LAYOUT=%r is onbekend; nu off", raw)
    return MODE_OFF


def is_shadow() -> bool:
    """True only when the layout mode resolves to ``shadow``."""
    return layout_mode() == MODE_SHADOW


def is_on() -> bool:
    """True only when the layout mode resolves to ``on``."""
    return layout_mode() == MODE_ON
