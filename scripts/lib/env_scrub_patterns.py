"""env_scrub_patterns.py — the default set of env-var name patterns to strip from a
claude subprocess's environment before Popen.

Measured gap (2026-09-03): SubprocessAdapter.deliver() and spawn_claude() accept a
``scrub_env_keys`` parameter, but only the DeepSeek- and GLM-harness lanes ever passed
it (their own ``_HARNESS_SCRUB_KEYS``, an exact-name frozenset scoped to their own
account-safety concern). Every other production spawn path — the default headless lane
(envelope_adapters_claude.py), the terminal-pinned subprocess lane
(subprocess_dispatch_internals/delivery.py), the benchmark path (provider_dispatch.py),
and the direct-deliver stream_events() path (adapters/claude_adapter.py) — passed
``None``, so the worker subprocess inherited the FULL ambient environment, secrets
included (``VNX_SMTP_PASS`` measured present in-process).

These are glob PATTERNS (matched with ``fnmatch.fnmatchcase`` against each env var name
in SubprocessAdapter.deliver(), not exact keys) — a literal name with no wildcard chars
still matches only itself. ``CLAUDE_CODE_OAUTH_TOKEN``, ``ANTHROPIC_API_KEY``,
``ANTHROPIC_AUTH_TOKEN`` and ``VNX_SMTP_PASS`` are already covered by the ``*_TOKEN``/
``*_KEY``/``*_PASS`` globs below; they are listed explicitly anyway so they stay
scrubbed even if the glob set is narrowed later — the first three because an inherited
value can silently displace a valid CLI login ahead of it in the auth precedence order
(measured 2026-08-31), not only because they are secrets.

OI-1619 (2026-09-04): the same gap existed a level below SubprocessAdapter — the three
provider lanes that Popen a worker CLI directly instead of going through
SubprocessAdapter.deliver() (``provider_spawns/kimi_spawn.py``, ``codex_spawn.py``,
``gemini_spawn.py``) each built their child env as ``{**os.environ, **(extra_env or
{})}`` with no scrub call at all — not even the narrower per-lane pattern
``litellm_spawn.py`` uses for its own external-model lane. ``scrub_env()`` below is the
free-function counterpart of SubprocessAdapter.deliver()'s inline fnmatch loop, for
callers that Popen directly and have no SubprocessAdapter to route through.
"""
from __future__ import annotations

import fnmatch
from typing import Dict, Iterable, Mapping

DEFAULT_SCRUB_KEY_PATTERNS: frozenset = frozenset({
    "*_PASS",
    "*_PASSWORD",
    "*_SECRET",
    "*_TOKEN",
    "*_KEY",
    "VNX_SMTP_PASS",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
})


def scrub_env(env: Mapping[str, str], patterns: Iterable[str]) -> Dict[str, str]:
    """Return a copy of *env* with every key matching *patterns* removed.

    *patterns* are fnmatch-style globs matched with ``fnmatch.fnmatchcase`` against
    each key name — the same matching rule SubprocessAdapter.deliver() applies inline
    (subprocess_adapter.py), so a direct-Popen spawn lane (one with no
    SubprocessAdapter to route through — see OI-1619 above) scrubs identically to the
    claude lane rather than inventing a second rule. A literal name with no wildcard
    character still matches only itself.
    """
    scrubbed = dict(env)
    for _key in list(scrubbed.keys()):
        if any(fnmatch.fnmatchcase(_key, _pattern) for _pattern in patterns):
            scrubbed.pop(_key, None)
    return scrubbed
