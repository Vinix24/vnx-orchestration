# vnx-orchestration

VNX glass-box governance for multi-agent coordination — pip-installable package.

## Status

**Phase 0a** (current): package skeleton. The `vnx_core` and `vnx_cli` modules are
placeholders. Real runtime modules migrate in Phase 1.

## Installation

### Editable install (development / local repo)

```bash
pip install -e dist/vnx-orchestration
```

### With OTel export support

```bash
pip install -e "dist/vnx-orchestration[otel]"
```

### From PyPI (Phase 3+)

```bash
pip install vnx-orchestration
```

## CLI

After install the `vnx` entry point is available:

```bash
vnx --version
```

Phase 0a prints a placeholder. Real CLI surface (dispatch, receipt, queue, gate
commands) lands in Phase 1+.

## Documentation

- Full operator docs: see [README.md](../../README.md) at repo root.
- Build / package runbook: see [docs/operations/PACKAGE_BUILD.md](../../docs/operations/PACKAGE_BUILD.md).
- Architecture: see [docs/](../../docs/) for all design docs.

## Package layout

```
dist/vnx-orchestration/
  pyproject.toml          — PEP 621 build metadata
  vnx_core/               — Runtime libraries (subprocess dispatch, intelligence, etc.)
  vnx_cli/                — CLI entry point
  tests/                  — Package-level smoke tests
```

## Roadmap

| Phase | Scope |
|-------|-------|
| 0a (this PR) | Package skeleton, build validation, smoke tests |
| 1 | Migrate `scripts/lib/` core modules into `vnx_core` |
| 2 | Wire shim layer in `scripts/lib/` to import from `vnx_core` |
| 3 | PyPI publish, versioned releases |

## License

MIT — see [LICENSE](../../LICENSE).
