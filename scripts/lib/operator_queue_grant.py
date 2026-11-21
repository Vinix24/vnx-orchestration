"""``answer --grant`` (ADR-088 decisions 5 and 7, PG-3c): only ``operator`` or ``t0`` (``actor_class.derive``) grants,
measured by ``pr_size_policy.measure`` on the head ``gh pr view`` names now; any failure is QueueRefused, never a grant."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

try:
    import actor_class
    import operator_queue as oq
    import pr_size_policy
    from forge_project_target import project_checkout_path
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_grant
    from scripts.lib import actor_class
    from scripts.lib import operator_queue as oq
    from scripts.lib import pr_size_policy
    from scripts.lib.forge_project_target import project_checkout_path

ALLOWED = (actor_class.OPERATOR, actor_class.T0)


def check_actor(env: Optional[Mapping[str, str]] = None, isatty: Optional[bool] = None) -> str:
    """The actor class when it may grant; QueueRefused for worker, automation and unknown."""
    who = actor_class.derive(env, isatty)
    if who not in ALLOWED:
        raise oq.QueueRefused(f"answer --grant: actor class {who} may not grant; only operator or t0")
    return who


def pr_head(repo: Path, pr: int) -> Dict[str, str]:
    """``headRefOid``, ``baseRefName`` and ``state`` of an OPEN PR; QueueRefused naming why for anything else."""
    try:
        done = subprocess.run(["gh", "pr", "view", str(pr), "--json", "headRefOid,baseRefName,state"],
                              cwd=str(repo), capture_output=True, text=True, timeout=20)
        data = json.loads(done.stdout) if done.returncode == 0 else None
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise oq.QueueRefused(f"answer --grant: gh pr view {pr} unreadable ({type(exc).__name__})") from exc
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k].strip()
                                             for k in ("headRefOid", "baseRefName", "state")):
        raise oq.QueueRefused(f"answer --grant: gh pr view {pr} unreadable (exit {done.returncode})")
    if data["state"] != "OPEN":
        raise oq.QueueRefused(f"answer --grant: PR {pr} is {data['state'][:20]}, not OPEN")
    return data


def measure_grant(project_id: str, point: Mapping[str, Any], stamp: str) -> Dict[str, Any]:
    """The ``grant`` field for an answer on this ``pr:<n>`` point, measured now; QueueRefused on any failure."""
    pr = int(str(point["ref"]).split(":", 1)[1])
    repo = project_checkout_path(project_id)
    if repo is None:
        raise oq.QueueRefused(f"answer --grant: no checkout registered for {project_id}")
    head = pr_head(Path(repo), pr)
    try:
        got = pr_size_policy.measure(pr, head["headRefOid"], head["baseRefName"], Path(repo))
    except pr_size_policy.PrSizeUnmeasured as exc:
        raise oq.QueueRefused(f"answer --grant: measurement failed at {exc.step}: {exc.detail}") from exc
    if point["ceiling"] < got.lines:
        raise oq.QueueRefused(f"answer --grant: ceiling {point['ceiling']} is below the measured {got.lines} lines")
    return {"ceiling": point["ceiling"], "reason": point["reason"], "head_sha": head["headRefOid"], "base_ref": got.base_ref,
            "lines": got.lines, "measured_at": stamp}
