# PACKAGE_BUILD — vnx-orchestration operator runbook

Covers local development builds, editable installs, and test execution for the
`vnx-orchestration` pip package (Wave 2).

## Prerequisites

```bash
pip install --user build     # installs the 'build' frontend
pip install --user pytest    # test runner
```

Or inside the project venv:

```bash
pip install build pytest
```

## Build a wheel locally

```bash
cd dist/vnx-orchestration
python -m build --wheel
```

Output appears in `dist/vnx-orchestration/dist/*.whl`.

The version string is derived from `setuptools_scm` via the nearest git tag.
In a clean checkout without a matching tag, `fallback_version = "1.0.0-rc1.dev0"`
is used.

## Editable install (development mode)

```bash
# From repo root:
pip install -e dist/vnx-orchestration
```

Editable installs link directly to the source tree, so code changes take effect
immediately without reinstalling.

## Verify imports and entry point

```bash
python -c 'import vnx_core; import vnx_cli; print(vnx_core.__version__)'
vnx --version
```

## Run smoke tests

```bash
cd dist/vnx-orchestration
python -m pytest tests/test_smoke.py -v
```

Expected output (Phase 0a):

```
PASSED tests/test_smoke.py::test_vnx_core_import
PASSED tests/test_smoke.py::test_vnx_cli_import
PASSED tests/test_smoke.py::test_vnx_cli_runs
PASSED tests/test_smoke.py::test_vnx_cli_version_flag
4 passed
```

## Phase roadmap

| Phase | Scope |
|-------|-------|
| **0a** (this file) | Package skeleton — empty `vnx_core` / `vnx_cli`, smoke tests, build validation |
| **1** | Migrate `scripts/lib/` core modules into `vnx_core`; wire `dependencies` in pyproject.toml |
| **2** | Shim layer in `scripts/lib/` imports from `vnx_core` (backward-compat bridge) |
| **3** | PyPI publish via CI; versioned releases from git tags |

## Notes

- `dist/vnx-orchestration/dist/` (wheel output) is gitignored via
  `dist/vnx-orchestration/.gitignore`. Do not commit built artifacts.
- The `[tool.setuptools_scm] root = "../.."` setting anchors version detection to
  the repository root, not the package subdirectory.
- Do not add real runtime dependencies to `pyproject.toml` until Phase 1 module
  migration; premature deps bloat the install surface.
