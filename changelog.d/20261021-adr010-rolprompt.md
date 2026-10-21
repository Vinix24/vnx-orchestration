- Review gates (dispatch 20261021-adr010-rolprompt): the reviewer and security-engineer role prompts summarise ADR-010 as ADR-010 says it.
  Claude is reached only through the `claude` CLI subprocess; `severity: error` covers an Anthropic SDK import, direct HTTP to `api.anthropic.com` or a REST shim around it, LiteLLM for Claude, and an Anthropic API key or OAuth token in code or config.
  A non-Claude provider called over HTTP or LiteLLM with its own key is outside ADR-010 and no longer a finding under it; the entry cites the ADR line that permits it.
  The old "LLM delivery happens exclusively via a CLI subprocess" made codex block a DeepSeek HTTP lane on one run and pass the same question on another.
  `tests/test_adr010_scope_prompts.py` pins the wording through `PromptAssembler`, the path the gates read.
