"""The two host-wide locations of the capacity store and the operator policy.

ADR-062 fixes them: the store and the policy are host-wide by design, shared by every user
on the machine, so they are not a user path. No environment variable chooses a path. The
doctor scan excludes this module by name.
"""
from pathlib import Path

DEFAULT_STORE_DIR = Path("/Users/Shared/vnx-slots")
DEFAULT_POLICY_PATH = Path("/Users/Shared/vnx-slots-policy/host.json")
