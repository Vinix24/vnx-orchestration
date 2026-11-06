"""Tests for scripts/ci/profile_a_slow_tests.py (track test-suite-kosten, TSK-0).

Every test writes JUnit XML into ``tmp_path`` and runs the real CLI in a
subprocess: the sums, counts, median, top list, fixed baseline set and module
exclusions are read from the tool's own JSON (and, once, its text) output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "ci" / "profile_a_slow_tests.py"
)


def _write_junit(path: Path, cases: list[tuple[str, str, float]]) -> Path:
    body = "".join(
        f'<testcase classname="{classname}" name="{name}" time="{seconds}"/>'
        for classname, name, seconds in cases
    )
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<testsuites><testsuite>{body}</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _json(*args: str) -> dict:
    result = _run(*args, "--json")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout)


def _single_file(tmp_path: Path) -> Path:
    return _write_junit(
        tmp_path / "junit.xml",
        [
            ("tests.test_alpha", "test_fast", 0.05),
            ("tests.test_alpha", "test_slow", 3.0),
            ("tests.test_alpha.TestThing", "test_on_threshold", 2.0),
            ("tests.test_beta", "test_slower", 5.5),
        ],
    )


def test_single_file_sums_and_counts(tmp_path: Path):
    report = _json(str(_single_file(tmp_path)))

    row = report["files"][0]
    assert row["test_count"] == 4
    assert row["total_time"] == 10.55
    assert row["slow_count"] == 2
    assert row["slow_time"] == 8.5
    assert report["threshold"] == 2.0


def test_text_output_names_the_sums(tmp_path: Path):
    result = _run(str(_single_file(tmp_path)))

    assert result.returncode == 0, result.stderr
    assert "  tests: 4  total: 10.6 s" in result.stdout
    assert "  above 2.0 s: 2 tests, 8.5 s" in result.stdout


def test_median_over_three_files(tmp_path: Path):
    first = _single_file(tmp_path)
    second = _write_junit(
        tmp_path / "second.xml", [("tests.test_b", f"test_{i}", 4.0) for i in range(5)]
    )
    third = _write_junit(
        tmp_path / "third.xml", [("tests.test_c", f"test_{i}", 2.5) for i in range(9)]
    )

    report = _json(str(first), str(second), str(third))

    assert [row["slow_count"] for row in report["files"]] == [2, 5, 9]
    assert report["median"] == {"slow_time": 20.0, "slow_count": 5}


def test_top_groups_class_and_module_level_tests(tmp_path: Path):
    junit = _write_junit(
        tmp_path / "junit.xml",
        [
            ("tests.test_gamma.TestGamma", "test_a", 3.0),
            ("tests.test_gamma", "test_b", 2.0),
            ("tests.test_delta", "test_c", 4.0),
            ("tests.test_delta.Delta", "test_d", 0.5),
        ],
    )

    report = _json(str(junit), "--top", "2")

    assert report["top"] == [
        {"module": "tests.test_gamma", "seconds": 5.0},
        {"module": "tests.test_delta", "seconds": 4.5},
    ]


def test_missing_file_exits_2_with_nothing_on_stdout(tmp_path: Path):
    result = _run(str(tmp_path / "absent.xml"))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "file not found" in result.stderr


def test_broken_xml_exits_2_with_nothing_on_stdout(tmp_path: Path):
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites><testcase", encoding="utf-8")

    result = _run(str(broken))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "does not parse" in result.stderr


def test_xml_without_testcases_exits_2_with_nothing_on_stdout(tmp_path: Path):
    empty = tmp_path / "empty.xml"
    empty.write_text("<testsuites><testsuite/></testsuites>", encoding="utf-8")

    result = _run(str(empty))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "no testcase elements" in result.stderr


def test_testcase_without_time_is_refused(tmp_path: Path):
    bad = tmp_path / "no_time.xml"
    bad.write_text(
        '<testsuites><testcase classname="tests.test_x" name="test_y"/></testsuites>',
        encoding="utf-8",
    )

    result = _run(str(bad))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "without a time attribute" in result.stderr


def test_top_zero_is_refused(tmp_path: Path):
    result = _run(str(_single_file(tmp_path)), "--top", "0")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--top must be at least 1" in result.stderr


def test_baseline_fixes_the_set(tmp_path: Path):
    baseline = _write_junit(
        tmp_path / "baseline.xml",
        [
            ("tests.test_base", "test_keep", 4.0),
            ("tests.test_base", "test_drop", 3.0),
            ("tests.test_base", "test_fast", 0.5),
        ],
    )
    later = _write_junit(
        tmp_path / "later.xml",
        [
            ("tests.test_base", "test_keep", 6.0),
            ("tests.test_base", "test_drop", 0.5),
            ("tests.test_base", "test_new", 9.0),
        ],
    )
    thinner = _write_junit(
        tmp_path / "thinner.xml",
        [("tests.test_base", "test_keep", 1.0), ("tests.test_base", "test_new", 5.0)],
    )

    report = _json("--baseline", str(baseline), str(later), str(thinner))

    first, second = report["files"]
    assert first["baseline_total"] == 2
    assert first["baseline_count"] == 2
    assert first["baseline_time"] == 6.5
    assert first["missing"] == []
    assert second["baseline_count"] == 1
    assert second["baseline_time"] == 1.0
    assert second["missing"] == ["tests.test_base::test_drop"]
    assert report["median"] == {"baseline_time": 3.75, "baseline_count": 1.5}


def test_exclude_module_leaves_every_sum_and_is_reported(tmp_path: Path):
    baseline = _write_junit(
        tmp_path / "baseline.xml",
        [
            ("tests.test_base", "test_keep", 4.0),
            ("tests.test_base", "test_drop", 3.0),
            ("tests.test_noise", "test_noise", 5.0),
        ],
    )
    later = _write_junit(
        tmp_path / "later.xml",
        [
            ("tests.test_base", "test_keep", 6.0),
            ("tests.test_base", "test_drop", 0.5),
            ("tests.test_base", "test_new", 9.0),
            ("tests.test_noise", "test_noise", 7.0),
        ],
    )

    unfiltered = _json("--baseline", str(baseline), str(later))
    filtered = _json(
        "--baseline",
        str(baseline),
        "--exclude-module",
        "tests.test_noise",
        str(later),
    )

    assert unfiltered["files"][0]["slow_time"] == 22.0
    assert unfiltered["files"][0]["baseline_time"] == 13.5
    row = filtered["files"][0]
    assert row["slow_time"] == 15.0
    assert row["baseline_time"] == 6.5
    assert row["baseline_total"] == 2
    assert row["test_count"] == 3
    assert filtered["excluded"] == [
        {
            "module": "tests.test_noise",
            "per_file_seconds": {str(later): 7.0},
            "total_seconds": 7.0,
        }
    ]
