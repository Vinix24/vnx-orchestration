"""TEMPORARY red run (c) of D1 (track ci-profile-a-xdist): sleeps past a 2 minute step timeout.

170 s stays under the per-test --timeout=180, so only the step timeout can end
it. Lives only on the draft red-run branch, which is closed and deleted after
the run.
"""

import time


def test_sleeps_past_the_reduced_step_timeout():
    time.sleep(170)
