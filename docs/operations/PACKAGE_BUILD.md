# PACKAGE_BUILD — vnx-orchestration operator runbook

Covers local development builds, editable installs, and packaging smoke tests
for the `vnx-orchestration` pip package. Published to PyPI 2026-07-02
(`pip install vnx-orchestration`, `v1.0.0`); this runbook covers building and
verifying a wheel locally, not the publish step itself.

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

The package builds from the repo root. There is no separate `dist/`
source subdirectory:

```bash
python -m build --wheel .
```

Output appears in `dist/*.whl` at the repo root (gitignored).

The version string is single-sourced from the root `VERSION` file
(`[tool.setuptools.dynamic] version = {file = ["VERSION"]}` in
`pyproject.toml`), which `vnx_cli.__version__` also reads back at runtime.
There is no `setuptools_scm` fallback version.

## Editable install (development mode)

```bash
# From repo root:
pip install -e .
```

Editable installs link directly to the source tree, so code changes take effect
immediately without reinstalling.

## Verify imports and entry point

```bash
python -c 'import vnx_cli; print(vnx_cli.__version__)'
vnx --version
```

## Packaging smoke tests

Two scripts exercise the built wheel end to end, each building it from repo
root if no wheel path is supplied:

```bash
# Profile D (CI): wheel hygiene (no __pycache__/.pyc), pip-install-mode
# path resolution (catches hardcoded Path(__file__).parents[N] regressions)
bash scripts/ci/pip_install_smoke.sh

# Fresh-venv functional smoke: vnx --version, vnx doctor, VNX_HOME
# resolution into site-packages, pip-native state layout in a pristine HOME
bash scripts/test_wheel_install.sh
```

`scripts/ci/pip_install_smoke.sh` runs in CI as Profile D (`.github/workflows/vnx-ci.yml`).

## Notes

- `dist/` (wheel output) is gitignored at the repo root. Do not commit built
  artifacts.
- Two distributions ship from the same wheel: `vnx_cli` (the importable
  console-script package) and `vnx_orchestration` (a PEP 420 namespace package
  mapped onto the repo root via `[tool.setuptools.package-dir]`, carrying the
  engine trees `scripts/`, `schemas/`, `skills/`, `templates/`, `configs/`,
  `hooks/`, `examples/`, `agents/` as package data). The engine is loaded by
  path injection (`sys.path.insert` on `scripts/lib`), not imported as
  `vnx_orchestration.scripts.*`.
- `[tool.setuptools.exclude-package-data]` strips `__pycache__`, `.pyc`, logs,
  tests, and the benchmark suite (`scripts/benchmark/`, `scripts/benchmarks/`,
  `scripts/llm_benchmark.py`) from the wheel: dev/research tooling that stays
  repo-only.
