"""OI-2599: the conftest tripwire refuses a signal aimed at the test's own group.

Signal 0 is the only signal these tests send to a group they do not own: it
checks, it never delivers.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from provider_spawns import codex_spawn


class TestTripwireRefusesOwnGroup:
    def test_killpg_zero_is_refused(self):
        with pytest.raises(AssertionError):
            os.killpg(0, 0)

    def test_killpg_init_group_is_refused(self):
        with pytest.raises(AssertionError):
            os.killpg(1, 0)

    def test_killpg_own_group_is_refused(self):
        with pytest.raises(AssertionError):
            os.killpg(os.getpgrp(), 0)

    def test_kill_pid_zero_is_refused(self):
        with pytest.raises(AssertionError):
            os.kill(0, 0)

    def test_kill_negative_own_group_is_refused(self):
        with pytest.raises(AssertionError):
            os.kill(-os.getpgrp(), 0)


class TestTripwirePassesForeignGroups:
    def test_killpg_of_a_real_child_group_passes_and_the_child_dies(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            child_pgid = os.getpgid(child.pid)
            assert child_pgid != os.getpgrp()
            os.killpg(child_pgid, signal.SIGTERM)
            assert child.wait(timeout=10) == -signal.SIGTERM
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


class TestRunnerCondition:
    def test_kill_proc_with_pgid_zero_trips_instead_of_signalling(self):
        proc = MagicMock()
        proc.pid = 99
        with patch.object(codex_spawn.os, "getpgid", return_value=0):
            with pytest.raises(AssertionError):
                codex_spawn._kill_proc(proc)
