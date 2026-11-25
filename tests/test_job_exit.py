"""Tests for scripts/lib/job_exit.py (OI-3079): crash is 70, one closing line."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import job_exit
from job_exit import EXIT_CRASH, argv_has_flag, run_job

STATUSES = {0: "ok", 1: "finding", 2: "unmeasurable"}


def _last(capsys):
    out = capsys.readouterr()
    return out.out.splitlines()[-1], out.err


def test_crash_is_70_with_traceback_and_closing_line(capsys):
    def main():
        print("partial output")
        raise RuntimeError("boom")

    assert run_job("x", main, STATUSES) == EXIT_CRASH == 70
    last, err = _last(capsys)
    assert last == "x: crash (exit 70)"
    assert "Traceback" in err and "RuntimeError: boom" in err


@pytest.mark.parametrize("code,word", [(0, "ok"), (1, "finding"), (2, "unmeasurable"), (5, "exit_5")])
def test_returned_codes_map_to_status_words(capsys, code, word):
    assert run_job("x", lambda: code, STATUSES) == code
    assert _last(capsys)[0] == f"x: {word} (exit {code})"


def test_systemexit_2_is_usage_error(capsys):
    def main():
        raise SystemExit(2)

    assert run_job("x", main, STATUSES) == 2
    assert _last(capsys)[0] == "x: usage_error (exit 2)"


def test_systemexit_int_code_is_the_exit_code(capsys):
    def main():
        raise SystemExit(1)

    assert run_job("x", main, STATUSES) == 1
    assert _last(capsys)[0] == "x: finding (exit 1)"


def test_closing_line_off_leaves_stdout_untouched(capsys):
    def main():
        print('{"a": 1}')
        return 1

    assert run_job("x", main, STATUSES, closing_line=False) == 1
    assert capsys.readouterr().out == '{"a": 1}\n'


def test_crash_without_closing_line_still_70_and_stdout_clean(capsys):
    def main():
        raise ValueError("bad")

    assert run_job("x", main, STATUSES, closing_line=False) == 70
    out = capsys.readouterr()
    assert out.out == "" and "Traceback" in out.err


def test_keyboard_interrupt_passes_through():
    def main():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_job("x", main, STATUSES)


def test_argv_has_flag_matches_exact_and_equals_form():
    assert argv_has_flag(["--json"], "--json")
    assert argv_has_flag(["--reach=/t"], "--reach")
    assert not argv_has_flag(["--jsonx", "--reachable"], "--json", "--reach")
