"""Contract test for the daily lead-time measurement ADR (track
`doorloop-en-effectiviteit-dagelijks`, rij PR-0).

The ADR is found by glob on its slug, never by a hard-coded number: the number
is claimed at delivery time (plan open question 1), so a hard-coded number
would turn the next free number into an edit of this test. What is pinned is
the contract the fourteen later rows build against: exactly ten metric ids,
the four invariants the plan-gate asked for, and a title/filename number that
agree.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DIR = REPO_ROOT / "docs" / "governance" / "decisions"
ADR_GLOB = "ADR-*-dagelijkse-doorloopmeting.md"

# The ten ids of plan decision 3 and the rule table of decision 26, verbatim.
REQUIRED_TEN = {
    "merges",
    "pr_open_to_merge_median_hours",
    "pr_open_to_merge_p75_hours",
    "rounds_per_pr",
    "ci_duration_seconds_median",
    "gate_duration_seconds_by_seat",
    "silence_windows_over_30m",
    "operator_wait_hours",
    "cost_per_merged_pr_usd",
    "merges_without_receipt_count",
}


def _adr_paths() -> list[Path]:
    return sorted(DECISIONS_DIR.glob(ADR_GLOB))


def _single_adr() -> Path:
    paths = _adr_paths()
    assert len(paths) == 1, (
        f"expected exactly one {ADR_GLOB} under docs/governance/decisions/, "
        f"found {[p.name for p in paths]}"
    )
    assert paths[0].is_file(), f"{paths[0]} is not a file"
    return paths[0]


def _text() -> str:
    return _single_adr().read_text(encoding="utf-8")


class TestAdrDailyMetricsDocument:
    def test_exactly_one_adr_with_the_slug(self):
        assert _single_adr().is_file()

    def test_declares_exactly_the_ten_metric_ids(self):
        text = _text()
        declared = set(re.findall(r"metric:\s*([a-z0-9_]+)", text))
        assert declared == REQUIRED_TEN, (
            "the ADR must declare exactly the ten plan ids; "
            f"missing={sorted(REQUIRED_TEN - declared)} extra={sorted(declared - REQUIRED_TEN)}"
        )

    def test_unknown_is_never_zero(self):
        text = _text()
        assert "unknown, nooit 0" in text, (
            "the ADR must state that a missing value is unknown and never 0"
        )

    def test_no_partial_sum_is_published(self):
        text = _text()
        assert "partiële som" in text, (
            "the ADR must state that a partial sum is never published as the total"
        )

    def test_settled_day_and_settle_hours(self):
        text = _text()
        assert "SETTLE_HOURS = 2" in text and "afgeronde dag" in text, (
            "the ADR must name the settled-day contract with SETTLE_HOURS = 2"
        )

    def test_structural_approval_with_its_residual_risk(self):
        text = _text()
        assert "structurele goedkeuringspoort" in text and "restrisico" in text, (
            "the ADR must name the structural approval gate and its residual risk"
        )

    def test_scope_reduction_of_ready_work(self):
        text = _text()
        assert "scopeverkleining" in text and "work_ready_coverage" in text, (
            "the ADR must state the scope reduction of 'klaarstaand werk' "
            "and the work_ready_coverage record"
        )

    def test_no_schema_change(self):
        text = _text()
        assert "geen schemawijziging" in text, (
            "the ADR must state that there is no schema change: no migration, "
            "no table, no prune"
        )

    def test_number_in_title_matches_filename(self):
        path = _single_adr()
        name_number = re.match(r"ADR-(\d+)-", path.name)
        assert name_number, f"{path.name} does not start with ADR-<number>-"
        title = _text().splitlines()[0]
        title_number = re.search(r"^#\s+ADR-(\d+)\b", title)
        assert title_number, f"first line is not an ADR title: {title!r}"
        assert int(title_number.group(1)) == int(name_number.group(1)), (
            f"title says ADR-{title_number.group(1)}, filename says ADR-{name_number.group(1)}"
        )
