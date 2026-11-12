#!/usr/bin/env python3
"""Class table of operator steering (ADR-072 decision 4), Dutch and English.

One ordered table of classes, each with a definition and its markers. `classify`
maps one prompt text to zero, one or more classes and names the marker that
matched, so a reader can check why. Deterministic: no model, no network, no I/O.

Normalisation is lowercase, diacritics folded, apostrophes dropped and other
punctuation turned into a space. Nothing more.

A prompt that matches no class gets an empty result from `classify`. The report
bucket `ongeclassificeerd` (ADR-072 decision 4) is applied by `primary_class`,
the one place that turns "no match" into a label for counting.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

UNCLASSIFIED = "ongeclassificeerd"

# Marker kinds.
RAW_PREFIX = "raw_prefix"  # the stripped, lowercased raw text starts with the value
PHRASE = "phrase"  # the value occurs as whole words in the normalised text
EXACT = "exact"  # the normalised text is exactly the value
STARTS = "starts"  # the normalised text starts with the value as whole words
QUESTION_MARK = "question_mark"  # the raw text contains a question mark


@dataclass(frozen=True)
class Marker:
    kind: str
    value: str
    lang: str


@dataclass(frozen=True)
class SteeringClass:
    name: str
    definition: str
    markers: tuple[Marker, ...]


@dataclass(frozen=True)
class ClassMatch:
    klasse: str
    marker: str
    lang: str


def _m(kind: str, lang: str, *values: str) -> tuple[Marker, ...]:
    return tuple(Marker(kind, v, lang) for v in values)


# Marker values are written in normalised form (lowercase, no diacritics,
# no apostrophes, no punctuation).
STEERING_CLASSES: tuple[SteeringClass, ...] = (
    SteeringClass(
        "handmatige_actie",
        "The operator runs a command by hand: the prompt begins with an exclamation mark.",
        _m(RAW_PREFIX, "any", "!"),
    ),
    SteeringClass(
        "onderbreking",
        "The operator interrupted the agent: the harness marker for an interrupted turn.",
        _m(RAW_PREFIX, "en", "[request interrupted by user"),
    ),
    SteeringClass(
        "correctie",
        "The operator says the previous result or step was wrong or not what was meant.",
        _m(PHRASE, "nl", "dat klopt niet", "klopt niet", "dat is fout", "dat is niet goed",
           "niet wat ik", "ik bedoelde", "ik bedoel", "verkeerd", "onjuist",
           "doe dat niet", "niet zo", "nee niet")
        + _m(PHRASE, "en", "that is wrong", "thats wrong", "that isnt right", "not what i",
             "i meant", "wrong", "incorrect", "do not do that", "dont do that",
             "not like that", "no not"),
    ),
    SteeringClass(
        "nieuw_besluit",
        "The operator states a decision or rule that holds from now on.",
        _m(PHRASE, "nl", "voortaan", "vanaf nu", "ik besluit", "besloten", "ga over op",
           "stop met", "niet meer")
        + _m(PHRASE, "en", "from now on", "going forward", "i decided", "i have decided",
             "switch to", "stop using", "no longer"),
    ),
    SteeringClass(
        "vrijgave",
        "The operator approves or releases a step the agent may take.",
        _m(EXACT, "nl", "akkoord", "go", "ok", "oke", "doe maar", "ja")
        + _m(PHRASE, "nl", "ga door", "ga verder", "je mag", "goedgekeurd", "mag door")
        + _m(EXACT, "en", "yes", "approved", "lgtm")
        + _m(PHRASE, "en", "go ahead", "proceed", "ship it", "lgtm", "you may"),
    ),
    SteeringClass(
        "statusvraag",
        "The operator asks where things stand.",
        _m(PHRASE, "nl", "hoe ver", "waar staan we", "stand van zaken", "wat is de stand",
           "wat is de status", "hoeveel nog", "wat staat er nog open")
        + _m(PHRASE, "en", "how far", "where are we", "where do we stand", "status update",
             "whats the status", "what is the status", "whats left", "what is left"),
    ),
    SteeringClass(
        "vraag",
        "The operator asks a question: a question mark or an interrogative opening.",
        _m(QUESTION_MARK, "any", "?")
        + _m(STARTS, "nl", "hoe", "wat", "waarom", "waar", "wie", "welke", "kan je",
             "kun je", "is er", "heb je")
        + _m(STARTS, "en", "how", "what", "why", "where", "who", "which", "can you",
             "could you", "is there", "do you", "does"),
    ),
    SteeringClass(
        UNCLASSIFIED,
        "Fallback bucket for the report: a prompt that matched no other class.",
        (),
    ),
)


def normalize_text(text: str) -> str:
    """Lowercase, fold diacritics, drop apostrophes, turn other punctuation into a space."""
    folded = unicodedata.normalize("NFKD", text).lower()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[\'’‘`]", "", folded)
    folded = re.sub(r"[^\w\s]|_", " ", folded)
    return " ".join(folded.split())


def _marker_hits(marker: Marker, raw: str, norm: str) -> bool:
    if marker.kind == RAW_PREFIX:
        return raw.startswith(marker.value)
    if marker.kind == QUESTION_MARK:
        return "?" in raw
    if marker.kind == EXACT:
        return norm == marker.value
    padded = f" {norm} "
    if marker.kind == STARTS:
        return padded.startswith(f" {marker.value} ")
    return f" {marker.value} " in padded


def classify(text: str) -> tuple[ClassMatch, ...]:
    """Every class whose marker matches, in table order, with the first marker that hit.

    Returns an empty tuple when nothing matches or the input is not a string.
    """
    if not isinstance(text, str):
        return ()
    raw = text.strip().lower()
    norm = normalize_text(text)
    if not norm and not raw:
        return ()
    found = []
    for cls in STEERING_CLASSES:
        for marker in cls.markers:
            if _marker_hits(marker, raw, norm):
                found.append(ClassMatch(cls.name, marker.value, marker.lang))
                break
    return tuple(found)


def primary_class(text: str) -> str:
    """The first class in table order, or `ongeclassificeerd` when nothing matched."""
    matches = classify(text)
    return matches[0].klasse if matches else UNCLASSIFIED
