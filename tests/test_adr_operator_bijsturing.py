"""Documentation contract for the operator-steering ADR (track operator-bijsturing-leert, row OB-0).

The code of rows OB-1 to OB-7 builds against this ADR, and code may not overtake
a decision. The ADR number is assigned by the T0, so the file is found by glob,
not by number. Each assert below is a concrete string the ADR must carry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DIR = REPO_ROOT / "docs" / "governance" / "decisions"
DOCS_INDEX = REPO_ROOT / "docs" / "DOCS_INDEX.md"


def _adr_path() -> Path:
    matches = sorted(DECISIONS_DIR.glob("ADR-*-operator-bijsturing-leert.md"))
    assert matches, "ADR-*-operator-bijsturing-leert.md ontbreekt"
    assert len(matches) == 1, f"meer dan één operator-bijsturing-ADR: {matches}"
    return matches[0]


@pytest.fixture(scope="module")
def adr() -> str:
    return _adr_path().read_text(encoding="utf-8")


def test_adr_number_is_072():
    assert _adr_path().name.startswith("ADR-072-"), "ADR-nummer is niet 072"


def test_adr_is_nygard_dutch(adr):
    assert adr.startswith("# ADR-072 - "), "titelregel '# ADR-072 - ' ontbreekt"
    assert "**Status:**" in adr, "Status-regel ontbreekt"
    for heading in ("## Context", "## Besluit", "## Consequenties", "## Terugdraaien"):
        assert heading in adr, f"kop {heading} ontbreekt"


def test_decisions_one_to_eighteen_each_numbered(adr):
    decision = adr.split("## Besluit", 1)[1].split("\n## Consequenties", 1)[0]
    numbers = [int(m) for m in re.findall(r"^### (\d+)\. ", decision, flags=re.M)]
    assert numbers[:18] == list(range(1, 19)), f"besluiten 1 tot en met 18 niet genummerd en in volgorde: {numbers}"


@pytest.mark.parametrize(
    "needle, what",
    [
        ("steering_sources.json", "het bindingsbestand steering_sources.json"),
        ("steering_sources/1", "het schema steering_sources/1"),
        ("STEERING_CLASSES", "de klassetabel STEERING_CLASSES"),
        ("handmatige_actie", "de klasse handmatige_actie"),
        ("correctie", "de klasse correctie"),
        ("steering_report/1", "het schema steering_report/1"),
        ("steering_report.json", "het bestand steering_report.json"),
        ("steering_rca/1", "het schema steering_rca/1"),
        ("steering_proposal_store", "de module steering_proposal_store"),
        ("steering_proposals.json", "de projectie steering_proposals.json"),
        ("steering_proposal_decisions.ndjson", "het besluitrecord steering_proposal_decisions.ndjson"),
        ("steering_cases.ndjson", "het case-grootboek steering_cases.ndjson"),
        ("proposal_id", "de voorstel-identiteit proposal_id"),
        ("case_id", "de case-identiteit case_id"),
        ("insufficient_evidence", "de RCA-uitkomst insufficient_evidence"),
        ("suppressed_rejected", "de telling suppressed_rejected"),
        ("reopened", "het heropenrecord reopened"),
        ("com.vnx.auto-dream", "de launchd-job com.vnx.auto-dream"),
        ("ADR-066", "de verwijzing naar ADR-066"),
        ("ADR-065", "de verwijzing naar ADR-065"),
        ("vnx learning decide-steering", "het CLI-werkwoord decide-steering"),
        ("vnx learning steering proposals", "het CLI-werkwoord steering proposals"),
        ("operator_steering_rca.py", "het RCA-script operator_steering_rca.py"),
        ("OB-7", "de rij OB-7 als eigenaar van de dream-stap"),
    ],
)
def test_concrete_strings(adr, needle, what):
    assert needle in adr, f"{what} ontbreekt in de ADR"


def test_privacy_rule(adr):
    assert "Prompttekst blijft lokaal" in adr, "de privacyregel 'Prompttekst blijft lokaal' ontbreekt"
    assert "tellingen, prompt-id's en klassen, nooit tekst" in adr, "het rapport-zonder-tekst-verbod ontbreekt"
    assert "een test greept" in adr.lower(), "de testplicht op de privacyregel ontbreekt"
    assert "40 tekens" in adr, "de citaatweigering van 40 tekens ontbreekt"


def test_leren_blijft_voorstellen(adr):
    assert "Leren blijft voorstellen" in adr, "de regel 'Leren blijft voorstellen' ontbreekt"
    assert "Geen codepad past een voorstel toe zonder besluit in de store" in adr, (
        "de regel dat geen codepad een voorstel toepast zonder besluit ontbreekt"
    )


def test_producer_consumer_rule_for_dream(adr):
    assert "producent-consumentregel" in adr.lower(), "de producent-consumentregel van besluit 17 ontbreekt"
    assert "dream_liveness/1" in adr, "dream_liveness/1 (één producent) ontbreekt"
    assert "ADR-066 verandert niet" in adr, "de zin 'ADR-066 verandert niet' ontbreekt"


def test_replacing_decisions_name_what_they_replace(adr):
    for needle in ("vervangt besluit 5", "vervangt de sleutel van besluit 10", "vervangt de zin in besluit 11"):
        assert needle in adr, f"de vervangingsregel '{needle}' ontbreekt"


def test_rollback_paragraph(adr):
    section = adr.split("## Terugdraaien", 1)[1]
    assert "steering_sources.json" in section, "terugdraaien noemt het bindingsbestand niet"
    assert "geen datamigratie" in section.lower(), "terugdraaien zegt niet dat er geen datamigratie is"


def test_docs_index_lists_the_adr():
    index = DOCS_INDEX.read_text(encoding="utf-8")
    assert "ADR-072" in index, "DOCS_INDEX.md noemt ADR-072 niet"
    assert _adr_path().name.split(".")[0].startswith("ADR-072") and "operator-bijsturing" in index.lower(), (
        "DOCS_INDEX.md-regel noemt het ADR-bestand niet"
    )
