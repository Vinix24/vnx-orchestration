#!/usr/bin/env python3
"""
VNX Model-Inference Guard — difficulty-controlled, infra-excluded model routing inference.

Prevents the self-learning loop from emitting harmful "route this task-class away
from model X" hints derived from CONFOUNDED data. Two confounds are guarded:

  1. Selection bias / task-difficulty. The orchestrator model (opus, which also runs
     T0 and does the hard investigation work) receives the HARD tasks: long, high-token,
     multi-file diagnoses. Cheaper models receive routine work. Comparing raw
     error/success rates across models that ran vastly different task-difficulty is
     invalid. We bucket every session by a difficulty proxy (output-token band) and
     only compare models WITHIN the same difficulty bucket, each with a minimum
     comparable sample.

  2. Resilience mislabelled as failure. `has_error_recovery` marks a session that
     recovered from an error. In a long orchestration session that error is usually a
     system/infra/permission/tool/lane failure the model DIAGNOSED and worked around —
     that is resilience, not a model-quality defect. `has_error_recovery` therefore
     conflates model-reasoning errors with infra errors and is NOT a clean
     model-quality signal. Absent an infra-excluded reasoning-error signal, the guard
     refuses to emit a model-quality verdict and returns ``insufficient_comparable_data``.

Governance: advisory-only. Prefer "insufficient comparable data" over a confident but
wrong routing hint. A wrong route-away hint poisons every downstream dispatch.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

# Minimum comparable sessions PER MODEL, within a shared difficulty bucket, before a
# model-vs-model comparison is allowed. Small samples produce noise that reads as signal.
MIN_COMPARABLE_SAMPLE = 8

# Success-rate gap below which two models are treated as indistinguishable.
MIN_MEANINGFUL_GAP = 0.15

INSUFFICIENT = "insufficient_comparable_data"
HINT = "hint"

# Difficulty buckets by output-token band. Bands are chosen so the orchestrator's heavy
# investigation sessions (>200K output tokens) never share a bucket with routine work —
# that separation is exactly what the difficulty control needs to enforce.
_TOKEN_BANDS = [
    (50_000, "trivial"),
    (200_000, "routine"),
    (600_000, "substantial"),
    (float("inf"), "heavy"),
]


def difficulty_bucket(total_output_tokens: Optional[float]) -> str:
    """Map a session's output-token count to a coarse difficulty band."""
    tok = total_output_tokens or 0
    for ceiling, label in _TOKEN_BANDS:
        if tok < ceiling:
            return label
    return "heavy"


def _reasoning_failure_signal(session: Dict[str, Any]) -> Optional[bool]:
    """Return the session's infra-EXCLUDED reasoning-error verdict, or None.

    ``True``  -> a genuine model-reasoning error occurred.
    ``False`` -> the session completed without a model-reasoning error.
    ``None``  -> no clean signal exists for this session.

    ``has_error_recovery`` is intentionally NOT consulted: it conflates resilience /
    infra recovery (permission hangs, tool failures, lane no-delivers the model
    diagnosed) with model-reasoning errors, so it cannot attribute blame to a model.
    Only an explicit ``reasoning_error`` marker — produced by a classifier that has
    already excluded infra/system/environment errors — counts.
    """
    val = session.get("reasoning_error")
    if val is None:
        return None
    return bool(val)


def evaluate_activity_routing(
    activity: str,
    sessions_by_model: Dict[str, List[Dict[str, Any]]],
    *,
    min_sample: int = MIN_COMPARABLE_SAMPLE,
    min_gap: float = MIN_MEANINGFUL_GAP,
) -> Dict[str, Any]:
    """Decide whether a routing hint is defensible for one activity / task-type.

    ``sessions_by_model`` maps model name -> list of per-session dicts. Each session
    dict should carry ``total_output_tokens`` (difficulty proxy) and may carry a clean
    ``reasoning_error`` marker (infra-excluded). ``has_error_recovery`` is ignored on
    purpose — see :func:`_reasoning_failure_signal`.

    Returns one of:

      {"status": "hint", "task_type", "recommended_model", "avoid_model",
       "confidence", "bucket", "evidence"}

      {"status": "insufficient_comparable_data", "task_type", "reason"}
    """
    # --- Gate A: difficulty-comparable sample ------------------------------------
    by_bucket: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for model, sessions in sessions_by_model.items():
        for session in sessions:
            band = difficulty_bucket(session.get("total_output_tokens"))
            by_bucket[band][model].append(session)

    comparable = None
    for band, models in by_bucket.items():
        qualified = {m: ss for m, ss in models.items() if len(ss) >= min_sample}
        if len(qualified) >= 2:
            comparable = (band, qualified)
            break

    if comparable is None:
        return {
            "status": INSUFFICIENT,
            "task_type": activity,
            "reason": (
                f"No difficulty-comparable bucket with >= {min_sample} sessions per "
                f"model. Models ran different task-difficulty; a raw comparison would "
                f"be confounded by selection bias (the orchestrator model gets the "
                f"hard tasks)."
            ),
        }

    bucket_label, models = comparable

    # --- Gate B: infra-excluded model-quality signal -----------------------------
    rates: Dict[str, float] = {}
    for model, sessions in models.items():
        clean = [v for v in (_reasoning_failure_signal(s) for s in sessions) if v is not None]
        if len(clean) < min_sample:
            return {
                "status": INSUFFICIENT,
                "task_type": activity,
                "reason": (
                    "Only the infra-conflated error signal is available "
                    "(has_error_recovery counts resilience / infra recovery as error). "
                    "No infra-excluded reasoning-error signal exists to attribute model "
                    "blame, so no model-quality verdict is emitted."
                ),
            }
        failures = sum(1 for v in clean if v)
        rates[model] = 1.0 - failures / len(clean)

    # --- Gate C: meaningful gap --------------------------------------------------
    ordered = sorted(rates.items(), key=lambda kv: kv[1], reverse=True)
    best_model, best_rate = ordered[0]
    worst_model, worst_rate = ordered[-1]
    gap = best_rate - worst_rate
    if gap < min_gap:
        return {
            "status": INSUFFICIENT,
            "task_type": activity,
            "reason": (
                f"Reasoning-success gap {gap:.2f} within the '{bucket_label}' bucket is "
                f"below the meaningful threshold {min_gap:.2f}; models are comparable."
            ),
        }

    n_best = len(models[best_model])
    confidence = round(min(0.95, 0.5 + (n_best / 20) * 0.3 + gap * 0.5), 2)
    return {
        "status": HINT,
        "task_type": activity,
        "recommended_model": best_model,
        "avoid_model": worst_model,
        "confidence": confidence,
        "bucket": bucket_label,
        "evidence": (
            f"Within the '{bucket_label}' difficulty bucket: {best_model} "
            f"{best_rate:.0%} vs {worst_model} {worst_rate:.0%} reasoning-success "
            f"(n >= {min_sample}/model, infra errors excluded)."
        ),
    }


def routing_hints(
    activity_sessions: Dict[str, Dict[str, List[Dict[str, Any]]]],
    *,
    min_sample: int = MIN_COMPARABLE_SAMPLE,
    min_gap: float = MIN_MEANINGFUL_GAP,
) -> List[Dict[str, Any]]:
    """Evaluate every activity and return only the defensible (hint) results.

    ``activity_sessions`` maps activity -> {model -> [session dict, ...]}.
    Insufficient-data activities are dropped (the conservative default).
    """
    hints: List[Dict[str, Any]] = []
    for activity, sessions_by_model in activity_sessions.items():
        result = evaluate_activity_routing(
            activity, sessions_by_model, min_sample=min_sample, min_gap=min_gap
        )
        if result.get("status") == HINT:
            hints.append(result)
    return sorted(hints, key=lambda h: h["confidence"], reverse=True)
