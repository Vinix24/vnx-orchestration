"""Which review seats a review of a PR needs (OI-2058, reparatiegolf R5a).

One answer for every reader that has to know it: the request side
(``gate_request_handler.request_reviews``) walks these seats, and the merge
side evaluates against exactly the list a request recorded. Two readers that
each derived their own list is how the scheduler requested codex and kimi for a
project whose stack said otherwise, and how the door checked one seat while the
stack named two.

A SEAT is a stack entry that names a review peer (``closure_verifier.
_REVIEW_PEER_GATES``, the gates that may sign a code review) and is not
optional. Each seat needs its own reviewer: a reviewer gate fills at most one
seat of a request.

A stack may also name gates that are no seat. They are still requested, once,
in stack order, and nothing is counted against them:

- ``ci_gate`` checks CI (tests, lint, build), not the change itself. The merge
  door enforces CI as its own requirement (ADR-037 addendum, OI-1645).
- ``wiring_gate`` is not a review peer: the closure verifier cannot read it.
- ``claude_github_optional`` is optional by name. It may never run, and
  ``gate_executor`` already keeps it out of a call's success.

An unknown or retired name is refused by name (:class:`ReviewSeatError`), the
way the dispatch door refuses a stack it cannot read
(``smart_router._primary_review_gate``). A stack is an operator instruction:
dropping a name nobody can run would quietly request less review than was
asked for.

Where the seats come from (``ReviewSeats.source``):

- ``config``: ``VNX_DEFAULT_REVIEW_STACK``, read through ``config_runtime.get``
  on every call. Never ``review_gate_manager.DEFAULT_REVIEW_STACK``: that is
  computed once at import, so a long-running process kept requesting the stack
  of the moment it started. ``config_runtime`` is single-tenant, so the project
  is this process's project, which is the PR's project. ``ci_gate`` joins as an
  extra gate when ``VNX_CI_GATE_REQUIRED`` is on, as before.
- ``explicit``: the caller named the stack (``--review-stack``, ``vnx gate``,
  the obligation runner's declared gate). Its seats are the request's seats; an
  explicit stack may name no seat at all (``--review-stack ci_gate``), a config
  stack may not.

Governance profiles (``VNX_PROFILE_SELECTOR=1``): a profile adds no seat and
removes no seat. The seats of a config-sourced review are always the project's
``VNX_DEFAULT_REVIEW_STACK``; the profile only decides the extra gates (its
non-seat entries, such as ``ci``). The profile's ``required_gates`` list is a
hard-coded seat pair in every built-in profile, so letting it name seats meant
a project whose stack said ``glm_gate`` was reviewed by codex and kimi, and a
``light`` profile (``required_gates: [ci]``) asked for no review at all while
the door still required one. A file glob in a YAML must not decide how many
reviewers a merge needs.

BILLING SAFETY: no Anthropic SDK, no network, no subprocess.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent
for _path in (str(_LIB_DIR), str(_SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from dispatch_spec import REGISTERED_GATE_NAMES, ReviewGateConfigError, retired_gate_hint

logger = logging.getLogger(__name__)

REVIEW_STACK_KEY = "VNX_DEFAULT_REVIEW_STACK"
SOURCE_CONFIG = "config"
SOURCE_EXPLICIT = "explicit"

# Review peers that are optional by name: they may never run, so a review never
# waits on them and they never count as a seat.
OPTIONAL_REVIEW_GATES = frozenset({"claude_github_optional"})


class ReviewSeatError(ReviewGateConfigError):
    """A review stack names a gate nobody can run, or a config stack names no seat.

    A ``ReviewGateConfigError``, so every reader that already refuses an
    unreadable stack by name (the dispatch door) refuses this one the same way.
    """


@dataclass(frozen=True)
class ReviewSeats:
    """The seats of one review, the extra gates beside them, and where they came from.

    ``stack`` is every entry to request, in order: the seats and the extra
    gates. ``value`` is the stack as configured or named, verbatim.
    ``profile_stack`` is the stack a governance profile derived from the changed
    files when one applied (its extra gates are in ``stack``), else ``None``.
    """

    seats: Tuple[str, ...]
    stack: Tuple[str, ...]
    source: str
    value: str
    profile_stack: Optional[Tuple[str, ...]] = None

    @property
    def extra_gates(self) -> Tuple[str, ...]:
        return tuple(gate for gate in self.stack if gate not in self.seats)

    def to_record(self) -> Dict[str, Any]:
        """The shape a request record carries, so an evaluator can use what was requested."""
        return {
            "seats": list(self.seats),
            "extra_gates": list(self.extra_gates),
            "source": self.source,
            "key": REVIEW_STACK_KEY if self.source == SOURCE_CONFIG else "",
            "value": self.value,
            "profile_stack": list(self.profile_stack) if self.profile_stack is not None else None,
        }


def review_peer_gates() -> frozenset:
    """The gates that may sign a code review: the closure verifier's own set, read, not copied."""
    # Imported at call time: closure_verifier's import tail has no business loading
    # when this module is imported, and the merge side imports this module back.
    from closure_verifier import _REVIEW_PEER_GATES

    return _REVIEW_PEER_GATES


def _split(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _refuse_unknown(names: Sequence[str], *, origin: str, value: str) -> None:
    for name in names:
        if name not in REGISTERED_GATE_NAMES:
            raise ReviewSeatError(
                f"{origin} names {name!r}{retired_gate_hint(name) or ', an unknown gate'} "
                f"(value={value!r}); legal gates: {', '.join(sorted(REGISTERED_GATE_NAMES))}. "
                "Refusing the review rather than requesting less than the stack asks for."
            )


def classify_stack(names: Iterable[str], *, origin: str, value: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(seats, stack)`` for an ordered list of gate names.

    Pure: no config, no I/O beyond reading the peer set. Duplicates collapse to
    their first position (a seat is a name; naming it twice is one seat). An
    unknown or retired name raises :class:`ReviewSeatError` naming it.
    """
    stack: List[str] = []
    for name in (str(item).strip() for item in names):
        if name and name not in stack:
            stack.append(name)
    _refuse_unknown(stack, origin=origin, value=value)
    peers = review_peer_gates()
    seats = tuple(gate for gate in stack if gate in peers and gate not in OPTIONAL_REVIEW_GATES)
    return seats, tuple(stack)


def _profile_stack(changed_files: Sequence[str], project_root: Optional[Path]) -> Optional[List[str]]:
    # Looked up on the module at call time, so the resolver and anything that
    # replaces it read the same function.
    import profile_gate_resolver

    if project_root is None:
        return profile_gate_resolver.resolve_gate_stack(list(changed_files))
    return profile_gate_resolver.resolve_gate_stack(list(changed_files), project_root=project_root)


def _config_stack_entries(raw: str, changed_files: Sequence[str], project_root: Optional[Path]) -> "tuple[List[str], Optional[List[str]]]":
    """The entries to request for a config-sourced review, and the profile stack when one applied."""
    import config_runtime

    configured = _split(raw)
    profile = _profile_stack(changed_files, project_root) if changed_files else None
    if profile is None:
        entries = list(configured)
        if config_runtime.get_bool("VNX_CI_GATE_REQUIRED"):
            entries.append("ci_gate")
        return entries, None
    config_seats, _stack = classify_stack(configured, origin=REVIEW_STACK_KEY, value=raw)
    profile_seats, _stack = classify_stack(profile, origin="the governance profile", value=",".join(profile))
    if profile_seats != config_seats:
        logger.info(
            "review_seats: the governance profile names the reviewers %r; a profile adds and "
            "removes no seat, so the seats stay %s=%r", list(profile_seats), REVIEW_STACK_KEY, raw,
        )
    return list(config_seats) + [gate for gate in profile if gate not in profile_seats], profile


def resolve_review_seats(
    *,
    explicit_stack: Optional[Iterable[str]] = None,
    changed_files: Sequence[str] = (),
    project_root: Optional[Path] = None,
) -> ReviewSeats:
    """The seats a review of this PR needs, fresh, with their source.

    ``explicit_stack`` is a stack the caller named; ``None`` reads
    ``VNX_DEFAULT_REVIEW_STACK`` through ``config_runtime.get`` now, and applies
    a governance profile for ``changed_files`` when the profile selector is on
    (see the module docstring for what a profile may change).
    """
    if explicit_stack is not None:
        names = [str(item).strip() for item in explicit_stack if str(item).strip()]
        value = ",".join(names)
        seats, stack = classify_stack(names, origin="the requested review stack", value=value)
        return ReviewSeats(seats=seats, stack=stack, source=SOURCE_EXPLICIT, value=value)

    import config_runtime

    raw = config_runtime.get(REVIEW_STACK_KEY) or ""
    entries, profile = _config_stack_entries(raw, changed_files, project_root)
    seats, stack = classify_stack(entries, origin=REVIEW_STACK_KEY, value=raw)
    if not seats:
        raise ReviewSeatError(
            f"{REVIEW_STACK_KEY}={raw!r} names no review seat: a seat is a review gate "
            f"({', '.join(sorted(review_peer_gates() - OPTIONAL_REVIEW_GATES))}); ci_gate, "
            "wiring_gate and optional gates are not. Set the project's review stack."
        )
    return ReviewSeats(
        seats=seats, stack=stack, source=SOURCE_CONFIG, value=raw,
        profile_stack=tuple(profile) if profile is not None else None,
    )
