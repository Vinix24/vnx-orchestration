"""Pin a project's review stack for one test, with no config store behind it (OI-2058, R5b).

Since R5b the merge door, ``vnx-gate/review`` and ``vnx pr-ready`` require every
seat of the project's review stack, resolved through ``config_runtime`` when no
request on the head recorded it. A test that exercises the evidence routes of
one seat therefore states which project it runs in: one whose stack names that
seat. This helper sets that stack through the operator override
(``VNX_OVERRIDE_DEFAULT_REVIEW_STACK``) and keeps every config store out:
``config_runtime.autowire`` finds nothing and ``config_registry`` has no DB
resolver, so no value of the operator's own store can reach the test.

A plain helper, not a fixture: each test file calls it from its own fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

REVIEW_STACK_OVERRIDE = "VNX_OVERRIDE_DEFAULT_REVIEW_STACK"


def no_config_store(monkeypatch) -> None:
    """Every config value resolves from the environment or the registry default; no store is read."""
    import config_registry
    import config_runtime

    monkeypatch.setattr(config_runtime, "autowire", lambda *args, **kwargs: False)
    monkeypatch.setattr(config_registry, "_db_resolver", None)
    for key in ("VNX_DEFAULT_REVIEW_STACK", REVIEW_STACK_OVERRIDE, "VNX_REVIEW_GATE_TAKEOVER_CHAIN",
                "VNX_OVERRIDE_REVIEW_GATE_TAKEOVER_CHAIN", "VNX_CI_GATE_REQUIRED",
                "VNX_OVERRIDE_CI_GATE_REQUIRED", "VNX_PROFILE_SELECTOR"):
        monkeypatch.delenv(key, raising=False)


def pin_review_stack(monkeypatch, stack: str) -> None:
    """The project's review stack is ``stack`` for this test, and no store is read."""
    no_config_store(monkeypatch)
    monkeypatch.setenv(REVIEW_STACK_OVERRIDE, stack)


def open_pr_in_ledger(state_dir: Path, pr) -> None:
    """Open PR ``pr`` in the store's round ledger, as the gate request that starts its first run does (R1).

    For a test that writes a verdict without going through ``request_reviews``
    (a record put on disk, a standalone gate CLI): since R1 the gate refuses a
    PR with earlier verdicts on disk that is not opened, and the merge door
    counts no PASS of it. Opening it here is the state the request leaves behind.
    """
    import review_rounds

    review_gates = Path(state_dir) / "review_gates"
    review_rounds.open_pr(
        review_rounds.ledger_path(review_gates), review_rounds.store_project_id(review_gates), int(pr),
        basis=review_rounds.BASIS_NEW, evidence=0,
    )
