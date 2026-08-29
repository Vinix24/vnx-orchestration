"""No new writer may derive its scratch file from the destination alone (OI-1486).

``atomic_io.atomic_write_text`` exists because this pattern was already
consolidated once::

    tmp = target.with_suffix(".tmp")
    tmp.write_text(content)
    os.replace(tmp, target)

The consolidation added the helper and left the inline copies in place, so the
form kept spreading. It is not a style preference: the scratch name is a pure
function of the DESTINATION, so every concurrent writer of one path picks the
same scratch file. They overwrite each other's bytes, the first to rename
lands somebody else's content under its own name, and the second renames a
path that is already gone. OI-1486 measured that on the gate-result slot: two
threads, and the writer that reported success was not the writer whose bytes
were on disk.

This test pins the TREE, not the five call sites OI-1486 repaired. A new
occurrence in any ``scripts/lib`` module fails it, and so does an extra
occurrence in a module that still carries known ones. The allowlist below is
the backlog, counted rather than hidden — shrink an entry when you convert
one, and delete the entry at zero.

Not flagged: a scratch name that already carries a per-writer discriminator
(``mkstemp``, ``os.getpid()``, a uuid). ``getpid`` is the weaker form — it
separates processes but not two threads of one process — and six sites in this
tree use it. That is a narrower defect than the one measured here and it is
not what this guard is for.
"""
from __future__ import annotations

import ast
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"

# A scratch name containing any of these is discriminated per writer.
_DISCRIMINATORS = ("mkstemp", "getpid", "uuid", "tempfile", "NamedTemporary")

# path relative to scripts/lib -> occurrences still carrying the form.
# Converting one of these means decrementing its number in the same commit.
_KNOWN_BACKLOG: dict[str, int] = {
    "context_rotation.py": 2,
    "dispatch_broker.py": 1,
    "dispatch_cli.py": 1,
    "event_store.py": 1,
    "headless_dispatch_writer.py": 1,
    "health_beacon.py": 1,
    "injection_effectiveness_probe.py": 1,
    "intelligence_aggregator.py": 1,
    "objective_reconcile.py": 2,
    "provider_spawns/kimi_spawn.py": 1,
    "receipt_classifier.py": 2,
    "skill_refinement.py": 1,
    "smart_router.py": 1,
    "state_rebuild_trigger.py": 1,
    "tmux_interactive_dispatch.py": 1,
}


def _scratch_sites(module: Path) -> list[tuple[int, str]]:
    """Assignments whose value builds a ``.tmp`` path with no writer identity.

    Parsed rather than grepped: every docstring in this tree that explains the
    defect contains the defective text, and a grep counts those as instances
    of it. Only real assignment expressions reach this list.
    """
    try:
        tree = ast.parse(module.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        expr = ast.unparse(node.value)
        if ".tmp'" not in expr:
            continue
        if any(d in expr for d in _DISCRIMINATORS):
            continue
        sites.append((node.lineno, expr))
    return sites


def _sweep() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for module in sorted(LIB_DIR.rglob("*.py")):
        sites = _scratch_sites(module)
        if sites:
            found[module.relative_to(LIB_DIR).as_posix()] = sites
    return found


def test_the_sweep_finds_a_case_that_is_known_to_exist():
    """The detector must be able to see the thing before its count means anything.

    A sweep that silently matches nothing reports a clean tree in exactly the
    same words as a sweep that works. This asserts against a backlog entry
    that is known to be present.
    """
    found = _sweep()
    assert "smart_router.py" in found, (
        "the sweep found no scratch-name site in smart_router.py, which is "
        "known to carry one — the detector is broken, not the tree clean"
    )


def test_the_repaired_call_sites_stay_repaired():
    """The five OI-1486 conversions, named so a revert cannot pass quietly."""
    found = _sweep()
    for repaired in (
        "gate_recorder.py",
        "gate_request_handler.py",
        "governance_emit.py",
    ):
        assert repaired not in found, (
            f"{repaired} carries a destination-derived scratch name again: "
            f"{found.get(repaired)} — use atomic_io.atomic_write_text/_json, "
            "and atomic_io.slot_lock when a read decides the write"
        )


def test_no_module_gains_a_destination_derived_scratch_name():
    found = _sweep()
    offenders: list[str] = []
    for module, sites in found.items():
        allowed = _KNOWN_BACKLOG.get(module, 0)
        if len(sites) > allowed:
            rendered = ", ".join(f"line {ln}: {src}" for ln, src in sites)
            offenders.append(
                f"{module}: {len(sites)} site(s), {allowed} allowed — {rendered}"
            )
    assert not offenders, (
        "a scratch file whose name comes from the destination alone is shared "
        "by every concurrent writer of that destination (OI-1486). Use "
        "atomic_io.atomic_write_text / atomic_write_json, which take a "
        "per-writer mkstemp name; add atomic_io.slot_lock when the write "
        "depends on something the caller just read.\n" + "\n".join(offenders)
    )


def test_backlog_entries_that_are_gone_are_deleted_from_the_list():
    """An allowlist that outlives what it lists stops describing the tree."""
    found = _sweep()
    stale = {
        module: allowed
        for module, allowed in _KNOWN_BACKLOG.items()
        if len(found.get(module, [])) < allowed
    }
    assert not stale, (
        "these entries claim more remaining sites than the tree has — the "
        "conversion landed without shrinking the number, so the backlog now "
        "over-reports: " + repr(stale)
    )
