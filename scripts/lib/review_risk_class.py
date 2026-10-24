"""The risk class of a PR review, derived from the PR diff (R3a, track review-stopregel-en-lichte-stoelen).

Operator decision 2026-10-17: codex is the only required seat per round; the
second seat runs on the last head before the merge, and only when the risk
class is high (schema, security, gate or merge logic, data loss). That rule
needs one class per PR head that no caller can talk down. This module derives
it, ``gate_request_handler.request_reviews`` records it on every seat's request
record, and :func:`head_risk_class` is the reading of the head a merge is about.

**Derived from the diff (B1).** The files of the PR diff against the merge base
(:func:`read_pr_diff`: ``git merge-base``, then ``git diff --name-only
--no-renames -z``, so a rename names both its old and its new path). Every
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
path that cannot be normalised, an unknown caller value and a dispatch spec
that cannot be read are each ``high``, and the reason says which.

**The class belongs to the head (B3).** A request records ``review_risk``
(:meth:`RiskClass.to_record`): the class, the head, the merge base, the rule
version (``smart_router.plan_scope_rule_version``, a hash of the path tables)
and the files that decided it. :func:`head_risk_class` reads the records on the
head it is asked about and keeps what a diff cannot show (a caller's raise, an
irreversible spec). It takes the path part from a record only when that record
has the current merge base and rule version; otherwise it computes the class
itself with :func:`derive_risk_class`. A head that adds a high-risk path is
high, whatever an earlier head was.

R3a changes no merge outcome: the door does not read this class yet (R3b).

BILLING SAFETY: no Anthropic SDK, no network. The only subprocess is a local
``git`` read.
"""
from __future__ import annotations

import json
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
DEFAULT_BASE_REF = "origin/main"

SOURCE_DERIVED = "derived"
SOURCE_RECORD = "record"
SOURCE_RECOMPUTED = "recomputed"

CATEGORY_UNNORMALISABLE = "unnormalisable"

_GIT_TIMEOUT_SECONDS = 30
_EXPLAIN_PATHS = 3
_SHA_RE = re.compile(r"[0-9a-f]{7,64}")


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
    base_ref: str = DEFAULT_BASE_REF
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


def read_pr_diff(head_sha: str, *, base_ref: str = DEFAULT_BASE_REF, cwd: Optional[Path] = None) -> DiffReading:
    """The files of the diff of ``head_sha`` against its merge base with ``base_ref``, read with local git.

    Nothing is fetched: a head that is not in the checkout cannot be read, and
    the class of an unreadable diff is ``high``.
    """
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
    base_ref: str = DEFAULT_BASE_REF
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
    versions = sorted({str(record.get("rule_version") or "") for record in records})
    return (
        f"computed here: the records of this head were made under merge base {', '.join(bases)} and "
        f"rule version {', '.join(versions)}; now {reading.merge_base[:12]} and {version}"
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
    head are kept: they only raise. When a record on this head has the current
    merge base and rule version, its class stands (``source`` ``record``) and can
    only raise the fresh one; otherwise the class is computed here
    (``recomputed``) and ``note`` says why.
    """
    head = (head_sha or "").strip()
    records = recorded_on_head(requests_dir, pr_id, head)
    version = rule_version()
    caller = _highest_caller([caller_class] + [record.get("caller") for record in records])
    flags = ([spec] if spec is not None else []) + [_recorded_spec(record) for record in records]
    risk = derive_risk_class(reading, caller_class=caller, spec=_strictest_spec(flags), head_sha=head)
    holding = [
        record for record in records
        if reading.files is not None and record.get("merge_base") == reading.merge_base
        and record.get("rule_version") == version
    ]
    if not holding:
        return replace(risk, source=SOURCE_RECOMPUTED, note=_why_recomputed(records, reading, version, head))
    recorded = max((record.get("risk_class") for record in holding), key=_rank)
    note = f"recorded on head {head[:12]} under merge base {reading.merge_base[:12]} and rule version {version}"
    if _rank(recorded) > _RANK[risk.risk_class]:
        known = known_class(recorded) or HIGH
        risk = replace(risk, risk_class=known, reason=f"{risk.reason}; the record of this head says {recorded!r}")
    return replace(risk, source=SOURCE_RECORD, note=note)
