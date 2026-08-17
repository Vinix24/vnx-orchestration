"""test_dispatch_path_wire_format.py — pins dispatch_cli._dispatch_path_wire_entry's wire form.

OI-1271 review (PR #1602 replacement): the fix threads DispatchPath.access onto
the tmux-lane ``--dispatch-paths`` wire via ``_dispatch_path_wire_entry``, but no
test pinned the actual string each PathAccess value produces. The two review
points this file closes:

  * "byte-for-byte identical for READ_WRITE" was an unverified claim in the
    original commit message, not a test assertion.
  * WRITE and CREATE had no test of their own at all.

This test is deliberately about the WIRE STRING, not about write-scope
resolution (test_dispatch_path_access_provenance.py already covers the
door -> lane -> resolve_dispatch_write_scope chain end to end). Pinning the
exact string here means the chosen wire form — only READ gets a ``:access``
suffix, WRITE/READ_WRITE/CREATE stay bare paths — cannot silently drift back
to suffixing everything (or nothing) without a red test.
"""
from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import _dispatch_path_wire_entry
from dispatch_spec import DispatchPath, PathAccess


# ---------------------------------------------------------------------------
# One row per PathAccess value — the closed set has exactly four members.
# ---------------------------------------------------------------------------

WIRE_FORM_CASES = [
    pytest.param(PathAccess.READ, "scripts/lib/secret_module.py:read", id="read"),
    pytest.param(PathAccess.WRITE, "scripts/lib/secret_module.py", id="write"),
    pytest.param(PathAccess.READ_WRITE, "scripts/lib/secret_module.py", id="read_write"),
    pytest.param(PathAccess.CREATE, "scripts/lib/secret_module.py", id="create"),
]


class TestDispatchPathWireEntry:
    @pytest.mark.parametrize("access, expected_wire_string", WIRE_FORM_CASES)
    def test_wire_string_per_access_value(
        self, access: PathAccess, expected_wire_string: str
    ) -> None:
        dp = DispatchPath(PurePosixPath("scripts/lib/secret_module.py"), access=access)

        assert _dispatch_path_wire_entry(dp) == expected_wire_string

    def test_all_four_path_access_values_are_covered(self) -> None:
        """Fail loud if PathAccess ever grows/shrinks without this test noticing."""
        covered = {case.values[0] for case in WIRE_FORM_CASES}

        assert covered == set(PathAccess), (
            f"WIRE_FORM_CASES covers {sorted(a.value for a in covered)} but "
            f"PathAccess has {sorted(a.value for a in PathAccess)} — add/remove a "
            "case so every access value stays pinned"
        )

    def test_only_read_gets_a_suffix(self) -> None:
        """Cross-check the four rows above against the write-granting/narrowing split.

        READ is the only PathAccess value that is NOT in WRITE_GRANTING_PATH_ACCESS
        (see dispatch_spec.py) — it is also the only one that gets a wire suffix.
        This test fails if that correspondence is ever broken in either direction,
        independent of the exact string each case above pins.
        """
        from dispatch_spec import WRITE_GRANTING_PATH_ACCESS

        for access in PathAccess:
            dp = DispatchPath(PurePosixPath("some/path.py"), access=access)
            wire = _dispatch_path_wire_entry(dp)
            has_suffix = wire != "some/path.py"

            assert has_suffix == (access not in WRITE_GRANTING_PATH_ACCESS), (
                f"{access!r}: wire form {wire!r} suffix presence ({has_suffix}) "
                f"disagrees with WRITE_GRANTING_PATH_ACCESS membership"
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
