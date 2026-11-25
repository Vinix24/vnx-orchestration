# docs-style-file

Appends a configured writing-style file when staging documentation or design tasks, or dispatches whose non-changelog paths are all documentation.
An empty setting disables it; an unusable configured file refuses staging, while a usable file contributes its resolved path and appended-text hash to the spec.

## Entry points

- [`scripts/lib/dispatch_bridge.py::is_docs_dispatch`](../../scripts/lib/dispatch_bridge.py)
- [`scripts/lib/dispatch_bridge.py::_apply_doc_style`](../../scripts/lib/dispatch_bridge.py)

## Tests

- [tests/test_doc_style_file.py](../../tests/test_doc_style_file.py)

## Decisions

[ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md)

## Flag

`VNX_DOC_STYLE_FILE`
