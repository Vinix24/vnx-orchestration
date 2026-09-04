"""tests/test_launchd_project_scope.py — OI-1509/OI-1510 regression guard.

OI-1509/OI-1510 measured two real defects on this machine (2026-09-04):

  1. ``scripts/launchd/com.vnx.gate-obligation-runner.plist``'s Label was the
     bare literal ``com.vnx.gate-obligation-runner``, with no per-project
     suffix — even though its own EnvironmentVariables entry already carried
     ``${VNX_PROJECT_ID}``. Two projects installing this template both landed
     on the SAME launchd Label (and, via ``reload_plist.sh``'s old behavior,
     the SAME destination file): a second project's install unloaded the
     first project's job.

  2. ``com.vnx.receipt-processor`` had no launchd template, and therefore no
     per-project (or any) launchd-driven instance at all, for any project.

Live evidence measured while building this guard: ``launchctl list | grep
vnx`` showed BOTH ``com.vnx.gate-obligation-runner`` (bare — mission-control's
job) and ``com.vnx.gate-obligation-runner.vnx-dev`` (a hand-installed
workaround, produced by no script in this repo) loaded at the same time, and
no ``com.vnx.receipt-processor*`` label at all.

This module is the guard against both regressing: ``scripts/launchd/
launchd_project_scope.py`` checks (a) every required family's template
declares a project-scoped Label (static, real files, no injection needed —
this is the actual regression check) and (b) an injected ``launchctl list``
snapshot never carries a bare/malformed label for a required family and
always carries THIS project's own instance (synthetic fixtures — a test must
not measure whichever host it happens to run on, so platform/path/launchctl
output are all injected, never read from the real machine).
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Iterable, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_DIR = REPO_ROOT / "scripts" / "launchd"

sys.path.insert(0, str(LAUNCHD_DIR))
import launchd_project_scope as lps  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PLIST_TEMPLATE = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key><string>{label}</string>
      <key>ProgramArguments</key>
      <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>echo hi</string>
      </array>
    </dict>
    </plist>
    """
)


def _write_template(directory: Path, family: str, label: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{family}.plist"
    path.write_text(_PLIST_TEMPLATE.format(label=label), encoding="utf-8")
    return path


def _launchctl_text(entries: Iterable[Tuple[str, str, int]]) -> str:
    """Build 'launchctl list'-shaped text: PID<TAB>Status<TAB>Label per line,
    plus the header line real launchctl always emits first."""
    lines = ["PID\tStatus\tLabel"]
    for label, pid, status in entries:
        lines.append(f"{pid}\t{status}\t{label}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# check_template_contract — real repo templates (the actual regression guard)
# ---------------------------------------------------------------------------


def test_required_families_constant_is_not_empty() -> None:
    # Nul-is-eerst-een-meetfout: every "ok" result below is only meaningful
    # if the family list actually has entries to check.
    assert len(lps.REQUIRED_PER_PROJECT_FAMILIES) == 2
    assert "com.vnx.gate-obligation-runner" in lps.REQUIRED_PER_PROJECT_FAMILIES
    assert "com.vnx.receipt-processor" in lps.REQUIRED_PER_PROJECT_FAMILIES


def test_real_repo_launchd_templates_are_per_project_scoped() -> None:
    result = lps.check_template_contract(LAUNCHD_DIR)
    assert result["ok"] is True, result["violations"]
    labels = {c["family"]: c["label"] for c in result["checked"]}
    assert labels["com.vnx.gate-obligation-runner"] == "com.vnx.gate-obligation-runner.${VNX_PROJECT_ID}"
    assert labels["com.vnx.receipt-processor"] == "com.vnx.receipt-processor.${VNX_PROJECT_ID}"


def test_check_template_contract_flags_a_bare_label(tmp_path: Path) -> None:
    _write_template(tmp_path, "com.vnx.gate-obligation-runner", "com.vnx.gate-obligation-runner")
    _write_template(
        tmp_path, "com.vnx.receipt-processor", "com.vnx.receipt-processor.${VNX_PROJECT_ID}"
    )
    result = lps.check_template_contract(tmp_path)
    assert result["ok"] is False
    kinds = {(v["family"], v["kind"]) for v in result["violations"]}
    assert ("com.vnx.gate-obligation-runner", "label_not_project_scoped") in kinds
    assert ("com.vnx.receipt-processor", "label_not_project_scoped") not in kinds


def test_check_template_contract_flags_a_missing_template(tmp_path: Path) -> None:
    _write_template(
        tmp_path, "com.vnx.gate-obligation-runner", "com.vnx.gate-obligation-runner.${VNX_PROJECT_ID}"
    )
    # no com.vnx.receipt-processor.plist at all
    result = lps.check_template_contract(tmp_path)
    assert result["ok"] is False
    kinds = {(v["family"], v["kind"]) for v in result["violations"]}
    assert ("com.vnx.receipt-processor", "template_missing") in kinds


def test_check_template_contract_flags_unreadable_template(tmp_path: Path) -> None:
    _write_template(
        tmp_path, "com.vnx.receipt-processor", "com.vnx.receipt-processor.${VNX_PROJECT_ID}"
    )
    (tmp_path / "com.vnx.gate-obligation-runner.plist").write_text("not xml at all", encoding="utf-8")
    result = lps.check_template_contract(tmp_path)
    assert result["ok"] is False
    kinds = {(v["family"], v["kind"]) for v in result["violations"]}
    assert ("com.vnx.gate-obligation-runner", "template_unreadable") in kinds


def test_check_template_contract_clean_state_is_ok(tmp_path: Path) -> None:
    _write_template(
        tmp_path, "com.vnx.gate-obligation-runner", "com.vnx.gate-obligation-runner.${VNX_PROJECT_ID}"
    )
    _write_template(
        tmp_path, "com.vnx.receipt-processor", "com.vnx.receipt-processor.${VNX_PROJECT_ID}"
    )
    result = lps.check_template_contract(tmp_path)
    assert result["ok"] is True, result["violations"]


# ---------------------------------------------------------------------------
# check_installed_state — synthetic launchctl snapshots, always injected
# ---------------------------------------------------------------------------


def test_check_installed_state_flags_missing_instance() -> None:
    text = _launchctl_text([("com.vnx.gate-obligation-runner.vnx-dev", "-", 11)])
    result = lps.check_installed_state(text, "vnx-dev")
    assert result["ok"] is False
    kinds = {(v["family"], v["kind"]) for v in result["violations"]}
    assert ("com.vnx.receipt-processor", "missing_instance") in kinds
    assert ("com.vnx.gate-obligation-runner", "missing_instance") not in kinds


def test_check_installed_state_flags_bare_label_measured_real_shape() -> None:
    # Mirrors the real 'launchctl list' snapshot measured live on this
    # machine 2026-09-04 (module docstring): a correctly-suffixed vnx-dev job
    # AND a bare mission-control job for the same family, loaded at once.
    text = _launchctl_text(
        [
            ("com.vnx.gate-obligation-runner.vnx-dev", "-", 11),
            ("com.vnx.gate-obligation-runner", "-", 20),
        ]
    )
    result = lps.check_installed_state(text, "vnx-dev")
    assert result["ok"] is False
    kinds = {(v["family"], v["kind"]) for v in result["violations"]}
    assert ("com.vnx.gate-obligation-runner", "non_per_project_label") in kinds
    assert ("com.vnx.receipt-processor", "missing_instance") in kinds
    # vnx-dev's own, correctly-scoped instance must never itself be flagged
    assert ("com.vnx.gate-obligation-runner", "missing_instance") not in kinds


def test_check_installed_state_flags_malformed_suffix() -> None:
    text = _launchctl_text(
        [
            ("com.vnx.gate-obligation-runner.Mission_Control", "-", 0),
            ("com.vnx.receipt-processor.vnx-dev", "1234", 0),
        ]
    )
    result = lps.check_installed_state(text, "vnx-dev")
    assert result["ok"] is False
    kinds = {(v["family"], v["kind"]) for v in result["violations"]}
    assert ("com.vnx.gate-obligation-runner", "malformed_label") in kinds
    # still missing its OWN (vnx-dev) instance even though a malformed one exists
    assert ("com.vnx.gate-obligation-runner", "missing_instance") in kinds
    assert ("com.vnx.receipt-processor", "missing_instance") not in kinds


def test_check_installed_state_clean_multi_project_is_ok() -> None:
    # The goal end state: two projects, each properly suffixed, no bare
    # label anywhere. Must never be flagged for either project.
    text = _launchctl_text(
        [
            ("com.vnx.gate-obligation-runner.vnx-dev", "-", 11),
            ("com.vnx.gate-obligation-runner.mission-control", "-", 20),
            ("com.vnx.receipt-processor.vnx-dev", "1234", 0),
            ("com.vnx.receipt-processor.mission-control", "5678", 0),
        ]
    )
    result_vnx_dev = lps.check_installed_state(text, "vnx-dev")
    assert result_vnx_dev["ok"] is True, result_vnx_dev["violations"]
    result_mc = lps.check_installed_state(text, "mission-control")
    assert result_mc["ok"] is True, result_mc["violations"]


def test_check_installed_state_empty_snapshot_flags_everything_missing() -> None:
    # Zero violations from zero jobs checked would be a measurement error,
    # not a clean result -- an empty launchctl snapshot must flag ALL
    # required families as missing, never silently report ok=True.
    result = lps.check_installed_state("PID\tStatus\tLabel", "vnx-dev")
    assert result["ok"] is False
    assert len(result["violations"]) == len(lps.REQUIRED_PER_PROJECT_FAMILIES)
    assert all(v["kind"] == "missing_instance" for v in result["violations"])


def test_check_installed_state_rejects_invalid_project_id() -> None:
    result = lps.check_installed_state("PID\tStatus\tLabel", "Not Valid!")
    assert result["ok"] is False
    assert result["violations"] == [
        {
            "family": None,
            "kind": "invalid_project_id",
            "detail": "project_id 'Not Valid!' does not match ^[a-z][a-z0-9-]{1,31}$",
        }
    ]


# ---------------------------------------------------------------------------
# check_project_scope — both layers combined
# ---------------------------------------------------------------------------


def test_check_project_scope_combines_both_layers(tmp_path: Path) -> None:
    _write_template(tmp_path, "com.vnx.gate-obligation-runner", "com.vnx.gate-obligation-runner")
    _write_template(
        tmp_path, "com.vnx.receipt-processor", "com.vnx.receipt-processor.${VNX_PROJECT_ID}"
    )
    text = _launchctl_text([("com.vnx.gate-obligation-runner", "-", 20)])

    result = lps.check_project_scope(tmp_path, "vnx-dev", text)
    assert result["ok"] is False
    kinds = {v["kind"] for v in result["violations"]}
    assert "label_not_project_scoped" in kinds  # template layer
    assert "non_per_project_label" in kinds  # installed layer, bare label present
    assert "missing_instance" in kinds  # neither family has vnx-dev's own instance


def test_check_project_scope_real_templates_clean_installed_state_is_ok() -> None:
    # Real repo templates (already proven project-scoped above) + a clean,
    # synthetic installed snapshot -> the whole guard is green.
    text = _launchctl_text(
        [
            ("com.vnx.gate-obligation-runner.vnx-dev", "-", 11),
            ("com.vnx.receipt-processor.vnx-dev", "4321", 0),
        ]
    )
    result = lps.check_project_scope(LAUNCHD_DIR, "vnx-dev", text)
    assert result["ok"] is True, result["violations"]


# ---------------------------------------------------------------------------
# main() -- CLI wiring: platform gate, project-id resolution failure, and
# read-only behavior (never calls launchctl load/unload/bootstrap).
# ---------------------------------------------------------------------------


def test_main_skips_cleanly_on_non_darwin(monkeypatch, capsys) -> None:
    monkeypatch.setattr(lps.sys, "platform", "linux")
    rc = lps.main([])
    assert rc == 0
    assert "not darwin" in capsys.readouterr().out


def test_main_fails_closed_when_project_id_unresolvable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(lps.sys, "platform", "darwin")
    monkeypatch.setattr(lps.vnx_paths, "resolve_project_id", lambda: None)
    rc = lps.main([])
    assert rc == 2
    assert "cannot resolve a project id" in capsys.readouterr().err


def test_main_reports_violations_with_nonzero_exit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(lps.sys, "platform", "darwin")
    monkeypatch.setattr(
        lps, "_run_real_launchctl_list", lambda: "PID\tStatus\tLabel"
    )
    rc = lps.main(["--project-id", "vnx-dev", "--templates-dir", str(LAUNCHD_DIR)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "VIOLATIONS" in out
    assert "missing_instance" in out


def test_main_json_mode_emits_parseable_json(monkeypatch, capsys) -> None:
    import json

    monkeypatch.setattr(lps.sys, "platform", "darwin")
    monkeypatch.setattr(
        lps,
        "_run_real_launchctl_list",
        lambda: _launchctl_text(
            [
                ("com.vnx.gate-obligation-runner.vnx-dev", "-", 11),
                ("com.vnx.receipt-processor.vnx-dev", "1", 0),
            ]
        ),
    )
    rc = lps.main(["--project-id", "vnx-dev", "--templates-dir", str(LAUNCHD_DIR), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
