"""OB-2: the class table of ADR-072 decision 4, Dutch and English.

Every prompt below is an invented paraphrase; none is copied from a real transcript.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import operator_steering_class as osc

EXPECTED_ORDER = (
    "handmatige_actie",
    "onderbreking",
    "correctie",
    "nieuw_besluit",
    "vrijgave",
    "statusvraag",
    "vraag",
    "ongeclassificeerd",
)

# (prompt, expected classes in table order)
PARAPHRASES = [
    ("!git status", ("handmatige_actie",)),
    ("  ! bin/vnx doctor", ("handmatige_actie",)),
    ("[Request interrupted by user for tool use]", ("onderbreking",)),
    ("[Request interrupted by user]", ("onderbreking",)),
    ("Dat klopt niet, de teller staat op nul.", ("correctie",)),
    ("Ik bedoelde de tweede map.", ("correctie",)),
    ("That is wrong, the file lives elsewhere.", ("correctie",)),
    ("I meant the staging branch.", ("correctie",)),
    ("Voortaan eerst meten en dan pas bouwen.", ("nieuw_besluit",)),
    ("Vanaf nu draait elke bouwer op het eigen account.", ("nieuw_besluit",)),
    ("From now on every merge goes through the door.", ("nieuw_besluit",)),
    ("Going forward, stop using the old lane.", ("nieuw_besluit",)),
    ("Akkoord", ("vrijgave",)),
    ("Ga door met de volgende rij.", ("vrijgave",)),
    ("Go ahead and merge it.", ("vrijgave",)),
    ("LGTM", ("vrijgave",)),
    ("Wat is de stand van zaken bij de track?", ("statusvraag", "vraag")),
    ("Waar staan we met de sprint", ("statusvraag", "vraag")),
    ("What's the status of the review gate", ("statusvraag",)),
    ("How far are we with the release?", ("statusvraag", "vraag")),
    ("Waarom faalt de tweede stoel", ("vraag",)),
    ("Kun je de logs laten zien?", ("vraag",)),
    ("Why does the lease expire so early", ("vraag",)),
    ("Is there a way to reopen it?", ("vraag",)),
]

NO_CLASS = [
    "",
    "   ",
    "De bouwer is klaar met de tests.",
    "Kijk even naar de logs van gisteren.",
    "Review the decisions file tomorrow.",
    "The song is long.",
    "Tests are green on the branch.",
    "Gogh was een schilder.",
    "Het statusbord hangt aan de muur.",
]


def test_table_is_ordered_and_closed() -> None:
    assert tuple(c.name for c in osc.STEERING_CLASSES) == EXPECTED_ORDER
    for cls in osc.STEERING_CLASSES:
        assert cls.definition
        if cls.name != "ongeclassificeerd":
            assert cls.markers


def test_every_class_has_dutch_and_english_markers() -> None:
    for cls in osc.STEERING_CLASSES:
        if cls.name in {"ongeclassificeerd", "handmatige_actie", "onderbreking"}:
            continue
        assert {m.lang for m in cls.markers} >= {"nl", "en"}, cls.name


@pytest.mark.parametrize("text,expected", PARAPHRASES)
def test_paraphrases_classify(text: str, expected: tuple[str, ...]) -> None:
    assert tuple(m.klasse for m in osc.classify(text)) == expected


def test_at_least_two_paraphrases_per_class_nl_and_en() -> None:
    seen: dict[str, int] = {}
    for _text, expected in PARAPHRASES:
        for name in expected:
            seen[name] = seen.get(name, 0) + 1
    for name in EXPECTED_ORDER:
        if name != "ongeclassificeerd":
            assert seen.get(name, 0) >= 2, name


@pytest.mark.parametrize("text", NO_CLASS)
def test_no_match_returns_empty_never_a_default(text: str) -> None:
    assert osc.classify(text) == ()


def test_none_and_non_string_are_empty() -> None:
    assert osc.classify(None) == ()  # type: ignore[arg-type]
    assert osc.classify(42) == ()  # type: ignore[arg-type]


def test_two_classes_in_one_prompt() -> None:
    got = osc.classify("Nee, dat klopt niet. Ga door met de andere rij.")
    assert tuple(m.klasse for m in got) == ("correctie", "vrijgave")


def test_match_reports_the_marker() -> None:
    (match,) = osc.classify("Voortaan eerst meten.")
    assert match.klasse == "nieuw_besluit"
    assert match.marker == "voortaan"
    assert match.lang == "nl"
    (match,) = osc.classify("LGTM")
    assert match.marker == "lgtm"


@pytest.mark.parametrize(
    "a,b",
    [
        ("DAT KLOPT NIET", "dat klopt niet"),
        ("Ik bedoelde de map", "ik bedoélde de map"),
        ("Akkoord!", "akkoord"),
        ("VOORTAAN meten", "voortaan meten"),
        ("Wáár staan we?", "waar staan we?"),
        ("Dat   klopt\tniet.", "dat klopt niet"),
        ("That's wrong", "thats wrong"),
        ("That’s wrong", "that's wrong"),
    ],
)
def test_case_diacritics_and_punctuation_classify_the_same(a: str, b: str) -> None:
    assert osc.classify(a) and osc.classify(a) == osc.classify(b)


def test_normalize_text() -> None:
    assert osc.normalize_text("  Één  Dag,  Café! ") == "een dag cafe"
    assert osc.normalize_text("") == ""


def test_primary_class_falls_back_only_for_the_report() -> None:
    assert osc.primary_class("Ga door") == "vrijgave"
    assert osc.primary_class("Hoe ver zijn we?") == "statusvraag"
    assert osc.primary_class("De bouwer is klaar.") == "ongeclassificeerd"
    assert osc.primary_class("") == "ongeclassificeerd"


def test_classify_is_deterministic_and_pure() -> None:
    first = osc.classify("Hoe ver zijn we met 1.9?")
    assert first == osc.classify("Hoe ver zijn we met 1.9?")
    assert isinstance(first, tuple)
