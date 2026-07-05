"""tests/test_horizon_parity.py — D3 of claudedocs/2026-07-05-horizon-planning-module-PLAN.md.

Proves `vnx horizon` (D1) at three levels, all derived by introspecting the
real argparse trees / dispatch tables rather than hand-listing a subset:

1. Verb + flag + exit-code parity: every verb `planning_cli`'s engine exposes
   is also exposed by the `vnx horizon` subparser (superset check on optional
   flags, exact match on positionals + required/choices/nargs/type), AND each
   horizon verb delegates to the identical `planning_cli.cmd_*` function,
   passing its return value straight through as the process exit code (R1 in
   the plan: horizon must delegate, never reimplement).
2. `--help` parity: `vnx horizon --help` / `vnx horizon <verb> --help` (and
   the `objective`/`deliverable` alias help) name every subcommand/flag the
   engine exposes.
3. ADR-007 cross-project isolation + env-conflict safety: two distinct temp
   central stores never leak into each other; a stray/conflicting
   VNX_DATA_DIR (without the EXPLICIT guard) or VNX_PROJECT_ID in the env
   never redirects a write; `objective`/`deliverable` read the same store as
   `horizon` even under a conflicting env.

Self-contained (tests/ has no __init__.py, so fixtures are duplicated from
test_horizon_cli.py rather than cross-imported).
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
_SCRIPTS = REPO_ROOT / "scripts"
_MIGRATIONS = REPO_ROOT / "schemas" / "migrations"
for _p in (str(REPO_ROOT), str(_LIB), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schema_migration  # noqa: E402
import planning_cli  # noqa: E402

import vnx_cli.main as vnx_main_mod  # noqa: E402
from vnx_cli import _engine  # noqa: E402
from vnx_cli.commands import horizon as horizon_mod  # noqa: E402
from vnx_cli.main import main as vnx_main  # noqa: E402


# ---------------------------------------------------------------------------
# Introspection helpers — walk the real argparse trees, no hand-listing.
# ---------------------------------------------------------------------------

def _subparsers_action(parser: argparse.ArgumentParser):
    for action in parser._actions:  # noqa: SLF001 - introspection is the point
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return action
    return None


def _verb_map(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """{subcommand_name: its subparser} for the first subparsers action found."""
    action = _subparsers_action(parser)
    return dict(action.choices) if action is not None else {}


_EXCLUDED_DESTS = {"state_dir", "project_dir"}


def _optional_specs(subparser: argparse.ArgumentParser, exclude=_EXCLUDED_DESTS) -> dict:
    """{dest: spec} for every optional (flagged) argument, minus known,
    documented, intentional divergences (state_dir is never exposed by
    horizon — it's resolved internally; project_dir is horizon-only, needed
    to resolve the central store)."""
    specs = {}
    for action in subparser._actions:  # noqa: SLF001
        if not action.option_strings or action.dest in ("help", *exclude):
            continue
        specs[action.dest] = {
            "options": tuple(sorted(action.option_strings)),
            "required": bool(getattr(action, "required", False)),
            "choices": set(action.choices) if action.choices else None,
            "nargs": action.nargs,
            "action_type": type(action).__name__,
            "value_type": getattr(action, "type", None),
        }
    return specs


def _positional_dests(subparser: argparse.ArgumentParser) -> list[str]:
    return [
        a.dest
        for a in subparser._actions  # noqa: SLF001
        if not a.option_strings and not isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
        and a.dest != "help"
    ]


def _assert_flag_parity(pc_sub: argparse.ArgumentParser, hz_sub: argparse.ArgumentParser, verb: str) -> None:
    pc_specs = _optional_specs(pc_sub)
    hz_specs = _optional_specs(hz_sub)

    missing = set(pc_specs) - set(hz_specs)
    assert not missing, (
        f"`{verb}`: vnx horizon subparser is missing flag(s) {missing} that the "
        f"planning_cli engine accepts"
    )
    for dest, pc_spec in pc_specs.items():
        hz_spec = hz_specs[dest]
        assert hz_spec == pc_spec, f"`{verb}` --{dest}: spec differs — engine={pc_spec} horizon={hz_spec}"

    pc_pos = _positional_dests(pc_sub)
    hz_pos = _positional_dests(hz_sub)
    assert hz_pos == pc_pos, f"`{verb}`: positional args differ — engine={pc_pos} horizon={hz_pos}"


# ---------------------------------------------------------------------------
# Build both parser trees ONCE at import time (pure introspection — no env,
# no filesystem I/O) so parametrize ids are derived from the real trees.
# ---------------------------------------------------------------------------

_PC_PARSER = planning_cli._build_parser()  # noqa: SLF001
_PC_DOMAINS = _verb_map(_PC_PARSER)
_PC_OBJECTIVE_VERBS = _verb_map(_PC_DOMAINS["objective"])
_PC_DELIVERABLE_VERBS = _verb_map(_PC_DOMAINS["deliverable"])
_PC_PLAN_GATE_VERBS = _verb_map(_PC_DOMAINS["plan-gate"])


def _build_horizon_top_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="vnx")
    subs = top.add_subparsers(dest="command", metavar="COMMAND")
    vnx_main_mod._register_horizon_subparser(subs)  # noqa: SLF001
    vnx_main_mod._register_objective_subparser(subs)  # noqa: SLF001
    vnx_main_mod._register_deliverable_subparser(subs)  # noqa: SLF001
    return top


_HZ_PARSER = _build_horizon_top_parser()
_HZ_TOP = _verb_map(_HZ_PARSER)
_HZ_HORIZON_ALL = _verb_map(_HZ_TOP["horizon"])
_HZ_OBJECTIVE_VERBS = {k: v for k, v in _HZ_HORIZON_ALL.items() if k not in ("deliverable", "plan-gate")}
_HZ_DELIVERABLE_VERBS = _verb_map(_HZ_HORIZON_ALL["deliverable"])
_HZ_PLAN_GATE_VERBS = _verb_map(_HZ_HORIZON_ALL["plan-gate"])
_HZ_ALIAS_OBJECTIVE_VERBS = _verb_map(_HZ_TOP["objective"])
_HZ_ALIAS_DELIVERABLE_VERBS = _verb_map(_HZ_TOP["deliverable"])


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_horizon_cli.py — duplicated, no package init)
# ---------------------------------------------------------------------------

def _bootstrap_store(state_dir: Path) -> None:
    """Pre-migrate a runtime_coordination.db far enough for the planning
    surface (tracks/deliverables/plan-gate): 22 (track layer), 24 (tenant
    scoping), 27 (horizon + deliverables view), 28 (derived_status)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
        "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
        "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
        "attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "expires_after TEXT, metadata_json TEXT DEFAULT '{}', "
        "UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT, event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, "
        "to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.commit()
    for version, filename in [
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()


@pytest.fixture()
def isolated_env(monkeypatch):
    """Strip ambient VNX_* env so resolution is deterministic per-test."""
    for key in (
        "VNX_DATA_HOME", "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT",
        "VNX_PROJECT_ID", "VNX_STATE_DIR", "VNX_CANONICAL_ROOT", "PROJECT_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def project(tmp_path, isolated_env, monkeypatch):
    """An isolated, non-git project dir + a pre-migrated CENTRAL store wired
    via VNX_DATA_DIR_EXPLICIT (the highest-precedence override)."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data_root = tmp_path / "central-data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
    state_dir = data_root / "state"
    _bootstrap_store(state_dir)
    monkeypatch.chdir(project_dir)
    return project_dir, state_dir


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["vnx", *argv])
    with pytest.raises(SystemExit) as exc:
        vnx_main()
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


# ===========================================================================
# 1. Verb-set parity — the domain surfaces match exactly (introspected).
# ===========================================================================

def test_objective_domain_verb_set_matches_engine():
    assert set(_HZ_OBJECTIVE_VERBS) == set(_PC_OBJECTIVE_VERBS)


def test_deliverable_domain_verb_set_matches_engine():
    assert set(_HZ_DELIVERABLE_VERBS) == set(_PC_DELIVERABLE_VERBS)


def test_plan_gate_domain_verb_set_matches_engine():
    assert set(_HZ_PLAN_GATE_VERBS) == set(_PC_PLAN_GATE_VERBS)


def test_objective_alias_verb_set_matches_horizon():
    assert set(_HZ_ALIAS_OBJECTIVE_VERBS) == set(_HZ_OBJECTIVE_VERBS)


def test_deliverable_alias_verb_set_matches_horizon_deliverable():
    assert set(_HZ_ALIAS_DELIVERABLE_VERBS) == set(_HZ_DELIVERABLE_VERBS)


# ===========================================================================
# 2. Flag parity — every flag the engine accepts is accepted by horizon,
#    with matching required/choices/nargs/type (introspected per verb).
# ===========================================================================

@pytest.mark.parametrize("verb", sorted(_PC_OBJECTIVE_VERBS))
def test_objective_verb_flags_match_engine(verb):
    _assert_flag_parity(_PC_OBJECTIVE_VERBS[verb], _HZ_OBJECTIVE_VERBS[verb], f"horizon {verb}")


@pytest.mark.parametrize("verb", sorted(_PC_DELIVERABLE_VERBS))
def test_deliverable_verb_flags_match_engine(verb):
    _assert_flag_parity(_PC_DELIVERABLE_VERBS[verb], _HZ_DELIVERABLE_VERBS[verb], f"horizon deliverable {verb}")


@pytest.mark.parametrize("verb", sorted(_PC_PLAN_GATE_VERBS))
def test_plan_gate_verb_flags_match_engine(verb):
    _assert_flag_parity(_PC_PLAN_GATE_VERBS[verb], _HZ_PLAN_GATE_VERBS[verb], f"horizon plan-gate {verb}")


@pytest.mark.parametrize("verb", sorted(_PC_OBJECTIVE_VERBS))
def test_objective_alias_verb_flags_match_engine(verb):
    _assert_flag_parity(_PC_OBJECTIVE_VERBS[verb], _HZ_ALIAS_OBJECTIVE_VERBS[verb], f"objective {verb}")


@pytest.mark.parametrize("verb", sorted(_PC_DELIVERABLE_VERBS))
def test_deliverable_alias_verb_flags_match_engine(verb):
    _assert_flag_parity(_PC_DELIVERABLE_VERBS[verb], _HZ_ALIAS_DELIVERABLE_VERBS[verb], f"deliverable {verb}")


# ===========================================================================
# 3. Delegation + exit-code passthrough — every verb calls the IDENTICAL
#    planning_cli.cmd_* function (discovered via source inspection, not
#    hand-mapped) and returns its value unchanged as the process exit code.
#    Guards R1: if a verb is ever reimplemented instead of delegated, this
#    fails loud (either no `pc.cmd_*(args)` call is found, or the stub is
#    never invoked / the exit code stops matching the sentinel).
# ===========================================================================

_PC_CALL_RE = re.compile(r"pc\.(cmd_\w+)\(args\)")


def _discover_delegate_name(fn) -> str:
    src = inspect.getsource(fn)
    m = _PC_CALL_RE.search(src)
    assert m, f"{fn.__name__}: no `pc.cmd_*(args)` delegation call found — does it still delegate?"
    return m.group(1)


_BASE_FLAGS = ["--project-id", "delegate-test", "--project-dir"]

_OBJECTIVE_ARGV = {
    "add": ["delegate-track", "Delegate Title", "Delegate goal state"],
    "list": [],
    "show": ["delegate-track"],
    "sync": [],
    "drift": [],
    "reconcile": [],
    "reconcile-review": ["run-123", "--reviewer", "tester", "--verdict", "ok"],
    "reconcile-streak": [],
    "close": ["delegate-track"],
    "reopen": ["delegate-track"],
}
_DELIVERABLE_ARGV = {
    "add": ["--objective", "delegate-track", "--output-kind", "doc", "--title", "Delegate deliverable"],
    "list": [],
    "promote": ["dlv-fake-id"],
}
_PLAN_GATE_ARGV = {
    "seed": ["delegate-track"],
    "run": ["delegate-track", "--doc", "some/plan-doc.md"],
    "status": ["delegate-track"],
}


def _delegate_argv(prefix: list[str], verb: str, table: dict, project_dir: Path) -> list[str]:
    return [*prefix, verb, *table[verb], "--project-id", "delegate-test", "--project-dir", str(project_dir)]


@pytest.mark.parametrize("verb", sorted(horizon_mod._VERB_DISPATCH))  # noqa: SLF001
def test_objective_verb_delegates_and_passes_through_exit_code(verb, monkeypatch, project, capsys):
    project_dir, _ = project
    fn = horizon_mod._VERB_DISPATCH[verb]  # noqa: SLF001
    delegate_name = _discover_delegate_name(fn)
    assert hasattr(planning_cli, delegate_name)

    calls = []

    def _stub(args):
        calls.append(args)
        return 42

    monkeypatch.setattr(planning_cli, delegate_name, _stub)

    argv = _delegate_argv(["horizon"], verb, _OBJECTIVE_ARGV, project_dir)
    rc, _, err = _run(monkeypatch, capsys, argv)

    assert rc == 42, f"`horizon {verb}`: exit code not passed through from {delegate_name} (stderr={err!r})"
    assert len(calls) == 1, f"`horizon {verb}`: expected exactly 1 call to {delegate_name}, got {len(calls)}"
    bound = calls[0]
    assert bound.state_dir == str(_engine.resolve_data_root(project_dir) / "state")
    assert bound.project_id == "delegate-test"


@pytest.mark.parametrize("verb", sorted(horizon_mod._DELIVERABLE_DISPATCH))  # noqa: SLF001
def test_deliverable_verb_delegates_and_passes_through_exit_code(verb, monkeypatch, project, capsys):
    project_dir, _ = project
    fn = horizon_mod._DELIVERABLE_DISPATCH[verb]  # noqa: SLF001
    delegate_name = _discover_delegate_name(fn)
    assert hasattr(planning_cli, delegate_name)

    calls = []

    def _stub(args):
        calls.append(args)
        return 42

    monkeypatch.setattr(planning_cli, delegate_name, _stub)

    argv = _delegate_argv(["horizon", "deliverable"], verb, _DELIVERABLE_ARGV, project_dir)
    rc, _, err = _run(monkeypatch, capsys, argv)

    assert rc == 42, f"`horizon deliverable {verb}`: exit code not passed through (stderr={err!r})"
    assert len(calls) == 1
    assert calls[0].state_dir == str(_engine.resolve_data_root(project_dir) / "state")
    assert calls[0].project_id == "delegate-test"


@pytest.mark.parametrize("verb", sorted(horizon_mod._PLAN_GATE_DISPATCH))  # noqa: SLF001
def test_plan_gate_verb_delegates_and_passes_through_exit_code(verb, monkeypatch, project, capsys):
    project_dir, _ = project
    fn = horizon_mod._PLAN_GATE_DISPATCH[verb]  # noqa: SLF001
    delegate_name = _discover_delegate_name(fn)
    assert hasattr(planning_cli, delegate_name)

    calls = []

    def _stub(args):
        calls.append(args)
        return 42

    monkeypatch.setattr(planning_cli, delegate_name, _stub)

    argv = _delegate_argv(["horizon", "plan-gate"], verb, _PLAN_GATE_ARGV, project_dir)
    rc, _, err = _run(monkeypatch, capsys, argv)

    assert rc == 42, f"`horizon plan-gate {verb}`: exit code not passed through (stderr={err!r})"
    assert len(calls) == 1
    assert calls[0].state_dir == str(_engine.resolve_data_root(project_dir) / "state")
    assert calls[0].project_id == "delegate-test"


def test_objective_and_deliverable_alias_route_to_the_same_dispatch_table(monkeypatch, project, capsys):
    """The alias entry points (`vnx objective`, `vnx deliverable`) must route
    through the SAME dispatch dict as `vnx horizon` — not a second copy."""
    project_dir, _ = project

    calls = []

    def _stub(args):
        calls.append(args)
        return 42

    monkeypatch.setattr(planning_cli, "cmd_objective_list", _stub)
    rc, _, err = _run(monkeypatch, capsys, _delegate_argv(["objective"], "list", _OBJECTIVE_ARGV, project_dir))
    assert rc == 42, err
    assert len(calls) == 1

    monkeypatch.setattr(planning_cli, "cmd_deliverable_list", _stub)
    rc, _, err = _run(monkeypatch, capsys, _delegate_argv(["deliverable"], "list", _DELIVERABLE_ARGV, project_dir))
    assert rc == 42, err
    assert len(calls) == 2


# ===========================================================================
# 4. --help parity — every verb + every flag the engine exposes is named in
#    the real `--help` output (live CLI invocation, not just object parity).
# ===========================================================================

def test_horizon_help_lists_every_domain_verb():
    action = _subparsers_action(_HZ_PARSER)
    horizon_parser = action.choices["horizon"]
    text = horizon_parser.format_help()
    for verb in (*_PC_OBJECTIVE_VERBS, "deliverable", "plan-gate"):
        assert verb in text, f"`vnx horizon --help` missing verb: {verb}"


@pytest.mark.parametrize("verb", sorted(_PC_OBJECTIVE_VERBS))
def test_horizon_verb_help_lists_every_engine_flag(verb, monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, ["horizon", verb, "--help"])
    assert rc == 0
    for dest, spec in _optional_specs(_PC_OBJECTIVE_VERBS[verb]).items():
        flag = max(spec["options"], key=len)
        assert flag in out, f"`vnx horizon {verb} --help` missing flag {flag}"


@pytest.mark.parametrize("verb", sorted(_PC_DELIVERABLE_VERBS))
def test_horizon_deliverable_verb_help_lists_every_engine_flag(verb, monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, ["horizon", "deliverable", verb, "--help"])
    assert rc == 0
    for dest, spec in _optional_specs(_PC_DELIVERABLE_VERBS[verb]).items():
        flag = max(spec["options"], key=len)
        assert flag in out, f"`vnx horizon deliverable {verb} --help` missing flag {flag}"


@pytest.mark.parametrize("verb", sorted(_PC_PLAN_GATE_VERBS))
def test_horizon_plan_gate_verb_help_lists_every_engine_flag(verb, monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, ["horizon", "plan-gate", verb, "--help"])
    assert rc == 0
    for dest, spec in _optional_specs(_PC_PLAN_GATE_VERBS[verb]).items():
        flag = max(spec["options"], key=len)
        assert flag in out, f"`vnx horizon plan-gate {verb} --help` missing flag {flag}"


def test_objective_and_deliverable_alias_help_list_every_domain_verb():
    action = _subparsers_action(_HZ_PARSER)
    obj_text = action.choices["objective"].format_help()
    for verb in _PC_OBJECTIVE_VERBS:
        assert verb in obj_text, f"`vnx objective --help` missing verb: {verb}"

    dlv_text = action.choices["deliverable"].format_help()
    for verb in _PC_DELIVERABLE_VERBS:
        assert verb in dlv_text, f"`vnx deliverable --help` missing verb: {verb}"


# ===========================================================================
# 5. ADR-007 cross-project isolation + env-conflict safety
# ===========================================================================

def test_two_distinct_central_stores_do_not_leak(tmp_path, isolated_env, monkeypatch, capsys):
    """`vnx horizon add --project-id A` against store A does NOT appear in a
    totally distinct store B (`--project-id B`) — two independent temp
    central stores, proving no cross-store leak."""
    project_a = tmp_path / "proj-a"
    project_a.mkdir()
    data_root_a = tmp_path / "central-data-a"
    _bootstrap_store(data_root_a / "state")

    project_b = tmp_path / "proj-b"
    project_b.mkdir()
    data_root_b = tmp_path / "central-data-b"
    _bootstrap_store(data_root_b / "state")

    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_root_a))
    rc, _, err = _run(monkeypatch, capsys, [
        "horizon", "add", "leak-check", "Leak check", "shipped",
        "--project-id", "tenant-a", "--project-dir", str(project_a),
    ])
    assert rc == 0, err

    # Store A really has the row.
    conn = sqlite3.connect(str(data_root_a / "state" / "runtime_coordination.db"))
    try:
        row = conn.execute(
            "SELECT track_id FROM tracks WHERE track_id='leak-check' AND project_id='tenant-a'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None

    # Store B (a completely different central store) never saw it.
    monkeypatch.setenv("VNX_DATA_DIR", str(data_root_b))
    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "list", "--project-id", "tenant-a", "--project-dir", str(project_b), "--json",
    ])
    assert rc == 0, err
    assert json.loads(out) == []

    conn = sqlite3.connect(str(data_root_b / "state" / "runtime_coordination.db"))
    try:
        count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "store B must remain completely untouched by the store-A write"


def test_stray_unguarded_vnx_data_dir_does_not_redirect_the_write(tmp_path, isolated_env, monkeypatch, capsys):
    """A stray `VNX_DATA_DIR` left over in the env (e.g. from a previous
    session) must NOT redirect the write when `VNX_DATA_DIR_EXPLICIT` is not
    set to '1' — per vnx_paths._resolve_state_root, the explicit override is
    only honored when BOTH are present. The real store here is reached via
    the next-highest-precedence mechanism (VNX_DATA_HOME + resolved
    project_id from a `.vnx-project-id` marker), which must win instead."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".vnx-project-id").write_text("stray-guard-tenant\n", encoding="utf-8")

    real_data_home = tmp_path / "real-data-home"
    real_state_dir = real_data_home / "stray-guard-tenant" / "state"
    _bootstrap_store(real_state_dir)

    decoy_dir = tmp_path / "decoy-stray"
    # VNX_DATA_DIR_EXPLICIT deliberately NOT set (isolated_env already
    # stripped it) — only the stray VNX_DATA_DIR + VNX_DATA_HOME are set.
    monkeypatch.setenv("VNX_DATA_DIR", str(decoy_dir))
    monkeypatch.setenv("VNX_DATA_HOME", str(real_data_home))

    rc, _, err = _run(monkeypatch, capsys, [
        "horizon", "add", "feat-guard", "Guard feature", "shipped",
        "--project-id", "stray-guard-tenant", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    # Landed at the real, resolved store.
    conn = sqlite3.connect(str(real_state_dir / "runtime_coordination.db"))
    try:
        row = conn.execute(
            "SELECT track_id FROM tracks WHERE track_id='feat-guard' AND project_id='stray-guard-tenant'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None

    # The stray VNX_DATA_DIR was never even created — no redirect happened.
    assert not decoy_dir.exists(), "a stray unguarded VNX_DATA_DIR must never be written to"


def test_negative_control_regressed_resolver_would_be_caught(tmp_path, isolated_env, monkeypatch, capsys):
    """Negative control for the test above: prove the isolation assertion is
    not vacuously true. Simulate the historical bug class — a resolver that
    honors VNX_DATA_DIR unconditionally, ignoring the EXPLICIT guard — and
    show the SAME kind of assertion (decoy dir stays untouched) now fails,
    i.e. the regression manifests as a write landing in the decoy dir."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    decoy_dir = tmp_path / "decoy-stray"
    (decoy_dir / "state").mkdir(parents=True)  # pre-create so sqlite can open a file there

    monkeypatch.setenv("VNX_DATA_DIR", str(decoy_dir))  # stray, unguarded (no EXPLICIT flag)

    def _regressed_resolve_state_dir(project_dir):
        env_val = os.environ.get("VNX_DATA_DIR")
        if env_val:
            return Path(env_val) / "state"
        return _engine.resolve_data_root(Path(project_dir).resolve()) / "state"

    monkeypatch.setattr(horizon_mod, "resolve_state_dir", _regressed_resolve_state_dir)

    _run(monkeypatch, capsys, [
        "horizon", "add", "feat-neg", "Negative control", "shipped",
        "--project-id", "neg-tenant", "--project-dir", str(project_dir),
    ])

    # Under the regression, the write attempt lands in the decoy dir (the
    # empty runtime_coordination.db file gets created there) -- proving that
    # if resolve_state_dir ever regressed to this behavior, the "decoy dir
    # stays untouched" assertion in the sibling test above would fail.
    assert (decoy_dir / "state" / "runtime_coordination.db").exists(), (
        "negative control did not reproduce the regression — the isolation "
        "test above would not actually catch this bug class"
    )


def test_stray_vnx_project_id_env_does_not_override_explicit_project_id_flag(project, monkeypatch, capsys):
    """A stray/conflicting VNX_PROJECT_ID in the env must not override an
    explicit --project-id flag (ADR-007 tenant stamping)."""
    project_dir, state_dir = project
    monkeypatch.setenv("VNX_PROJECT_ID", "stray-tenant")

    rc, _, err = _run(monkeypatch, capsys, [
        "horizon", "add", "feat-explicit", "Explicit wins", "shipped",
        "--project-id", "explicit-tenant", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        row = conn.execute(
            "SELECT project_id FROM tracks WHERE track_id='feat-explicit'"
        ).fetchone()
        stray_count = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE project_id='stray-tenant'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "explicit-tenant"
    assert stray_count == 0


def test_objective_and_deliverable_alias_read_same_store_under_stray_env_conflict(project, monkeypatch, capsys):
    """The `objective`/`deliverable` aliases must read the SAME central store
    as `vnx horizon` even while a stray VNX_PROJECT_ID sits in the env."""
    project_dir, state_dir = project
    monkeypatch.setenv("VNX_PROJECT_ID", "stray-tenant")

    rc, _, err = _run(monkeypatch, capsys, [
        "horizon", "add", "feat-alias-conflict", "Alias under conflict", "shipped",
        "--project-id", "conflict-tenant", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    rc, out, err = _run(monkeypatch, capsys, [
        "objective", "list", "--project-id", "conflict-tenant",
        "--project-dir", str(project_dir), "--json",
    ])
    assert rc == 0, err
    assert "feat-alias-conflict" in {d["track_id"] for d in json.loads(out)}

    rc, out, err = _run(monkeypatch, capsys, [
        "deliverable", "add", "--objective", "feat-alias-conflict", "--output-kind", "doc",
        "--title", "via alias under conflict",
        "--project-id", "conflict-tenant", "--project-dir", str(project_dir),
    ])
    assert rc == 0, err

    rc, out, err = _run(monkeypatch, capsys, [
        "horizon", "deliverable", "list", "--objective", "feat-alias-conflict",
        "--project-id", "conflict-tenant", "--project-dir", str(project_dir), "--json",
    ])
    assert rc == 0, err
    assert len(json.loads(out)) == 1

    # Not redirected into the 'stray-tenant' the env var suggested.
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        stray_count = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE project_id='stray-tenant'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stray_count == 0
