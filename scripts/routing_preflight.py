#!/usr/bin/env python3
"""Routing preflight — validate provider and model readiness at kickoff.

Implements PR-3: Kickoff, Preset, and Preflight Provider Readiness.
Contract: docs/core/100_VERIFIED_PROVIDER_MODEL_ROUTING_CONTRACT.md §11 PR-3.

Checks terminal configuration against routing requirements from FEATURE_PLAN
or individual dispatch metadata before dispatching. Surfaces capability gaps
as deterministic readiness feedback so T0 does not discover failures mid-chain.

Usage:
  python scripts/routing_preflight.py
  python scripts/routing_preflight.py --terminal T1 --provider codex_cli required --model opus required
  python scripts/routing_preflight.py --feature-plan FEATURE_PLAN.md --pr-id PR-3
  python scripts/routing_preflight.py --check-pinned --json

Exit codes:
  0  All required routing checks pass — ready to dispatch
  1  One or more required routing gaps — blocks dispatch
  2  Fatal error (missing files, parse failure)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Terminal pinned assumptions (contract §5.3)
# ---------------------------------------------------------------------------

PINNED_MODELS: Dict[str, str] = {
    "T0": "default",
    "T1": "sonnet",
    "T2": "sonnet",
    "T3": "default",
}

PINNED_PROVIDERS: Dict[str, str] = {
    "T0": "claude_code",
    "T1": "claude_code",
    "T2": "claude_code",
    "T3": "claude_code",
}

KNOWN_PROVIDERS = {"claude_code", "codex_cli", "codex", "gemini_cli", "gemini"}
MODEL_SWITCH_CAPABLE = {"claude_code", "codex_cli", "codex"}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RoutingRequirement:
    """A single provider or model routing requirement extracted from metadata."""
    dimension: str          # "provider" or "model"
    value: str              # e.g. "codex_cli", "opus"
    strength: str           # "required" or "advisory"
    source: str             # "FEATURE_PLAN", "dispatch", "cli"
    pr_id: str = ""
    terminal_id: str = ""   # Target terminal if known


@dataclass
class ReadinessResult:
    """Result of checking one routing requirement against terminal state."""
    terminal_id: str
    dimension: str          # "provider" or "model"
    required_value: str
    actual_value: str
    strength: str
    ready: bool
    gap: str = ""           # "unsupported", "unavailable", "misconfigured", ""
    diagnostic: str = ""
    can_switch: bool = False


@dataclass
class PinnedAssumptionCheck:
    """Result of verifying one terminal's pinned assumptions."""
    terminal_id: str
    expected_provider: str
    actual_provider: str
    expected_model: str
    actual_model: str
    provider_ok: bool
    model_ok: bool
    source: str = "env"     # "env" means machine-verifiable (PA-1 compliant)


@dataclass
class PreflightReport:
    """Full preflight readiness report."""
    ready: bool
    checks: List[ReadinessResult] = field(default_factory=list)
    pinned: List[PinnedAssumptionCheck] = field(default_factory=list)
    blocking: List[ReadinessResult] = field(default_factory=list)
    warnings: List[ReadinessResult] = field(default_factory=list)
    checked_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [asdict(c) for c in self.checks],
            "pinned": [asdict(p) for p in self.pinned],
            "blocking": [asdict(b) for b in self.blocking],
            "warnings": [asdict(w) for w in self.warnings],
            "checked_at": self.checked_at,
        }


# ---------------------------------------------------------------------------
# Terminal configuration resolution
# ---------------------------------------------------------------------------

def resolve_terminal_provider(terminal_id: str) -> str:
    env_key = f"VNX_{terminal_id}_PROVIDER"
    val = os.environ.get(env_key, "").strip().lower()
    return val if val else PINNED_PROVIDERS.get(terminal_id, "claude_code")


def resolve_terminal_model(terminal_id: str) -> str:
    env_key = f"VNX_{terminal_id}_MODEL"
    val = os.environ.get(env_key, "").strip().lower()
    return val if val else PINNED_MODELS.get(terminal_id, "default")


def normalize_model(model: str) -> str:
    """Normalize model names: opus == default."""
    return "default" if model == "opus" else model


# ---------------------------------------------------------------------------
# Readiness checks
# ---------------------------------------------------------------------------

def check_provider_readiness(
    terminal_id: str, required_provider: str, strength: str
) -> ReadinessResult:
    if not required_provider:
        return ReadinessResult(
            terminal_id=terminal_id, dimension="provider",
            required_value="", actual_value="", strength=strength,
            ready=True, diagnostic="No provider requirement",
        )

    actual = resolve_terminal_provider(terminal_id)
    if required_provider == actual:
        return ReadinessResult(
            terminal_id=terminal_id, dimension="provider",
            required_value=required_provider, actual_value=actual,
            strength=strength, ready=True,
        )

    gap = "misconfigured" if required_provider in KNOWN_PROVIDERS else "unsupported"
    diag = (
        f"Terminal {terminal_id} runs {actual} but dispatch requires "
        f"{required_provider} ({strength}). Gap: {gap}."
    )
    ready = strength != "required"
    return ReadinessResult(
        terminal_id=terminal_id, dimension="provider",
        required_value=required_provider, actual_value=actual,
        strength=strength, ready=ready, gap=gap, diagnostic=diag,
    )


def check_model_readiness(
    terminal_id: str, required_model: str, strength: str
) -> ReadinessResult:
    if not required_model:
        return ReadinessResult(
            terminal_id=terminal_id, dimension="model",
            required_value="", actual_value="", strength=strength,
            ready=True, diagnostic="No model requirement",
        )

    actual = resolve_terminal_model(terminal_id)
    provider = resolve_terminal_provider(terminal_id)

    if normalize_model(required_model) == normalize_model(actual):
        return ReadinessResult(
            terminal_id=terminal_id, dimension="model",
            required_value=required_model, actual_value=actual,
            strength=strength, ready=True,
        )

    # Model differs — can the provider switch at runtime?
    if provider in MODEL_SWITCH_CAPABLE:
        return ReadinessResult(
            terminal_id=terminal_id, dimension="model",
            required_value=required_model, actual_value=actual,
            strength=strength, ready=True, can_switch=True,
            diagnostic=f"Model switch needed: {actual} -> {required_model} (provider {provider} supports /model)",
        )

    gap = "unsupported"
    diag = (
        f"Terminal {terminal_id} pinned to {actual} on {provider} "
        f"(no runtime model switching). Dispatch requires {required_model} ({strength})."
    )
    ready = strength != "required"
    return ReadinessResult(
        terminal_id=terminal_id, dimension="model",
        required_value=required_model, actual_value=actual,
        strength=strength, ready=ready, gap=gap, diagnostic=diag,
    )


def check_pinned_assumptions() -> List[PinnedAssumptionCheck]:
    results = []
    for tid in ("T0", "T1", "T2", "T3"):
        exp_p = PINNED_PROVIDERS[tid]
        act_p = resolve_terminal_provider(tid)
        exp_m = PINNED_MODELS[tid]
        act_m = resolve_terminal_model(tid)
        results.append(PinnedAssumptionCheck(
            terminal_id=tid,
            expected_provider=exp_p, actual_provider=act_p,
            expected_model=exp_m, actual_model=act_m,
            provider_ok=(exp_p == act_p),
            model_ok=(normalize_model(exp_m) == normalize_model(act_m)),
        ))
    return results


# ---------------------------------------------------------------------------
# FEATURE_PLAN metadata extraction
# ---------------------------------------------------------------------------

_PROVIDER_RE = re.compile(
    r"Requires-Provider:\s*(\S+)(?:\s+(required))?", re.IGNORECASE
)
_MODEL_RE = re.compile(
    r"Requires-Model:\s*(\S+)(?:\s+(required))?", re.IGNORECASE
)
_TRACK_RE = re.compile(r"\*\*Track\*\*:\s*([A-C])", re.IGNORECASE)

TRACK_TO_TERMINAL = {"A": "T1", "B": "T2", "C": "T3"}


def extract_requirements_from_feature_plan(
    feature_plan: Path, pr_id: Optional[str] = None
) -> List[RoutingRequirement]:
    """Extract routing requirements from FEATURE_PLAN.md PR sections."""
    if not feature_plan.is_file():
        return []

    text = feature_plan.read_text()
    reqs: List[RoutingRequirement] = []

    # Split into PR sections
    sections = re.split(r"^## (PR-\d+)", text, flags=re.MULTILINE)
    # sections[0] = preamble, then pairs of (pr_name, content)
    for i in range(1, len(sections) - 1, 2):
        section_pr = sections[i]
        section_body = sections[i + 1]

        if pr_id and section_pr != pr_id:
            continue

        # Determine target terminal from Track field
        track_m = _TRACK_RE.search(section_body)
        terminal = TRACK_TO_TERMINAL.get(track_m.group(1).upper(), "") if track_m else ""

        for m in _PROVIDER_RE.finditer(section_body):
            reqs.append(RoutingRequirement(
                dimension="provider",
                value=m.group(1).lower(),
                strength="required" if m.group(2) else "advisory",
                source="FEATURE_PLAN",
                pr_id=section_pr,
                terminal_id=terminal,
            ))

        for m in _MODEL_RE.finditer(section_body):
            reqs.append(RoutingRequirement(
                dimension="model",
                value=m.group(1).lower(),
                strength="required" if m.group(2) else "advisory",
                source="FEATURE_PLAN",
                pr_id=section_pr,
                terminal_id=terminal,
            ))

    return reqs


# ---------------------------------------------------------------------------
# Dispatch file metadata extraction
# ---------------------------------------------------------------------------

def extract_requirements_from_dispatch(
    dispatch_file: Path,
) -> Tuple[List[RoutingRequirement], str]:
    """Extract routing requirements and track from a dispatch file."""
    if not dispatch_file.is_file():
        return [], ""

    text = dispatch_file.read_text()
    reqs: List[RoutingRequirement] = []

    # Track
    track_m = re.search(r"\[\[TARGET:([A-C])\]\]", text)
    terminal = TRACK_TO_TERMINAL.get(track_m.group(1), "") if track_m else ""

    for m in _PROVIDER_RE.finditer(text):
        reqs.append(RoutingRequirement(
            dimension="provider",
            value=m.group(1).lower(),
            strength="required" if m.group(2) else "advisory",
            source="dispatch",
            terminal_id=terminal,
        ))

    for m in _MODEL_RE.finditer(text):
        reqs.append(RoutingRequirement(
            dimension="model",
            value=m.group(1).lower(),
            strength="required" if m.group(2) else "advisory",
            source="dispatch",
            terminal_id=terminal,
        ))

    return reqs, terminal


# ---------------------------------------------------------------------------
# Main preflight runner
# ---------------------------------------------------------------------------

def run_routing_preflight(
    requirements: List[RoutingRequirement],
    check_pinned: bool = True,
) -> PreflightReport:
    """Run all routing readiness checks and return a structured report."""
    report = PreflightReport(
        ready=True,
        checked_at=datetime.now(tz=timezone.utc).isoformat(),
    )

    # Check pinned assumptions first
    if check_pinned:
        report.pinned = check_pinned_assumptions()

    # Check each routing requirement
    for req in requirements:
        terminal = req.terminal_id
        if not terminal:
            continue

        if req.dimension == "provider":
            result = check_provider_readiness(terminal, req.value, req.strength)
        elif req.dimension == "model":
            result = check_model_readiness(terminal, req.value, req.strength)
        else:
            continue

        report.checks.append(result)
        if not result.ready:
            if result.strength == "required":
                report.blocking.append(result)
                report.ready = False
            else:
                report.warnings.append(result)
        elif result.gap or (result.diagnostic and result.strength == "advisory"
                            and result.required_value and result.required_value != result.actual_value):
            # Advisory mismatch that is still ready (not blocking) but has a diagnostic
            report.warnings.append(result)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Routing preflight — validate provider/model readiness"
    )
    p.add_argument("--terminal", help="Target terminal (T0|T1|T2|T3)")
    p.add_argument(
        "--provider", nargs="+", metavar="VAL",
        help="Required provider and optional 'required' strength"
    )
    p.add_argument(
        "--model", nargs="+", metavar="VAL",
        help="Required model and optional 'required' strength"
    )
    p.add_argument("--feature-plan", help="Path to FEATURE_PLAN.md")
    p.add_argument("--pr-id", help="Check specific PR from FEATURE_PLAN")
    p.add_argument("--dispatch", help="Path to dispatch file")
    p.add_argument(
        "--check-pinned", action="store_true",
        help="Check pinned terminal assumptions against env"
    )
    p.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Print JSON result to stdout"
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()
    requirements: List[RoutingRequirement] = []

    # Source 1: CLI arguments
    if args.terminal:
        if args.provider:
            val = args.provider[0].lower()
            strength = "required" if len(args.provider) > 1 and args.provider[1].lower() == "required" else "advisory"
            requirements.append(RoutingRequirement(
                dimension="provider", value=val, strength=strength,
                source="cli", terminal_id=args.terminal,
            ))
        if args.model:
            val = args.model[0].lower()
            strength = "required" if len(args.model) > 1 and args.model[1].lower() == "required" else "advisory"
            requirements.append(RoutingRequirement(
                dimension="model", value=val, strength=strength,
                source="cli", terminal_id=args.terminal,
            ))

    # Source 2: FEATURE_PLAN.md
    if args.feature_plan:
        fp = Path(args.feature_plan)
        requirements.extend(extract_requirements_from_feature_plan(fp, args.pr_id))

    # Source 3: Dispatch file
    if args.dispatch:
        reqs, _ = extract_requirements_from_dispatch(Path(args.dispatch))
        requirements.extend(reqs)

    report = run_routing_preflight(
        requirements, check_pinned=args.check_pinned
    )

    if args.json_output:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        if report.ready:
            print("[ok] Routing preflight passed — all required checks ready")
        else:
            print("[!] ROUTING PREFLIGHT FAILED — required capability gaps detected", file=sys.stderr)

        if report.blocking:
            for b in report.blocking:
                print(f"  [BLOCKED] {b.terminal_id} {b.dimension}: {b.diagnostic}", file=sys.stderr)

        if report.warnings:
            for w in report.warnings:
                print(f"  [WARN] {w.terminal_id} {w.dimension}: {w.diagnostic}", file=sys.stderr)

        if args.check_pinned and report.pinned:
            drift = [p for p in report.pinned if not p.provider_ok or not p.model_ok]
            if drift:
                print("\n  Pinned assumption drift:", file=sys.stderr)
                for d in drift:
                    parts = []
                    if not d.provider_ok:
                        parts.append(f"provider: expected={d.expected_provider} actual={d.actual_provider}")
                    if not d.model_ok:
                        parts.append(f"model: expected={d.expected_model} actual={d.actual_model}")
                    print(f"    {d.terminal_id}: {'; '.join(parts)}", file=sys.stderr)
            else:
                print("  Pinned assumptions: all verified")

    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
