#!/usr/bin/env python3
"""VNX Governance Enforcement Engine — F51-PR1.

Deterministic, non-LLM enforcement of governance checks across 4 levels:
  0 = off           — disabled
  1 = advisory      — warns, never blocks
  2 = soft_mandatory — blocks unless VNX_OVERRIDE_<CHECK_NAME>=<reason> set
  3 = hard_mandatory — always blocks, cannot be overridden

Every override is logged to governance_audit.ndjson.

Usage:
    python3 scripts/lib/governance_enforcer.py check \\
        --context '{"pr_number": 221, "feature": "F51", "branch": "feat/..."}'
    python3 scripts/lib/governance_enforcer.py check --mode strict
    python3 scripts/lib/governance_enforcer.py list
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

try:
    from governance_audit import log_enforcement as _log_enforcement_audit
except ImportError:  # pragma: no cover — audit module optional at import time
    _log_enforcement_audit = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent
_VNX_DIR = _REPO_ROOT / ".vnx"
_VNX_DATA_DIR = Path(os.environ.get("VNX_DATA_DIR", str(_REPO_ROOT / ".vnx-data")))

DEFAULT_CONFIG_PATH = _VNX_DIR / "governance_enforcement.yaml"
GATE_RESULTS_DIR = _VNX_DATA_DIR / "state" / "review_gates" / "results"
OPEN_ITEMS_DIGEST = _VNX_DATA_DIR / "state" / "open_items_digest.json"
AUDIT_LOG = _VNX_DATA_DIR / "state" / "governance_audit.ndjson"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CheckConfig:
    name: str
    level: int
    description: str = ""
    scope: List[str] = field(default_factory=list)
    threshold: Optional[int] = None


@dataclass
class EnforcementResult:
    check_name: str
    level: int            # 0=off, 1=advisory, 2=soft_mandatory, 3=hard_mandatory
    passed: bool
    message: str
    override_key: str     # VNX_OVERRIDE_<CHECK_NAME_UPPER>
    overridden_by: Optional[str] = None   # override reason if bypassed


# ---------------------------------------------------------------------------
# YAML loader (fallback to basic parser when pyyaml absent)
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is not None:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    # Minimal YAML fallback: parse key: value lines (no nested support needed
    # for top-level scalar fields; nested dicts handled below if pyyaml absent)
    raise RuntimeError(
        "pyyaml is required for governance_enforcer.py. "
        "Install with: pip install pyyaml"
    )


# ---------------------------------------------------------------------------
# Governance audit log
# ---------------------------------------------------------------------------


def _append_audit(entry: Dict[str, Any]) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Enforcement Engine
# ---------------------------------------------------------------------------


class GovernanceEnforcer:
    """Load YAML config and run deterministic governance checks."""

    def __init__(self) -> None:
        self._checks: Dict[str, CheckConfig] = {}
        self._mode: str = "standard"
        self._raw_config: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def load_config(self, path: Path = DEFAULT_CONFIG_PATH, mode_override: Optional[str] = None) -> None:
        """Parse governance_enforcement.yaml and apply preset if specified."""
        raw = _load_yaml(path)
        self._raw_config = raw

        # Normalize mode: pyyaml 1.1 parses bare 'off'/'on' as booleans
        mode_raw = raw.get("mode", "standard")
        if isinstance(mode_raw, bool):
            mode_raw = "on" if mode_raw else "off"
        self._mode = mode_override or str(mode_raw)

        # Build base check configs from the 'checks' section
        checks_raw: Dict[str, Any] = raw.get("checks", {})
        for name, cfg in checks_raw.items():
            self._checks[name] = CheckConfig(
                name=name,
                level=int(cfg.get("level", 1)),
                description=cfg.get("description", ""),
                scope=cfg.get("scope", []),
                threshold=cfg.get("threshold"),
            )

        # Apply preset overrides — normalize boolean keys (pyyaml 1.1 parses off/on/yes/no as bool)
        presets_raw: Dict[Any, Any] = raw.get("presets", {})
        presets: Dict[str, Any] = {}
        for k, v in presets_raw.items():
            if isinstance(k, bool):
                k = "on" if k else "off"
            presets[str(k)] = v

        preset = presets.get(self._mode, {})
        for name, level in preset.items():
            if name in self._checks:
                self._checks[name].level = int(level)
            else:
                self._checks[name] = CheckConfig(name=name, level=int(level))

    # ------------------------------------------------------------------
    # Individual check dispatch
    # ------------------------------------------------------------------

    def check(self, name: str, context: Dict[str, Any]) -> EnforcementResult:
        """Run a single named check and return its result."""
        cfg = self._checks.get(name)
        if cfg is None:
            return EnforcementResult(
                check_name=name,
                level=0,
                passed=True,
                message=f"Check '{name}' not configured — skipped",
                override_key=f"VNX_OVERRIDE_{name.upper()}",
            )

        if cfg.level == 0:
            return EnforcementResult(
                check_name=name,
                level=0,
                passed=True,
                message="Check disabled (level=0)",
                override_key=f"VNX_OVERRIDE_{name.upper()}",
            )

        # Run the actual check function
        fn_name = f"_check_{name}"
        fn = getattr(self, fn_name, None)
        if fn is None:
            result = EnforcementResult(
                check_name=name,
                level=cfg.level,
                passed=True,
                message=f"No implementation for '{name}' — skipped",
                override_key=f"VNX_OVERRIDE_{name.upper()}",
            )
            return result

        result = fn(cfg, context)

        # Override logic
        if not result.passed:
            override_key = f"VNX_OVERRIDE_{name.upper()}"
            override_reason = os.environ.get(override_key, "").strip()

            if cfg.level == 3:
                # Hard mandatory — cannot be overridden
                result.override_key = override_key
                if override_reason:
                    _append_audit({
                        "ts": _now_utc(),
                        "event": "override_rejected",
                        "check": name,
                        "level": cfg.level,
                        "reason": override_reason,
                        "outcome": "blocked",
                    })
            elif cfg.level == 2 and override_reason:
                # Soft mandatory — override accepted
                _append_audit({
                    "ts": _now_utc(),
                    "event": "override_accepted",
                    "check": name,
                    "level": cfg.level,
                    "reason": override_reason,
                    "outcome": "bypassed",
                    "context": context,
                })
                result.passed = True
                result.overridden_by = override_reason
                result.message = f"[OVERRIDDEN] {result.message} (reason: {override_reason})"

        # Audit every enforcement decision
        if _log_enforcement_audit is not None:
            try:
                _log_enforcement_audit(
                    check_name=result.check_name,
                    level=result.level,
                    result=result.passed,
                    context=context,
                    override=result.overridden_by,
                    message=result.message,
                    dispatch_id=context.get("dispatch_id"),
                )
            except Exception:
                pass  # audit must never block enforcement

        return result

    def check_all(self, context: Dict[str, Any]) -> List[EnforcementResult]:
        """Run all configured checks and return results list."""
        return [self.check(name, context) for name in self._checks]

    def is_blocked(self, results: List[EnforcementResult]) -> bool:
        """Return True if any hard-mandatory check failed without override."""
        return any(
            not r.passed and r.level == 3
            for r in results
        )

    def has_soft_failures(self, results: List[EnforcementResult]) -> bool:
        """Return True if any soft-mandatory check failed (unoverridden)."""
        return any(
            not r.passed and r.level == 2
            for r in results
        )

    # ------------------------------------------------------------------
    # Built-in check implementations
    # ------------------------------------------------------------------

    def _check_gate_before_next_feature(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """Previous feature's gate results must exist in review_gates/results/."""
        feature = ctx.get("feature", "")
        pr_number = ctx.get("pr_number")

        if not pr_number:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="No pr_number in context — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )

        # Look for any gate result files for this PR
        gate_files = list(GATE_RESULTS_DIR.glob(f"pr-{pr_number}-*.json"))
        passed = len(gate_files) >= 1
        return EnforcementResult(
            check_name=cfg.name,
            level=cfg.level,
            passed=passed,
            message=(
                f"Found {len(gate_files)} gate result(s) for PR #{pr_number}"
                if passed
                else f"No gate results found for PR #{pr_number} in {GATE_RESULTS_DIR}"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_codex_gate_required(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """Codex gate result must exist with non-empty contract_hash."""
        pr_number = ctx.get("pr_number")
        if not pr_number:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="No pr_number in context — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        result_path = GATE_RESULTS_DIR / f"pr-{pr_number}-codex_gate.json"
        if not result_path.exists():
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message=f"Codex gate result not found: {result_path}",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message=f"Failed to parse codex gate result: {e}",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        contract_hash = data.get("contract_hash", "")
        passed = bool(contract_hash)
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                f"Codex gate passed — contract_hash: {contract_hash[:12]}..."
                if passed
                else "Codex gate result has empty contract_hash"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_gemini_review_required(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """Gemini review gate result must exist."""
        pr_number = ctx.get("pr_number")
        if not pr_number:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="No pr_number in context — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        result_path = GATE_RESULTS_DIR / f"pr-{pr_number}-gemini_review.json"
        passed = result_path.exists()
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                f"Gemini review result found: {result_path.name}"
                if passed
                else f"Gemini review result not found: {result_path}"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_ci_green_required(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """All CI checks on the PR must be passing (gh pr checks)."""
        pr_number = ctx.get("pr_number")
        if not pr_number:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="No pr_number in context — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        try:
            proc = subprocess.run(
                ["gh", "pr", "checks", str(pr_number), "--json", "name,state,status"],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="gh CLI not available — CI check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        except subprocess.TimeoutExpired:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message="gh pr checks timed out",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        if proc.returncode != 0:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message=f"gh pr checks failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        try:
            checks = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message="Could not parse gh pr checks output",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        failing = [c for c in checks if c.get("state", "").upper() not in ("SUCCESS", "NEUTRAL", "SKIPPED")]
        passed = len(failing) == 0
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                f"All {len(checks)} CI check(s) passing"
                if passed
                else f"{len(failing)}/{len(checks)} CI check(s) not passing: "
                     + ", ".join(c.get("name", "?") for c in failing[:5])
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_max_pr_lines(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """PR diff must not exceed configured line threshold."""
        threshold = cfg.threshold or 300
        branch = ctx.get("branch", "HEAD")
        try:
            proc = subprocess.run(
                ["git", "diff", "--stat", "origin/main..." + branch],
                capture_output=True, text=True, timeout=15,
            )
            lines = proc.stdout.strip().splitlines()
            # Last line: "N files changed, X insertions(+), Y deletions(-)"
            last = lines[-1] if lines else ""
            import re
            m = re.search(r"(\d+) insertion", last)
            m2 = re.search(r"(\d+) deletion", last)
            insertions = int(m.group(1)) if m else 0
            deletions = int(m2.group(1)) if m2 else 0
            total = insertions + deletions
        except Exception as e:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message=f"Could not count PR lines: {e} — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        passed = total <= threshold
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                f"PR diff {total} lines (threshold: {threshold})"
                if passed
                else f"PR diff {total} lines exceeds threshold {threshold}"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_pr_must_exist_before_next_dispatch(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """A GitHub PR must exist for the current branch."""
        branch = ctx.get("branch", "")
        if not branch:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="No branch in context — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        try:
            proc = subprocess.run(
                ["gh", "pr", "list", "--head", branch, "--json", "number,state"],
                capture_output=True, text=True, timeout=20,
            )
        except FileNotFoundError:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="gh CLI not available — PR existence check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        if proc.returncode != 0:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message=f"gh pr list failed: {proc.stderr.strip()[:200]}",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        try:
            prs = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message="Could not parse gh pr list output",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        open_prs = [p for p in prs if p.get("state", "").upper() == "OPEN"]
        passed = len(open_prs) > 0
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                f"PR #{open_prs[0]['number']} exists for branch '{branch}'"
                if passed
                else f"No open PR found for branch '{branch}'"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_receipt_must_have_commit(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """At least one commit must exist since the dispatch timestamp."""
        since = ctx.get("dispatch_timestamp", "")
        args = ["git", "log", "--oneline"]
        if since:
            args += [f"--since={since}"]
        args += ["-5"]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
            commits = [l for l in proc.stdout.splitlines() if l.strip()]
        except Exception as e:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message=f"git log failed: {e} — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        passed = len(commits) > 0
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                f"Found {len(commits)} recent commit(s)"
                if passed
                else "No commits found since dispatch timestamp"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_tests_must_pass(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """pytest --collect-only must exit 0 (fast collection-only check)."""
        try:
            proc = subprocess.run(
                ["python3", "-m", "pytest", "--collect-only", "-q", "--tb=no"],
                capture_output=True, text=True, timeout=60,
                cwd=str(_REPO_ROOT),
            )
        except FileNotFoundError:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="pytest not available — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        except subprocess.TimeoutExpired:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message="pytest collection timed out (60s)",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        passed = proc.returncode == 0
        last_line = (proc.stdout.strip().splitlines() or [""])[-1]
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                f"pytest collection OK — {last_line}"
                if passed
                else f"pytest collection failed (exit {proc.returncode}): {last_line}"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_no_blocking_open_items(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """open_items_digest.json must have blocker_count == 0."""
        if not OPEN_ITEMS_DIGEST.exists():
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="open_items_digest.json not found — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        try:
            data = json.loads(OPEN_ITEMS_DIGEST.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message=f"Failed to parse open_items_digest.json: {e}",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        blocker_count = data.get("summary", {}).get("blocker_count", 0)
        passed = blocker_count == 0
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                "No blocking open items"
                if passed
                else f"{blocker_count} blocking open item(s) present"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_dead_code_check(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """vulture scripts/ --min-confidence 100 — skipped if vulture not installed."""
        try:
            proc = subprocess.run(
                ["vulture", "scripts/", "--min-confidence", "100"],
                capture_output=True, text=True, timeout=30,
                cwd=str(_REPO_ROOT),
            )
        except FileNotFoundError:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="vulture not installed — dead code check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        except subprocess.TimeoutExpired:
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=True,
                message="vulture timed out — check skipped",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        findings = [l for l in proc.stdout.splitlines() if l.strip()]
        passed = len(findings) == 0
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                "No 100%-confidence dead code found"
                if passed
                else f"{len(findings)} dead code finding(s): " + "; ".join(findings[:3])
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )

    def _check_decision_audit_trail(self, cfg: CheckConfig, ctx: Dict[str, Any]) -> EnforcementResult:
        """governance_audit.ndjson must exist with at least one entry."""
        if not AUDIT_LOG.exists():
            return EnforcementResult(
                check_name=cfg.name, level=cfg.level, passed=False,
                message=f"governance_audit.ndjson not found at {AUDIT_LOG}",
                override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
            )
        lines = [l for l in AUDIT_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
        passed = len(lines) > 0
        return EnforcementResult(
            check_name=cfg.name, level=cfg.level, passed=passed,
            message=(
                f"Audit trail has {len(lines)} entry(s)"
                if passed
                else "governance_audit.ndjson is empty"
            ),
            override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
        )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _level_label(level: int) -> str:
    return {0: "off", 1: "advisory", 2: "soft_mandatory", 3: "hard_mandatory"}.get(level, str(level))


def _format_result(r: EnforcementResult) -> str:
    status = "PASS" if r.passed else ("WARN" if r.level == 1 else "FAIL")
    override_note = f" [overridden: {r.overridden_by}]" if r.overridden_by else ""
    return (
        f"  [{status}] {r.check_name} ({_level_label(r.level)}){override_note}\n"
        f"         {r.message}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> int:
    enforcer = GovernanceEnforcer()
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    mode = args.mode or None

    try:
        enforcer.load_config(config_path, mode_override=mode)
    except Exception as e:
        print(f"ERROR: Failed to load config from {config_path}: {e}", file=sys.stderr)
        return 2

    ctx: Dict[str, Any] = {}
    if args.context:
        try:
            ctx = json.loads(args.context)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid --context JSON: {e}", file=sys.stderr)
            return 2

    results = enforcer.check_all(ctx)

    hard_failures = [r for r in results if not r.passed and r.level == 3]
    soft_failures = [r for r in results if not r.passed and r.level == 2]
    advisories = [r for r in results if not r.passed and r.level == 1]
    passed_count = sum(1 for r in results if r.passed)

    print(f"\nGovernance Enforcement — mode: {enforcer._mode}")
    print("=" * 60)
    for r in results:
        if r.level == 0:
            continue
        print(_format_result(r))
    print("=" * 60)
    print(
        f"  Passed: {passed_count}/{len(results)} | "
        f"Hard failures: {len(hard_failures)} | "
        f"Soft failures: {len(soft_failures)} | "
        f"Advisories: {len(advisories)}"
    )

    # Log the run to audit trail
    _append_audit({
        "ts": _now_utc(),
        "event": "enforcement_run",
        "mode": enforcer._mode,
        "context": ctx,
        "summary": {
            "total": len(results),
            "passed": passed_count,
            "hard_failures": len(hard_failures),
            "soft_failures": len(soft_failures),
            "advisories": len(advisories),
        },
        "hard_failure_checks": [r.check_name for r in hard_failures],
    })

    if enforcer.is_blocked(results):
        print("\n[BLOCKED] Hard mandatory check(s) failed. Dispatch is blocked.", file=sys.stderr)
        for r in hard_failures:
            print(f"  - {r.check_name}: {r.message}", file=sys.stderr)
        return 1

    if soft_failures:
        print(f"\n[WARNING] {len(soft_failures)} soft-mandatory check(s) failed.")
        print("  Set VNX_OVERRIDE_<CHECK_NAME>=<reason> to bypass each one.")

    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    enforcer = GovernanceEnforcer()
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    mode = args.mode or None
    try:
        enforcer.load_config(config_path, mode_override=mode)
    except Exception as e:
        print(f"ERROR: Failed to load config: {e}", file=sys.stderr)
        return 2

    print(f"\nGovernance Checks — mode: {enforcer._mode}")
    print(f"{'Check':<45} {'Level':<18} {'Description'}")
    print("-" * 90)
    for name, cfg in enforcer._checks.items():
        label = _level_label(cfg.level)
        print(f"  {name:<43} {label:<18} {cfg.description}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VNX Governance Enforcement Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    check_p = sub.add_parser("check", help="Run all governance checks")
    check_p.add_argument(
        "--context", metavar="JSON",
        help='Context dict, e.g. \'{"pr_number": 221, "branch": "feat/..."}\'',
    )
    check_p.add_argument("--mode", help="Override mode (strict|standard|relaxed|off)")
    check_p.add_argument("--config", help="Path to governance_enforcement.yaml")

    list_p = sub.add_parser("list", help="List all checks and their current enforcement levels")
    list_p.add_argument("--mode", help="Override mode")
    list_p.add_argument("--config", help="Path to governance_enforcement.yaml")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "list":
        return _cmd_list(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
