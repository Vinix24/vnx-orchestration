"""OI-2903: the receipt processor logs an already processed report once, not every cycle."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "receipt_processor"
SUPERVISOR = REPO / "scripts" / "receipt_processor_supervisor.sh"


def test_prefilter_summary_counts_exclusions_on_every_cycle(tmp_path):
    from test_receipt_processor_poll_prefilter import reports, run_cycle

    reports(tmp_path, old=2, fresh=0)
    result = run_cycle(tmp_path, "_poll_new_reports_cycle 1 100\n_poll_new_reports_cycle 2 100")
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Skipped 2 too-old report(s) this cycle") == 2
    assert "Report too old:" not in result.stderr


def _run_cycles(tmp_path, cycles, report_names, processed_names):
    reports = tmp_path / "reports"
    reports.mkdir()
    hashes = tmp_path / "hashes.txt"
    log_file = tmp_path / "processing.log"
    hashes.write_text("")
    for name in report_names:
        (reports / name).write_text(f"body of {name}\n")
    for name in processed_names:
        digest = subprocess.run(
            ["shasum", "-a", "256", str(reports / name)], capture_output=True, text=True, check=True
        ).stdout.split()[0]
        with hashes.open("a") as fh:
            fh.write(digest + "\n")
    script = f"""
set -u
source "{LIB}/rp_logging.sh"
source "{LIB}/rp_dedup.sh"
_spr_get_cutoff_seconds() {{ echo 0; }}
_sha256() {{ shasum -a 256 "$1" | cut -d' ' -f1; }}
for _c in $(seq 1 {cycles}); do
  for f in "{reports}"/*.md; do should_process_report "$f" && echo "PROCESS $(basename "$f")"; done
done
true
"""
    env = {**os.environ, "PROCESSING_LOG": str(log_file), "PROCESSED_HASHES": str(hashes)}
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, log_file.read_text()


def test_already_processed_logged_once_over_three_cycles(tmp_path):
    out, log = _run_cycles(tmp_path, 3, ["done.md"], ["done.md"])
    assert out == ""
    assert log.count("Already processed: done.md") == 1
    assert "Report accepted" not in log


def test_each_processed_report_logged_once_by_name(tmp_path):
    _, log = _run_cycles(tmp_path, 3, ["a.md", "b.md"], ["a.md", "b.md"])
    assert log.count("Already processed: a.md") == 1
    assert log.count("Already processed: b.md") == 1


def test_new_report_still_accepted_and_returned(tmp_path):
    out, log = _run_cycles(tmp_path, 1, ["new.md", "old.md"], ["old.md"])
    assert out.split() == ["PROCESS", "new.md"]
    assert log.count("Report accepted: new.md") == 1
    assert "Report accepted: old.md" not in log


def test_unreadable_report_still_errors(tmp_path):
    reports = tmp_path / "r"
    reports.mkdir()
    script = f"""
source "{LIB}/rp_logging.sh"; source "{LIB}/rp_dedup.sh"
should_process_report "{reports}/missing.md"; echo "rc=$?"
"""
    env = {**os.environ, "PROCESSING_LOG": str(tmp_path / "p.log")}
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert "rc=1" in proc.stdout
    assert "[ERROR] Cannot get modification time" in (tmp_path / "p.log").read_text()


def _rotation_snippet():
    text = SUPERVISOR.read_text()
    start = text.index("_rotate_supervisor_log()")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _rotate(tmp_path, size_mb, limit_env):
    log = tmp_path / "sup.log"
    with log.open("wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    old = tmp_path / "sup.log.1"
    old.write_text("older copy")
    env = {k: v for k, v in os.environ.items() if k != "VNX_SUPERVISOR_LOG_MAX_MB"}
    env.update(limit_env)
    script = f'{_rotation_snippet()}\n_rotate_supervisor_log "{log}"\n'
    subprocess.run(["bash", "-c", script], check=True, env=env)
    return log, old


def test_rotation_above_limit_replaces_old_copy(tmp_path):
    log, old = _rotate(tmp_path, 51, {})
    assert not log.exists()
    assert old.stat().st_size == 51 * 1024 * 1024
    assert not (tmp_path / "sup.log.2").exists()


def test_rotation_respects_env_limit(tmp_path):
    log, old = _rotate(tmp_path, 3, {"VNX_SUPERVISOR_LOG_MAX_MB": "2"})
    assert not log.exists() and old.stat().st_size == 3 * 1024 * 1024


def test_no_rotation_below_limit_or_bad_limit(tmp_path):
    log, old = _rotate(tmp_path, 1, {})
    assert log.exists() and old.read_text() == "older copy"
    log2, old2 = _rotate(tmp_path, 51, {"VNX_SUPERVISOR_LOG_MAX_MB": "junk"})
    assert not log2.exists()  # bad limit falls back to the 50 MB default


def test_rotation_with_gnu_style_stat(tmp_path):
    """GNU stat reads `-f %z` as --file-system plus a second operand: multi-line stdout, exit 1."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "stat"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-f" ]; then printf "  File: x\\n    ID: 0 Namelen: 255\\nBlock size: 4096\\n"; exit 1; fi\n'
        'if [ "$1" = "-c" ]; then wc -c < "$3" | tr -d " "; exit 0; fi\n'
        "exit 2\n"
    )
    fake.chmod(0o755)
    log = tmp_path / "sup.log"
    with log.open("wb") as fh:
        fh.truncate(51 * 1024 * 1024)
    env = {k: v for k, v in os.environ.items() if k != "VNX_SUPERVISOR_LOG_MAX_MB"}
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    script = f'{_rotation_snippet()}\n_rotate_supervisor_log "{log}"\n'
    subprocess.run(["bash", "-c", script], check=True, env=env)
    assert not log.exists()
    assert (tmp_path / "sup.log.1").stat().st_size == 51 * 1024 * 1024
