"""vnx_ids.py — shared VNX identifier constants.

Single source of truth for project_id validation regex.
Both state_aggregator and control_centre_cli import from here so the
pattern is never defined in two places.
"""
from __future__ import annotations

import re

# Canonical project_id: lowercase letter start, then 1-31 chars of
# lowercase alphanum or hyphens. Total length: 2-32.
# Matches: vnx-dev, sales-copilot, seocrawler-v2
# Rejects: Sales (uppercase), proj_ (underscore), a (single char), x*33
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
