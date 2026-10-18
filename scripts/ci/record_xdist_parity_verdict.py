#!/usr/bin/env python3
"""Record the xdist parity verdict: the last word of the parity job before its upload.

Track ``ci-profile-a-xdist``, D2b. Runs in the job "xdist parity" after the
comparison step (``check_xdist_parity.py compare``, step id ``parity``), with
``if: always()``. It decides between two cases:

* The comparison step wrote a verdict. The step's own outcome is added to it,
  as ``steps.parity``, and as ``parity.parity=<outcome>`` in
  ``step_failures`` when it is not success. The comparison step can fail
  after its verdict is written (the summary append failed, the step timed
  out); that verdict may say exit 0 with no step failure, and the run must
  never count as clean (OI-2145). The comparison cannot record its own
  outcome, this step can. Exit 1 and 2 also fail the step, so a verdict with
  differences carries ``parity.parity=failure`` next to its exit.
* It wrote none, or none that reads as a verdict (not a JSON object, or
  without a ``steps`` object and a ``step_failures`` list). Then this writes
  an emergency verdict: exit 2, ``invalid_evidence`` ``no_verdict``, and every
  field a verdict of the comparison has, null or empty where nothing was read,
  so a reader never needs a default to read it (OI-2147). The shadow record
  and Profile A's phase result were not read, which the verdict says as
  ``not_read``.

The workflow step falls back to plain bash when this script is absent (the
checkout failed); ``tests/test_ci_xdist_parity_workflow.py`` holds that
fallback's verdict equal to this script's for the same inputs.

Exit 0 when the verdict and the summary are written; 2 with a reason when
either cannot be. The verdict is always written first, so a summary that
fails never undoes it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_xdist_parity import (COMPARE_STEP, RECORDED_STEPS, VERDICT_SCHEMA, read_json_object, step_failures,
                                write_json_atomic)

NOT_READ = "not_read"
META_FIELDS = ("head_sha", "tested_sha", "run_id", "run_attempt", "event", "ref")


def read_parity_verdict(path: Path) -> dict | None:
    """The verdict the comparison step wrote; None when there is none that carries steps and step failures."""
    verdict = read_json_object(path)
    if verdict is None or not isinstance(verdict.get("steps"), dict) or not isinstance(verdict.get("step_failures"), list):
        return None
    return verdict


def record_compare_outcome(verdict: dict, outcome: str) -> dict:
    """*verdict* with the comparison step's own outcome in ``steps`` and, when not success, in ``step_failures``."""
    failures = list(verdict["step_failures"])
    failure = f"parity.{COMPARE_STEP}={outcome or 'unknown'}"
    if outcome != "success" and failure not in failures:
        failures.append(failure)
    return {**verdict, "steps": {**verdict["steps"], COMPARE_STEP: outcome}, "step_failures": failures}


def emergency_verdict(meta: dict, profile: str, steps: dict, upstream: dict) -> dict:
    """Exit 2 with every field of a comparison verdict: nothing compared, nothing read, every step as it ended."""
    shadow = {"record": NOT_READ, "job_result": upstream.get("shadow") or None}
    reference = {"job_result": upstream.get("profile_a") or None, "result": NOT_READ, "exit": None, "duration_s": None}
    detail = (f"the parity script wrote no verdict (checkout {steps.get('checkout')}, "
              f"parity step {steps.get(COMPARE_STEP)})")
    return {
        "schema": VERDICT_SCHEMA,
        **{name: meta.get(name, "") for name in META_FIELDS},
        "profile": profile or "unknown",
        "steps": steps,
        "shadow": shadow,
        "reference": reference,
        "selection_fingerprint": None,
        "selection_fingerprint_source": None,
        "differences": None,
        "counts": None,
        "tool_versions": None,
        "invalid_evidence": [{"reason": "no_verdict", "detail": detail}],
        "exit": 2,
        "verdict": "invalid_evidence",
        "step_failures": ["parity.verdict=missing", *step_failures(steps, shadow, reference, RECORDED_STEPS)],
    }


def summary_line(verdict: dict, written: dict | None) -> str:
    """What the step summary gains: why there is an emergency verdict, or a failed step under a clean exit."""
    if written is None:
        return ("### xdist parity\n\nNot a required check. Verdict: **invalid_evidence** (exit 2): "
                "the parity script wrote no verdict.\n")
    outcome = verdict["steps"][COMPARE_STEP]
    if outcome == "success" or written.get("exit") not in (0, None):
        return ""
    return (f"\nThe parity step ended `{outcome}` after it wrote exit {written.get('exit')}: "
            f"recorded as `parity.{COMPARE_STEP}={outcome}`, so this run does not count as clean.\n")


def record(args) -> tuple:
    """(the verdict to upload, the summary text it adds): *args* as the workflow step passes them."""
    steps = dict(args.step or [])
    written = read_parity_verdict(Path(args.verdict))
    if written is not None:
        verdict = record_compare_outcome(written, steps.get(COMPARE_STEP, ""))
    else:
        meta = {name: getattr(args, name) for name in META_FIELDS}
        verdict = emergency_verdict(meta, args.profile, steps, dict(args.upstream or []))
    return verdict, summary_line(verdict, written)


def _pair(value: str) -> tuple:
    """NAME=VALUE of ``--step`` and ``--upstream``; argparse turns the error into its usage exit 2."""
    name, sep, outcome = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"expects NAME=VALUE, got {value!r}")
    return name, outcome


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verdict", required=True, help="the xdist-parity-verdict.json the comparison writes")
    parser.add_argument("--summary", default="", help="append a markdown note here (GITHUB_STEP_SUMMARY)")
    parser.add_argument("--profile", default="", help="light or heavy, from the CI profile classifier")
    for name in META_FIELDS:
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, default="")
    parser.add_argument("--step", action="append", type=_pair,
                        help=f"NAME=OUTCOME of a parity-job step, {', '.join(RECORDED_STEPS)} (repeatable)")
    parser.add_argument("--upstream", action="append", type=_pair, help="NAME=RESULT of a needed job (repeatable)")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    verdict, note = record(args)
    try:
        write_json_atomic(Path(args.verdict), verdict)
    except OSError as exc:
        print(f"record_xdist_parity_verdict: cannot write the verdict to {args.verdict}: {exc}", file=sys.stderr)
        return 2
    if note and args.summary:
        try:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write(note)
        except OSError as exc:
            print(f"record_xdist_parity_verdict: cannot append the summary to {args.summary}: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
