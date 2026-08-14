"""availability.py — Decision-time lane availability + cooldown for the smart router.

Until now the smart router encoded provider availability as source comments: a
lane that hit quota was commented out and only came back via a second code edit
plus a release (OI-940 commented out the kimi route; the kimi quota has since
been restored and the router still cannot select it). This module replaces that
with an explicit availability layer that runs at decision time and is cheap:

  - required env vars present   (deepseek: DEEPSEEK_API_KEY — existing behaviour)
  - required CLI on PATH        (kimi: kimi CLI — kimi-via-cli-only)
  - not in cooldown             (a prior quota/auth failure marks the lane
                                 inactive for an env-configurable period)

Cooldown state lives in the central state dir, resolved via the existing
``vnx_paths.resolve_state_dir`` helper (never a hardcoded ``.vnx-data/`` path)
and is written atomically via ``atomic_io.atomic_write_json``.

Fail-open by design: a broken availability layer never blocks a dispatch. Any
unexpected error is logged loudly and the lane is treated as available, so the
router falls through to its existing behaviour.

``record_lane_failure`` is the producer side of the cooldown contract:
lane-execution paths call it when a lane fails on quota/auth
(403/429/quota-exhausted). Wiring those call sites is the fallback-chain op;
this module provides the mechanism and the decision-time check.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 3600  # 60 minutes
COOLDOWN_ENV = "VNX_ROUTER_COOLDOWN_SECONDS"

# Lane names this module understands. Values are provider strings the
# tier-routing engine emits; the regex keeps the per-lane state filename safe.
_LANE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

LOCAL_GEMMA_DISABLED_REASON = (
    "local models skipped per operator decision 2026-08-02; reactivate when "
    "gemma-4-12b-integration ships"
)


@dataclass(frozen=True)
class LaneCheck:
    """Static availability requirements for one lane.

    env_vars / cli_names are the cheap, decision-time gates. disabled_reason
    marks a lane intentionally out of service (explicit, inspectable) instead
    of a commented-out route block.
    """

    lane: str
    env_vars: tuple = ()
    cli_names: tuple = ()
    disabled_reason: Optional[str] = None


# The lanes the tier-routing engine can emit. deepseek and kimi are the two
# cheap lanes actually gated at decision time; claude and codex are deliberately
# ungated (claude is the subscription floor / t0-opus-only, codex is the
# last-resort vangnet). local-gemma is explicitly disabled via this layer.
_LANE_CHECKS: dict[str, LaneCheck] = {
    "deepseek": LaneCheck(lane="deepseek", env_vars=("DEEPSEEK_API_KEY",)),
    "kimi": LaneCheck(lane="kimi", cli_names=("kimi",)),
    "codex": LaneCheck(lane="codex"),
    "claude": LaneCheck(lane="claude"),
    "local-gemma": LaneCheck(lane="local-gemma", disabled_reason=LOCAL_GEMMA_DISABLED_REASON),
}


def _validate_lane(lane: str) -> None:
    if not isinstance(lane, str) or not _LANE_RE.match(lane):
        raise ValueError(f"invalid lane name: {lane!r}")


def _cooldown_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "router_lane_cooldown"


def _cooldown_file(state_dir: Path, lane: str) -> Path:
    return _cooldown_dir(state_dir) / f"{lane}.json"


def _resolve_state_dir() -> Path:
    """Resolve the central state dir via the existing data-dir helper."""
    from vnx_paths import resolve_state_dir  # noqa: PLC0415

    return resolve_state_dir()


def _now() -> float:
    return time.time()


def cooldown_seconds(env: Optional[dict] = None) -> int:
    """Return the configured lane-cooldown duration in seconds.

    Default 60 minutes (3600s), overridable via VNX_ROUTER_COOLDOWN_SECONDS.
    Invalid or negative values fall back to the default with a loud log.
    """
    _env = env if env is not None else dict(os.environ)
    raw = str(_env.get(COOLDOWN_ENV, "")).strip()
    if not raw:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning(
            "smart-router: invalid %s=%r; using default %ds",
            COOLDOWN_ENV, raw, DEFAULT_COOLDOWN_SECONDS,
        )
        return DEFAULT_COOLDOWN_SECONDS
    if value < 0:
        logger.warning(
            "smart-router: %s=%d is negative; using default %ds",
            COOLDOWN_ENV, value, DEFAULT_COOLDOWN_SECONDS,
        )
        return DEFAULT_COOLDOWN_SECONDS
    return value


def record_lane_failure(
    lane: str,
    reason: str,
    *,
    state_dir: Optional[Path] = None,
    duration_seconds: Optional[int] = None,
    now: Optional[float] = None,
) -> None:
    """Mark a lane as in cooldown after a quota/auth failure.

    Best-effort and fail-open: a failure to persist cooldown state is logged and
    swallowed — cooldown bookkeeping must never break a dispatch.
    """
    try:
        _validate_lane(lane)
        sd = Path(state_dir) if state_dir is not None else _resolve_state_dir()
        # New write surface: fail loud if this would write the live central
        # store under pytest (test-store-isolation guard, w19c/OI-934).
        from vnx_paths import refuse_real_central_store_write_under_pytest  # noqa: PLC0415

        refuse_real_central_store_write_under_pytest(sd)
        duration = duration_seconds if duration_seconds is not None else cooldown_seconds()
        until = (now if now is not None else _now()) + duration
        from atomic_io import atomic_write_json  # noqa: PLC0415

        atomic_write_json(
            _cooldown_file(sd, lane),
            {"lane": lane, "reason": reason, "until": until},
        )
        logger.info(
            "smart-router: lane %r entered cooldown for %ds (reason=%s)",
            lane, duration, reason,
        )
    except Exception as exc:  # vnx-silent-except: cooldown bookkeeping must never break a dispatch
        logger.warning(
            "smart-router: failed to record lane %r cooldown (reason=%s): %s",
            lane, reason, exc,
        )


def lane_cooldown_remaining(
    lane: str,
    *,
    state_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> float:
    """Return seconds of cooldown remaining for a lane (0.0 when active).

    Fail-open: an unreadable/corrupt state file reads as not-in-cooldown so a
    broken cooldown reader never removes a lane from service.
    """
    try:
        _validate_lane(lane)
        sd = Path(state_dir) if state_dir is not None else _resolve_state_dir()
        state_file = _cooldown_file(sd, lane)
        if not state_file.is_file():
            return 0.0
        data = json.loads(state_file.read_text(encoding="utf-8"))
        until = float(data.get("until", 0.0))
        return max(0.0, until - (now if now is not None else _now()))
    except Exception as exc:  # vnx-silent-except: unreadable cooldown state reads as not-in-cooldown (fail-open)
        logger.warning(
            "smart-router: failed to read lane %r cooldown state; treating as "
            "not-in-cooldown (fail-open): %s",
            lane, exc,
        )
        return 0.0


def lane_available(
    lane: str,
    *,
    env: Optional[dict] = None,
    state_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> Tuple[bool, str]:
    """Return (available, reason) for a lane at decision time.

    Cheap gates only — no network call in the hot path: env vars, CLI presence
    on PATH, and cooldown state. Fail-open: any unexpected error treats the lane
    as available and logs loudly so the router never declines because its own
    availability layer is broken.
    """
    _env = env if env is not None else dict(os.environ)
    check = _LANE_CHECKS.get(lane)
    if check is None:
        # Unknown lane: not gated here — do not remove a lane the router may
        # legitimately emit that this layer has no requirements for.
        return True, "no availability requirements for lane"
    try:
        if check.disabled_reason is not None:
            return False, check.disabled_reason
        for var in check.env_vars:
            if not _env.get(var):
                return False, f"missing required env var {var}"
        for cli in check.cli_names:
            if shutil.which(cli) is None:
                return False, f"required CLI {cli!r} not on PATH"
        remaining = lane_cooldown_remaining(lane, state_dir=state_dir, now=now)
        if remaining > 0:
            return False, f"lane in cooldown ({remaining:.0f}s remaining)"
        return True, "available"
    except Exception as exc:  # vnx-silent-except: broken availability layer must fail open, never decline a lane
        logger.warning(
            "smart-router: availability check for lane %r failed; treating as "
            "available (fail-open): %s",
            lane, exc,
        )
        return True, f"availability check failed; fail-open ({exc})"


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "COOLDOWN_ENV",
    "LOCAL_GEMMA_DISABLED_REASON",
    "LaneCheck",
    "cooldown_seconds",
    "record_lane_failure",
    "lane_cooldown_remaining",
    "lane_available",
]
