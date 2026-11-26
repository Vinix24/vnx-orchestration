# Worker registry discovery

`scripts/lib/worker_registry.py` reads `.vnx/vnx_workers.yaml` from the project
root returned by `scripts/lib/project_root.py::resolve_project_root()`.
Without an explicit root argument, discovery starts from the current working
directory, not the fabric's installation directory. An unresolved project root
raises `RuntimeError`; an explicit `repo_root` remains supported.

Role validation runs the fabric's `scripts/validate_skill.py --list`, located
relative to the resolved `worker_registry.py` module path. Default loading,
including import and reload, never selects a validator from the consumer
project. A project without that script still has its roles validated against
the fabric's skill list. For controlled test callers, an explicit `repo_root`
retains its existing meaning: it binds both the project YAML root and the
validator root. The packaged default remains relative to the module.

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

AG-2a2 adds `resolve_worker(terminal_id=None, *, pool_id="default", spec=None,
repo_root=None, project_id=None)` without migrating callers. It returns a dict
with `provider`, `model`, `source` and `overridden`. Sources are `spec`, `env`
(the ADR-087 config reader), `registry` and `default`, in that order. Each
source yields a whole pair: a model infers its provider from the existing
`providers/provider_registry.py` catalogue; a provider alone requires that
catalogue's explicit `default_model`. Unknown models, conflicting pairs and
missing provider defaults raise `ValueError` with `worker_model_unknown`,
`worker_pair_conflict` or `worker_model_unresolved`. Read/config errors refuse.

Omitting `terminal_id` selects a pool, without terminal env overrides. Only
builder rows count; `orchestrator` is excluded. Multiple builder pairs require
`pools[].default_pair: {provider, model}`, validated against the catalogue;
otherwise resolution raises `pool_default_ambiguous` naming the pool. Missing
targets fall through to the packaged registry; empty builder pools refuse.
Each successful resolution logs `worker_resolution: provider=<p> model=<m>
source=<s> overridden=<list>`. `overridden` lists populated lower sources,
excluding the shipped baseline. Thus a spec model of `kimi-k3` beats an env
provider of `claude` as a whole pair and reports `overridden=[env]`.
