"""ADR-090 account folders; invalid mappings raise ValueError before any evidence is written."""
import json
import os
from pathlib import Path

import config_runtime


def account_dirs(home=None):
    """Load the global mapping once; return slot -> resolved folder, including an unmapped config dir."""
    home = Path(home) if home is not None else Path.home()
    raw = config_runtime.get("VNX_ACCOUNT_DIRS", strict=True)

    def folder(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("map must be a nonempty path string")
        path = Path(str(home) + value[1:]) if value == "~" or value.startswith("~/") else Path(value).expanduser()
        return path.resolve()

    try:
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise ValueError("expected a list of {map, slot} objects")
        declared = {}
        for row in rows:
            if not isinstance(row, dict) or "map" not in row or row.get("slot") not in ("vnx", "sm"):
                raise ValueError("each row needs map and slot vnx or sm")
            path = folder(row["map"])
            if path in declared:
                raise ValueError("duplicate folder")
            declared[path] = row["slot"]
        default = folder("~/.claude")
        known = {default: "vnx", **declared}
        if len(set(known.values())) != len(known):
            raise ValueError("duplicate slot (including the default folder)")
        ordered = {default: known[default]}
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        if configured is not None:
            path = folder(configured)
            ordered[path] = known.get(path, "unknown")
        ordered.update(declared)
        return {slot: path for path, slot in ordered.items()}
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        raise ValueError(f"VNX_ACCOUNT_DIRS={raw!r}: {exc}") from exc
