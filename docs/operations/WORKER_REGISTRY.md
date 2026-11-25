# Worker registry discovery

`scripts/lib/worker_registry.py` reads `.vnx/vnx_workers.yaml` from the project
root returned by `scripts/lib/project_root.py::resolve_project_root()`.
Without an explicit root argument, discovery starts from the current working
directory, not the fabric's installation directory. An unresolved project root
raises `RuntimeError`; an explicit `repo_root` remains supported.

When the project has no override, the loader reads
`scripts/lib/defaults/vnx_workers.default.yaml` beside the package's modules.
The existing `scripts/**/*` package-data glob includes that file. This is the
single shipped default; the former `.vnx/vnx_workers.default.yaml` location in
ADR-013, ADR-018 and ADR-091 is historical. Copy the packaged file to the
project's `.vnx/vnx_workers.yaml` to customize workers or pools.

The legacy hardcoded fallback remains until AG-2d and emits a warning if both
files are absent. A selected file that cannot be read or parsed raises instead
of silently switching to another registry. AG-2a1 changes discovery only:
worker roles, provider/model pairs, pools and registry callers are unchanged.
