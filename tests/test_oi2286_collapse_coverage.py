"""OI-2286 r2: the coverage a gate records is the block the reviewer got, and a
pass on an empty block cannot happen.

codex_gate on PR #2170 (f06f6f9b): ``collapse_data_sections`` took review
input out of the prompt while the callers kept measuring the full diff. In
isolated glm_gate and kimi_gate runs an 11.535-char CSV-only diff produced an
empty diff block, and both accepted a mocked pass with ``diff_chars=11535``
and ``diff_truncated=false``.

Three rules, one guard per gate path (glm_gate, kimi_gate, the harness lane
through gate_runner, codex_gate):

1. A diff of only collapsible data sections goes into the block whole.
2. ``execution_depth`` measures the block after the collapse and names each
   collapsed section (path, chars, sha256 prefix). Collapsing is not a cut:
   it never makes a run ``diff_truncated`` or ``partial_review``.
3. A cut keeps its meaning: an oversized data-only diff is still booked
   ``partial_review``.
4. A lock file is code, not data (OI-2290, codex_gate on 83f13d74): it
   decides which code gets installed, so a ``package-lock.json`` section that
   moves a download URL reaches the reviewer whole, at any size, and nothing
   is booked under ``collapsed_sections``.

Real code throughout; only the provider call (or the codex process), the diff
fetch and the gh identity lookups are stubbed.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
for _path in (SCRIPTS_DIR, SCRIPTS_DIR / "lib", TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import gate_depth
import gate_prompt
import gate_runner
from gate_runner import GateRunner
from gate_status import is_pass
from review_seat_support import open_pr_in_ledger

# The real config_runtime isolation: env vars alone do not stop a populated
# project_config DB on a dev machine from overriding a gate's cap.
from test_gate_lane_contract import isolate_config_runtime

BRANCH = "feature/oi2286"
HEAD_SHA = "c011a95eda7a"

PASS_REPORT = (
    "Reviewed the diff.\nChecked the changed functions.\nNo issues found.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

# A real codex_gate run that ends in a bare pass verdict (see test_gate_runner).
CODEX_PASS_STREAM = (
    TESTS_DIR / "fixtures" / "gate_verdict" / "pr-1869-codex_gate-bare-verdict.ndjson"
).read_bytes()

ROW_MARK = "measured output column"

CODE_SECTION = (
    "diff --git a/scripts/measure.py b/scripts/measure.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/scripts/measure.py\n"
    "+++ b/scripts/measure.py\n"
    "@@ -1 +1,2 @@\n"
    " import csv\n"
    "+ROWS = 151\n"
)


def _data_section(path: str, rows: int) -> str:
    body = "".join(f"+{i},git log --oneline -1,abc {ROW_MARK},0\n" for i in range(rows))
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{rows} @@\n"
        f"{body}"
    )


CSV_PATH = "docs/investigations/2026-10-20-golf-1-hermeting.csv"
NDJSON_PATH = "state/events.ndjson"
CSV = _data_section(CSV_PATH, 200)
NDJSON = _data_section(NDJSON_PATH, 60)
DATA_ONLY_DIFF = CSV + NDJSON
MIXED_DIFF = CODE_SECTION + CSV

# WIDE holds every diff here whole. NARROW sits between the block of the mixed
# diff and the diff itself: the cap a full-diff measurement trips on and a
# block measurement does not. The data-only diff is over NARROW.
WIDE_CAP = 400_000
NARROW_CAP = 8_000
assert min(len(CSV), len(NDJSON)) > gate_prompt.COLLAPSE_MIN_SECTION_CHARS
assert len(CODE_SECTION) < NARROW_CAP < len(MIXED_DIFF)
assert NARROW_CAP < len(CSV) < len(DATA_ONLY_DIFF) < WIDE_CAP


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


CSV_ENTRY = {"path": CSV_PATH, "chars": len(CSV), "sha256_prefix": _sha12(CSV)}

# The six lock file names the collapse rule used to take for data (OI-2290).
LOCK_FILE_NAMES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock", "Cargo.lock")
OLD_URL = "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz"
NEW_URL = "https://mirror.example.invalid/left-pad/-/left-pad-1.3.0.tgz"


def _lock_section(path: str) -> str:
    """A lock file section that moves one package's download URL, padded with
    unchanged entries past the collapse threshold."""
    context = "".join(
        f'       "resolved": "https://registry.npmjs.org/pkg-{i}/-/pkg-{i}-1.0.{i}.tgz",\n'
        for i in range(40)
    )
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,41 +1,41 @@\n"
        f"{context}"
        f'-      "resolved": "{OLD_URL}",\n'
        f'+      "resolved": "{NEW_URL}",\n'
    )


LOCK_PATH = "web/package-lock.json"
LOCK_DIFF = CODE_SECTION + _lock_section(LOCK_PATH)
assert len(_lock_section(LOCK_PATH)) > gate_prompt.COLLAPSE_MIN_SECTION_CHARS
assert len(LOCK_DIFF) < WIDE_CAP


def _block(prompt: str) -> str:
    """The text between the block markers: what the reviewer got of the diff."""
    begin = prompt.index(gate_prompt.BEGIN_DIFF_MARKER) + len(gate_prompt.BEGIN_DIFF_MARKER) + 1
    return prompt[begin:prompt.index(gate_prompt.END_DIFF_MARKER) - 1]


def _assert_whole_data_only_block(prompt: str) -> None:
    assert _block(prompt) == DATA_ONLY_DIFF, "a data-only diff goes into the block whole"
    assert gate_prompt._COLLAPSE_NOTICE_HEAD not in prompt
    assert gate_prompt.COLLAPSED_SCAN_NOTE not in prompt


def _assert_mixed_block(prompt: str) -> None:
    assert _block(prompt) == CODE_SECTION
    assert f"{CSV_PATH}: +200 -0 lines, {len(CSV)} chars, sha256 {_sha12(CSV)}" in prompt
    assert ROW_MARK not in prompt


def _assert_full_coverage(depth: dict, block: str, collapsed: list) -> None:
    assert depth["diff_chars"] == len(block.strip()) > 0
    assert depth["diff_truncated"] is False
    assert depth["truncated_files"] == []
    assert depth["collapsed_sections"] == collapsed


def _assert_lock_file_reviewed_whole(prompt: str, depth: dict) -> None:
    block = _block(prompt)
    assert NEW_URL in block and OLD_URL in block, "the reviewer must see the URL the lock file moves"
    assert block == LOCK_DIFF, "a lock file is code: its section goes into the block whole"
    assert gate_prompt._COLLAPSE_NOTICE_HEAD not in prompt
    _assert_full_coverage(depth, block, [])


# --- The collapse and the measurement themselves


def test_a_data_only_diff_is_not_collapsed():
    assert gate_prompt.collapse_data_sections(DATA_ONLY_DIFF) == (DATA_ONLY_DIFF, [])
    preamble = "From 0000000 Mon Sep 17 00:00:00 2001\n\n"
    assert gate_prompt.collapse_data_sections(preamble + CSV) == (preamble + CSV, []), (
        "text before the first header is no file section: it cannot carry the block alone"
    )


def test_the_block_of_a_data_only_diff_is_the_whole_diff():
    prompt = gate_prompt.build_review_prompt(
        gate_name="glm_gate", pr="2170", diff_text=DATA_ONLY_DIFF,
        verdict_contract="CONTRACT", max_chars=0,
    )
    _assert_whole_data_only_block(prompt)


@pytest.mark.parametrize("name", LOCK_FILE_NAMES)
def test_a_lock_file_section_stays_whole_beside_code(name):
    """Beside a code section, so the data-only rule cannot be what keeps it."""
    section = _lock_section(name)
    diff = CODE_SECTION + section
    assert gate_prompt.collapse_data_sections(diff) == (diff, [])
    prompt = gate_prompt.build_review_prompt(
        gate_name="codex_gate", pr="2170", diff_text=diff,
        verdict_contract="CONTRACT", max_chars=0,
    )
    assert _block(prompt) == diff, f"{name} decides which code gets installed and must be reviewed whole"
    assert gate_prompt._COLLAPSE_NOTICE_HEAD not in prompt
    assert gate_depth.diff_coverage(diff, 0)["collapsed_sections"] == []


def test_diff_coverage_measures_the_block_and_names_what_was_collapsed():
    coverage = gate_depth.diff_coverage(MIXED_DIFF, NARROW_CAP)
    assert coverage == {
        "diff_chars": len(CODE_SECTION.strip()),
        "diff_truncated": False,
        "diff_limit": NARROW_CAP,
        "truncated_files": [],
        "collapsed_sections": [CSV_ENTRY],
    }
    assert gate_depth.diff_coverage(DATA_ONLY_DIFF, 0)["collapsed_sections"] == []


def test_a_cut_after_the_collapse_is_measured_on_the_block():
    """The cut lands in the block, so the cut files are files of the block: a
    collapsed CSV ahead of an oversized code section is not one of them."""
    big_code = "diff --git a/scripts/big.py b/scripts/big.py\n" + "+x = 1\n" * 2000
    assert len(big_code) > NARROW_CAP
    coverage = gate_depth.diff_coverage(CSV + big_code, NARROW_CAP)
    assert coverage["diff_truncated"] is True
    assert coverage["truncated_files"] == ["scripts/big.py"]
    assert coverage["diff_chars"] == len(big_code.strip())
    assert coverage["collapsed_sections"] == [CSV_ENTRY]


def test_collapsed_sections_survive_the_record_round_trip():
    depth = gate_depth.single_shot_depth_from_coverage(gate_depth.diff_coverage(MIXED_DIFF, NARROW_CAP))
    assert depth.mode == "single_shot" and depth.parsed is True
    assert depth.collapsed_sections == (CSV_ENTRY,)
    assert depth.to_dict()["collapsed_sections"] == [CSV_ENTRY]
    assert gate_depth.from_dict(json.loads(json.dumps(depth.to_dict()))) == depth
    assert gate_depth.coverage_gap(depth) == "", "collapsing is not a cut"


def test_a_record_without_collapsed_sections_reads_as_nothing_collapsed():
    """A coverage or depth record written before OI-2286 has no such key."""
    coverage = {"diff_chars": 10, "diff_truncated": False, "diff_limit": 0, "truncated_files": []}
    assert gate_depth.single_shot_depth_from_coverage(coverage).collapsed_sections == ()
    assert gate_depth.from_dict({"parsed": True, "diff_chars": 3}).collapsed_sections == ()


# --- glm_gate.py and kimi_gate.py, standalone


def _run_standalone(module, gate, tmp_path, monkeypatch, *, diff, pr):
    data_dir = tmp_path / "data"
    sent: list = []

    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            sent.append(instruction)
            reports = data_dir / "unified_reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / f"{dispatch_id}.md").write_text(PASS_REPORT, encoding="utf-8")
            return PASS_REPORT
        return _dispatch

    monkeypatch.setattr(module, "_get_diff", lambda pr_arg, diff_file: diff)
    monkeypatch.setattr(module, "_make_default_dispatcher", _make)
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: BRANCH)
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: HEAD_SHA)
    open_pr_in_ledger(data_dir / "state", pr)
    rc = module.main(["--pr", pr, "--data-dir", str(data_dir)])
    results_dir = data_dir / "state" / "review_gates" / "results"
    record = json.loads((results_dir / f"pr-{pr}-{gate}.json").read_text(encoding="utf-8"))
    assert len(sent) == 1
    return rc, record, sent[0]


@pytest.fixture(params=["glm_gate", "kimi_gate"])
def standalone_gate(request, monkeypatch, isolate_config_runtime):
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)
    monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)
    return __import__(request.param), request.param


def _set_cap(monkeypatch, gate: str, cap: int) -> None:
    monkeypatch.setenv(f"VNX_{gate.upper()}_MAX_DIFF_CHARS", str(cap))


def test_standalone_pass_on_a_data_only_diff_covers_the_whole_block(
    standalone_gate, tmp_path, monkeypatch,
):
    module, gate = standalone_gate
    _set_cap(monkeypatch, gate, WIDE_CAP)
    rc, record, prompt = _run_standalone(module, gate, tmp_path, monkeypatch, diff=DATA_ONLY_DIFF, pr="2170")

    _assert_whole_data_only_block(prompt)
    _assert_full_coverage(record["execution_depth"], _block(prompt), [])
    assert (record["status"], rc) == ("pass", 0)


def test_standalone_pass_on_a_mixed_diff_records_the_block_and_the_collapsed_csv(
    standalone_gate, tmp_path, monkeypatch,
):
    module, gate = standalone_gate
    _set_cap(monkeypatch, gate, NARROW_CAP)
    rc, record, prompt = _run_standalone(module, gate, tmp_path, monkeypatch, diff=MIXED_DIFF, pr="2171")

    _assert_mixed_block(prompt)
    _assert_full_coverage(record["execution_depth"], _block(prompt), [CSV_ENTRY])
    assert (record["status"], rc) == ("pass", 0), "collapsing is no ground for partial_review"
    assert record.get("reason") != "diff_truncated"


def test_standalone_oversized_data_only_diff_is_still_a_partial_review(
    standalone_gate, tmp_path, monkeypatch,
):
    module, gate = standalone_gate
    _set_cap(monkeypatch, gate, NARROW_CAP)
    rc, record, prompt = _run_standalone(module, gate, tmp_path, monkeypatch, diff=DATA_ONLY_DIFF, pr="2172")

    assert _block(prompt).startswith(DATA_ONLY_DIFF[:NARROW_CAP])
    assert gate_prompt.TRUNCATION_NOTICE in prompt
    depth = record["execution_depth"]
    assert (depth["diff_chars"], depth["diff_truncated"], depth["diff_limit"]) == (
        len(DATA_ONLY_DIFF.strip()), True, NARROW_CAP)
    assert depth["truncated_files"] == [CSV_PATH, NDJSON_PATH]
    assert depth["collapsed_sections"] == []
    assert (record["status"], record["reason"]) == ("partial_review", "diff_truncated")
    assert rc != 0


def test_standalone_pass_on_a_lock_file_diff_reviewed_the_lock_file(
    standalone_gate, tmp_path, monkeypatch,
):
    module, gate = standalone_gate
    _set_cap(monkeypatch, gate, WIDE_CAP)
    rc, record, prompt = _run_standalone(module, gate, tmp_path, monkeypatch, diff=LOCK_DIFF, pr="2173")

    _assert_lock_file_reviewed_whole(prompt, record["execution_depth"])
    assert (record["status"], rc) == ("pass", 0)


# --- The harness lane through gate_runner


@pytest.fixture
def runner_env(tmp_path, monkeypatch, isolate_config_runtime):
    data_dir = tmp_path / "project" / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    (state_dir / "review_gates" / "requests").mkdir(parents=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)
    monkeypatch.setattr(gate_runner, "fetch_branch", lambda *a, **kw: None)
    monkeypatch.setattr(gate_runner, "create_gate_worktree", lambda **kw: tmp_path / "_gate_worktree")
    monkeypatch.setattr(gate_runner, "remove_gate_worktree", lambda *a, **kw: None)
    return {"state_dir": state_dir, "reports_dir": reports_dir,
            "results_dir": state_dir / "review_gates" / "results"}


def _run_harness_lane(runner_env, monkeypatch, *, diff, pr):
    sent: list = []

    def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None,
                total_deadline_seconds=None):
        def dispatch(provider, model, instruction, dispatch_id):
            sent.append(instruction)
            return PASS_REPORT
        return dispatch

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
    monkeypatch.setattr(GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda pr_number: diff))
    payload = {"gate": "glm_gate", "status": "requested", "branch": BRANCH, "pr_number": pr,
               "report_path": str(runner_env["reports_dir"] / f"glm-gate-pr{pr}.md")}
    runner = GateRunner(state_dir=runner_env["state_dir"], reports_dir=runner_env["reports_dir"])
    result = runner.run(gate="glm_gate", request_payload=payload, pr_number=pr)
    saved = json.loads((runner_env["results_dir"] / f"pr-{pr}-glm_gate.json").read_text())
    assert len(sent) == 1
    return result, saved, sent[0]


def test_harness_lane_pass_on_a_data_only_diff_covers_the_whole_block(runner_env, monkeypatch):
    _set_cap(monkeypatch, "glm_gate", WIDE_CAP)
    result, saved, prompt = _run_harness_lane(runner_env, monkeypatch, diff=DATA_ONLY_DIFF, pr=2170)

    _assert_whole_data_only_block(prompt)
    _assert_full_coverage(saved["execution_depth"], _block(prompt), [])
    assert result["status"] == saved["status"] == "completed"
    assert is_pass(saved)[0] is True


def test_harness_lane_pass_on_a_mixed_diff_records_the_block_and_the_collapsed_csv(
    runner_env, monkeypatch,
):
    _set_cap(monkeypatch, "glm_gate", NARROW_CAP)
    result, saved, prompt = _run_harness_lane(runner_env, monkeypatch, diff=MIXED_DIFF, pr=2171)

    _assert_mixed_block(prompt)
    _assert_full_coverage(saved["execution_depth"], _block(prompt), [CSV_ENTRY])
    assert result["status"] == saved["status"] == "completed", "collapsing is no ground for partial_review"
    assert saved.get("reason") != "diff_truncated"


def test_harness_lane_oversized_data_only_diff_is_still_a_partial_review(runner_env, monkeypatch):
    _set_cap(monkeypatch, "glm_gate", NARROW_CAP)
    result, saved, prompt = _run_harness_lane(runner_env, monkeypatch, diff=DATA_ONLY_DIFF, pr=2172)

    assert _block(prompt).startswith(DATA_ONLY_DIFF[:NARROW_CAP])
    depth = saved["execution_depth"]
    assert (depth["diff_chars"], depth["diff_truncated"]) == (len(DATA_ONLY_DIFF.strip()), True)
    assert depth["collapsed_sections"] == []
    assert result["status"] == saved["status"] == "partial_review"


def test_harness_lane_pass_on_a_lock_file_diff_reviewed_the_lock_file(runner_env, monkeypatch):
    _set_cap(monkeypatch, "glm_gate", WIDE_CAP)
    result, saved, prompt = _run_harness_lane(runner_env, monkeypatch, diff=LOCK_DIFF, pr=2173)

    _assert_lock_file_reviewed_whole(prompt, saved["execution_depth"])
    assert result["status"] == saved["status"] == "completed"
    assert is_pass(saved)[0] is True


# --- codex_gate through gate_runner (agentic, uncapped)


def _run_codex(runner_env, monkeypatch, *, diff, pr):
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")
    monkeypatch.setattr(GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda pr_number: diff))

    proc = MagicMock()
    proc.stdout.fileno.return_value = 10
    proc.stderr.fileno.return_value = 11
    proc.poll.return_value = 0
    proc.returncode = 0
    proc.pid = 77777
    reads = [0]

    def _read(fd, size):
        reads[0] += 1
        return CODEX_PASS_STREAM if fd == 10 and reads[0] == 1 else b""

    payload = {"gate": "codex_gate", "status": "requested", "branch": BRANCH, "pr_number": pr,
               "risk_class": "medium",
               "report_path": str(runner_env["reports_dir"] / f"codex-gate-pr{pr}.md")}
    runner = GateRunner(state_dir=runner_env["state_dir"], reports_dir=runner_env["reports_dir"])
    with patch("gate_runner.subprocess.Popen", return_value=proc), \
         patch("gate_runner.select.select", return_value=([], [], [])), \
         patch("gate_runner.os.read", side_effect=_read), \
         patch("gate_runner.os.getpgid", return_value=77777):
        result = runner.run(gate="codex_gate", request_payload=payload, pr_number=pr)
    saved = json.loads((runner_env["results_dir"] / f"pr-{pr}-codex_gate.json").read_text())
    prompt = proc.stdin.write.call_args[0][0].decode("utf-8")
    return result, saved, prompt


def test_codex_pass_on_a_data_only_diff_covers_the_whole_block(runner_env, monkeypatch):
    result, saved, prompt = _run_codex(runner_env, monkeypatch, diff=DATA_ONLY_DIFF, pr=2170)

    _assert_whole_data_only_block(prompt)
    _assert_full_coverage(saved["execution_depth"], _block(prompt), [])
    assert saved["execution_depth"]["diff_limit"] == 0
    assert result["status"] == saved["status"] == "completed"


def test_codex_pass_on_a_mixed_diff_records_the_block_and_the_collapsed_csv(runner_env, monkeypatch):
    result, saved, prompt = _run_codex(runner_env, monkeypatch, diff=MIXED_DIFF, pr=2171)

    _assert_mixed_block(prompt)
    _assert_full_coverage(saved["execution_depth"], _block(prompt), [CSV_ENTRY])
    assert result["status"] == saved["status"] == "completed"


def test_codex_pass_on_a_lock_file_diff_reviewed_the_lock_file(runner_env, monkeypatch):
    result, saved, prompt = _run_codex(runner_env, monkeypatch, diff=LOCK_DIFF, pr=2173)

    _assert_lock_file_reviewed_whole(prompt, saved["execution_depth"])
    assert saved["execution_depth"]["diff_limit"] == 0
    assert result["status"] == saved["status"] == "completed"
