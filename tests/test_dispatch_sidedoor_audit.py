"""tests/test_dispatch_sidedoor_audit.py — the PR-12 exhaustiveness gate.

This is the regression gate for PR-11 (the single-entry flip): if a NEW file invokes a lane
script as a delivery path without going through dispatch_bridge, this test fails — forcing it
to be audited + wired before the flag can flip. Turns the review's "prove exhaustiveness, do
not assert it" finding into an executable check.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_sidedoor_audit as audit_mod  # noqa: E402


def test_no_unaudited_side_door_callers():
    result = audit_mod.audit()
    assert result["unaudited"] == set(), (
        "New direct lane-script delivery caller(s) appeared — audit them and wire through "
        "dispatch_bridge before flipping VNX_SINGLE_ENTRY_DISPATCH: "
        + ", ".join(sorted(result["unaudited"]))
    )


def test_scan_still_detects_known_callers():
    # guards against the scanner silently going blind (e.g. a regex/docstring-skip regression):
    # the known delivery callers must still be detected.
    found = audit_mod.scan_delivery_callers()
    for caller in (
        "scripts/lib/plan_gate_panel.py",
        "scripts/commands/dispatch.sh",
        "scripts/lib/pool_worker_runner.py",
    ):
        assert caller in found, f"scanner no longer detects {caller}"


def test_docstring_mention_is_not_a_caller():
    # the over-flag fix: a lane named only in a docstring/comment must NOT be a caller.
    found = audit_mod.scan_delivery_callers()
    for reference_only in (
        "scripts/lib/governance_emit.py",   # docstring: "Used by both subprocess_dispatch.py..."
        "scripts/lib/smart_router.py",      # docstring: "...in provider_dispatch.py"
        "scripts/lib/dispatch_cli.py",      # the door itself (excluded)
    ):
        assert reference_only not in found, f"{reference_only} false-flagged as a caller"


def test_no_unaudited_raw_claude_spawns():
    result = audit_mod.audit()
    assert result["raw_claude_unaudited"] == set(), (
        "New raw claude -p/--print spawn(s) appeared — route via a governed lane or audit them: "
        + ", ".join(sorted(result["raw_claude_unaudited"]))
    )


def test_process_cleanup_detector_is_not_flagged_as_raw_claude_spawn():
    # Regression: process_cleanup.py holds claude -p/--print as DETECTION patterns for its
    # process-hygiene scan (#1029); it never spawns claude and must not be flagged.
    result = audit_mod.audit()
    assert "scripts/lib/process_cleanup.py" not in result["raw_claude_unaudited"], (
        "process_cleanup.py is a detector, not a spawner; it should be excluded from the raw scan"
    )


def test_check_no_file_derived_data_paths_guard_is_not_flagged_as_delivery_caller():
    # Regression (OI-898): check_no_file_derived_data_paths.py holds lane/path literals as
    # DETECTION patterns for its path-guard scan; it never delivers a dispatch and must not
    # be flagged as a delivery caller (same shape as process_cleanup.py on the raw side).
    found = audit_mod.scan_delivery_callers()
    assert "scripts/check_no_file_derived_data_paths.py" not in found, (
        "check_no_file_derived_data_paths.py is a pattern-definition guard, not a delivery "
        "caller; it should be excluded from the delivery scan"
    )


def test_delivery_exclusion_does_not_blind_scanner_to_real_new_caller(tmp_path):
    # the exclusion is scoped to the guard file: a genuinely NEW delivery caller (different
    # basename) under the same tree IS still returned, proving the scanner is not blind.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "new_side_door.py").write_text(
        'subprocess.run(["python3", "scripts/lib/subprocess_dispatch.py", "--deliver"])\n',
        encoding="utf-8",
    )
    found = audit_mod.scan_delivery_callers(root=tmp_path)
    assert "scripts/new_side_door.py" in found, (
        "scanner went blind: a real new delivery caller is no longer detected"
    )


def test_delivery_exclusion_is_anchored_on_guard_basename(tmp_path):
    # the rule is substring-anchored on the basename path, not on incidental content: a
    # planted file NAMED check_no_file_derived_data_paths.py holding a lane literal is
    # excluded, while the identical content under another name is flagged.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    content = 'LANE = "scripts/lib/subprocess_dispatch.py"\n'
    (scripts / "check_no_file_derived_data_paths.py").write_text(content, encoding="utf-8")
    (scripts / "other_guard.py").write_text(content, encoding="utf-8")
    found = audit_mod.scan_delivery_callers(root=tmp_path)
    assert "scripts/check_no_file_derived_data_paths.py" not in found, (
        "basename-anchored exclusion did not apply to the planted guard-named file"
    )
    assert "scripts/other_guard.py" in found, (
        "exclusion leaked beyond the guard basename — identical content must still be flagged"
    )


def test_scan_detects_known_raw_claude_spawns():
    found = audit_mod.scan_raw_claude_spawns()
    for caller in (
        "scripts/lib/report_classifier.py",
        "scripts/headless_trigger.py",
    ):
        assert caller in found, f"scanner no longer detects {caller}"


def test_raw_claude_docstring_only_not_flagged():
    # a file that only mentions the primitive in a docstring/comment must NOT be flagged.
    found = audit_mod.scan_raw_claude_spawns()
    assert "scripts/lib/decision_parser.py" not in found, (
        "docstring-only mention false-flagged as raw claude spawn"
    )


def test_provider_mix_list_not_flagged():
    # provider_mix lists like ["claude"] carry no -p/--print flag and must NOT be flagged.
    found = audit_mod.scan_raw_claude_spawns()
    assert "scripts/lib/pool_state_repo.py" not in found, (
        "provider_mix list false-flagged as raw claude spawn"
    )


def _raw_match(s: str) -> bool:
    return any(p.search(s) for p in audit_mod._RAW_CLAUDE_PATTERNS)


def test_raw_claude_pattern_catches_nonadjacent_and_multiline():
    # codex G5-2/G5-8: reordered/multi-line/wrapped argv + shell-string spawns must not evade the gate.
    assert _raw_match('cmd = ["claude", "--model", model, "--print", prompt]')      # flag not adjacent
    assert _raw_match('cmd = [\n    "claude",\n    "--model", m,\n    "-p", prompt,\n]')  # multi-line
    assert _raw_match('subprocess.run("claude --model opus --print hi", shell=True)')  # reordered shell
    assert _raw_match('subprocess.run("claude -p", shell=True)')                    # shell-string (was over-suppressed)
    assert _raw_match('cmd = ["timeout", "3s", "claude", "-p", prompt]')            # claude wrapped, not first
    assert _raw_match('["claude", "-p", "--verbose"]')                              # adjacent still caught


def test_raw_claude_pattern_no_false_positives():
    assert not _raw_match('provider_mix = ["claude", "claude", "codex"]')           # no flag
    assert not _raw_match('subprocess.Popen(["claude", "--dangerously-skip-permissions", p])')  # interactive
    assert not _raw_match("provider_mix = json.loads(row or '[\"claude\"]')")       # data literal
    assert not _raw_match('["claude", "--print-config", "x"]')                      # --print-config != the print flag
    assert not _raw_match('x = "claude"\ny = "-p"')                                 # unrelated statements, no shared list


def test_raw_scan_does_not_skip_lane_basenames(tmp_path):
    # codex G5-4: a raw claude -p spawn in a lane-named script must be audited, not hidden.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "subprocess_dispatch.py").write_text('cmd = ["claude", "-p", "x"]\n', encoding="utf-8")
    found = audit_mod.scan_raw_claude_spawns(root=tmp_path)
    assert "scripts/subprocess_dispatch.py" in found, (
        "raw scan must not blind-spot a raw spawn added to a lane-named script"
    )


def test_raw_scan_detects_split_binary_args_idiom(tmp_path):
    # codex G5-6: `"binary": "claude"` + args=[..., "--print"] assembled as [binary]+args is a real
    # claude spawn a literal-argv regex can't see; the config idiom must be detected + audited.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "some_adapter.py").write_text(
        'CFG = {"binary": "claude", "args": ["--print", "--output-format", "text"]}\n',
        encoding="utf-8",
    )
    assert "scripts/some_adapter.py" in audit_mod.scan_raw_claude_spawns(root=tmp_path)


def test_real_headless_adapter_is_audited():
    # headless_adapter's split binary/args construction is detected and audited (not unaudited).
    assert "scripts/lib/headless_adapter.py" in audit_mod.scan_raw_claude_spawns()
    assert "scripts/lib/headless_adapter.py" in audit_mod.KNOWN_RAW_CLAUDE_CALLERS


def test_real_mention_files_are_audited_not_hidden():
    # codex G5-8: dispatch.sh (help text) + vnx_tag_vocabulary (keyword) trip the pattern; they are
    # AUDITED in the allowlist (not line-suppressed), so a real spawn added there still matches.
    for f in ("scripts/commands/dispatch.sh", "scripts/lib/vnx_tag_vocabulary.py"):
        assert f in audit_mod.KNOWN_RAW_CLAUDE_CALLERS


def test_raw_scan_covers_extensionless_shebang_scripts(tmp_path):
    # codex G5-7: bin/vnx-style extensionless executables can spawn claude too and must be scanned.
    binp = tmp_path / "bin"
    binp.mkdir()
    vnx = binp / "vnx"
    vnx.write_text('#!/usr/bin/env bash\nclaude -p "$@"\n', encoding="utf-8")
    assert "bin/vnx" in audit_mod.scan_raw_claude_spawns(root=tmp_path)
    # an extensionless NON-shebang data file is not scanned as a script
    (binp / "data").write_text('claude -p something\n', encoding="utf-8")
    assert "bin/data" not in audit_mod.scan_raw_claude_spawns(root=tmp_path)


# --- OI-1220: an argparse `help=` string documents the flag, it does not invoke it. ---
# PR #1531 added a `--allow-headless` flag whose help prose mentions "(claude -p)"; the raw
# scan flagged `vnx_cli/main.py` even though the full diff was two `add_argument` calls. The
# help-string exemption below fixes that false positive WITHOUT blinding the gate to a real
# spawn: a spawn is an argv list or a subprocess/shell call, never a `help=`-bound string.


def test_help_string_with_claude_p_is_not_flagged(tmp_path):
    # OI-1220: the exact PR #1531 shape — a parenthesized, multi-line help string — must not trip.
    scripts = tmp_path / "vnx_cli"
    scripts.mkdir()
    (scripts / "main.py").write_text(
        'dispatch_parser.add_argument(\n'
        '    "--allow-headless",\n'
        '    action="store_true",\n'
        '    help=(\n'
        '        "opt into the claude_headless lane (claude -p). Requires "\n'
        '        "--headless-reason and a claude provider. The lane runs on the "\n'
        '        "subscription unless an own ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL "\n'
        '        "is present."\n'
        '    ),\n'
        ')\n',
        encoding="utf-8",
    )
    found = audit_mod.scan_raw_claude_spawns(root=tmp_path)
    assert "vnx_cli/main.py" not in found, (
        "help= string with 'claude -p' false-flagged as raw claude spawn (OI-1220)"
    )


def test_single_line_help_string_with_claude_p_is_not_flagged(tmp_path):
    # OI-1220: a bare one-line help= literal is the same documentation channel.
    scripts = tmp_path / "vnx_cli"
    scripts.mkdir()
    (scripts / "main.py").write_text(
        'parser.add_argument("--allow-headless", help="opt into the claude -p lane")\n',
        encoding="utf-8",
    )
    found = audit_mod.scan_raw_claude_spawns(root=tmp_path)
    assert "vnx_cli/main.py" not in found, (
        "single-line help= string with 'claude -p' false-flagged as raw claude spawn"
    )


def test_docstring_with_claude_p_is_not_flagged(tmp_path):
    # OI-1220: the docstring channel is already skipped by _code_lines; pin it explicitly.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "prose.py").write_text(
        '"""Documents the claude -p primitive without ever spawning it.\n'
        'The raw receipt-bypass primitive is `claude -p` / `claude --print`.\n'
        '"""\n'
        'def describe():\n'
        '    return "headless lane"\n',
        encoding="utf-8",
    )
    found = audit_mod.scan_raw_claude_spawns(root=tmp_path)
    assert "scripts/prose.py" not in found, (
        "docstring-only mention of claude -p false-flagged as raw claude spawn"
    )


def test_argv_list_spawn_in_same_file_as_help_string_is_flagged(tmp_path):
    # OI-1220 negative: the help-string exemption must NOT blind the gate to a real argv spawn
    # in the same file.
    scripts = tmp_path / "vnx_cli"
    scripts.mkdir()
    (scripts / "main.py").write_text(
        'parser.add_argument("--allow-headless", help="opt into the claude -p lane")\n'
        'cmd = ["claude", "--model", m, "--print", p]\n',
        encoding="utf-8",
    )
    found = audit_mod.scan_raw_claude_spawns(root=tmp_path)
    assert "vnx_cli/main.py" in found, (
        "help-string exemption blinded the gate to a real argv spawn in the same file"
    )


def test_subprocess_run_spawn_in_same_file_as_help_string_is_flagged(tmp_path):
    # OI-1220 negative: an argv subprocess.run spawn next to a help string is still a spawn.
    scripts = tmp_path / "vnx_cli"
    scripts.mkdir()
    (scripts / "main.py").write_text(
        'parser.add_argument("--allow-headless", help="opt into the claude -p lane")\n'
        'subprocess.run(["claude", "-p", "--verbose"])\n',
        encoding="utf-8",
    )
    found = audit_mod.scan_raw_claude_spawns(root=tmp_path)
    assert "vnx_cli/main.py" in found, (
        "help-string exemption blinded the gate to a real subprocess argv spawn"
    )


def test_shell_string_spawn_is_not_treated_as_help_string(tmp_path):
    # OI-1220: the executable-string distinction is the whole point — a shell-string spawn is
    # NOT bound to `help=`, so it must still be flagged even though it is a quoted string.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "runner.py").write_text(
        'parser.add_argument("--allow-headless", help="opt into the claude -p lane")\n'
        'subprocess.run("claude -p", shell=True)\n',
        encoding="utf-8",
    )
    found = audit_mod.scan_raw_claude_spawns(root=tmp_path)
    assert "scripts/runner.py" in found, (
        "shell-string spawn false-negatived as if it were a help= documentation string"
    )


# --- Three-state partition + operator override (sidedoor-drie-toestanden) ---


def _delivery_result(unaudited=(), open_doors=()):
    return {
        "unaudited": set(unaudited),
        "known_open_side_doors": set(open_doors),
    }


def test_dispatch_agent_cli_is_door_handled_not_open():
    # bucket (a): the packaged CLI routes through deliver_via_door — handled, no finding.
    assert "vnx_cli/commands/dispatch_agent.py" in audit_mod.DOOR_HANDLED_CALLERS
    assert "vnx_cli/commands/dispatch_agent.py" not in audit_mod.KNOWN_OPEN_SIDE_DOORS


def test_plan_gate_panel_is_a_known_open_side_door():
    # bucket (b): plan_gate_panel.py is a KNOWN OPEN side door, and its rule carries
    # structured fields (owner / close condition / follow-up), not just a comment.
    assert "scripts/lib/plan_gate_panel.py" in audit_mod.KNOWN_OPEN_SIDE_DOORS
    rule = audit_mod.KNOWN_OPEN_SIDE_DOORS["scripts/lib/plan_gate_panel.py"]
    assert rule.owner
    assert rule.closes_when
    assert rule.followup


def test_partition_is_exhaustive_and_disjoint():
    # every audited delivery caller sits in exactly one of the two "known" buckets, and the
    # two buckets never overlap — "known" must not be one list with two meanings.
    a = set(audit_mod.DOOR_HANDLED_CALLERS)
    b = set(audit_mod.KNOWN_OPEN_SIDE_DOORS)
    assert a & b == set(), "a caller cannot be both door-handled and a known open side door"
    historical = {
        "scripts/lib/dispatch_deliver.sh",
        "scripts/lib/pool_worker_runner.py",
        "scripts/lib/headless_dispatch_daemon.py",
        "scripts/lib/adapters/claude_adapter.py",
        "scripts/commands/dispatch-agent.sh",
        "scripts/commands/dispatch.sh",
        "scripts/lib/plan_gate_panel.py",
        "vnx_cli/commands/dispatch_agent.py",
    }
    assert a | b == historical, "every audited caller must sit in exactly one bucket"
    assert audit_mod.audit()["known"] == a | b, "audit()['known'] must equal the bucket union"


def test_plan_gate_panel_surfaces_as_known_open_side_door_in_audit():
    # the red run: on the current tree the watcher must see the open side door, not swallow it.
    result = audit_mod.audit()
    assert "scripts/lib/plan_gate_panel.py" in result["known_open_side_doors"]
    assert "scripts/lib/plan_gate_panel.py" not in result["unaudited"]


def test_known_open_side_door_fails_without_override():
    # no override -> the known open side door is a finding and the gate FAILS.
    result = _delivery_result(open_doors=["scripts/lib/plan_gate_panel.py"])
    assert audit_mod._decide_delivery(result, flag="", reason="") is True


def test_override_with_reason_allows_and_records(tmp_path):
    # override + reason -> the known open side door is allowed AND recorded to the ledger.
    ledger = tmp_path / "overrides.ndjson"
    result = _delivery_result(open_doors=["scripts/lib/plan_gate_panel.py"])
    failed = audit_mod._decide_delivery(
        result, flag="1", reason="known open side door; closed by part 2", ledger=ledger
    )
    assert failed is False
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["reason"] == "known open side door; closed by part 2"
    assert [d["path"] for d in entry["side_doors"]] == ["scripts/lib/plan_gate_panel.py"]
    assert entry["side_doors"][0]["owner"]
    assert entry["side_doors"][0]["closes_when"]
    assert entry["side_doors"][0]["followup"]


def test_override_without_reason_refuses():
    # flag set but reason empty -> the flag is INERT and the refusal stays blocking.
    result = _delivery_result(open_doors=["scripts/lib/plan_gate_panel.py"])
    assert audit_mod._decide_delivery(result, flag="1", reason="") is True


def test_override_does_not_allow_unaudited(tmp_path):
    # the override downgrades ONLY known open side doors; a NEW (unaudited) caller still fails.
    ledger = tmp_path / "overrides.ndjson"
    result = _delivery_result(unaudited=["scripts/lib/new_side_door.py"])
    assert audit_mod._decide_delivery(result, flag="1", reason="any reason", ledger=ledger) is True
    assert not ledger.exists(), "no override may be recorded when the gate fails on an unaudited caller"


def test_read_override_trims_whitespace():
    flag, reason = audit_mod._read_override(
        {audit_mod.SIDEDOOR_OVERRIDE_ENV: " 1 ", audit_mod.SIDEDOOR_OVERRIDE_REASON_ENV: " r "}
    )
    assert (flag, reason) == ("1", "r")
