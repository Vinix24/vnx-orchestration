"""provider_reachability.py — measured, cached reachability per provider.

``is_available()`` used to answer "is the binary on PATH / is the package
importable". That is presence. Measured 2026-08-23 (OI-1454): three of four
reader adapters reported available while none of them could answer a call
(codex quota spent, litellm key 401), so a fallback that picked on it chose a
dead seat with full confidence.

Presence and reachability are two questions and this module owns the second:

    reachable      a real call to this provider was answered recently
    unreachable    a real call was refused for a stated reason
                   (quota_exhausted, insufficient_balance, auth_401), or the
                   provider is not present at all (not_present)
    unmeasured     nobody has asked it recently

``unmeasured`` is a state of its own and never reads as ``reachable``. A
provider that has not been asked cannot be reported as one that answered. It
is still *usable* (``Reachability.is_usable``): a candidate nobody has proven
dead must stay askable, or nothing would ever produce the first outcome.

Where the evidence comes from
-----------------------------
Real outcomes the fabric already sees, so no extra paid call is made per check:
a gate result booked ``lane_exhausted`` or decided (``gate_recorder``), a
provider-lane dispatch that failed on credit/auth or succeeded
(``provider_dispatch._emit_governance``), an adapter or classifier call that
was refused or answered. Each writes here with a ``source`` naming who saw it.

There is deliberately NO probe in this module. A probe run with the caller's
environment does not test the lane: the lane's process loads its key from its
own source (``~/.config/vnx/provider-usage.env``, the litellm proxy), and
measured 2026-08-29 (OI-1507) the two held different keys, one of them expired.
A lane's own refusal is the only measurement that carries the lane's
credential. Nothing here reads or forwards a credential, and failure text is
scrubbed of anything key-shaped before it is stored.

Time
----
Every record expires, so a stale verdict can never pin a provider out (or in)
forever. An unreachable record lives as long as the provider-quota recovery
contract's cooldown (``incident_taxonomy``, the single cooldown clock,
OI-1188); a reachable record lives a short fixed window, since a quota can run
out at any minute. Past its window a record reads as ``unmeasured``.

Auth is treated the same way here although its recovery contract says "never
auto-recovers": that is a retry policy for the router's cooldown. A
reachability record is evidence, not a lock, and nobody clears a file after
rotating a key. One fresh attempt per window re-measures it.

State lives in ``<state_dir>/provider_reachability/<provider>.json``. Every
function takes ``state_dir`` explicitly and only falls back to the resolver
when it is omitted. Writes are best-effort and never raise into a dispatch;
reads never raise and never fail towards ``reachable``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

STATE_SUBDIR = "provider_reachability"

REASON_NOT_PRESENT = "not_present"
REASON_QUOTA_EXHAUSTED = "quota_exhausted"
REASON_INSUFFICIENT_BALANCE = "insufficient_balance"
# The provider rejected the credential: HTTP 401, or a 403 the fabric
# classifies as ``auth_rejected`` (failure_classification).
REASON_AUTH_401 = "auth_401"
# An UNMEASURED record whose state file could not be read. Loud, never silent.
REASON_UNREADABLE_RECORD = "unreadable_record"

# The reasons a real outcome may stamp on an unreachable record.
OUTCOME_REASONS = frozenset({
    REASON_QUOTA_EXHAUSTED,
    REASON_INSUFFICIENT_BALANCE,
    REASON_AUTH_401,
})

# A confirmation is only as good as it is recent. Short on purpose.
REACHABLE_TTL_SECONDS = 900

_DETAIL_MAX_CHARS = 300

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class ReachabilityState(str, Enum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNMEASURED = "unmeasured"


@dataclass(frozen=True)
class Reachability:
    """One provider's reachability at one moment, with the reason it holds."""

    provider: str
    state: ReachabilityState
    reason: str = ""
    detail: str = ""
    measured_at: Optional[float] = None
    expires_at: Optional[float] = None
    source: str = ""

    @property
    def is_reachable(self) -> bool:
        """True only for a fresh confirmation. Unmeasured is NOT reachable."""
        return self.state is ReachabilityState.REACHABLE

    @property
    def is_known_unreachable(self) -> bool:
        return self.state is ReachabilityState.UNREACHABLE

    @property
    def is_usable(self) -> bool:
        """May be tried: not known to be dead. Says nothing about being proven alive."""
        return self.state is not ReachabilityState.UNREACHABLE

    def describe(self) -> str:
        """One line saying what is known and why, for logs, annotations and reports."""
        if self.state is ReachabilityState.UNREACHABLE:
            head = f"{self.provider} unreachable ({self.reason})"
        elif self.state is ReachabilityState.REACHABLE:
            head = f"{self.provider} reachable"
        else:
            head = f"{self.provider} unmeasured"
            if self.reason:
                head += f" ({self.reason})"
        tail = []
        if self.detail:
            tail.append(self.detail)
        if self.source:
            tail.append(f"source {self.source}")
        if self.measured_at is not None:
            tail.append(f"measured {_iso(self.measured_at)}")
        if self.expires_at is not None and self.state is not ReachabilityState.UNMEASURED:
            tail.append(f"expires {_iso(self.expires_at)}")
        return head + (": " + "; ".join(tail) if tail else "")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_provider(provider: str) -> None:
    if not isinstance(provider, str) or not _PROVIDER_RE.match(provider):
        raise ValueError(f"invalid provider key: {provider!r}")


def is_valid_provider_key(provider: str) -> bool:
    return isinstance(provider, str) and bool(_PROVIDER_RE.match(provider))


def _resolve_state_dir() -> Path:
    from vnx_paths import resolve_state_dir

    return resolve_state_dir()


def _record_path(state_dir: Path, provider: str) -> Path:
    return Path(state_dir) / STATE_SUBDIR / f"{provider}.json"


def unreachable_ttl_seconds() -> int:
    """Lifetime of an unreachable record: the quota recovery contract's cooldown."""
    from incident_taxonomy import IncidentClass, get_cooldown_seconds

    return get_cooldown_seconds(IncidentClass.PROVIDER_QUOTA_EXHAUSTED, 0)


# ---------------------------------------------------------------------------
# Failure text -> reason
# ---------------------------------------------------------------------------

# A credential refusal, matched on phrases and on a status code that sits NEXT
# to auth language. A bare "401" or "unauthorized" is not enough: this scans
# failure text that may quote a diff, a line number or a token count.
_AUTH_401_RE = re.compile(
    r"\b401\b[^\n]{0,60}?(?:unauthori[sz]ed|authenticat|api[ _-]?key|credential|expired|invalid)"
    r"|(?:unauthori[sz]ed|authenticat\w*|api[ _-]?key|credential\w*|expired|invalid)[^\n]{0,60}?\b401\b"
    r"|(?:error code|status(?:[ _]code)?|http(?:/\d(?:\.\d)?)?)[ :=]+401\b"
    r"|invalid[ _-]api[ _-]key|incorrect api key|api[ _-]key (?:has )?expired"
    r"|authentication_error|not authenticated",
    re.IGNORECASE,
)

# Which flavour of exhaustion a matched quota text describes. The marker list
# itself is the fabric's (governance_emit._LANE_EXHAUSTED_MARKERS); this only
# labels a text that list already matched.
_BALANCE_RE = re.compile(r"balance|credit", re.IGNORECASE)


def _bounded(text: str, anchor: int = 0) -> str:
    text = " ".join(text.split())
    if len(text) <= _DETAIL_MAX_CHARS:
        return text
    start = max(0, min(anchor, len(text) - _DETAIL_MAX_CHARS))
    return text[start:start + _DETAIL_MAX_CHARS]


_SECRET_VALUE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bbearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S{8,}", re.IGNORECASE),
)
_SECRET_ENV_NAME_RE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD)$")


def _scrub(text: str) -> str:
    """Remove anything key-shaped, and any literal secret this process holds."""
    for name, value in os.environ.items():
        if len(value) >= 12 and _SECRET_ENV_NAME_RE.search(name):
            text = text.replace(value, "[redacted]")
    text = _SECRET_VALUE_RES[0].sub("[redacted]", text)
    text = _SECRET_VALUE_RES[1].sub("[redacted]", text)
    return _SECRET_VALUE_RES[2].sub(r"\1[redacted]", text)


def classify_failure_text(text: str) -> Optional[Tuple[str, str]]:
    """Return ``(reason, bounded_detail)`` when failure text names a quota,
    balance or credential refusal, else ``None``.

    ``None`` is the common answer and means "this failure says nothing about
    reachability": a timeout, a crash or a malformed prompt must not mark a
    provider out. Quota text is matched by the fabric's single marker list
    (``governance_emit._classify_lane_log_text``), never a second scan.
    """
    if not text or not text.strip():
        return None
    from governance_emit import _classify_lane_log_text

    scrubbed = _scrub(text)
    state, snippet = _classify_lane_log_text(scrubbed)
    if state == "lane_exhausted":
        detail = _bounded(snippet or scrubbed)
        reason = REASON_INSUFFICIENT_BALANCE if _BALANCE_RE.search(detail) else REASON_QUOTA_EXHAUSTED
        return reason, detail
    match = _AUTH_401_RE.search(scrubbed)
    if match:
        return REASON_AUTH_401, _bounded(scrubbed, anchor=max(0, match.start() - 40))
    return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _read_record(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def get(provider: str, *, state_dir: Optional[Path] = None, now: Optional[float] = None) -> Reachability:
    """Measured reachability from recorded outcomes only, with TTL applied.

    No record, an expired record, or a record that cannot be read all answer
    ``unmeasured``. None of them answers ``reachable``. Never raises.
    """
    try:
        _validate_provider(provider)
        sd = Path(state_dir) if state_dir is not None else _resolve_state_dir()
        path = _record_path(sd, provider)
        data = _read_record(path)
    except Exception as exc:  # vnx-silent-except: logged with provider and reason; a broken reader reports unmeasured, never reachable
        logger.warning(
            "provider_reachability: cannot read the record for %r (%s) -- reporting unmeasured, "
            "not reachable",
            provider, exc,
        )
        return Reachability(provider=provider, state=ReachabilityState.UNMEASURED,
                            reason=REASON_UNREADABLE_RECORD, detail=str(exc)[:120])
    if data is None:
        return Reachability(provider=provider, state=ReachabilityState.UNMEASURED)
    try:
        state = ReachabilityState(data["state"])
        measured_at = float(data["measured_at"])
        expires_at = float(data["expires_at"])
        reason = str(data.get("reason") or "")
        detail = str(data.get("detail") or "")
        source = str(data.get("source") or "")
        if state is ReachabilityState.UNMEASURED:
            raise ValueError("an unmeasured provider has no record to store")
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "provider_reachability: malformed record for %r (%s) -- reporting unmeasured, "
            "not reachable",
            provider, exc,
        )
        return Reachability(provider=provider, state=ReachabilityState.UNMEASURED,
                            reason=REASON_UNREADABLE_RECORD, detail=str(exc)[:120])
    if (now if now is not None else time.time()) >= expires_at:
        return Reachability(provider=provider, state=ReachabilityState.UNMEASURED,
                            reason="expired", detail=f"last {state.value} record ran out at {_iso(expires_at)}",
                            measured_at=measured_at, expires_at=expires_at, source=source)
    return Reachability(provider=provider, state=state, reason=reason, detail=detail,
                        measured_at=measured_at, expires_at=expires_at, source=source)


def assess(
    provider: str,
    *,
    present: bool,
    state_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> Reachability:
    """Presence as the cheap precondition, then the measured outcome.

    A provider that is not present is unreachable whatever any record says: the
    binary is gone. A present one takes its measured state.
    """
    if not present:
        return Reachability(provider=provider, state=ReachabilityState.UNREACHABLE,
                            reason=REASON_NOT_PRESENT, source="presence")
    return get(provider, state_dir=state_dir, now=now)


# ---------------------------------------------------------------------------
# Write (best-effort: a bookkeeping failure never breaks a dispatch)
# ---------------------------------------------------------------------------


def _write(
    provider: str,
    state: ReachabilityState,
    reason: str,
    detail: str,
    source: str,
    *,
    state_dir: Optional[Path],
    measured_at: Optional[float],
    now: Optional[float],
) -> Optional[Reachability]:
    from atomic_io import atomic_write_json, slot_lock

    _validate_provider(provider)
    sd = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    from vnx_paths import refuse_real_central_store_write_under_test_runner

    refuse_real_central_store_write_under_test_runner(sd)

    clock = now if now is not None else time.time()
    stamp = measured_at if measured_at is not None else clock
    ttl = REACHABLE_TTL_SECONDS if state is ReachabilityState.REACHABLE else unreachable_ttl_seconds()
    path = _record_path(sd, provider)
    record = Reachability(provider=provider, state=state, reason=reason, detail=detail,
                          measured_at=stamp, expires_at=stamp + ttl, source=source)
    with slot_lock(path):
        existing = None
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    "provider_reachability: replacing an unreadable record for %r (%s)", provider, exc,
                )
        if existing is not None:
            try:
                if float(existing["measured_at"]) >= stamp:
                    # A rewrite of an older record (a takeover annotation, a
                    # re-anchor) carries its original timestamp and must not
                    # overrule a fresher observation, nor refresh its own age.
                    return None
            except (KeyError, TypeError, ValueError):
                pass
        atomic_write_json(path, {
            "provider": provider,
            "state": state.value,
            "reason": reason,
            "detail": detail,
            "source": source,
            "measured_at": stamp,
            "expires_at": stamp + ttl,
        })
    return record


def record_unreachable(
    provider: str,
    reason: str,
    detail: str,
    *,
    source: str,
    state_dir: Optional[Path] = None,
    measured_at: Optional[float] = None,
    now: Optional[float] = None,
) -> Optional[Reachability]:
    """Record that a real call to ``provider`` was refused for ``reason``.

    Returns the record written, or ``None`` when nothing was written (older
    than what is on file, or the write failed and was logged).
    """
    try:
        if reason not in OUTCOME_REASONS:
            raise ValueError(f"reason {reason!r} is not one of {sorted(OUTCOME_REASONS)}")
        return _write(provider, ReachabilityState.UNREACHABLE, reason, _bounded(_scrub(detail or "")), source,
                      state_dir=state_dir, measured_at=measured_at, now=now)
    except Exception as exc:  # vnx-silent-except: logged with provider and reason; bookkeeping must never break a dispatch
        logger.warning(
            "provider_reachability: could not record %r unreachable (%s) from %s: %s",
            provider, reason, source, exc,
        )
        return None


def record_failure(
    provider: str,
    failure_text: str,
    *,
    source: str,
    state_dir: Optional[Path] = None,
    measured_at: Optional[float] = None,
    now: Optional[float] = None,
) -> Optional[Reachability]:
    """Record a failed call, but only when its text names a quota, balance or
    credential refusal. Any other failure says nothing about reachability and
    leaves the record as it is; ``None`` is returned for it.
    """
    try:
        classified = classify_failure_text(failure_text)
    except Exception as exc:  # vnx-silent-except: logged with provider and reason; classification trouble must never break a dispatch
        logger.warning(
            "provider_reachability: could not classify a failure for %r from %s: %s", provider, source, exc,
        )
        return None
    if classified is None:
        return None
    reason, detail = classified
    return record_unreachable(provider, reason, detail, source=source, state_dir=state_dir,
                              measured_at=measured_at, now=now)


def record_success(
    provider: str,
    *,
    source: str,
    state_dir: Optional[Path] = None,
    measured_at: Optional[float] = None,
    now: Optional[float] = None,
) -> Optional[Reachability]:
    """Record that a real call to ``provider`` was answered."""
    try:
        return _write(provider, ReachabilityState.REACHABLE, "", "", source,
                      state_dir=state_dir, measured_at=measured_at, now=now)
    except Exception as exc:  # vnx-silent-except: logged with provider and reason; bookkeeping must never break a dispatch
        logger.warning(
            "provider_reachability: could not record %r reachable from %s: %s", provider, source, exc,
        )
        return None


__all__ = [
    "OUTCOME_REASONS",
    "REACHABLE_TTL_SECONDS",
    "REASON_AUTH_401",
    "REASON_INSUFFICIENT_BALANCE",
    "REASON_NOT_PRESENT",
    "REASON_QUOTA_EXHAUSTED",
    "REASON_UNREADABLE_RECORD",
    "Reachability",
    "ReachabilityState",
    "assess",
    "classify_failure_text",
    "get",
    "is_valid_provider_key",
    "record_failure",
    "record_success",
    "record_unreachable",
    "unreachable_ttl_seconds",
]
