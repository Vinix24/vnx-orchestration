"""Tests for query_inspector.detect_unsafe.

DIAGNOSIS (root cause of the original failure):
    The original test mocked ``builders.build_user_query`` with
    ``mock.patch(..., return_value="SELECT * FROM users")`` and then passed the
    resulting ``MagicMock`` to ``detect_unsafe``. ``detect_unsafe`` reads a
    function's source via ``inspect.getsource(fn)`` — a MagicMock has no source
    object, so ``inspect.getsource`` raises
    ``TypeError: ... got MagicMock`` deep inside inspect. The traceback points
    at query_inspector, which is MISLEADING: the inspector is correct.

    The bug was the mock. You cannot introspect the source of a function you
    have replaced with a mock. The fix is to drop the mock entirely and test
    ``detect_unsafe`` against REAL functions, which is exactly what it inspects
    in production (dev-time linting of query-builder source).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_detect_unsafe_on_user_query():
    """Fixed: pass the REAL builders functions (no mock).

    The original test mocked build_user_query and then asked the inspector to
    read the mock's source — impossible. detect_unsafe must see real source.
    """
    import builders
    from query_inspector import detect_unsafe

    # build_user_query uses an f-string SQL -> unsafe pattern detected.
    assert detect_unsafe(builders.build_user_query) is True
    # build_user_query_safe is parameterized -> no unsafe pattern.
    assert detect_unsafe(builders.build_user_query_safe) is False


def _real_fstring_builder(user_id: int) -> str:
    """Real (un-mocked) function using f-string SQL — should be flagged."""
    return f"SELECT * FROM accounts WHERE id = {user_id}"


def _real_param_builder(user_id: int) -> tuple[str, tuple]:
    """Real (un-mocked) function using a parameterized query — safe."""
    return "SELECT * FROM accounts WHERE id = ?", (user_id,)


def test_detect_unsafe_real_function_with_fstring():
    """A real f-string SQL builder is detected as unsafe."""
    from query_inspector import detect_unsafe

    assert detect_unsafe(_real_fstring_builder) is True


def test_detect_unsafe_real_function_with_param():
    """A real parameterized query builder is reported safe."""
    from query_inspector import detect_unsafe

    assert detect_unsafe(_real_param_builder) is False
