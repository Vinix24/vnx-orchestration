"""Contracttest voor het `dream_liveness/1`-ADR (track `dream-liveness-watch`,
rij PR-1).

Het ADR wordt op zijn vaste bestandsnaam gevonden: de T0 heeft nummer 066
toegewezen (het plan noemt 056, maar dat is bezet). Wat hier vastligt is het
contract waar PR-2 tot en met PR-7 tegen bouwen: de vier kopstukken waaronder
het contract is uitgeschreven, en de correctie van bevinding 7 — de
30-dagendrempel voor de reviewwachtrij is **nieuw** beleid en ADR-019 zet hem
niet. De tweede assert is daarom letterlijk: "legt geen maximumleeftijd".
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DIR = REPO_ROOT / "docs" / "governance" / "decisions"
adr = DECISIONS_DIR / "ADR-066-dream-liveness-watch.md"

REQUIRED_HEADINGS = {"schema", "nachten", "voortgang", "reviewleeftijd"}


def _text() -> str:
    """Read the ADR, failing on the existence assert when it is absent.

    Every test goes through this helper so that, on a tree without ADR-066,
    the whole file fails on `assert adr.exists()` rather than three tests
    failing on a raw FileNotFoundError.
    """
    assert adr.exists(), (
        f"ADR ontbreekt: {adr}. PR-1 levert het `dream_liveness/1`-contract "
        "en de 30-dagendrempel als nieuw beleid; zonder dit bestand heeft "
        "PR-2 tot en met PR-7 geen contract om tegen te bouwen."
    )
    return adr.read_text(encoding="utf-8")


class TestAdrDreamLivenessDocument:
    def test_adr_exists(self):
        assert adr.exists(), (
            f"ADR ontbreekt: {adr}. PR-1 levert het `dream_liveness/1`-contract "
            "en de 30-dagendrempel als nieuw beleid; zonder dit bestand heeft "
            "PR-2 tot en met PR-7 geen contract om tegen te bouwen."
        )
        assert adr.is_file(), f"{adr} is geen bestand"

    def test_adr_has_the_contract_headings(self):
        text = _text()
        assert {"schema", "nachten", "voortgang", "reviewleeftijd"} <= {
            h.strip() for h in re.findall(r"^## (.+)$", text, re.M)
        }, (
            "ADR-066 moet de niveau-2-kopstukken schema, nachten, voortgang en "
            "reviewleeftijd dragen; het contract is daarover uitgeschreven."
        )

    def test_30_day_threshold_is_new_policy_not_adr_019(self):
        text = _text()
        assert "legt geen maximumleeftijd" in text, (
            "ADR-066 moet met zoveel woorden zeggen dat ADR-019 geen "
            "maximumleeftijd legt (de correctie van bevinding 7): de "
            "30-dagendrempel is nieuw beleid dat hier wordt ingevoerd."
        )

    def test_number_in_title_matches_filename(self):
        name_number = re.match(r"ADR-(\d+)-", adr.name)
        assert name_number, f"{adr.name} begint niet met ADR-<nummer>-"
        title = _text().splitlines()[0]
        title_number = re.search(r"^#\s+ADR-(\d+)\b", title)
        assert title_number, f"eerste regel is geen ADR-titel: {title!r}"
        assert int(title_number.group(1)) == int(name_number.group(1)), (
            f"titel zegt ADR-{title_number.group(1)}, bestandsnaam zegt "
            f"ADR-{name_number.group(1)}"
        )
