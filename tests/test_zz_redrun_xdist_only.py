"""TEMPORARY red run (a) of D1 (track ci-profile-a-xdist): fails only under xdist.

Lives only on the draft red-run branch dispatch/20261016-xd-d1-shadow-redrun,
which is closed and deleted after the run.
"""

import os


def test_fails_only_under_xdist():
    assert os.environ.get("PYTEST_XDIST_WORKER") is None
