"""Existing extractor pipeline (production). The agent engine drives/extends this.

Extractors are pure functions `extract(raw: dict) -> dict`. The runner fans a raw
input through a registry of extractors and merges their outputs. Deterministic;
no network, no state. Network fetches happen UPSTREAM and are passed in as `raw`.
"""
from __future__ import annotations

from typing import Callable

Extractor = Callable[[dict], dict]

_REGISTRY: dict[str, Extractor] = {}


def register(name: str) -> Callable[[Extractor], Extractor]:
    def deco(fn: Extractor) -> Extractor:
        _REGISTRY[name] = fn
        return fn
    return deco


def run_pipeline(raw: dict, extractors: list[str] | None = None) -> dict:
    """Run the named extractors (or all) over one raw input; merge their dicts."""
    names = extractors or list(_REGISTRY)
    out: dict = {}
    for name in names:
        out[name] = _REGISTRY[name](raw)
    return out


@register("titles")
def _titles(raw: dict) -> dict:
    return {"title": raw.get("title", ""), "h1": raw.get("h1", "")}


@register("status")
def _status(raw: dict) -> dict:
    return {"status_code": raw.get("status_code")}
