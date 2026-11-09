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

import gate_profile
from dispatch_spec import REGISTERED_GATE_NAMES, ReviewGateConfigError, retired_gate_hint

logger = logging.getLogger(__name__)

REVIEW_STACK_KEY = "VNX_DEFAULT_REVIEW_STACK"
SOURCE_CONFIG = "config"
SOURCE_EXPLICIT = "explicit"
STACK_SOURCE_PROFILE = "profile"

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

    ``stack_source`` says who supplied the stack: ``profile`` (the gate profile's
    ``review_stack``), ``override``, ``project_config``, ``env`` or ``default`` (the source
    ``config_registry`` reports for ``VNX_DEFAULT_REVIEW_STACK``). A stack the caller named is
    ``override``. A record can then tell a profile's one-seat stack from an operator's.

    ``profile``, ``profile_source`` and ``profile_error`` are the gate profile
    (``gate_profile``, ``VNX_GOVERNANCE_PROFILE``) in force when the seats were
    resolved. Not the glob governance profile above. A config-sourced review that
    meets an invalid profile value raises ``GateProfileConfigError``; only an
    explicit stack, which does not read the profile, records the error here and
    ``profile`` stays ``None``: a record never says ``standard`` for a value that failed.
    """

    seats: Tuple[str, ...]
    stack: Tuple[str, ...]
    source: str
    value: str
    profile_stack: Optional[Tuple[str, ...]] = None
    profile: Optional[str] = None
    profile_source: Optional[str] = None
    profile_error: Optional[str] = None
    stack_source: str = "override"

    @property
    def extra_gates(self) -> Tuple[str, ...]:
        return tuple(gate for gate in self.stack if gate not in self.seats)

    def to_record(self) -> Dict[str, Any]:
        """The shape a request record carries, so an evaluator can use what was requested.

        ``stack_source`` is on the record (P5): a one-seat stack the profile supplied must be told
        from an operator's. ``profile``, ``profile_source`` and ``profile_error`` are on it since P6
        (``gate-profiel-per-project``), so the seat record and the ``review_gate_result`` receipt can
        say under which gate profile the stack was decided. They are not ``profile_stack``, which is
        the glob governance profile's stack. ``None`` is unknown, never ``standard``.
        """
        return {
            "seats": list(self.seats),
            "extra_gates": list(self.extra_gates),
            "source": self.source,
            "key": REVIEW_STACK_KEY if self.source == SOURCE_CONFIG else "",
            "value": self.value,
            "profile_stack": list(self.profile_stack) if self.profile_stack is not None else None,
            "stack_source": self.stack_source,
            "profile": self.profile,
            "profile_source": self.profile_source,
            "profile_error": self.profile_error,
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


def _explicit_profile_fields(project_id: Optional[str] = None) -> Dict[str, Optional[str]]:
    """The profile record fields for an explicit stack, which wins over the profile.

    The stack does not depend on the profile, so an invalid value does not stop it; it is
    recorded by name instead (``profile`` stays ``None``).
    """
    try:
        resolution = gate_profile.resolve(project_id)
    except gate_profile.GateProfileConfigError as exc:
        return {"profile": None, "profile_source": exc.source, "profile_error": str(exc)}
    return {"profile": resolution.profile, "profile_source": resolution.source, "profile_error": None}


def _config_stack_source(contributed: Optional[str]) -> str:
    """Who supplied a config-sourced stack: the profile, else the registry's own source for the key."""
    if contributed is not None:
        return STACK_SOURCE_PROFILE
    import config_registry

    return config_registry.resolve_with_source(REVIEW_STACK_KEY)[1]


def light_profile_governs_stack(project_id: Optional[str] = None) -> Optional[gate_profile.ProfileResolution]:
    """The resolution when profile ``light`` is what supplies the review stack, else ``None``.

    ``None`` for ``standard``, ``heavy`` and for ``light`` when an operator set the stack
    itself (``VNX_OVERRIDE_*`` or a ``project_config`` row): that stack wins, as with every
    profile, so it is not ``light``'s one-seat stack and the floor has nothing to refuse.
    Raises ``GateProfileConfigError`` for a profile value that is not legal (never a quiet ``None``).
    """
    resolution = gate_profile.resolve(project_id)
    if resolution.profile != "light":
        return None
    contributed = gate_profile.value("review_stack", project_id, resolution=resolution)
    return resolution if contributed is not None else None


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
        return ReviewSeats(
            seats=seats, stack=stack, source=SOURCE_EXPLICIT, value=value, **_explicit_profile_fields(),
        )

    import config_runtime

    # Raises GateProfileConfigError for an invalid profile value: nothing is requested on a
    # configuration that cannot be read. The profile's stack never outranks one an operator set.
    resolution = gate_profile.resolve()
    contributed = gate_profile.value("review_stack", resolution=resolution)
    raw = contributed if contributed is not None else (config_runtime.get(REVIEW_STACK_KEY) or "")
    stack_source = _config_stack_source(contributed)
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
        profile=resolution.profile, profile_source=resolution.source, profile_error=None,
        stack_source=stack_source,
    )
