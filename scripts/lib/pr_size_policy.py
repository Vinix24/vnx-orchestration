"""PR-size policy of the merge door (ADR-088, PG-1; wired in PG-1d): pure, every failure named, never a zero."""
import logging
import re
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import config_registry
import review_risk_class
from auto_merge_policy import GOVERNANCE_PATH_MARKERS

KEYS = ("VNX_PR_SIZE_ENFORCE", "VNX_PR_SIZE_HOLD", "VNX_PR_SIZE_HOLD_SECURITY", "VNX_SECURITY_PATHS")
_LIMITS = KEYS[1:3]
# The approved layers (V2.1): the project row and the global file, each with an approval-id writer.
FLOOR_LAYERS = ("project_config", "global_config", "default")
_MODES = ("off", "warn", "refuse")  # loosest first: a higher index is stricter
_HEAD_RE = re.compile(r"[0-9a-f]{7,64}")
Measurement = namedtuple("Measurement", "lines paths base_ref merge_base")  # lines: added plus deleted


class PrSizeUnmeasured(Exception):
    """A measurement failure; ``step`` names the step that failed."""
    def __init__(self, step: str, detail: str):
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


@dataclass(frozen=True)
class Verdict:
    outcome: str  # GO, NO-GO or WARN
    reason: str = ""
    lines: Optional[int] = None
    limit: Optional[int] = None
    limit_kind: str = ""  # hold or security
    security_paths_hit: Tuple[str, ...] = ()
    mode: str = ""
    source: str = ""
    waived: str = ""
    detail: str = ""
    errors: Tuple[str, ...] = ()


def _git(step: str, args, cwd: Path) -> str:
    out, why = review_risk_class._git(args, cwd)
    if out is None:
        raise PrSizeUnmeasured(step, why)
    return out


def measure(pr, head_sha: str, base_branch, cwd: Path) -> Measurement:
    """Lines (added plus deleted, tests included) and paths over ``merge_base(base, head)..head``; no default base."""
    base_ref, why = review_risk_class.base_ref_for(base_branch)
    if base_ref is None:
        raise PrSizeUnmeasured("base_branch", why)
    head = (head_sha or "").strip()
    if not _HEAD_RE.fullmatch(head):
        raise PrSizeUnmeasured("head", f"the PR head {head_sha!r} is no commit sha")
    why = review_risk_class.fetch_merge_refs(str(pr), None, cwd=cwd, base_ref=base_ref)
    if why:
        raise PrSizeUnmeasured("fetch", why)
    merge_base = _git("merge_base", ["merge-base", base_ref, head], cwd).strip()
    if not merge_base:
        raise PrSizeUnmeasured("merge_base", f"empty merge base of {base_ref} and {head[:12]}")
    numstat = _git("numstat", ["diff", "--numstat", "--no-renames", merge_base, head], cwd)
    names = _git("name_only", ["diff", "--name-only", "--no-renames", "-z", merge_base, head], cwd)
    lines = 0
    for row in numstat.splitlines():
        cols = row.split("\t")
        if len(cols) >= 3 and cols[0] == cols[1] == "-":
            continue  # a binary file: 0 lines, its path stays in the list
        if len(cols) < 3 or not (cols[0].isdigit() and cols[1].isdigit()):
            raise PrSizeUnmeasured("numstat", f"unreadable numstat row {row!r}")
        lines += int(cols[0]) + int(cols[1])
    paths = tuple(p for p in names.split("\0") if p)
    if not paths:
        raise PrSizeUnmeasured("empty_diff", f"no changed path between {merge_base[:12]} and {head[:12]}")
    return Measurement(lines, paths, base_ref, merge_base)


def _mode(value, source) -> Tuple[str, str]:
    """``(mode, error)``; a value outside the three reads as refuse, with an ERROR naming value and source."""
    v = (value or "").strip().lower()
    if v in _MODES:
        return v, ""
    why = f"VNX_PR_SIZE_ENFORCE={value!r} from {source} is not off, warn or refuse; read as refuse"
    logging.getLogger(__name__).error("pr_size_policy: %s", why)
    return "refuse", why


def _limit(value) -> Optional[int]:
    v = (value or "").strip()
    return int(v) if v.isascii() and v.isdigit() and int(v) > 0 else None


def read_policy(project_id=None, resolver=None):
    """``(policy, None)``, or ``(None, Verdict)`` when unreadable or loosened; floor and chain are read strict."""
    floor, eff = {}, {}  # {key: (value, source)}
    for key in KEYS:
        for into, layers in ((floor, FLOOR_LAYERS), (eff, config_registry.CHAIN_LAYERS)):
            into[key] = value, source = config_registry.resolve_with_source(
                key, project_id, resolver, strict_resolver=True, layers=layers)
            if source in config_registry.ERROR_SOURCES or source == "unset":
                return None, Verdict("NO-GO", "pr_size_config_unreadable", detail=f"{key}: {source}")
            if key in _LIMITS and _limit(value) is None:
                return None, Verdict("NO-GO", "pr_size_config_unreadable",
                                     detail=f"{key}: {value!r} from {source} is not a positive integer")
    (floor_mode, floor_err), (eff_mode, eff_err) = _mode(*floor[KEYS[0]]), _mode(*eff[KEYS[0]])
    errors = tuple(e for e in (floor_err, eff_err) if e)
    looser = {KEYS[0]: _MODES.index(eff_mode) < _MODES.index(floor_mode)}
    looser.update({k: _limit(eff[k][0]) > _limit(floor[k][0]) for k in _LIMITS})
    for key in (k for k, loose in looser.items() if loose and floor_mode == "refuse"):
        return None, Verdict("NO-GO", "pr_size_config_loosened", mode=floor_mode, source=eff[key][1],
                             errors=errors, detail=(f"{key}={eff[key][0]!r} from {eff[key][1]} is looser "
                                                    f"than the floor {floor[key][0]!r} from {floor[key][1]}"))
    extra = tuple(p.strip() for layer in (floor, eff) for p in (layer[KEYS[3]][0] or "").split(",") if p.strip())
    return {"mode": eff_mode, "mode_source": eff[KEYS[0]][1], "errors": errors,
            "hold": (_limit(eff[KEYS[1]][0]), eff[KEYS[1]][1]),
            "security": (_limit(eff[KEYS[2]][0]), eff[KEYS[2]][1]),
            "security_prefixes": tuple(GOVERNANCE_PATH_MARKERS) + extra}, None


def verdict(pr, head_sha: str, base_branch, cwd: Path, *, project_id=None, resolver=None,
            measurer=measure) -> Verdict:
    """GO, NO-GO or WARN for one PR, with the record fields of "De preflight" (no exception yet)."""
    policy, refused = read_policy(project_id, resolver)
    if refused is not None:
        return refused
    mode, errors = policy["mode"], policy["errors"]
    if mode == "off":
        return Verdict("GO", mode=mode, source=policy["mode_source"], waived="pr_size", errors=errors)
    on_fail = "NO-GO" if mode == "refuse" else "WARN"
    try:
        m = measurer(pr, head_sha, base_branch, cwd)
    except PrSizeUnmeasured as exc:
        return Verdict(on_fail, "pr_size_unmeasured", mode=mode, errors=errors, detail=str(exc))
    hits = tuple(p for p in m.paths if p.startswith(policy["security_prefixes"]))
    limit, source = policy["security"] if hits else policy["hold"]
    fields = dict(lines=m.lines, limit=limit, limit_kind="security" if hits else "hold",
                  security_paths_hit=hits[:3], mode=mode, source=source, errors=errors)
    if m.lines > limit:
        return Verdict(on_fail, "pr_size_above_limit", detail=f"{m.lines} lines above {limit}", **fields)
    return Verdict("GO", **fields)
