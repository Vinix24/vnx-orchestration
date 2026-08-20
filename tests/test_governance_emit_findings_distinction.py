"""tests/test_governance_emit_findings_distinction.py — "niet opgevangen" is geen "nul".

D3 of track `report-wrapper-machine-readable`. These tests assert the invariant that
holds AFTER the fix (D4), not the defect before it, so they are RED on this branch and
turn green when D4 lands. Source is untouched by this file on purpose: the fix is a
separate dispatch, so a test written here cannot be written toward its own solution.

The defect being closed: ``governance_emit.emit_unified_report()`` renders an empty
findings list as the literal word ``None`` — byte-identical to what it writes when no
findings were ever captured. A reader cannot tell "zero findings, someone looked" from
"nobody looked". Worse, every caller hardcodes ``findings=[]`` today, so at the wrapper
boundary the distinction does not even exist yet.

The seam is fixed by T0, the shape is not:
  - ``findings=None`` means **not captured**.
  - ``findings=[]`` means **zero findings, and someone looked**.

Whether that lands as an absent section or as a section with an explicit sentinel is
D4's call (open question 2 of the plan). These tests therefore assert the DIFFERENCE,
never a specific text and never the presence or absence of the ``## Findings`` heading,
so they stay valid whichever shape D4 picks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for _p in (str(_LIB), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from governance_emit import emit_unified_report  # noqa: E402


@pytest.fixture()
def tmp_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


def _wrapper_kwargs(data_dir, **overrides):
    kwargs = dict(
        dispatch_id="20260820-d3-findings-distinction",
        terminal_id="T2",
        provider="litellm:deepseek",
        instruction="Do the thing",
        response_text="Done.",
        findings=[],
        duration_seconds=4.2,
        data_dir=data_dir,
    )
    kwargs.update(overrides)
    return kwargs


def _differing_regions(left: str, right: str):
    """Split *left* and *right* into the two contiguous substrings where they
    diverge, after stripping their shared prefix and suffix.

    Form-free by construction: it never names ``## Findings`` nor any sentinel,
    so it stays valid whichever shape D4 picks (absent section vs sentinel). The
    only input that varies between the two renders is the findings field, so the
    differing region IS the findings display.
    """
    prefix = 0
    max_prefix = min(len(left), len(right))
    while prefix < max_prefix and left[prefix] == right[prefix]:
        prefix += 1

    suffix = 0
    max_suffix = min(len(left), len(right)) - prefix
    while suffix < max_suffix and left[-1 - suffix] == right[-1 - suffix]:
        suffix += 1

    return (
        left[prefix:len(left) - suffix],
        right[prefix:len(right) - suffix],
    )


def test_findings_none_distinguishable_from_empty(tmp_data):
    """D3 invariants 1+2: a not-captured findings field (findings=None) renders
    observably different from zero findings (findings=[]), and neither rendering
    can be read as the other.

    Everything except the findings input is identical between the two renders
    (same dispatch-id, same response, same duration), so the only way the two
    reports can differ at all is in how the findings field is displayed. We
    compare that differing region directly, without naming the heading or a
    sentinel.
    """
    none_content = emit_unified_report(
        **_wrapper_kwargs(tmp_data, findings=None),
    ).read_text(encoding="utf-8")
    empty_content = emit_unified_report(
        **_wrapper_kwargs(tmp_data), overwrite=True,
    ).read_text(encoding="utf-8")

    # 1. They are not identical.
    assert none_content != empty_content, (
        "findings=None and findings=[] render the same findings display; a reader "
        "cannot tell 'zero findings' from 'nobody looked'"
    )

    none_region, empty_region = _differing_regions(none_content, empty_content)

    # 2. Neither is readable as the other: the non-empty rendering must not occur
    #    inside the other rendering. (An absent-section form leaves one region
    #    empty; an empty region trivially contains nothing, so only the non-empty
    #    side is checked.)
    if none_region:
        assert none_region not in empty_region, (
            f"findings=None rendering appears inside the findings=[] rendering: "
            f"{none_region!r}"
        )
    if empty_region:
        assert empty_region not in none_region, (
            f"findings=[] rendering appears inside the findings=None rendering: "
            f"{empty_region!r}"
        )


def test_findings_nonempty_still_rendered(tmp_data):
    """D3 invariant 3: real findings keep showing up in the report."""
    content = emit_unified_report(
        **_wrapper_kwargs(
            tmp_data,
            findings=[{"severity": "warning", "message": "Something smells"}],
        ),
    ).read_text(encoding="utf-8")

    assert "Something smells" in content, (
        "a report with real findings must keep rendering them; the distinction fix "
        "must not drop the findings themselves"
    )
