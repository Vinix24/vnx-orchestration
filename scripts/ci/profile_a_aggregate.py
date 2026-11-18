#!/usr/bin/env python3
"""Profile A aggregator: the fail-closed decision over shards and guards.

Track ``pr-ci-onder-zes-minuten``, C3. The required check ``Profile A (doctor +
core tests)`` becomes an aggregator over ``Profile A shard (k/N)`` jobs and a
``Profile A guards`` job. ``decide`` reads ``${{ toJSON(needs) }}`` and is
green only on positive evidence, in one of three modes:

* ``light``: xdist on, every shard ``success`` with profile ``light``, and the
  aggregator's own classifier says ``light``.
* ``sharded``: xdist on, every shard ``success`` with profile ``heavy``, the
  aggregator says ``heavy``, and ``<evidence-root>/shard-k/`` holds a file for
  every k.
* ``serial`` (the way back, ``VNX_CI_XDIST=0``): every shard ``skipped``.

In every mode the guards job must be ``success``. Anything else is red (exit
1) and the verdict names every reason. Invalid input is exit 2. Only exit 0
prints ``mode=<mode>`` for ``$GITHUB_OUTPUT``. The content of the evidence
(a test in two shards or none, a differing source) is checked by
``check_xdist_parity.py completeness``, not here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCHEMA = "vnx-ci-aggregate/1"
RESULTS = ("success", "failure", "cancelled", "skipped")


class InvalidInput(Exception):
    """The input cannot be decided on: exit 2."""


def _reject_constant(token: str) -> None:
    raise InvalidInput(f"needs file holds non-finite number {token}")


def load_needs(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InvalidInput(f"needs file unreadable: {exc}") from exc
    try:
        needs = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise InvalidInput(f"needs file is not JSON: {exc}") from exc
    if not isinstance(needs, dict):
        raise InvalidInput(f"needs is a {type(needs).__name__}, not an object")
    return needs


def parse_shard_jobs(raw: str, guards_job: str) -> list[str]:
    jobs = [j.strip() for j in raw.split(",")]
    if not raw.strip() or any(not j for j in jobs):
        raise InvalidInput(f"--shard-jobs {raw!r} holds an empty job id")
    if len(set(jobs)) != len(jobs):
        raise InvalidInput(f"--shard-jobs {raw!r} holds a duplicate job id")
    if guards_job in jobs:
        raise InvalidInput(f"--guards-job {guards_job!r} is also a shard job")
    return jobs


def _job(needs: dict, job_id: str) -> tuple[str | None, object]:
    """Return ``(result, outputs)`` of a job, or ``(None, None)`` when absent."""
    if job_id not in needs:
        return None, None
    entry = needs[job_id]
    if not isinstance(entry, dict):
        raise InvalidInput(f"needs[{job_id!r}] is not an object")
    result = entry.get("result")
    if not isinstance(result, str) or result not in RESULTS:
        raise InvalidInput(f"needs[{job_id!r}].result is {result!r}, not one of {RESULTS}")
    return result, entry.get("outputs")


def _has_file(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    for _root, _dirs, files in os.walk(directory):
        if files:
            return True
    return False


def decide(needs: dict, shard_jobs: list[str], guards_job: str, profile: str,
           evidence_root: Path, xdist_off: bool) -> dict:
    reasons: list[str] = []
    shards = []
    for k, job_id in enumerate(shard_jobs, start=1):
        result, outputs = _job(needs, job_id)
        # Display only: a non-object outputs has no profile, which is red below.
        shard_profile = outputs.get("profile") if isinstance(outputs, dict) else None
        if not isinstance(shard_profile, str) or not shard_profile:
            shard_profile = None
        evidence = _has_file(evidence_root / f"shard-{k}")
        shards.append({"k": k, "job": job_id, "result": result,
                       "profile": shard_profile, "evidence": evidence})
        if result is None:
            reasons.append(f"needs: no job {job_id}")
        elif xdist_off:
            if result != "skipped":
                reasons.append(f"shard {k}: result {result} while VNX_CI_XDIST=0")
        else:
            if result != "success":
                reasons.append(f"shard {k}: result {result}")
            if shard_profile is None:
                reasons.append(f"shard {k}: no profile output")
            elif shard_profile != profile:
                reasons.append(f"shard {k}: profile {shard_profile}, aggregator {profile}")
            if profile == "heavy" and not evidence:
                reasons.append(f"shard {k}: no evidence")
    guards_result, _ = _job(needs, guards_job)
    if guards_result is None:
        reasons.append(f"needs: no job {guards_job}")
    elif guards_result != "success":
        reasons.append(f"guards: result {guards_result}")
    mode = None
    if not reasons:
        mode = "serial" if xdist_off else ("sharded" if profile == "heavy" else "light")
    return {"schema": SCHEMA, "mode": mode, "green": mode is not None, "reasons": reasons,
            "shards": shards, "guards": guards_result, "xdist_off": xdist_off,
            "profile": profile}


def _summary_block(verdict: dict) -> str:
    if verdict["green"]:
        return f"### Profile A aggregate: green ({verdict['mode']})\n\n"
    lines = "".join(f"- {r}\n" for r in verdict["reasons"])
    return f"### Profile A aggregate: red\n\n{lines}\n"


def _write(verdict: dict, output: Path, summary: Path | None) -> None:
    output.write_text(json.dumps(verdict, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if summary is not None:
        with summary.open("a", encoding="utf-8") as fh:
            fh.write(_summary_block(verdict))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("decide", help="decide the Profile A check from toJSON(needs)")
    d.add_argument("--needs", type=Path, required=True)
    d.add_argument("--shard-jobs", required=True)
    d.add_argument("--guards-job", required=True)
    d.add_argument("--profile", choices=("light", "heavy"), required=True)
    d.add_argument("--evidence-root", type=Path, required=True)
    d.add_argument("--xdist-off", action="store_true")
    d.add_argument("--output", type=Path, required=True)
    d.add_argument("--summary", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        shard_jobs = parse_shard_jobs(args.shard_jobs, args.guards_job)
        verdict = decide(load_needs(args.needs), shard_jobs, args.guards_job, args.profile,
                         args.evidence_root, args.xdist_off)
    except InvalidInput as exc:
        verdict = {"schema": SCHEMA, "mode": None, "green": False,
                   "reasons": [f"invalid input: {exc}"], "shards": [], "guards": None,
                   "xdist_off": args.xdist_off, "profile": args.profile}
        print(f"profile_a_aggregate: invalid input: {exc}", file=sys.stderr)
        try:
            _write(verdict, args.output, args.summary)
        except OSError as write_exc:
            print(f"profile_a_aggregate: verdict not written: {write_exc}", file=sys.stderr)
        return 2
    try:
        _write(verdict, args.output, args.summary)
    except OSError as exc:
        print(f"profile_a_aggregate: verdict not written: {exc}", file=sys.stderr)
        return 2
    if not verdict["green"]:
        for reason in verdict["reasons"]:
            print(f"profile_a_aggregate: {reason}", file=sys.stderr)
        return 1
    print(f"mode={verdict['mode']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
