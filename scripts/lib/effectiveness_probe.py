"""effectiveness_probe — read-only "does it produce crap?" probe framework.

The cockpit (docs/core/SUBSYSTEMS.md) needs a HEALTH signal per subsystem that is
measured, not guessed. This module is the framework every concrete probe (PR-6's
injection-effectiveness probe, PR-7's governance/plan-gate/migration probes) plugs
into: a small ABC (``EffectivenessProbe``), a result shape (``ProbeResult``), a
subsystem -> probe-class registry (``EFFECTIVENESS_PROBES``), and the vocabulary
translation into ``health_beacon``'s status strings (``PROBE_TO_BEACON``).

ADR-007 scope: probes are read-only over existing stores and write only file-based
beacons under ``<state_dir>/health/`` via ``health_beacon.py`` (see
subsystem_health.py). They create no new central-DB table, so the ADR-007
composite-``project_id``-key requirement does not attach to this module.

A tampered/broken hash-chain maps to beacon ``fail``, NOT ``corrupt``:
``health_beacon.py`` derives ``corrupt`` only from unreadable JSON, and
``all_beacons()`` has no ``status == "corrupt"`` branch — routing a probe's tamper
signal there would silently fall through to the staleness/ok branch. A probe that
detects tampering must classify as ``produces_crap`` (-> beacon ``fail``) and record
``"tamper"`` in its raw detail; ``corrupt`` stays owned by the beacon layer.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Type

# The only status values a probe may report. A total vocabulary — every concrete
# probe's health() must return exactly one of these for every possible input.
PROBE_STATUSES = frozenset(("ok", "degraded", "produces_crap", "unknown"))


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of running one probe: a classification, a one-line summary,
    and the raw signal that produced them (for the beacon `details` payload)."""

    status: str
    signal: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in PROBE_STATUSES:
            raise ValueError(
                f"invalid ProbeResult.status {self.status!r}; must be one of "
                f"{sorted(PROBE_STATUSES)}"
            )


class EffectivenessProbe(abc.ABC):
    """Base class for a read-only subsystem effectiveness probe.

    Subclasses implement three total functions:

    - ``probe()``       -- gather raw, read-only signal. No side effects, no writes.
    - ``signal(raw)``   -- render the raw signal as a one-line human summary.
    - ``health(raw)``   -- classify the raw signal into one of ``PROBE_STATUSES``.

    ``run()`` is the concrete orchestrator: it calls the three in order and
    returns a ``ProbeResult``. Callers (``subsystem_health.aggregate()``) use
    ``run()``, not the individual hooks, so every probe is exercised the same way.
    """

    subsystem: str = ""

    @abc.abstractmethod
    def probe(self) -> Dict[str, Any]:
        """Gather raw signal for this subsystem. Read-only; no side effects."""
        raise NotImplementedError

    @abc.abstractmethod
    def signal(self, raw: Dict[str, Any]) -> str:
        """One-line human-readable summary of ``raw``."""
        raise NotImplementedError

    @abc.abstractmethod
    def health(self, raw: Dict[str, Any]) -> str:
        """Classify ``raw`` into one of ``PROBE_STATUSES``. Total function of ``raw``."""
        raise NotImplementedError

    def run(self) -> ProbeResult:
        """Run probe() -> health()/signal() -> ProbeResult, in that fixed order."""
        raw = self.probe()
        return ProbeResult(status=self.health(raw), signal=self.signal(raw), detail=raw)


# subsystem name -> probe class. Concrete probes register themselves here, either
# via direct assignment or the ``register_probe`` decorator below. Empty until
# PR-6/PR-7 register the injection-effectiveness and governance/plan-gate/migration
# probes; a subsystem with no entry here reports `unknown` (see subsystem_health.py).
EFFECTIVENESS_PROBES: Dict[str, Type["EffectivenessProbe"]] = {}


def register_probe(subsystem: str) -> Callable[[Type["EffectivenessProbe"]], Type["EffectivenessProbe"]]:
    """Class decorator: register a concrete probe under ``subsystem``.

    Usage::

        @register_probe("intelligence-self-learning-loop")
        class InjectionEffectivenessProbe(EffectivenessProbe):
            ...
    """

    def _decorate(cls: Type["EffectivenessProbe"]) -> Type["EffectivenessProbe"]:
        EFFECTIVENESS_PROBES[subsystem] = cls
        return cls

    return _decorate


# Probe status vocabulary -> health_beacon.py status string. Deliberately partial:
# "unknown" has NO entry — a subsystem with no registered probe gets no beacon
# written at all (see subsystem_health.aggregate()); it is reported as `unknown`
# with signal "no probe registered" in the aggregator's return value only.
PROBE_TO_BEACON: Dict[str, str] = {
    "ok": "ok",
    "degraded": "stale",
    "produces_crap": "fail",
}


__all__ = [
    "PROBE_STATUSES",
    "ProbeResult",
    "EffectivenessProbe",
    "EFFECTIVENESS_PROBES",
    "register_probe",
    "PROBE_TO_BEACON",
]
