"""CFX-17: gate-side codex severity translator.

Demotes findings from `error` -> `warning` (or `warning` -> `info`) when their
message matches a configured pattern in `codex_severity_policy.yaml`. The gate
layer counts `severity == "error"` as blocking; demoted findings drop out of
the blocking set without being silently dropped from evidence.

Each translated finding preserves its original severity in
`original_severity` and records the matched rationale in `demotion_reason`,
so the audit trail can show why the gate did not block.

Usage as a library:
    from codex_severity_translator import translate_findings
    translated = translate_findings(findings)

CLI (review/audit):
    python3 codex_severity_translator.py --review <result-file.json>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - PyYAML is a project dependency
    yaml = None  # type: ignore

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "codex_severity_policy.yaml"

_DEMOTION_CHAIN = ("error", "warning", "info")


def _load_policy_text(path: Path) -> Dict[str, Any]:
    """Parse a YAML policy file into a plain dict.

    Returns an empty dict when the file is missing or unreadable so callers
    can treat policy as opt-in. A malformed YAML file is also treated as
    no-op rather than raising — the gate must continue to work even when the
    policy file is being edited.
    """
    if not path.exists():
        return {}
    if yaml is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def load_policy(policy_path: Optional[Path] = None) -> Dict[str, List[Dict[str, str]]]:
    """Load a severity policy. Returns dict with `demote_to_warning`/`demote_to_info` rule lists.

    A rule is `{"pattern": <regex>, "rationale": <str>}`. Malformed rules are
    skipped silently so a single bad entry cannot break the gate.
    """
    path = Path(policy_path) if policy_path is not None else DEFAULT_POLICY_PATH
    raw = _load_policy_text(path)

    normalised: Dict[str, List[Dict[str, str]]] = {
        "demote_to_warning": [],
        "demote_to_info": [],
    }
    for key in normalised:
        rules = raw.get(key) or []
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            pattern = rule.get("pattern")
            rationale = rule.get("rationale", "")
            if not isinstance(pattern, str) or not pattern:
                continue
            normalised[key].append(
                {"pattern": pattern, "rationale": str(rationale)}
            )
    return normalised


def _match_rule(message: str, rules: Iterable[Dict[str, str]]) -> Optional[Dict[str, str]]:
    for rule in rules:
        try:
            if re.search(rule["pattern"], message, re.IGNORECASE):
                return rule
        except re.error:
            continue
    return None


def _next_severity(current: str, target: str) -> str:
    """Return the lower of `current` and `target` along the error->warning->info chain.

    Demotion is monotonic: once demoted to `info` a later `demote_to_warning`
    rule must not promote the finding back to `warning`.
    """
    try:
        current_idx = _DEMOTION_CHAIN.index(current)
    except ValueError:
        current_idx = 0
    try:
        target_idx = _DEMOTION_CHAIN.index(target)
    except ValueError:
        target_idx = current_idx
    return _DEMOTION_CHAIN[max(current_idx, target_idx)]


def translate_findings(
    findings: List[Dict[str, Any]],
    policy_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Apply severity policy to findings list. Returns a translated copy.

    The input list is not mutated. Each translated finding retains every
    original key; a demoted finding additionally carries:
    - `original_severity`: the severity before translation
    - `demotion_reason`: the rationale of the first matching rule

    First match wins per category. `demote_to_warning` is checked before
    `demote_to_info`, but the chain is monotonic so an `info` demotion is
    not promoted back to `warning` by a later rule.
    """
    if not findings:
        return []

    policy = load_policy(policy_path)
    warning_rules = policy.get("demote_to_warning", [])
    info_rules = policy.get("demote_to_info", [])

    translated: List[Dict[str, Any]] = []
    for finding in findings:
        f_copy = dict(finding) if isinstance(finding, dict) else {"message": str(finding)}
        original = str(f_copy.get("severity") or "error").lower()
        message = str(f_copy.get("message") or "")

        new_severity = original
        rationale: Optional[str] = None

        warning_match = _match_rule(message, warning_rules)
        if warning_match is not None:
            new_severity = _next_severity(new_severity, "warning")
            rationale = warning_match["rationale"]

        info_match = _match_rule(message, info_rules)
        if info_match is not None:
            promoted_severity = _next_severity(new_severity, "info")
            if promoted_severity != new_severity:
                new_severity = promoted_severity
                rationale = info_match["rationale"]

        f_copy["severity"] = new_severity
        if new_severity != original:
            f_copy["original_severity"] = original
            if rationale is not None:
                f_copy["demotion_reason"] = rationale
        translated.append(f_copy)

    return translated


def _extract_findings(payload: Any) -> List[Dict[str, Any]]:
    """Pull a list of findings out of a gate-result payload.

    Recognises three common shapes used in this repo:
    - `{"blocking_findings": [...]}` (gate result)
    - `{"findings": [...]}`           (codex parser output)
    - `[...]`                         (raw list)
    """
    if isinstance(payload, list):
        return [f for f in payload if isinstance(f, dict)]
    if isinstance(payload, dict):
        for key in ("blocking_findings", "findings"):
            value = payload.get(key)
            if isinstance(value, list):
                return [f for f in value if isinstance(f, dict)]
    return []


def _summary_line(finding: Dict[str, Any]) -> str:
    severity = finding.get("severity", "?")
    message = (finding.get("message") or "").strip().splitlines()[0] if finding.get("message") else ""
    if len(message) > 80:
        message = message[:77] + "..."
    return f"[{severity}] {message}"


def _review_cli(result_path: Path, policy_path: Optional[Path]) -> int:
    if not result_path.exists():
        print(f"error: result file not found: {result_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot parse result file: {exc}", file=sys.stderr)
        return 2

    findings = _extract_findings(payload)
    if not findings:
        print(f"no findings found in {result_path}")
        return 0

    translated = translate_findings(findings, policy_path=policy_path)

    print(f"Severity translation review for {result_path}")
    print(f"Policy: {policy_path or DEFAULT_POLICY_PATH}")
    print(f"Findings: {len(findings)}")
    demoted = 0
    for before, after in zip(findings, translated):
        before_sev = str(before.get("severity") or "error").lower()
        after_sev = str(after.get("severity") or "error").lower()
        marker = "  "
        if before_sev != after_sev:
            marker = "->"
            demoted += 1
        print(f"  {marker} {before_sev:<7} -> {after_sev:<7}  {_summary_line(before)}")
        if before_sev != after_sev:
            reason = after.get("demotion_reason", "")
            if reason:
                print(f"        reason: {reason}")

    blocking_before = sum(
        1 for f in findings if str(f.get("severity") or "error").lower() == "error"
    )
    blocking_after = sum(
        1 for f in translated if str(f.get("severity") or "error").lower() == "error"
    )
    print(
        f"Demoted: {demoted}  "
        f"Blocking before: {blocking_before}  "
        f"Blocking after: {blocking_after}"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="CFX-17 codex severity translator (gate-side)",
    )
    parser.add_argument(
        "--review",
        metavar="RESULT_FILE",
        help="Show before/after severity table for a gate result JSON file.",
    )
    parser.add_argument(
        "--policy",
        metavar="POLICY_FILE",
        default=None,
        help=f"Override policy file (default: {DEFAULT_POLICY_PATH}).",
    )
    args = parser.parse_args(argv)

    policy_path = Path(args.policy) if args.policy else None

    if args.review:
        return _review_cli(Path(args.review), policy_path)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
