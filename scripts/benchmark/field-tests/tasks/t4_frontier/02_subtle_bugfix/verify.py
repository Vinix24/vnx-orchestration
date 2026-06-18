"""verify.py for t4-02 — subtle TTLCache eviction bugfix (SWE-bench style).

Graded: existing suite must stay green (the fix must break nothing) + a hidden
regression suite (a live entry must survive when an expired non-LRU entry exists
under capacity pressure). correctness = pass_count / expected.

  unfixed (no change)        → 8/16 = 2.50  (existing green, regression all fail)
  correct fix                → 16/16 = 5.00
  fix that breaks existing   → lower (existing portion drops)
  "fix" by enlarging capacity → breaks LRU existing tests → lower
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

SEED_REL = "scripts/benchmark/field-tests/tasks/t4_frontier/02_subtle_bugfix/seed"
REQUIRED = ["cache.py", "tests/test_cache.py"]
EXISTING_WEIGHT = 8       # the original suite must stay green
N_REGRESSION = 8          # hidden scenarios — the actual fix
EXPECTED = EXISTING_WEIGHT + N_REGRESSION


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _load_cache(cell: Path):
    target = cell / "cache.py"
    spec = importlib.util.spec_from_file_location("worker_cache", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TTLCache


def _run_existing_suite(cell: Path) -> tuple[int, int]:
    """Run the worker's tests/ and return (passed, failed)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(cell), capture_output=True, text=True, timeout=120,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    import re
    passed = failed = 0
    m = re.search(r"(\d+) passed", out)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", out)
    if m:
        failed = int(m.group(1))
    if "error" in out.lower() and passed == 0 and failed == 0:
        failed = EXISTING_WEIGHT  # collection error → treat as all-broken
    return passed, failed


def _regression_checks(TTLCache) -> list[tuple[str, bool]]:
    """A LIVE entry must survive when an EXPIRED non-LRU entry exists under capacity
    pressure. The buggy code evicts the live LRU entry instead of reclaiming the
    expired slot, so a live entry goes missing.

    6 bug-trigger scenarios (buggy fails, correct passes) + 2 anti-cheat scenarios
    (both pass — they ensure a 'fix' that enlarges capacity / disables eviction fails).
    Pattern: 'old' set first (will expire) then made MRU via get(); the live entries
    are set LATER (later expiry) so they are the LRU but NOT expired at insert time.
    """
    results: list[tuple[str, bool]] = []

    def scenario(name, capacity, build):
        try:
            clk = _Clock()
            c = TTLCache(capacity=capacity, ttl=10, time_fn=clk)
            results.append((name, bool(build(c, clk))))
        except Exception as exc:  # noqa: BLE001
            results.append((f"{name} (raised {type(exc).__name__})", False))

    def trigger(capacity):
        def build(c, clk):
            c.set("old", -1)                              # t=0, exp=10
            clk.advance(6)
            lives = [f"L{i}" for i in range(capacity - 1)]
            for k in lives:
                c.set(k, k)                               # t=6, exp=16 ; cache now full
            c.get("old")                                  # old -> MRU ; LRU is a LIVE entry
            clk.advance(5)                                # t=11 : old expired, lives still live
            c.set("NEW", 99)                              # reclaim 'old', keep every live entry
            return all(c.get(k) == k for k in lives) and c.get("NEW") == 99
        return build

    scenario("live-LRU survives, expired-MRU (cap2)", 2, trigger(2))
    scenario("live-LRU survives, expired-MRU (cap3)", 3, trigger(3))
    scenario("live-LRU survives, expired-MRU (cap4)", 4, trigger(4))
    scenario("live-LRU survives, expired-MRU (cap5)", 5, trigger(5))

    def two_expired(c, clk):
        c.set("o1", -1); c.set("o2", -2)                  # t=0, exp=10
        clk.advance(6)
        c.set("Lx", 7)                                    # t=6, exp=16 ; full {o1,o2,Lx}
        c.get("o1"); c.get("o2")                          # both expired-to-be -> MRU; LRU=Lx (live)
        clk.advance(5)                                    # t=11 : o1,o2 expired, Lx live
        c.set("NEW", 99)
        return c.get("Lx") == 7 and c.get("NEW") == 99
    scenario("two expired reclaimed, live survives (cap3)", 3, two_expired)

    def exact_live_set(c, clk):
        c.set("old", -1)                                  # exp=10
        clk.advance(6); c.set("a", 1); c.set("b", 2)      # exp=16 ; full {old,a,b}
        c.get("old")                                      # old MRU ; LRU=a (live)
        clk.advance(5)                                    # old expired
        c.set("c", 3)
        return set(c.keys()) == {"a", "b", "c"}
    scenario("exact live set {a,b,c}", 3, exact_live_set)

    def anti_cheat_lru(c, clk):
        # all live → LRU eviction must still drop the LRU (a hack that disables eviction fails).
        c.set("a", 1); c.set("b", 2); c.set("c", 3)
        c.set("d", 4)
        return c.get("a") is None and c.get("d") == 4 and len(c.keys()) == 3
    scenario("anti-cheat: LRU still evicts (all live)", 3, anti_cheat_lru)

    def anti_cheat_capacity(c, clk):
        # capacity must still bound live entries (a hack that enlarges capacity fails).
        for i in range(6):
            c.set(i, i)
        return len(c.keys()) == 3
    scenario("anti-cheat: capacity still bounds to 3", 3, anti_cheat_capacity)

    return results


def verify(workdir: Path, task_meta: dict) -> dict[str, Any]:
    cell = Path(workdir) / SEED_REL
    files_written = [f for f in REQUIRED if (cell / f).exists()]

    if "cache.py" not in files_written:
        return {"pass": False, "evidence": "cache.py missing",
                "details": {"files_written": files_written, "pass_count": 0, "expected": EXPECTED}}

    # Existing suite must stay green.
    try:
        passed, failed = _run_existing_suite(cell)
    except Exception as exc:  # noqa: BLE001
        passed, failed = 0, EXISTING_WEIGHT
    existing_score = max(0, EXISTING_WEIGHT - failed) if failed else EXISTING_WEIGHT
    existing_score = min(existing_score, EXISTING_WEIGHT)

    # Hidden regression.
    try:
        TTLCache = _load_cache(cell)
        reg = _regression_checks(TTLCache)
    except Exception as exc:  # noqa: BLE001
        return {"pass": False, "evidence": f"cache import failed: {exc}"[:300],
                "details": {"files_written": files_written, "pass_count": existing_score, "expected": EXPECTED}}
    reg_pass = sum(1 for _n, ok in reg if ok)

    pass_count = existing_score + reg_pass
    fails = [n for n, ok in reg if not ok]
    evidence = (
        f"existing {existing_score}/{EXISTING_WEIGHT} green ({failed} failed); "
        f"regression {reg_pass}/{N_REGRESSION}"
        + (f"; FAILED: {', '.join(fails[:4])}" if fails else " (all regression scenarios pass)")
    )
    return {
        "pass": pass_count == EXPECTED,
        "evidence": evidence[:480],
        "details": {"files_written": files_written, "pass_count": pass_count, "expected": EXPECTED},
    }
