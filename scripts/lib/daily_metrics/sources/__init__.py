"""Discovered source modules for the daily-metrics collector.

``collect.discover_sources`` imports every ``*.py`` in this directory except
this ``__init__`` and keeps the modules that expose a callable
``collect(ctx) -> Mapping[str, ScalarMetric | KeyedMetric]`` (decision 4).
Nothing else lives in this package: later source rows (PR-4 to PR-8) drop a
module here and the collector picks it up on the next run without a register
edit. This file stays a docstring-only anchor.
"""
