# event-streams

Persists provider events as NDJSON streams, validating CanonicalEvent objects at append time. Streams are separated by terminal or run and rotate into dispatch archives at dispatch boundaries or size limits.

Background: [event stream lifecycle](../operations/EVENT_STREAMS.md), [receipt archive linkage](../core/11_RECEIPT_FORMAT.md).

## Entry points

[`scripts/lib/event_store.py::EventStore.append`](../../scripts/lib/event_store.py), [`scripts/lib/canonical_event.py::CanonicalEvent.validate_shape`](../../scripts/lib/canonical_event.py).

## Tests

[tests/test_event_store.py](../../tests/test_event_store.py), [tests/test_canonical_event.py](../../tests/test_canonical_event.py).

## Decisions

[ADR-016](../governance/decisions/ADR-016-unified-event-shape.md), [ADR-081](../governance/decisions/ADR-081-levenscyclus-van-het-eventarchief.md).

## Flag

—
