# headless-builder-context

Selects which Claude settings levels a headless builder loads, defaulting to `project,local`.
When user settings are excluded, it carries `cleanupPeriodDays` and excludes user memory paths; selecting all three levels restores the original CLI arguments.

## Entry points

- [`scripts/lib/envelope_adapters_claude.py::_builder_cli_args`](../../scripts/lib/envelope_adapters_claude.py)

## Tests

- [tests/test_envelope_claude_adapter_setting_sources.py](../../tests/test_envelope_claude_adapter_setting_sources.py)

## Decisions

[ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md)

## Flag

`VNX_BUILDER_SETTING_SOURCES`
