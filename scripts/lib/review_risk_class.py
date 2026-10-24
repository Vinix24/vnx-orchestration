"""The risk class of a PR review, derived from the PR diff (R3a, track review-stopregel-en-lichte-stoelen).

Operator decision 2026-10-17: codex is the only required seat per round; the
second seat runs on the last head before the merge, and only when the risk
class is high (schema, security, gate or merge logic, data loss). That rule
needs one class per PR head that no caller can talk down. This module derives
it, ``gate_request_handler.request_reviews`` records it on every seat's request
record, and :func:`head_risk_class` is the reading of the head a merge is about.

**Derived from the diff (B1).** The files of the PR diff against the merge base
with the PR's own base branch (:func:`read_pr_diff`: ``git merge-base``, then
``git diff --name-only --no-renames -z``, so a rename names both its old and
its new path). The base is the PR's ``baseRefName`` (:func:`base_ref_for`),
never a default: against ``main`` a PR for ``release`` that carries merge
logic already on ``main`` reads as a doc change. Every
path is classified by ``smart_router.plan_scope_path_category``, the table that
makes a track architecture work; every category it names (outside-repo,
schema, security, gate-merge, store-resolution) is ``high``. There is no second
path list: two lists drift. A dispatch spec with ``irreversible: true`` is
``high`` too, because data loss is not a path property (as in
``plan_scope.dispatch_rule``). Anything else is ``medium``: "no high-risk
path". The diff never derives ``low`` or ``critical``.

**The caller only raises it (B1, B7).** A caller's class (``--risk-class``) is
read only when it is a known value (:data:`RISK_CLASSES`) and counts only when
it is above the derived class. No value lowers ``high``.

**Fail-closed (B2).** A diff that cannot be read, a diff that names no file, a
path that cannot be normalised, an unknown caller value, a dispatch spec that
cannot be read and a PR base branch that is not known are each ``high``, and
the reason says which.

**The class belongs to the head (B3).** A request records ``review_risk``
(:meth:`RiskClass.to_record`): the class, the head, the base it was read
against, the merge base, the rule version
(``smart_router.plan_scope_rule_version``, a hash of the path tables) and the
files that decided it. :func:`head_risk_class` reads the records on the head it
is asked about and keeps what a diff cannot show (a caller's raise, an
irreversible spec). It takes the path part from a record only when that record
has the current base, merge base and rule version; otherwise it computes the
class itself with :func:`derive_risk_class`. A head that adds a high-risk path
is high, whatever an earlier head was.

**The door reads it (R3b).** :func:`merge_head_risk_class` is the class of the
head a merge is about: it fetches the PR head and the PR's base branch, reads
the diff and checks it against the records of the head. ``review_seat_evaluation``
asks it for every evaluation of a resolved stack, with the base each caller
read from the PR, so the merge door, ``vnx-gate/review`` and ``vnx pr-ready``
read one class, and ``review_seat_evaluation.seats_for_risk`` turns it into the
required seats. A base that is not known, a failed fetch and a checkout that
cannot be established are ``high``.

BILLING SAFETY: no Anthropic SDK. The subprocesses are ``git`` reads and one
read-only ``git fetch`` of the PR head and the base branch.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOW = "low"
MEDIUM = "medium"
HIGH = "high"
CRITICAL = "critical"
RISK_CLASSES = (LOW, MEDIUM, HIGH, CRITICAL)
_RANK = {name: rank for rank, name in enumerate(RISK_CLASSES)}

#: The key a request record carries the class under, next to ``review_seats``.
RECORD_KEY = "review_risk"

SOURCE_DERIVED = "derived"
SOURCE_RECORD = "record"
SOURCE_RECOMPUTED = "recomputed"

CATEGORY_UNNORMALISABLE = "unnormalisable"

_GIT_TIMEOUT_SECONDS = 30
_FETCH_TIMEOUT_SECONDS = 90
_REMOTE = "origin"
_EXPLAIN_PATHS = 3
_SHA_RE = re.compile(r"[0-9a-f]{7,64}")
#: A plain branch name: it goes into a fetch refspec and a ``git merge-base``
#: argument, so anything that could be read as an option or a second refspec
#: (``-x``, ``a:b``, ``+x``, ``a b``) is refused rather than quoted.
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def rule_version() -> str:
    """The version of the path rules the class is derived under (a hash of the category tables)."""
    import smart_router

    return smart_router.plan_scope_rule_version()


def known_class(value: Any) -> Optional[str]:
    """``value`` as one of :data:`RISK_CLASSES`, or ``None`` when it is no known class."""
    text = value.strip().lower() if isinstance(value, str) else None
    return text if text in _RANK else None


def _rank(value: Any) -> int:
    """The rank of a class value; a value that is no known class ranks as ``high`` (fail-closed)."""
    known = known_class(value)
    return _RANK[known] if known is not None else _RANK[HIGH]


# ---------------------------------------------------------------------------
# Reading the diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffReading:
    """The files of a PR diff against its merge base; ``files is None`` when they could not be read."""

    files: Optional[Tuple[str, ...]]
    merge_base: str = ""
    base_ref: str = ""
    error: str = ""


def _git(args: Sequence[str], cwd: Optional[Path]) -> Tuple[Optional[str], str]:
    """``(stdout, "")`` for a git read that exits 0, else ``(None, why)``."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
            encoding="utf-8", errors="surrogateescape", timeout=_GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git {args[0]}: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        last = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return None, f"git {args[0]} exited {proc.returncode}: {last[0]}"
    return proc.stdout, ""


def base_ref_for(base_branch: Any) -> Tuple[Optional[str], str]:
    """``("origin/<base>", "")`` for the PR's base branch (its ``baseRefName``), else ``(None, why)``.

    There is no default: a base that is missing, empty, no string or no plain
    branch name gives ``None``, and the class of a diff without a base is
    ``high``. Never ``main`` in its place: a PR for another branch read against
    ``main`` loses every commit ``main`` already has.
    """
    if not isinstance(base_branch, str) or not base_branch:
        return None, f"the PR's base branch is not known ({base_branch!r} given); no base is assumed"
    name = base_branch
    plain = (
        _BRANCH_RE.fullmatch(name) is not None and ".." not in name and "//" not in name
        and "/." not in name and not name.endswith(("/", ".", ".lock"))
    )
    if not plain:
        return None, f"the PR's base branch {name!r} is no plain branch name"
    return f"{_REMOTE}/{name}", ""


def read_pr_diff(head_sha: str, *, base_ref: str, cwd: Optional[Path] = None) -> DiffReading:
    """The files of the diff of ``head_sha`` against its merge base with ``base_ref``, read with local git.

    ``base_ref`` is the PR's base as a ref in the checkout (:func:`base_ref_for`),
    with no default. Nothing is fetched: a head or a base that is not in the
    checkout cannot be read, and the class of an unreadable diff is ``high``.
    """
    if not isinstance(base_ref, str) or not base_ref or base_ref.startswith("-"):
        return DiffReading(None, base_ref="", error=f"no base ref to read the diff against ({base_ref!r} given)")
    head = (head_sha or "").strip()
    if not head:
        return DiffReading(None, base_ref=base_ref, error="the PR head could not be resolved")
    if not _SHA_RE.fullmatch(head):
        return DiffReading(None, base_ref=base_ref, error=f"the PR head {head!r} is no commit sha")
    merge_base, why = _git(["merge-base", base_ref, head], cwd)
    if merge_base is None or not merge_base.strip():
        return DiffReading(None, base_ref=base_ref, error=(
            f"no merge base of {base_ref} and {head[:12]} in the local checkout ({why or 'empty output'})"
        ))
    merge_base = merge_base.strip()
    out, why = _git(["diff", "--name-only", "--no-renames", "-z", merge_base, head], cwd)
    if out is None:
        return DiffReading(None, merge_base, base_ref, f"the diff {merge_base[:12]}..{head[:12]} cannot be read ({why})")
    return DiffReading(tuple(name for name in out.split("\0") if name), merge_base, base_ref)


# ---------------------------------------------------------------------------
# The dispatch spec: irreversible
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecFlag:
    """What the dispatch spec says about data loss: ``True``, ``False``, or ``None`` when it cannot be read."""

    irreversible: Optional[bool] = False
    detail: str = ""


def spec_flag(dispatch_id: str, *, data_dir: Path) -> SpecFlag:
    """``irreversible`` from the dispatch specs of ``dispatch_id`` in ``data_dir`` (the review store's own).

    A request that names no dispatch, or a dispatch without a spec in this
    store, declares nothing. A spec that cannot be read, an id that is no plain
    name and a value that is no boolean are ``None``: whether the work can lose
    data is then unknown, and the class is ``high``.
    """
    from review_vendor import dispatch_spec_paths

    did = (dispatch_id or "").strip()
    if not did:
        return SpecFlag(False, "the review request names no dispatch")
    specs, why_none = dispatch_spec_paths(did, data_dir=Path(data_dir))
    if specs is None:
        return SpecFlag(None, why_none)
    for path in specs:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return SpecFlag(None, f"{path} cannot be read: {exc}")
        if not isinstance(data, dict):
            return SpecFlag(None, f"{path} is not a JSON object")
        value = data.get("irreversible", False)
        if value is True:
            return SpecFlag(True, str(path))
        if value is not False:
            return SpecFlag(None, f"{path} carries irreversible={value!r}, no boolean")
    return SpecFlag(False, why_none or f"no spec of {did} says irreversible: true")


# ---------------------------------------------------------------------------
# Deriving the class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskClass:
    """The risk class of one review of one head, and why.

    ``derived`` is the class of the diff alone and ``deciding`` the paths that
    made it high, each with its category. ``caller`` is the caller's value as
    given (``None``: none given) and ``irreversible`` what the spec said.
    ``source`` says where the class was read: derived for a request round, or
    for a merge from a record that still holds or recomputed (``note`` says why).
    """

    risk_class: str
    derived: str
    reason: str
    deciding: Tuple[Tuple[str, str], ...] = ()
    caller: Optional[str] = None
    irreversible: Optional[bool] = False
    head_sha: str = ""
    merge_base: str = ""
    base_ref: str = ""
    rule_version: str = ""
    files: int = 0
    source: str = SOURCE_DERIVED
    note: str = ""

    @property
    def is_high(self) -> bool:
        return _RANK[self.risk_class] >= _RANK[HIGH]

    def explain(self) -> str:
        """One line for the operator: the class that applied and why."""
        text = f"risk class {self.risk_class}: {self.reason}"
        return f"{text} ({self.note})" if self.note else text

    def to_record(self) -> Dict[str, Any]:
        """The ``review_risk`` a request record carries."""
        return {
            "risk_class": self.risk_class, "derived": self.derived, "reason": self.reason,
            "deciding": [{"path": path, "category": category} for path, category in self.deciding],
            "caller": self.caller, "irreversible": self.irreversible,
            "head_sha": self.head_sha, "merge_base": self.merge_base, "base_ref": self.base_ref,
            "rule_version": self.rule_version, "files": self.files, "source": self.source, "note": self.note,
        }


def _unnormalisable(path: Any) -> bool:
    """A path the classification cannot read as one repository path."""
    import smart_router

    if not isinstance(path, str) or not path.strip() or any(ch in path for ch in "\0\n\r"):
        return True
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in path):
        return True
    return not smart_router.normalize_dispatch_path(path)


def _deciding_text(deciding: Sequence[Tuple[str, str]]) -> str:
    shown = [
        f"path {path!r} cannot be normalised (fail-closed)" if category == CATEGORY_UNNORMALISABLE
        else f"{category} path {path}"
        for path, category in deciding[:_EXPLAIN_PATHS]
    ]
    more = len(deciding) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more else "")


def _diff_class(reading: DiffReading) -> Tuple[str, str, Tuple[Tuple[str, str], ...]]:
    """``(class, reason, deciding)`` of the diff alone."""
    import smart_router

    if reading.files is None:
        return HIGH, f"fail-closed: {reading.error or 'the diff cannot be read'}", ()
    if not reading.files:
        return HIGH, "fail-closed: the diff names no file", ()
    deciding: List[Tuple[str, str]] = []
    for path in reading.files:
        if _unnormalisable(path):
            deciding.append((str(path), CATEGORY_UNNORMALISABLE))
            continue
        category = smart_router.plan_scope_path_category(path)
        if category:
            deciding.append((smart_router.normalize_dispatch_path(path), category))
    if deciding:
        return HIGH, _deciding_text(deciding), tuple(deciding)
    return MEDIUM, f"no high-risk path in {len(reading.files)} file(s)", ()


def _raises(spec: SpecFlag, caller_class: Optional[str], derived: str) -> List[Tuple[str, str]]:
    """The ``(class, why)`` raises from outside the diff: the spec, and a caller's class above ``derived``."""
    raises: List[Tuple[str, str]] = []
    if spec.irreversible is True:
        raises.append((HIGH, f"the dispatch spec says irreversible: true ({spec.detail})"))
    elif spec.irreversible is not False:
        raises.append((HIGH, f"fail-closed: the dispatch spec cannot be read ({spec.detail})"))
    if caller_class is None:
        return raises
    known = known_class(caller_class)
    if known is None:
        raises.append((HIGH, f"fail-closed: the caller's class {caller_class!r} is no known class ({', '.join(RISK_CLASSES)})"))
    elif _RANK[known] > _RANK[derived]:
        raises.append((known, f"raised by the caller's --risk-class {known}"))
    return raises


def derive_risk_class(
    reading: DiffReading,
    *,
    caller_class: Optional[str] = None,
    spec: Optional[SpecFlag] = None,
    head_sha: str = "",
) -> RiskClass:
    """The risk class of a review of ``head_sha`` whose diff is ``reading`` (see the module docstring)."""
    spec = spec if spec is not None else SpecFlag()
    derived, diff_reason, deciding = _diff_class(reading)
    steps = [(derived, diff_reason)] + _raises(spec, caller_class, derived)
    final = max((cls for cls, _why in steps), key=_rank)
    reasons = [why for cls, why in steps if cls == final]
    if derived != final:
        reasons.append(f"the diff alone: {diff_reason}")
    lower = known_class(caller_class)
    if lower is not None and _RANK[lower] < _RANK[final]:
        reasons.append(f"the caller's --risk-class {lower} does not lower it")
    return RiskClass(
        risk_class=final, derived=derived, reason="; ".join(dict.fromkeys(reasons)), deciding=deciding,
        caller=caller_class, irreversible=spec.irreversible, head_sha=(head_sha or "").strip(),
        merge_base=reading.merge_base, base_ref=reading.base_ref, rule_version=rule_version(),
        files=len(reading.files or ()),
    )


# ---------------------------------------------------------------------------
# The class of the head a merge is about (B3)
# ---------------------------------------------------------------------------


def recorded_on_head(requests_dir: Path, pr_id: str, head_sha: str) -> List[Dict[str, Any]]:
    """Every ``review_risk`` the requests of ``pr_id`` on ``head_sha`` recorded (``review_seat_evaluation``'s scope)."""
    from review_seat_evaluation import pr_requests_on_head

    head = (head_sha or "").strip()
    if not head:
        return []
    return [
        request[RECORD_KEY] for request in pr_requests_on_head(Path(requests_dir), str(pr_id), head)
        if isinstance(request.get(RECORD_KEY), dict)
    ]


def _highest_caller(values: Iterable[Any]) -> Optional[str]:
    """Of the callers' values, the one that raises most; an unknown value raises to ``high``."""
    given = [value for value in values if value is not None]
    if not given:
        return None
    return max(given, key=lambda value: (_rank(value), known_class(value) is None))


def _strictest_spec(flags: Sequence[SpecFlag]) -> SpecFlag:
    """An ``irreversible: true`` first, then a flag that could not be read, else none declared."""
    return (
        next((flag for flag in flags if flag.irreversible is True), None)
        or next((flag for flag in flags if flag.irreversible is not False), None)
        or SpecFlag()
    )


def _recorded_spec(record: Dict[str, Any]) -> SpecFlag:
    value = record.get("irreversible")
    if value is True or value is False:
        return SpecFlag(value, "recorded on a request of this head")
    return SpecFlag(None, f"a request of this head recorded irreversible={value!r}")


def _why_recomputed(records: Sequence[Dict[str, Any]], reading: DiffReading, version: str, head: str) -> str:
    if reading.files is None:
        return "computed here: the diff cannot be read, so no record is trusted"
    if not records:
        return f"computed here: no request of head {head[:12]} recorded a class"
    bases = sorted({str(record.get("merge_base") or "")[:12] for record in records})
    refs = sorted({str(record.get("base_ref") or "") for record in records})
    versions = sorted({str(record.get("rule_version") or "") for record in records})
    return (
        f"computed here: the records of this head were made under merge base {', '.join(bases)} against "
        f"{', '.join(refs)} and rule version {', '.join(versions)}; now {reading.merge_base[:12]} against "
        f"{reading.base_ref} and {version}"
    )


def head_risk_class(
    requests_dir: Path,
    pr_id: str,
    head_sha: str,
    reading: DiffReading,
    *,
    caller_class: Optional[str] = None,
    spec: Optional[SpecFlag] = None,
) -> RiskClass:
    """The risk class of the head a merge is about: the records on it, checked against ``reading``.

    ``reading`` is the diff of ``head_sha`` as the reader sees it now
    (:func:`read_pr_diff`). The caller raises and spec flags recorded on this
    head are kept: they only raise. When a record on this head was read against
    the same base, under the current merge base and rule version, its class
    stands (``source`` ``record``) and can only raise the fresh one; otherwise
    the class is computed here (``recomputed``) and ``note`` says why. A record
    read against another base than ``reading`` is recomputed even when the
    merge bases agree: it is about another merge.
    """
    head = (head_sha or "").strip()
    records = recorded_on_head(requests_dir, pr_id, head)
    version = rule_version()
    caller = _highest_caller([caller_class] + [record.get("caller") for record in records])
    flags = ([spec] if spec is not None else []) + [_recorded_spec(record) for record in records]
    risk = derive_risk_class(reading, caller_class=caller, spec=_strictest_spec(flags), head_sha=head)
    holding = [
        record for record in records
        if reading.files is not None and record.get("base_ref") == reading.base_ref
        and record.get("merge_base") == reading.merge_base and record.get("rule_version") == version
    ]
    if not holding:
        return replace(risk, source=SOURCE_RECOMPUTED, note=_why_recomputed(records, reading, version, head))
    recorded = max((record.get("risk_class") for record in holding), key=_rank)
    note = (
        f"recorded on head {head[:12]} against {reading.base_ref} under merge base {reading.merge_base[:12]} "
        f"and rule version {version}"
    )
    if _rank(recorded) > _RANK[risk.risk_class]:
        known = known_class(recorded) or HIGH
        risk = replace(risk, risk_class=known, reason=f"{risk.reason}; the record of this head says {recorded!r}")
    return replace(risk, source=SOURCE_RECORD, note=note)


# ---------------------------------------------------------------------------
# The class the merge door reads (R3b, D2)
# ---------------------------------------------------------------------------


def _fetch_refspecs(pr_id: str, branch: Optional[str], base_ref: str) -> List[str]:
    """The base branch, and the PR head by its branch (else by the PR's own ref on the forge)."""
    specs: List[str] = []
    prefix = f"{_REMOTE}/"
    if base_ref.startswith(prefix):
        base = base_ref[len(prefix):]
        specs.append(f"+refs/heads/{base}:refs/remotes/{_REMOTE}/{base}")
    head_branch = (branch or "").strip()
    if head_branch:
        specs.append(f"+refs/heads/{head_branch}:refs/remotes/{_REMOTE}/{head_branch}")
    elif str(pr_id).isdigit():
        specs.append(f"refs/pull/{pr_id}/head")
    return specs


def fetch_merge_refs(pr_id: str, branch: Optional[str], *, cwd: Path, base_ref: str) -> str:
    """Fetch the PR head and the PR's base (``base_ref``) from ``origin`` into ``cwd``; ``""`` on success, else why.

    Read-only towards the forge: one ``git fetch`` that updates the
    remote-tracking refs and the object store, never a branch, the index or the
    working tree. It never prompts for credentials.
    """
    specs = _fetch_refspecs(pr_id, branch, base_ref)
    if not specs:
        return "no ref to fetch: no base branch on origin, no PR branch and no PR number"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(
            ["git", "fetch", "--no-tags", "--quiet", "--no-write-fetch-head", _REMOTE, *specs],
            cwd=str(cwd), capture_output=True, encoding="utf-8", errors="surrogateescape",
            timeout=_FETCH_TIMEOUT_SECONDS, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git fetch: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        last = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return f"git fetch {' '.join(specs)} exited {proc.returncode}: {last[0]}"
    return ""


def store_checkout(results_dir: Optional[Path]) -> Tuple[Optional[Path], str]:
    """``(checkout, "")`` of the project the review store belongs to, else ``(None, why)``.

    The cwd-independent chain the forge publisher reads
    (``forge_project_target.resolve_forge_target``): store, project id, the
    checkout that carries that id, its GitHub origin.
    """
    if results_dir is None:
        return None, "no checkout given and no review store to derive one from"
    from forge_check_run import ForgePublishRefused
    from forge_project_target import REGISTER_HINT, resolve_forge_target

    try:
        return resolve_forge_target(Path(results_dir)).project_root, ""
    except ForgePublishRefused as exc:
        # The refusal is worded for the publisher ("nothing was published");
        # only its cause and the repair belong in a class reason.
        cause = str(exc).split(" — ", 1)[0]
        return None, f"the checkout of the review store cannot be established ({cause}; {REGISTER_HINT})"


def merge_head_risk_class(
    requests_dir: Path,
    pr_id: str,
    head_sha: str,
    *,
    base_branch: Optional[str],
    branch: Optional[str] = None,
    checkout: Optional[Path] = None,
    results_dir: Optional[Path] = None,
) -> RiskClass:
    """The class of ``head_sha`` as a merge reads it: fetch, then :func:`head_risk_class` on the fresh diff.

    ``base_branch`` is the branch the PR merges into, as the PR names it
    (``baseRefName``); it has no default, and :func:`base_ref_for` refuses a
    missing or malformed one. The door runs in a checkout where the PR head
    and that base may be missing, so both are fetched first. ``checkout``
    ``None`` is the store's own checkout (:func:`store_checkout`). A base that
    is not known, a checkout that cannot be established, a failed fetch and an
    unreadable diff each give ``high`` with the reason; nothing here lowers a
    class.
    """
    head = (head_sha or "").strip()
    base_ref, why_no_base = base_ref_for(base_branch)
    where, why = (Path(checkout), "") if checkout is not None else store_checkout(results_dir)
    if base_ref is None:
        reading = DiffReading(None, error=why_no_base)
    elif where is None:
        reading = DiffReading(None, base_ref=base_ref, error=why)
    elif not head:
        reading = DiffReading(None, base_ref=base_ref, error="the PR head could not be resolved")
    else:
        failed = fetch_merge_refs(pr_id, branch, cwd=where, base_ref=base_ref)
        reading = (
            DiffReading(None, base_ref=base_ref, error=f"the PR head and {base_ref} could not be fetched ({failed})")
            if failed else read_pr_diff(head, base_ref=base_ref, cwd=where)
        )
    return head_risk_class(requests_dir, pr_id, head, reading)
