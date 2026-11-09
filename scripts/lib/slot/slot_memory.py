"""Host-wide memory samples and the memory condition for heavy requests (RS-5).

The functions work on the ``state`` dict of ADR-062 sections 4 and 11: the
clock (``now``) and the measurement come in from the caller, and nothing here
opens a file, takes a lock or starts a process. ``take_sample`` is the one
seam, and even there the caller injects the runner (RS-6 owns the subprocess).

One sample per 30-second bucket host-wide, retention on time (600 s, never on
count), a growth rule over the 5-minute window when two usable samples at least
4 minutes apart exist, otherwise the level rule. Recovery needs two consecutive
clean samples from two different buckets. A missing, stale or unreadable
measurement is ``memory_metric_unavailable``; it is never a value and never 0.

``policy`` is validated by RS-2 before it reaches here, so an absent profile or
threshold raises ``KeyError`` instead of admitting the request.
"""

from __future__ import annotations

import time

from process_tree_load import parse_swapusage, parse_vm_stat

BUCKET_SECONDS = 30
RETENTION_SECONDS = 600
FRESH_SECONDS = 120
GROWTH_WINDOW_SECONDS = 300
GROWTH_MIN_SPAN_SECONDS = 240
RECOVERY_BUCKETS = 2

ADMIT = "admit"
WAIT = "wait"
MEMORY_GUARD = "memory_guard"
MEMORY_METRIC_UNAVAILABLE = "memory_metric_unavailable"
MEMORY_REASONS = frozenset({MEMORY_GUARD, MEMORY_METRIC_UNAVAILABLE})
MEMORY_WAIT_EXIT = 75


def bucket_start(now: float) -> int:
    """Epoch second rounded down to its 30-second bucket."""
    return int(now) // BUCKET_SECONDS * BUCKET_SECONDS


def _number(value) -> float | None:
    """A real number, or None for anything else (null, bool, string). Never 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _usable(sample: dict) -> bool:
    """True only for a positively ``ok`` sample that carries both values."""
    return (sample.get("status") == "ok"
            and _number(sample.get("swap_used_mb")) is not None
            and _number(sample.get("compressor_gb")) is not None)


def _retained(sample: dict, now: float) -> bool:
    """Keep the last 600 seconds. A sample without a readable time is dropped."""
    started = _number(sample.get("bucket_start"))
    return started is not None and now - started <= RETENTION_SECONDS


def record_sample(state: dict, sample: dict, now: float) -> bool:
    """Write the sample for the bucket of ``now`` only when that bucket is empty.

    Samples older than ``RETENTION_SECONDS`` are dropped (on time, never on
    count) and the list stays sorted on time. Returns whether it wrote. A sample
    whose status is not exactly ``ok`` — or that claims ``ok`` without both
    values — is stored as ``unavailable`` with null values: unknown is not a
    measurement.
    """
    bucket = bucket_start(now)
    samples = state.setdefault("samples", [])
    samples[:] = [s for s in samples if _retained(s, now)]
    if any(s.get("bucket_start") == bucket for s in samples):
        return False
    stamped = _number(sample.get("measured_at"))
    ok = stamped is not None and _usable(sample)
    samples.append({
        "bucket_start": bucket,
        "measured_at": int(stamped) if stamped is not None else None,
        "swap_used_mb": sample.get("swap_used_mb") if ok else None,
        "compressor_gb": sample.get("compressor_gb") if ok else None,
        "status": "ok" if ok else "unavailable",
    })
    samples.sort(key=lambda s: _number(s.get("bucket_start")) or 0)
    return True


def needs_own_sample(state: dict, now: float) -> bool:
    """True when the bucket of ``now`` is empty: the request measures itself."""
    bucket = bucket_start(now)
    return not any(s.get("bucket_start") == bucket for s in state.get("samples") or [])


def _measurement_reason(latest: dict | None, now: float) -> str | None:
    """``memory_metric_unavailable`` unless the newest sample is fresh and usable."""
    if latest is None:
        return MEMORY_METRIC_UNAVAILABLE
    measured_at = _number(latest.get("measured_at"))
    if measured_at is None or not 0 <= now - measured_at <= FRESH_SECONDS:
        return MEMORY_METRIC_UNAVAILABLE
    return None if _usable(latest) else MEMORY_METRIC_UNAVAILABLE


def _memory_reasons(samples: list[dict], latest: dict, memory: dict, now: float) -> list[str]:
    """The guard reason, or none: growth rule when the window allows it, else level."""
    usable = [s for s in samples if _usable(s)
              and _number(s.get("measured_at")) is not None
              and 0 <= now - s["measured_at"] <= GROWTH_WINDOW_SECONDS]
    usable.sort(key=lambda s: s["measured_at"])
    enough_history = (len(usable) >= 2
                      and usable[-1]["measured_at"] - usable[0]["measured_at"]
                      >= GROWTH_MIN_SPAN_SECONDS)
    if enough_history:
        grew = usable[-1]["swap_used_mb"] - usable[0]["swap_used_mb"]
        compressed = usable[-1]["compressor_gb"] >= memory["compressor_gb"]
        return [MEMORY_GUARD] if grew >= memory["swap_growth_mb"] and compressed else []
    if (latest["swap_used_mb"] >= memory["swap_level_mb"]
            and latest["compressor_gb"] >= memory["compressor_gb"]):
        return [MEMORY_GUARD]
    return []


def memory_verdict(state: dict, policy: dict, profile: str, now: float) -> tuple[str, list[str]]:
    """``(decision, reasons)`` for one request of ``profile``, per ADR-062.

    ``heavy`` is a property of the request, so a nested lease or a top-up under
    a light parent is checked here exactly like a new request; the function has
    no parent argument on purpose. A non-heavy profile always admits, with no
    reason, and never touches ``memory_recovery``.

    A blocked heavy request goes through only after two consecutive clean
    recorded samples from two different buckets (``state["memory_recovery"]``:
    ``blocked`` and ``clean_buckets``); a bucket counts once.
    """
    if not policy["profiles"][profile].get("heavy"):
        return ADMIT, []
    samples = sorted(state.get("samples") or [],
                     key=lambda s: _number(s.get("bucket_start")) or 0)
    recovery = state.setdefault("memory_recovery", {"blocked": False, "clean_buckets": []})
    latest = samples[-1] if samples else None
    reason = _measurement_reason(latest, now)
    if reason is None:
        reasons = _memory_reasons(samples, latest, policy["memory"], now)
    if reason is not None or reasons:
        recovery["blocked"] = True
        recovery["clean_buckets"] = []
        return WAIT, [reason] if reason is not None else reasons
    if not recovery.get("blocked"):
        recovery["clean_buckets"] = []
        return ADMIT, []
    clean: list[int] = []
    for recorded in reversed(samples):
        if (_measurement_reason(recorded, now) is not None
                or _memory_reasons(samples, recorded, policy["memory"], now)):
            break
        clean.insert(0, recorded["bucket_start"])
    clean = clean[-RECOVERY_BUCKETS:]
    if len(clean) < RECOVERY_BUCKETS:
        recovery["clean_buckets"] = clean
        return WAIT, [MEMORY_GUARD]
    recovery["blocked"] = False
    recovery["clean_buckets"] = []
    return ADMIT, []


def memory_refusal(waited_s: float, max_wait_s: float, reasons: list[str]) -> int | None:
    """Exit 75 when memory is the only thing left after ``max_wait_s``, else None.

    ``observe`` only reports; RS-8 decides the mode. Any other waiting reason
    (capacity, queue) belongs to another component, so this returns None then.
    """
    if waited_s < max_wait_s:
        return None
    if not reasons or not set(reasons) <= MEMORY_REASONS:
        return None
    return MEMORY_WAIT_EXIT


def take_sample(run, now: float | None = None) -> dict:
    """Measure swap and the compressor through ``run`` (argv -> stdout text).

    The one impure seam: RS-6 injects the runner that starts ``sysctl
    vm.swapusage`` and ``vm_stat``, and the parsers of ``process_tree_load``
    read the text. A runner that raises, or text the parsers cannot read, gives
    ``status: unavailable`` with null values — never 0.
    """
    measured_at = int(time.time() if now is None else now)
    try:
        swap_text = run(["sysctl", "vm.swapusage"])
        compressor_text = run(["vm_stat"])
    except Exception:  # vnx-silent-except: a failed runner is an unavailable measurement, never a value
        swap_text = compressor_text = None
    swap_bytes = parse_swapusage(swap_text) if isinstance(swap_text, str) else None
    compressor_bytes = parse_vm_stat(compressor_text) if isinstance(compressor_text, str) else None
    if swap_bytes is None or compressor_bytes is None:
        return {"measured_at": measured_at, "swap_used_mb": None,
                "compressor_gb": None, "status": "unavailable"}
    return {"measured_at": measured_at,
            "swap_used_mb": round(swap_bytes / 1024 ** 2, 3),
            "compressor_gb": round(compressor_bytes / 1024 ** 3, 3),
            "status": "ok"}
