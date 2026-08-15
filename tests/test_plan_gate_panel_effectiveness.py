"""Tests for scripts/analysis/plan_gate_panel_effectiveness.py.

The ijkmeting (measurement) script that answers: how often does the five-seat
plan-gate panel reach a different decision than its first seat? These tests pin
the pure measurement logic — filename canonicalization, verdict parsing fallbacks,
round grouping (anchor + retry fold + orphans), and the end-to-end analysis — with
synthetic Report objects, so nothing here reads the real central store.

Dispatch-ID: 20260814-plangate-ijkmeting
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
_ANALYSIS = str(REPO_ROOT / "scripts" / "analysis")
for _p in (_LIB, _ANALYSIS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import plan_gate_panel_effectiveness as pe  # noqa: E402

SEATS = ["opus", "kimi", "glm-5.2-harness", "deepseek", "codex"]


def _fence(verdict: str, rationale: str = "r") -> str:
    return f'```vnx-plan-verdict\n{{"verdict": "{verdict}", "rationale": "{rationale}"}}\n```'


def _rep(
    track: str,
    seat: str,
    mtime: float,
    verdict: str = "pass",
    parse_error: bool = False,
    no_verdict: bool = False,
) -> pe.Report:
    return pe.Report(
        track=track,
        seat=seat,
        mtime=mtime,
        filename=f"plan-gate-{track}-{seat}-deadbeef.md",
        verdict=verdict,
        parse_error=parse_error,
        no_verdict=no_verdict,
    )


# ---------------------------------------------------------------------------
# Filename parsing + provider-label canonicalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("plan-gate-connector-seam-opus-1a2b3c4d.md", ("connector-seam", "opus")),
        ("plan-gate-connector-seam-kimi-1a2b3c4d.md", ("connector-seam", "kimi")),
        ("plan-gate-connector-seam-codex-1a2b3c4d.md", ("connector-seam", "codex")),
        ("plan-gate-connector-seam-deepseek-1a2b3c4d.md", ("connector-seam", "deepseek")),
        # label drift: old labels canonicalize onto today's five seats
        ("plan-gate-t-glm-5.2-1a2b3c4d.md", ("t", "glm-5.2-harness")),
        ("plan-gate-t-glm-5.2-harness-1a2b3c4d.md", ("t", "glm-5.2-harness")),
        ("plan-gate-t-deepseek-harness-1a2b3c4d.md", ("t", "deepseek")),
        ("plan-gate-t-deepseek-v4-pro-1a2b3c4d.md", ("t", "deepseek")),
        # a track slug that itself ends in a label word is not confused
        ("plan-gate-foo-opus-opus-1a2b3c4d.md", ("foo-opus", "opus")),
        ("plan-gate-my-glm-5.2-glm-5.2-1a2b3c4d.md", ("my-glm-5.2", "glm-5.2-harness")),
    ],
)
def test_split_track_and_seat_canonicalizes(filename, expected):
    assert pe._split_track_and_seat(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "plan-gate-no-hash.md",  # no hash8 tail
        "not-plan-gate-t-opus-1a2b3c4d.md",  # wrong prefix
        "plan-gate--opus-1a2b3c4d.md",  # empty track slug
        "plan-gate-t-1a2b3c4d.md",  # no provider label at all
    ],
)
def test_split_track_and_seat_rejects_malformed(filename):
    assert pe._split_track_and_seat(filename) is None


# ---------------------------------------------------------------------------
# Verdict parsing: fence first, prose fallback, synthesized, parse_error
# ---------------------------------------------------------------------------

def test_parse_report_text_reads_fence():
    verdict, parse_error, no_verdict, _ = pe._parse_report_text(_fence("pass", "ok"))
    assert (verdict, parse_error, no_verdict) == ("pass", False, False)


def test_parse_report_text_fence_block():
    verdict, parse_error, no_verdict, _ = pe._parse_report_text(_fence("block"))
    assert (verdict, parse_error, no_verdict) == ("block", False, False)


@pytest.mark.parametrize(
    ("body", "expected_verdict"),
    [
        ("Verdict: **revise**. The approach needs work.", "revise"),
        ("Outcome: REVISE\n\nmore prose", "revise"),
        ("verdict: block\n", "block"),
        ("verdict: pass\nrationale: clean", "pass"),
    ],
)
def test_parse_report_text_prose_fallback(body, expected_verdict):
    """The three pre-fence reports carry their verdict in prose, not a fence."""
    verdict, parse_error, no_verdict, _ = pe._parse_report_text(body)
    assert verdict == expected_verdict
    assert not parse_error
    assert not no_verdict


def test_parse_report_text_synthesized_is_no_verdict_not_parse_error():
    verdict, parse_error, no_verdict, _ = pe._parse_report_text(
        "body synthesized by governance (worker never delivered)"
    )
    assert (verdict, parse_error, no_verdict) == ("revise", False, True)


def test_parse_report_text_garbage_is_parse_error():
    verdict, parse_error, no_verdict, _ = pe._parse_report_text("lorem ipsum no verdict anywhere")
    assert verdict == "revise"
    assert parse_error
    assert not no_verdict


# ---------------------------------------------------------------------------
# Round grouping: anchor, retry fold, orphans, no-opus anchoring
# ---------------------------------------------------------------------------

def test_group_rounds_anchors_on_first_present_seat():
    reports = [
        _rep("t", "opus", 100, "pass"),
        _rep("t", "kimi", 101, "pass"),
        _rep("t", "glm-5.2-harness", 102, "pass"),
        _rep("t", "opus", 1000, "revise"),  # second round
        _rep("t", "kimi", 1001, "revise"),
    ]
    rounds, orphaned = pe.group_rounds(reports, SEATS, pe.DEFAULT_ANCHOR_RETRY_WINDOW)
    assert orphaned == 0
    assert len(rounds) == 2
    assert rounds[0].anchor_seat == "opus"
    assert set(rounds[0].seat_reports) == {"opus", "kimi", "glm-5.2-harness"}
    assert rounds[1].anchor_seat == "opus"
    assert set(rounds[1].seat_reports) == {"opus", "kimi"}


def test_group_rounds_folds_anchor_retry_within_window():
    reports = [
        _rep("t", "opus", 100, "pass"),
        _rep("t", "kimi", 101, "pass"),
        _rep("t", "opus", 120, "revise"),  # retry, inside the window -> same round
    ]
    rounds, orphaned = pe.group_rounds(reports, SEATS, 600.0)
    assert orphaned == 0
    assert len(rounds) == 1
    assert rounds[0].seat_reports["opus"].verdict == "revise"  # latest wins


def test_group_rounds_anchor_retry_outside_window_is_new_round():
    reports = [
        _rep("t", "opus", 100, "pass"),
        _rep("t", "opus", 10000, "revise"),  # far later -> a new round
    ]
    rounds, _ = pe.group_rounds(reports, SEATS, 600.0)
    assert len(rounds) == 2


def test_group_rounds_counts_orphans_before_first_anchor():
    reports = [
        _rep("t", "kimi", 50, "pass"),  # before the first opus -> orphan
        _rep("t", "deepseek", 60, "pass"),  # orphan too
        _rep("t", "opus", 100, "pass"),
        _rep("t", "kimi", 101, "pass"),
    ]
    rounds, orphaned = pe.group_rounds(reports, SEATS, 600.0)
    assert orphaned == 2
    assert len(rounds) == 1
    assert set(rounds[0].seat_reports) == {"opus", "kimi"}


def test_group_rounds_no_opus_anchors_on_kimi():
    reports = [
        _rep("t", "kimi", 100, "pass"),
        _rep("t", "glm-5.2-harness", 101, "pass"),
        _rep("t", "deepseek", 102, "pass"),
    ]
    rounds, orphaned = pe.group_rounds(reports, SEATS, 600.0)
    assert orphaned == 0
    assert len(rounds) == 1
    assert rounds[0].anchor_seat == "kimi"


# ---------------------------------------------------------------------------
# End-to-end analyze(): equal/diverged/direction, marginal, coverage
# ---------------------------------------------------------------------------

def _corpus() -> list:
    """Four scoring rounds + one no-verdict round + one no-opus track."""
    reports = [
        # round a: unanimous pass -> panel PASS (equal)
        _rep("a", "opus", 100, "pass"),
        _rep("a", "kimi", 101, "pass"),
        _rep("a", "glm-5.2-harness", 102, "pass"),
        _rep("a", "deepseek", 103, "pass"),
        _rep("a", "codex", 104, "pass"),
        # round b: opus pass, kimi block -> panel REVISE (diverged, stricter)
        _rep("b", "opus", 100, "pass"),
        _rep("b", "kimi", 101, "block"),
        # round c: opus revise, kimi pass, glm pass -> panel PASS (diverged, milder)
        _rep("c", "opus", 100, "revise"),
        _rep("c", "kimi", 101, "pass"),
        _rep("c", "glm-5.2-harness", 102, "pass"),
        # round e: opus produced no verdict -> skipped, never a comparison
        _rep("e", "opus", 100, "revise", no_verdict=True),
        _rep("e", "kimi", 101, "pass"),
        # track d: no opus at all -> excluded from primary, anchors on kimi
        _rep("d", "kimi", 100, "pass"),
        _rep("d", "glm-5.2-harness", 101, "pass"),
        _rep("d", "deepseek", 102, "pass"),
        _rep("d", "codex", 103, "pass"),
    ]
    return reports


def test_analyze_primary_equal_and_diverged():
    result = pe.analyze(_corpus(), SEATS)
    primary = result["primary_first_seat_vs_panel"]
    assert primary["first_seat"] == "opus"
    assert primary["complete_rounds"] == 3
    assert primary["skipped_rounds"] == 1
    assert primary["skipped_reasons"][0]["reason"] == "first seat produced no verdict"
    assert primary["equal"] == 1  # round a
    assert primary["diverged"] == 2  # rounds b, c
    assert primary["diverged_stricter"] == 1  # b: pass -> REVISE
    assert primary["diverged_milder"] == 1  # c: revise -> PASS
    assert primary["equal_pct"] == 33.3
    assert primary["diverged_pct"] == 66.7


def test_analyze_coverage_reports_no_opus_tracks():
    result = pe.analyze(_corpus(), SEATS)
    cov = result["coverage_without_first_seat"]
    assert cov["tracks"] == 1  # track d
    assert cov["reports"] == 4  # kimi/glm/deepseek/codex
    assert cov["tracks_detail"][0]["track"] == "d"


def test_analyze_marginal_contribution_opus_rounds():
    result = pe.analyze(_corpus(), SEATS)
    marginal = result["marginal_contribution_opus_rounds"]
    # seat 2 (kimi): changes in round b only (block flips PASS->REVISE)
    assert marginal["2"] == {"rounds_with_this_seat": 3, "decision_changed_after": 1}
    # seat 3 (glm): present in rounds a and c only (round b has no glm);
    # changes in round c only (second pass flips tie->PASS)
    assert marginal["3"] == {"rounds_with_this_seat": 2, "decision_changed_after": 1}
    # seats 4/5: present in round a only, never change the decision
    assert marginal["4"] == {"rounds_with_this_seat": 1, "decision_changed_after": 0}
    assert marginal["5"] == {"rounds_with_this_seat": 1, "decision_changed_after": 0}


def test_analyze_secondary_first_present_seat_includes_no_opus_track():
    result = pe.analyze(_corpus(), SEATS)
    fp = result["first_present_seat_vs_panel"]
    # rounds a, b, c, d compare (e skipped); a and d equal, b and c diverge
    assert fp["complete_rounds"] == 4
    assert fp["equal"] == 2
    assert fp["diverged"] == 2
    assert fp["diverged_stricter"] == 1
    assert fp["diverged_milder"] == 1


def test_analyze_counts_unparseable_non_anchor_seats():
    reports = _corpus()
    # add a glm parse_error seat to round a: abstains, does not score
    reports.append(_rep("a", "glm-5.2-harness", 105, "revise", parse_error=True))
    result = pe.analyze(reports, SEATS)
    unparseable = result["unparseable_seat_reports"]
    assert len(unparseable) == 1
    assert unparseable[0]["seat"] == "glm-5.2-harness"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
