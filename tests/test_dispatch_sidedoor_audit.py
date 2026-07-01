"""tests/test_dispatch_sidedoor_audit.py — the PR-12 exhaustiveness gate.

This is the regression gate for PR-11 (the single-entry flip): if a NEW file invokes a lane
script as a delivery path without going through dispatch_bridge, this test fails — forcing it
to be audited + wired before the flag can flip. Turns the review's "prove exhaustiveness, do
not assert it" finding into an executable check.
"""
from __future__ import annotations

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
