#!/usr/bin/env python3
"""Branch-protection config parsing, live-state fetch, and drift/weaken comparison
(Golf B, B1).

``PUT /repos/{owner}/{repo}/branches/{branch}/protection`` replaces the WHOLE
protection object: any field the caller omits falls back to its GitHub
default, silently. Anything this repo does not track in
``scripts/forge/branch_protection.yaml`` and re-apply explicitly can
therefore be reset by an unrelated apply. So the YAML is a complete
declaration, not a patch, and every write in ``apply_branch_protection.py``
sends the full built object rather than a partial one.

Two orthogonal comparisons live here, both built on the SAME field-level
diff engine (:func:`compare`) so they can never drift apart from each other:

  ``compare(a, b)``     lists every field where ``a`` and ``b`` disagree.
                         Direction-agnostic — used both for "is live drifted
                         from the YAML" (doctor, the merge-door's main-vs-live
                         check) and as the diff source :func:`is_weakening`
                         classifies.
  ``is_weakening(old, new)``
                         Classifies each ``compare(old, new)`` diff by field
                         polarity (a missing check, a protective boolean
                         flipped off, a review count lowered, auto-merge
                         turned on, ...) and reports which of those diffs
                         make ``new`` a WEAKER configuration than ``old``.
                         Used by ``apply_branch_protection.py`` (old=live,
                         new=YAML) and by ``pr_merge.py``'s preflight
                         (old=main's YAML, new=the PR's YAML).

Fail-closed throughout: an unreadable/unparseable live-state read raises
:class:`ProtectionDriftError`, and a YAML that violates the schema raises
:class:`ProtectionConfigError`. Neither degrades to "nothing required" — a
caller that cannot read the state must refuse, not pass.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import yaml

from ci_contexts import ANY_APP_ID, RequiredCheck

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Repo-relative path to the canonical config, used both as the default local
#: path and as the path queried through the GitHub contents API.
PROTECTION_YAML_RELATIVE_PATH = "scripts/forge/branch_protection.yaml"

VNX_GATE_PREFIX = "vnx-gate/"

_KNOWN_TOP_LEVEL_FIELDS = frozenset({
    "branch", "required_status_checks", "pending_checks",
    "required_pull_request_reviews", "enforce_admins", "required_signatures",
    "required_linear_history", "allow_force_pushes", "allow_deletions",
    "allow_fork_syncing", "block_creations", "lock_branch",
    "required_conversation_resolution", "restrictions", "repo", "rulesets",
})
_KNOWN_RSC_FIELDS = frozenset({"strict", "checks"})
_KNOWN_CHECK_FIELDS = frozenset({"context", "app_id"})
_KNOWN_PPR_FIELDS = frozenset({
    "required_approving_review_count", "dismiss_stale_reviews",
    "require_code_owner_reviews", "require_last_push_approval",
})
#: Present in the PUT body and in a GET only once configured. Optional in the
#: YAML (an absent key means "no bypass granted"), so a config written before
#: this field was modelled keeps parsing — but it is COMPARED either way, so
#: a grant appearing on live is drift against a YAML that stays silent.
_OPTIONAL_PPR_FIELDS = frozenset({"bypass_pull_request_allowances"})
_KNOWN_REPO_FIELDS = frozenset({"allow_auto_merge"})

#: The three actor kinds a bypass allowance can name. Flattened to sorted
#: ``"<kind>:<name>"`` strings so the live shape (objects carrying
#: ``login``/``slug``) and the YAML shape (plain names) compare as equals.
_BYPASS_KINDS = ("users", "teams", "apps")

_BOOL_TOP_LEVEL_FIELDS = (
    "enforce_admins", "required_signatures", "required_linear_history",
    "allow_force_pushes", "allow_deletions", "allow_fork_syncing",
    "block_creations", "lock_branch", "required_conversation_resolution",
)


class ProtectionConfigError(ValueError):
    """``branch_protection.yaml`` (or a ref's copy of it) violates the schema.

    Raised, never degraded to a partial read: an under-specified or
    malformed protection config must never be treated as "nothing required".
    """


class ProtectionDriftError(RuntimeError):
    """Live branch-protection state could not be read. Raised, never
    swallowed into an empty/default result — an unreadable live state must
    block, not pass as "no drift found"."""


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectionConfig:
    branch: str
    strict: bool
    checks: Tuple[RequiredCheck, ...]
    pending_checks: Tuple[str, ...]
    #: False when the YAML declares ``required_pull_request_reviews: null``,
    #: i.e. "no pull-request requirement at all". Distinct from a present
    #: block asking for zero approvals: GitHub OMITS the whole subobject from
    #: a GET when the requirement is off (the same way it omits
    #: ``restrictions`` when unset), so without this flag the strongest
    #: setting in the object and its complete absence normalize identically.
    required_pull_request_reviews_present: bool
    required_approving_review_count: int
    dismiss_stale_reviews: bool
    require_code_owner_reviews: bool
    require_last_push_approval: bool
    bypass_pull_request_allowances: Tuple[str, ...]
    enforce_admins: bool
    required_signatures: bool
    required_linear_history: bool
    allow_force_pushes: bool
    allow_deletions: bool
    allow_fork_syncing: bool
    block_creations: bool
    lock_branch: bool
    required_conversation_resolution: bool
    allow_auto_merge: bool
    rulesets: Tuple[str, ...]


def _require_object_fields(
    obj: Any, known: frozenset, where: str, *, optional: frozenset = frozenset(),
) -> None:
    if not isinstance(obj, dict):
        raise ProtectionConfigError(f"{where} moet een object zijn, kreeg {type(obj).__name__}")
    missing = known - set(obj)
    if missing:
        raise ProtectionConfigError(f"{where} mist verplichte velden: {sorted(missing)}")
    unknown = set(obj) - known - optional
    if unknown:
        raise ProtectionConfigError(f"{where} heeft onbekende velden: {sorted(unknown)}")


def _require_bool(obj: Dict[str, Any], field: str, where: str) -> bool:
    value = obj[field]
    if not isinstance(value, bool):
        raise ProtectionConfigError(f"{where}.{field} moet een boolean zijn, kreeg {type(value).__name__}")
    return value


def normalize_bypass_allowances(raw: Any) -> List[str]:
    """A ``bypass_pull_request_allowances`` object flattened to sorted
    ``"<kind>:<name>"`` strings.

    Accepts both shapes it is fed: the live GET's objects (``{"users":
    [{"login": "x"}]}``) and the YAML's plain names (``{"users": ["x"]}``).
    Anything unrecognizable normalizes away rather than raising — the strict
    side is :func:`_parse_bypass_allowances`, which validates the YAML at
    parse time; a live response is not this repo's to validate, only to
    compare.
    """
    if not isinstance(raw, dict):
        return []
    names: List[str] = []
    for kind in _BYPASS_KINDS:
        for entry in raw.get(kind) or []:
            name: Any = entry
            if isinstance(entry, dict):
                name = entry.get("login") or entry.get("slug") or entry.get("name")
            if isinstance(name, str) and name:
                names.append(f"{kind}:{name}")
    return sorted(set(names))


def bypass_allowances_to_api_object(entries: Tuple[str, ...]) -> Dict[str, List[str]]:
    """The inverse of :func:`normalize_bypass_allowances` for the PUT body."""
    obj: Dict[str, List[str]] = {kind: [] for kind in _BYPASS_KINDS}
    for entry in entries:
        kind, _, name = entry.partition(":")
        if kind in obj and name:
            obj[kind].append(name)
    return {kind: sorted(values) for kind, values in obj.items()}


def _parse_bypass_allowances(raw: Any, where: str) -> Tuple[str, ...]:
    """Strict YAML-side parse. Absent/null means "no bypass granted"."""
    field = f"{where}.bypass_pull_request_allowances"
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ProtectionConfigError(
            f"{field} moet een object zijn met users/teams/apps, kreeg {type(raw).__name__}"
        )
    unknown = set(raw) - set(_BYPASS_KINDS)
    if unknown:
        raise ProtectionConfigError(f"{field} heeft onbekende velden: {sorted(unknown)}")
    names: List[str] = []
    for kind in _BYPASS_KINDS:
        values = raw.get(kind)
        if values is None:
            continue
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ProtectionConfigError(f"{field}.{kind} moet een lijst van niet-lege strings zijn")
        names.extend(f"{kind}:{v}" for v in values)
    return tuple(sorted(set(names)))


def parse_protection_config(raw_text: str) -> ProtectionConfig:
    """Parse + validate ``branch_protection.yaml`` text. Raises
    :class:`ProtectionConfigError` on any schema violation — see module
    docstring: an unreadable config must never be treated as "nothing
    required"."""
    try:
        doc = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ProtectionConfigError(f"branch_protection.yaml is geen geldige YAML: {exc}") from exc
    _require_object_fields(doc, _KNOWN_TOP_LEVEL_FIELDS, "branch_protection.yaml")

    if not isinstance(doc["branch"], str) or not doc["branch"]:
        raise ProtectionConfigError("branch moet een niet-lege string zijn")

    rsc = doc["required_status_checks"]
    _require_object_fields(rsc, _KNOWN_RSC_FIELDS, "required_status_checks")
    strict = _require_bool(rsc, "strict", "required_status_checks")

    raw_checks = rsc["checks"]
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ProtectionConfigError("required_status_checks.checks moet een niet-lege lijst zijn")

    checks: List[RequiredCheck] = []
    seen_names: set = set()
    for index, entry in enumerate(raw_checks):
        where = f"required_status_checks.checks[{index}]"
        _require_object_fields(entry, _KNOWN_CHECK_FIELDS, where)
        context = entry["context"]
        if not isinstance(context, str) or not context:
            raise ProtectionConfigError(f"{where}.context moet een niet-lege string zijn")
        if context in seen_names:
            raise ProtectionConfigError(f"required_status_checks.checks bevat '{context}' dubbel")
        seen_names.add(context)
        app_id = entry["app_id"]
        if app_id is not None and not isinstance(app_id, int):
            raise ProtectionConfigError(f"{where}.app_id moet een integer of null zijn")
        if context.startswith(VNX_GATE_PREFIX) and (app_id is None or app_id == ANY_APP_ID):
            raise ProtectionConfigError(
                f"{where} ('{context}') is een vnx-gate/*-check zonder gebonden app_id "
                "(null of de 'elke app'-sentinel -1): dat zou elke app deze status laten zetten"
            )
        checks.append(RequiredCheck(context, None if app_id == ANY_APP_ID else app_id))

    pending_raw = doc["pending_checks"]
    if not isinstance(pending_raw, list) or not all(isinstance(p, str) for p in pending_raw):
        raise ProtectionConfigError("pending_checks moet een lijst van strings zijn")

    # An explicit `null` here declares "no pull-request requirement at all",
    # which is what GitHub reports by omitting the subobject entirely. The
    # values below are then unused (to_normalized_dict emits None for the
    # whole block, build_put_payload sends null) — they are not a default
    # standing in for a missing declaration.
    ppr = doc["required_pull_request_reviews"]
    ppr_present = ppr is not None
    review_count = 0
    dismiss_stale = require_code_owner = require_last_push = False
    bypass_allowances: Tuple[str, ...] = ()
    if ppr_present:
        _require_object_fields(
            ppr, _KNOWN_PPR_FIELDS, "required_pull_request_reviews", optional=_OPTIONAL_PPR_FIELDS,
        )
        review_count = ppr["required_approving_review_count"]
        if not isinstance(review_count, int) or isinstance(review_count, bool) or review_count < 0:
            raise ProtectionConfigError(
                "required_pull_request_reviews.required_approving_review_count moet een niet-negatieve integer zijn"
            )
        dismiss_stale = _require_bool(ppr, "dismiss_stale_reviews", "required_pull_request_reviews")
        require_code_owner = _require_bool(ppr, "require_code_owner_reviews", "required_pull_request_reviews")
        require_last_push = _require_bool(ppr, "require_last_push_approval", "required_pull_request_reviews")
        bypass_allowances = _parse_bypass_allowances(
            ppr.get("bypass_pull_request_allowances"), "required_pull_request_reviews",
        )

    bool_values = {f: _require_bool(doc, f, "branch_protection.yaml") for f in _BOOL_TOP_LEVEL_FIELDS}

    if doc["restrictions"] is not None:
        raise ProtectionConfigError("restrictions moet null zijn (push-restricties worden hier niet gemodelleerd)")

    repo_obj = doc["repo"]
    _require_object_fields(repo_obj, _KNOWN_REPO_FIELDS, "repo")
    allow_auto_merge = _require_bool(repo_obj, "allow_auto_merge", "repo")

    rulesets_raw = doc["rulesets"]
    if not isinstance(rulesets_raw, list) or not all(isinstance(r, str) for r in rulesets_raw):
        raise ProtectionConfigError("rulesets moet een lijst van strings zijn")

    return ProtectionConfig(
        branch=doc["branch"],
        strict=strict,
        checks=tuple(checks),
        pending_checks=tuple(pending_raw),
        required_pull_request_reviews_present=ppr_present,
        required_approving_review_count=review_count,
        dismiss_stale_reviews=dismiss_stale,
        require_code_owner_reviews=require_code_owner,
        require_last_push_approval=require_last_push,
        bypass_pull_request_allowances=bypass_allowances,
        enforce_admins=bool_values["enforce_admins"],
        required_signatures=bool_values["required_signatures"],
        required_linear_history=bool_values["required_linear_history"],
        allow_force_pushes=bool_values["allow_force_pushes"],
        allow_deletions=bool_values["allow_deletions"],
        allow_fork_syncing=bool_values["allow_fork_syncing"],
        block_creations=bool_values["block_creations"],
        lock_branch=bool_values["lock_branch"],
        required_conversation_resolution=bool_values["required_conversation_resolution"],
        allow_auto_merge=allow_auto_merge,
        rulesets=tuple(rulesets_raw),
    )


def load_protection_config(path: Path) -> ProtectionConfig:
    return parse_protection_config(Path(path).read_text(encoding="utf-8"))


def to_normalized_dict(config: ProtectionConfig) -> Dict[str, Any]:
    """The same JSON-comparable shape :func:`fetch_live_protection` returns,
    so :func:`compare` can diff a parsed YAML against live state (or another
    parsed YAML) without caring which side is which."""
    return {
        "required_status_checks": {
            "strict": config.strict,
            "checks": [{"context": c.context, "app_id": c.app_id} for c in config.checks],
        },
        # None, not a zero-filled object, when the YAML declares no
        # pull-request requirement — see ProtectionConfig's field comment.
        "required_pull_request_reviews": ({
            "required_approving_review_count": config.required_approving_review_count,
            "dismiss_stale_reviews": config.dismiss_stale_reviews,
            "require_code_owner_reviews": config.require_code_owner_reviews,
            "require_last_push_approval": config.require_last_push_approval,
            "bypass_pull_request_allowances": list(config.bypass_pull_request_allowances),
        } if config.required_pull_request_reviews_present else None),
        "enforce_admins": config.enforce_admins,
        "required_signatures": config.required_signatures,
        "required_linear_history": config.required_linear_history,
        "allow_force_pushes": config.allow_force_pushes,
        "allow_deletions": config.allow_deletions,
        "allow_fork_syncing": config.allow_fork_syncing,
        "block_creations": config.block_creations,
        "lock_branch": config.lock_branch,
        "required_conversation_resolution": config.required_conversation_resolution,
        "restrictions": None,
        "repo": {"allow_auto_merge": config.allow_auto_merge},
        "rulesets": sorted(config.rulesets),
    }


# ---------------------------------------------------------------------------
# Live-state fetch (gh api)
# ---------------------------------------------------------------------------


def _gh_json(argv: List[str], project_root: Path, timeout: int) -> Any:
    try:
        proc = subprocess.run(
            argv, cwd=str(project_root), capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ProtectionDriftError(f"gh CLI niet beschikbaar: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProtectionDriftError(f"{' '.join(argv)} liep vast na {timeout}s") from exc
    if proc.returncode != 0:
        raise ProtectionDriftError(
            f"{' '.join(argv)} faalde (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProtectionDriftError(f"{' '.join(argv)} gaf onparseerbare JSON: {exc}") from exc


def fetch_live_protection(
    project_root: Path, *, branch: str = "main", gh_bin: str = "gh", timeout: int = 20,
) -> Dict[str, Any]:
    """The live branch-protection + repo + rulesets state, normalized to the
    same shape :func:`to_normalized_dict` produces from the YAML.

    Three ``gh api`` calls: the branch protection object itself (which also
    carries ``required_signatures.enabled`` inline — no separate read is
    needed for that, only the WRITE side uses its own endpoint, see
    ``apply_branch_protection.py``), the repo object (for
    ``allow_auto_merge``), and the rulesets list. Raises
    :class:`ProtectionDriftError` on any unreadable/unparseable response —
    never returns a partial or default-filled result.
    """
    protection = _gh_json(
        [gh_bin, "api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection"], project_root, timeout,
    )
    if not isinstance(protection, dict):
        raise ProtectionDriftError(f"protection-antwoord is {type(protection).__name__}, verwacht een object")

    repo_obj = _gh_json([gh_bin, "api", "repos/{owner}/{repo}"], project_root, timeout)
    if not isinstance(repo_obj, dict):
        raise ProtectionDriftError(f"repo-antwoord is {type(repo_obj).__name__}, verwacht een object")

    rulesets = _gh_json([gh_bin, "api", "repos/{owner}/{repo}/rulesets"], project_root, timeout)
    if not isinstance(rulesets, list):
        raise ProtectionDriftError(f"rulesets-antwoord is {type(rulesets).__name__}, verwacht een lijst")

    rsc = protection.get("required_status_checks") or {}
    checks: List[Dict[str, Any]] = []
    for entry in rsc.get("checks") or []:
        if not isinstance(entry, dict):
            continue
        context = entry.get("context")
        if not isinstance(context, str) or not context:
            continue
        app_id = entry.get("app_id")
        checks.append({
            "context": context,
            "app_id": app_id if isinstance(app_id, int) and app_id != ANY_APP_ID else None,
        })

    # GitHub omits this subobject entirely when "Require a pull request
    # before merging" is off — the same omission it makes for `restrictions`.
    # Normalizing that absence into zeroes would make turning the PR
    # requirement off produce no drift and no weakening at all.
    raw_ppr = protection.get("required_pull_request_reviews")
    ppr_norm: Optional[Dict[str, Any]] = None
    if isinstance(raw_ppr, dict):
        ppr_norm = {
            "required_approving_review_count": int(raw_ppr.get("required_approving_review_count", 0)),
            "dismiss_stale_reviews": bool(raw_ppr.get("dismiss_stale_reviews", False)),
            "require_code_owner_reviews": bool(raw_ppr.get("require_code_owner_reviews", False)),
            "require_last_push_approval": bool(raw_ppr.get("require_last_push_approval", False)),
            "bypass_pull_request_allowances": normalize_bypass_allowances(
                raw_ppr.get("bypass_pull_request_allowances")
            ),
        }

    def _enabled(field: str) -> bool:
        block = protection.get(field)
        return bool(block.get("enabled", False)) if isinstance(block, dict) else False

    return {
        "required_status_checks": {"strict": bool(rsc.get("strict", False)), "checks": checks},
        "required_pull_request_reviews": ppr_norm,
        "enforce_admins": _enabled("enforce_admins"),
        "required_signatures": _enabled("required_signatures"),
        "required_linear_history": _enabled("required_linear_history"),
        "allow_force_pushes": _enabled("allow_force_pushes"),
        "allow_deletions": _enabled("allow_deletions"),
        "allow_fork_syncing": _enabled("allow_fork_syncing"),
        "block_creations": _enabled("block_creations"),
        "lock_branch": _enabled("lock_branch"),
        "required_conversation_resolution": _enabled("required_conversation_resolution"),
        "restrictions": protection.get("restrictions"),
        "repo": {"allow_auto_merge": bool(repo_obj.get("allow_auto_merge", False))},
        "rulesets": sorted(
            r.get("name") for r in rulesets if isinstance(r, dict) and isinstance(r.get("name"), str)
        ),
    }


# ---------------------------------------------------------------------------
# Fetching a copy of the YAML at an arbitrary ref (contents API)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class YamlFetchResult:
    """The result of fetching ``branch_protection.yaml`` at one git ref.

    ``not_found`` is a NAMED case, distinct from ``error``: a 404 on exactly
    the queried path AT A REF THAT EXISTS means "this ref has no such file"
    (the bootstrap case when queried against main, or "this PR deletes the
    file" when queried against a PR head) — never conflated with an
    unreadable/unparseable response, which is a fail-closed ``error``
    instead. The "at a ref that exists" half is not free: it costs the
    second call in :func:`_confirm_ref_exists`, because ``gh``'s 404 text is
    identical for an unknown ref, an unresolvable repo, and a missing path.
    """

    text: Optional[str]
    not_found: bool
    error: Optional[str]


_HTTP_404_MARKERS = ("HTTP 404", "Not Found (HTTP 404)")


def _confirm_ref_exists(
    project_root: Path, ref: str, *, gh_bin: str, timeout: int,
) -> Optional[str]:
    """``None`` when ``ref`` demonstrably exists in this repo, otherwise the
    reason it could not be confirmed.

    A 404 from the contents API says nothing about WHICH part of the request
    was not found. ``gh`` reports "Not Found (HTTP 404)" for an unresolvable
    repo, "No commit found for the ref ... (HTTP 404)" for an unknown ref,
    and the very same text for the one case the caller wants to act on: this
    ref exists, that path does not. The merge door turns that case into a GO
    that skips every remaining check, so an unconfirmed ref must not reach
    it — this second call is what separates the three.
    """
    argv = [
        gh_bin, "api", f"repos/{{owner}}/{{repo}}/commits/{quote(ref, safe='/')}", "--jq", ".sha",
    ]
    try:
        proc = subprocess.run(
            argv, cwd=str(project_root), capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        return f"gh CLI niet beschikbaar: {exc}"
    except subprocess.TimeoutExpired:
        return f"gh api commits/{ref} liep vast na {timeout}s"
    if proc.returncode != 0:
        return f"gh api commits/{ref} faalde (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}"
    if not (proc.stdout or "").strip().strip('"'):
        return f"gh api commits/{ref} gaf geen sha terug"
    return None


def fetch_yaml_from_ref(
    project_root: Path,
    ref: str,
    path: str = PROTECTION_YAML_RELATIVE_PATH,
    *,
    gh_bin: str = "gh",
    timeout: int = 20,
) -> YamlFetchResult:
    """Fetch ``path`` at ``ref`` via the GitHub contents API — never a local
    ref (see ``merge_preflight_adr_check.py``'s docstring for why the door
    never trusts a local ``origin/<ref>``: it is only as fresh as the last
    incidental fetch, and the door itself never fetches).
    """
    encoded_ref = quote(ref, safe="")
    try:
        proc = subprocess.run(
            [gh_bin, "api", f"repos/{{owner}}/{{repo}}/contents/{path}?ref={encoded_ref}"],
            cwd=str(project_root), capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        return YamlFetchResult(text=None, not_found=False, error=f"gh CLI niet beschikbaar: {exc}")
    except subprocess.TimeoutExpired:
        return YamlFetchResult(
            text=None, not_found=False,
            error=f"gh api contents/{path}?ref={ref} liep vast na {timeout}s",
        )

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if any(marker in stderr for marker in _HTTP_404_MARKERS):
            unconfirmed = _confirm_ref_exists(project_root, ref, gh_bin=gh_bin, timeout=timeout)
            if unconfirmed is None:
                return YamlFetchResult(text=None, not_found=True, error=None)
            return YamlFetchResult(
                text=None, not_found=False,
                error=(
                    f"404 op contents/{path}?ref={ref}, maar ref '{ref}' is zelf niet bevestigd "
                    f"({unconfirmed}): niet te onderscheiden van een onbekende ref of een "
                    "onbereikbare repo"
                ),
            )
        return YamlFetchResult(
            text=None, not_found=False,
            error=f"gh api contents/{path}?ref={ref} faalde (rc={proc.returncode}): {stderr[:200]}",
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return YamlFetchResult(text=None, not_found=False, error=f"onparseerbare JSON van contents-API: {exc}")

    if not isinstance(payload, dict):
        return YamlFetchResult(
            text=None, not_found=False,
            error=f"contents-antwoord is {type(payload).__name__}, verwacht een object",
        )
    content_b64 = payload.get("content")
    if not isinstance(content_b64, str):
        return YamlFetchResult(text=None, not_found=False, error="contents-antwoord mist 'content'-veld")
    try:
        text = base64.b64decode(content_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        return YamlFetchResult(text=None, not_found=False, error=f"content niet te decoderen: {exc}")
    return YamlFetchResult(text=text, not_found=False, error=None)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

_ABSENT = object()

#: Fields where True is the MORE-protective value: a diff going True -> False
#: is a weakening. "strict uit" (required_status_checks.strict) is the one
#: explicitly named in the dispatch alongside the general "a boolean going
#: true to false" rule.
_TRUE_IS_STRONGER = frozenset({
    "required_status_checks.strict",
    "enforce_admins",
    "required_linear_history",
    "required_conversation_resolution",
    "required_signatures",
    "block_creations",
    "lock_branch",
    "required_pull_request_reviews.dismiss_stale_reviews",
    "required_pull_request_reviews.require_code_owner_reviews",
    "required_pull_request_reviews.require_last_push_approval",
})

#: Fields where False is the MORE-protective value: a diff going False -> True
#: is a weakening ("auto-merge aan" is the one explicitly named — it runs the
#: opposite direction from the general true-to-false rule above).
_FALSE_IS_STRONGER = frozenset({
    "allow_force_pushes",
    "allow_deletions",
    "allow_fork_syncing",
    "repo.allow_auto_merge",
})


def compare(a: Dict[str, Any], b: Dict[str, Any]) -> List[Dict[str, Any]]:
    """List every field where normalized states ``a`` and ``b`` disagree.

    Direction-agnostic: this only reports WHAT differs, not whether ``b`` is
    weaker or stronger than ``a`` — see :func:`is_weakening` for that
    judgment, built on top of this same diff list (one function, two
    callers: a direct drift check, and ``is_weakening``'s classifier).

    Each diff is ``{"field": <dotted path>, "a": <value in a>, "b": <value in
    b>}``; a ``required_status_checks.checks[<name>]`` diff additionally
    carries ``a_present``/``b_present`` so a name-only add/remove (no app_id
    on either side to compare) is still fully described.
    """
    diffs: List[Dict[str, Any]] = []

    def scalar(field: str, av: Any, bv: Any) -> None:
        if av != bv:
            diffs.append({"field": field, "a": av, "b": bv})

    a_rsc = a.get("required_status_checks") or {}
    b_rsc = b.get("required_status_checks") or {}
    scalar("required_status_checks.strict", a_rsc.get("strict"), b_rsc.get("strict"))

    a_checks = {c["context"]: c.get("app_id") for c in (a_rsc.get("checks") or [])}
    b_checks = {c["context"]: c.get("app_id") for c in (b_rsc.get("checks") or [])}
    for name in sorted(set(a_checks) | set(b_checks)):
        av = a_checks.get(name, _ABSENT)
        bv = b_checks.get(name, _ABSENT)
        if av != bv:
            diffs.append({
                "field": f"required_status_checks.checks[{name}]",
                "a": None if av is _ABSENT else av,
                "b": None if bv is _ABSENT else bv,
                "a_present": av is not _ABSENT,
                "b_present": bv is not _ABSENT,
            })

    # Presence first, fields second: an absent block and a present block
    # asking for zero approvals are DIFFERENT states (see
    # ProtectionConfig.required_pull_request_reviews_present). Comparing
    # field-by-field across that boundary would report them as equal.
    a_ppr = a.get("required_pull_request_reviews")
    b_ppr = b.get("required_pull_request_reviews")
    a_ppr_present = isinstance(a_ppr, dict)
    b_ppr_present = isinstance(b_ppr, dict)
    if a_ppr_present != b_ppr_present:
        diffs.append({
            "field": "required_pull_request_reviews",
            "a": a_ppr, "b": b_ppr,
            "a_present": a_ppr_present, "b_present": b_ppr_present,
        })
    elif a_ppr_present:
        for f in ("required_approving_review_count", "dismiss_stale_reviews",
                  "require_code_owner_reviews", "require_last_push_approval"):
            scalar(f"required_pull_request_reviews.{f}", a_ppr.get(f), b_ppr.get(f))
        scalar(
            "required_pull_request_reviews.bypass_pull_request_allowances",
            sorted(a_ppr.get("bypass_pull_request_allowances") or []),
            sorted(b_ppr.get("bypass_pull_request_allowances") or []),
        )

    for f in _BOOL_TOP_LEVEL_FIELDS:
        scalar(f, a.get(f), b.get(f))
    scalar("restrictions", a.get("restrictions"), b.get("restrictions"))

    scalar(
        "repo.allow_auto_merge",
        (a.get("repo") or {}).get("allow_auto_merge"),
        (b.get("repo") or {}).get("allow_auto_merge"),
    )

    a_rulesets = sorted(a.get("rulesets") or [])
    b_rulesets = sorted(b.get("rulesets") or [])
    if a_rulesets != b_rulesets:
        diffs.append({"field": "rulesets", "a": a_rulesets, "b": b_rulesets})

    return diffs


def is_weakening(old: Dict[str, Any], new: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Is ``new`` a WEAKER branch-protection configuration than ``old``?

    Built on :func:`compare`'s diff list (old=a, new=b), classified per
    field: a required check present in ``old`` but missing from ``new``, a
    protective boolean flipped off, the required review count lowered, or
    ``allow_auto_merge`` turned on. Fields outside this vocabulary (e.g.
    ``rulesets``, which this repo only ever checks and never writes) never
    trigger a weakening verdict — see the module docstring's list of the
    exact cases this covers.
    """
    weak_fields: List[str] = []
    for diff in compare(old, new):
        field = diff["field"]
        if field.startswith("required_status_checks.checks["):
            if diff["a_present"] and not diff["b_present"]:
                weak_fields.append(field)
            continue
        if field == "required_pull_request_reviews":
            # The whole pull-request requirement dropped: the single most
            # consequential weakening the object can express.
            if diff["a_present"] and not diff["b_present"]:
                weak_fields.append(field)
            continue
        if field == "required_pull_request_reviews.bypass_pull_request_allowances":
            # Any actor granted a bypass that did not have one merges around
            # the requirement, however strong the rest of the block reads.
            if set(diff["b"] or []) - set(diff["a"] or []):
                weak_fields.append(field)
            continue
        if field == "required_pull_request_reviews.required_approving_review_count":
            if diff["b"] < diff["a"]:
                weak_fields.append(field)
            continue
        if field in _TRUE_IS_STRONGER and diff["a"] is True and diff["b"] is False:
            weak_fields.append(field)
            continue
        if field in _FALSE_IS_STRONGER and diff["a"] is False and diff["b"] is True:
            weak_fields.append(field)
            continue
    return (len(weak_fields) > 0, weak_fields)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_yaml_path() -> Path:
    return Path(__file__).resolve().parent.parent / "forge" / "branch_protection.yaml"


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="forge_protection_drift",
        description="Compare live branch protection to scripts/forge/branch_protection.yaml",
    )
    parser.add_argument("--yaml-path", default=str(_default_yaml_path()))
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--gh-bin", default="gh")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = load_protection_config(Path(args.yaml_path))
    except (ProtectionConfigError, OSError) as exc:
        print(f"FOUT: {exc}", file=sys.stderr)
        return 1

    try:
        live = fetch_live_protection(Path(args.project_root), branch=args.branch, gh_bin=args.gh_bin)
    except ProtectionDriftError as exc:
        print(f"FOUT: {exc}", file=sys.stderr)
        return 1

    diffs = compare(to_normalized_dict(config), live)
    if args.json:
        print(json.dumps(diffs, indent=2, default=str))
    elif not diffs:
        print(f"geen verschillen: branch-protection op {args.branch} komt overeen met {args.yaml_path}")
    else:
        for d in diffs:
            print(f"DRIFT {d['field']}: yaml={d['a']!r} live={d['b']!r}")
    return 0 if not diffs else 1


if __name__ == "__main__":
    sys.exit(main())
