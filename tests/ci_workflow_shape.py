"""Read Profile A of ``vnx-ci.yml`` in either shape, finding jobs by display name, never by id.

``single``: one job carries every step and the required check name (today).
``aggregator`` (C3): four ``Profile A shard (k/4)`` jobs, ``Profile A guards``, and the required
job, which needs all five and runs ``if: always()``. Anything else raises ``WorkflowShapeError``;
a missing job is never answered with an empty list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CHECK_NAME = "Profile A (doctor + core tests)"
GUARDS_NAME = "Profile A guards"
SHARD_NAME = re.compile(r"Profile A shard \((\d+)/(\d+)\)")

SINGLE = "single"
AGGREGATOR = "aggregator"
ROLE_SINGLE, ROLE_SHARD, ROLE_GUARDS, ROLE_AGGREGATOR = "single", "shard", "guards", "aggregator"


class WorkflowShapeError(ValueError):
    """The workflow is unreadable, or Profile A is in no shape this module knows."""


@dataclass(frozen=True)
class ProfileAJob:
    job_id: str
    name: str
    role: str
    body: dict

    @property
    def steps(self) -> list:
        steps = self.body.get("steps")
        if not isinstance(steps, list):
            raise WorkflowShapeError(f"job {self.job_id!r} ({self.name!r}) has no steps list")
        return steps

    @property
    def needs(self) -> list[str]:
        needs = self.body.get("needs")
        if needs is None:
            return []
        if isinstance(needs, str):
            return [needs]
        if isinstance(needs, list) and all(isinstance(item, str) for item in needs):
            return list(needs)
        raise WorkflowShapeError(f"job {self.job_id!r} has a needs value that is neither a string nor a list of strings: {needs!r}")

    @property
    def timeout(self) -> Any:
        """The raw ``timeout-minutes`` (an int or an expression string); ``None`` when absent."""
        return self.body.get("timeout-minutes")


@dataclass(frozen=True)
class ProfileAStep:
    job: ProfileAJob
    index: int
    step: dict
    name: object = None


def load_workflow(path: Path) -> dict:
    """The workflow as a mapping with a ``jobs`` mapping, or ``WorkflowShapeError``."""
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowShapeError(f"{path}: unreadable workflow ({exc})") from exc
    except yaml.YAMLError as exc:
        raise WorkflowShapeError(f"{path}: malformed YAML ({exc})") from exc
    if not isinstance(document, dict):
        raise WorkflowShapeError(f"{path}: workflow document is not a mapping (got {type(document).__name__})")
    return document


def _display_name(job_id: str, body: object) -> str:
    name = body.get("name") if isinstance(body, dict) else None
    return name if isinstance(name, str) and name else job_id


class ProfileA:
    def __init__(self, document: dict) -> None:
        jobs = document.get("jobs") if isinstance(document, dict) else None
        if not isinstance(jobs, dict) or not jobs:
            raise WorkflowShapeError("the workflow has no jobs mapping")
        named = [(str(i), _display_name(str(i), b), b) for i, b in jobs.items()]
        if bad := [i for i, _, b in named if not isinstance(b, dict)]:
            raise WorkflowShapeError(f"jobs that are not mappings: {bad}")
        required = [t for t in named if t[1] == REQUIRED_CHECK_NAME]
        if len(required) != 1:
            raise WorkflowShapeError(
                f"expected exactly one job named {REQUIRED_CHECK_NAME!r}, found {len(required)}")
        shards = [t for t in named if SHARD_NAME.fullmatch(t[1])]
        guards = [t for t in named if t[1] == GUARDS_NAME]
        rid, rname, rbody = required[0]
        if not shards and not guards:
            self.shape = SINGLE
            self.jobs = (ProfileAJob(rid, rname, ROLE_SINGLE, rbody),)
            if not isinstance(rbody.get("steps"), list):
                raise WorkflowShapeError(f"job {rid!r} ({rname!r}) has no steps list")
            return
        self.shape = AGGREGATOR
        self.jobs = self._aggregator_jobs(shards, guards, (rid, rname, rbody))

    @staticmethod
    def _aggregator_jobs(shards: list, guards: list, required: tuple) -> tuple[ProfileAJob, ...]:
        if len(guards) != 1:
            raise WorkflowShapeError(f"shards without exactly one {GUARDS_NAME!r} job: found {len(guards)}")
        parsed = [tuple(int(n) for n in SHARD_NAME.fullmatch(t[1]).groups()) for t in shards]
        totals = {total for _, total in parsed}
        if len(totals) != 1 or sorted(k for k, _ in parsed) != list(range(1, totals.copy().pop() + 1)):
            raise WorkflowShapeError(f"shard jobs do not cover 1..N exactly once: {[t[1] for t in shards]}")
        jobs = [ProfileAJob(i, n, ROLE_SHARD, b) for i, n, b in shards]
        jobs.append(ProfileAJob(guards[0][0], guards[0][1], ROLE_GUARDS, guards[0][2]))
        aggregator = ProfileAJob(required[0], required[1], ROLE_AGGREGATOR, required[2])
        missing = sorted({j.job_id for j in jobs} - set(aggregator.needs))
        if missing:
            raise WorkflowShapeError(f"the aggregator {aggregator.name!r} does not need {missing}")
        if "always()" not in str(aggregator.body.get("if")):
            raise WorkflowShapeError(f"the aggregator {aggregator.name!r} has no `if: always()`: {aggregator.body.get('if')!r}")
        return (*jobs, aggregator)

    @property
    def required_job(self) -> ProfileAJob:
        return next(j for j in self.jobs if j.name == REQUIRED_CHECK_NAME)

    @property
    def steps(self) -> list[ProfileAStep]:
        return [ProfileAStep(j, i, s, s.get("name")) for j in self.jobs for i, s in enumerate(j.steps) if isinstance(s, dict)]

    def jobs_with_role(self, role: str) -> list[ProfileAJob]:
        return [j for j in self.jobs if j.role == role]

    def steps_named(self, name: str, role: str | None = None) -> list[ProfileAStep]:
        return [s for s in self.steps if s.name == name and (role is None or s.job.role == role)]

    def step(self, name: str, role: str | None = None) -> ProfileAStep:
        """The one step called ``name`` (optionally within one role); zero or several raise."""
        found = self.steps_named(name, role)
        if len(found) != 1:
            raise WorkflowShapeError(f"step {name!r} found {len(found)} times in Profile A, expected exactly once")
        return found[0]

    def job_running(self, name: str, role: str | None = None) -> ProfileAJob:
        return self.step(name, role).job

    def timeouts(self) -> dict[str, Any]:
        return {j.job_id: j.timeout for j in self.jobs}


def profile_a(source: Path | dict) -> ProfileA:
    """Profile A of a workflow file or of an already loaded document."""
    return ProfileA(source if isinstance(source, dict) else load_workflow(source))
