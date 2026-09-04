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
"""
from __future__ import annotations

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
