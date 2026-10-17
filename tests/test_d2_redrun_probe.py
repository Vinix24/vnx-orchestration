"""Red-run probe of D2 (draft branch only), case (e): fails in the serial sweep only."""

import os


def test_probe_passes_only_under_xdist():
    assert os.environ.get("PYTEST_XDIST_WORKER"), "red run (e): this test fails serially only"
