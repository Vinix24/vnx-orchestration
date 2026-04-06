#!/usr/bin/env python3
"""CLI for enforcing function-size budgets in critical scripts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from function_size_gate import evaluate_function_budgets, load_function_budgets, render_violations


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enforce function-size budgets for critical scripts")
    parser.add_argument(
        "--config",
        default=str(SCRIPT_DIR / "function_size_budgets.json"),
        help="Path to budget config JSON",
    )
    parser.add_argument(
        "--scripts-root",
        default=str(SCRIPT_DIR),
        help="Root used to resolve relative file paths inside the budget config",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON result")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    scripts_root = Path(args.scripts_root).resolve()
    budgets = load_function_budgets(config_path, scripts_root=scripts_root)
    violations = evaluate_function_budgets(budgets)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": len(violations) == 0,
                    "checked": len(budgets),
                    "violations": render_violations(violations),
                }
            )
        )
    else:
        print(f"checked {len(budgets)} function budgets")
        if violations:
            print("violations:")
            for line in render_violations(violations):
                print(f"- {line}")
        else:
            print("all function budgets satisfied")

    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
