# business-light governance variant — schema draft (w16-1)

This file documents the design of the `business-light` governance variant introduced in w16-1. The actual production file lands at `docs/governance/variants/business-light.yaml` (or wherever Phase 9 placed variant definitions).

## Purpose

`business-light` is the policy bundle for content/marketing/sales-domain orchestrators and workers. It exists because the existing `coding-strict` variant — with mandatory `codex_gate`, hard PR-size limits, source-quality gates, and human-only merge — is wrong-shaped for content artifacts:

- Codex tone is wrong for prose review (codex is a code-domain reviewer; it has no view on whether a blog reads well).
- PR size limits are arbitrary for content artifacts (a 2000-word blog post is one artifact, not a "large PR").
- Source-quality gates (linters, type checks) don't apply.
- Auto-merge for low-risk content is acceptable provided gemini_review passes (operator can spot-check).

**What stays the same** (NOT relaxed):
- Capability-token verification (every dispatch is signed; verifier rejects forged or out-of-scope tokens).
- Receipt audit trail (every dispatch + response is recorded in NDJSON).
- Cross-domain isolation (marketing-lead cannot dispatch to code-domain workers).
- Permission denials (Bash, code execution still denied for content workers).

## Schema (proposed)

```yaml
# docs/governance/variants/business-light.yaml
variant_id: business-light
schema_version: 1
description: >
  Governance variant for content/marketing/sales-domain orchestrators and workers.
  Drops code-quality gates, allows auto-merge for low-risk content artifacts,
  retains capability-token enforcement and audit trail.

# --- Per-PR review stack (what gates run on every PR) ---
per_pr_gates:
  required:
    - gemini_review
  excluded:
    - codex_gate          # codex is wrong-tool-for-the-job for prose
  optional:
    - claude_github_optional  # operator can opt-in per repo

# --- Feature-end gates (run on the last wave of a feature) ---
feature_end_gates:
  required:
    - gemini_review
  excluded: []
  optional:
    - codex_gate          # ONLY when the feature touches code (e.g. MCP server) — opt-in per feature
    - claude_github_optional

# --- PR size policy ---
pr_size_limit:
  enabled: false
  rationale: |
    Content artifacts are sized by the artifact, not by lines. A 2000-word
    blog is one artifact, not a "large PR". Disable hard limits; operator
    spot-checks via gemini_review and (if applicable) operator-graded review.

# --- Auto-merge policy ---
auto_merge:
  enabled: true
  conditions:
    - all_required_per_pr_gates_passed: true
    - risk_class: low
    - no_open_blocking_items: true
    - artifact_type_in: [blog, linkedin_post, linkedin_carousel, retrospective, content_calendar, seo_report]
  human_override: always_allowed
  rationale: |
    Low-risk content artifacts can auto-merge once gemini_review passes.
    Operator retains override at any time. High-risk artifacts (e.g. sales
    outreach to real prospects) escalate to manual.

# --- Permitted tool surface (default for workers under this variant) ---
default_worker_permissions_template: content-worker.yaml

# --- Capability-token requirements (UNCHANGED from coding-strict) ---
capability_tokens:
  required: true
  attenuation_required_for_subdispatch: true
  signature_algorithm: ed25519

# --- Receipt audit (UNCHANGED) ---
receipts:
  required: true
  ndjson_audit_trail: true
  redact_credentials: true   # belt-and-braces; MCP servers also redact

# --- Cross-domain restrictions ---
cross_domain:
  outbound_dispatch_allowed_domains: [marketing, sales]   # variant carriers can only target these
  outbound_dispatch_forbidden_domains: [code, ops, research]
  memory_query_allowed_partitions:
    - vec_artifacts_marketing
    - vec_artifacts_sales
    - vec_operator_prefs   # always shared
  memory_query_forbidden_partitions:
    - vec_artifacts_code
    - vec_artifacts_ops
    - vec_artifacts_research

# --- Provider chain restrictions (variant-level default; orchestrator can narrow) ---
provider_chain:
  default: [claude, gemini]
  excluded: [codex]
  rationale: |
    Codex tone is unsuitable for prose orchestration and prose review.
    Default exclusion is variant-level; orchestrators inherit unless they
    explicitly override (and overrides are logged in decisions.ndjson).

# --- Failure semantics ---
on_provider_chain_exhausted: escalate_to_main
on_capability_token_invalid: reject_dispatch_with_audit
on_cross_domain_violation: reject_dispatch_with_audit
on_permission_denied: reject_dispatch_with_audit
```

## Companion: `content-worker.yaml` permissions template

```yaml
# .claude/agents/_templates/permissions/content-worker.yaml
template_id: content-worker
schema_version: 1
description: >
  Default permissions for any content-domain worker (blog-writer,
  linkedin-writer, seo-analyst, ga4-analyst, future sales-outreach worker).

allowed_tools:
  - Read
  - Write
  - WebFetch
  - Glob
  - Grep
denied_tools:
  - Bash
  - Edit:
      pattern_deny:
        - "*.py"
        - "*.ts"
        - "*.tsx"
        - "*.sh"
        - "*.yaml"   # workers don't edit governance/runtime files
        - "*.yml"
        - "scripts/**"
        - ".vnx/**"
        - ".claude/agents/**"
        - "docs/governance/**"

# MCP grants (per-worker; this template provides none — workers extend in their own permissions.yaml)
mcp_grants:
  default: []   # explicit grants only

# Filesystem write surface (where the worker may write outputs)
write_paths_allowed:
  - "claudedocs/blog-drafts/**"
  - "claudedocs/linkedin-drafts/**"
  - "claudedocs/seo-reports/**"
  - "claudedocs/ga4-reports/**"
  - ".vnx-data/unified_reports/**"   # for receipts
write_paths_denied:
  - "scripts/**"
  - ".vnx/**"
  - ".claude/agents/**"
  - "docs/governance/**"
  - "*.py"
  - "*.ts"

# Failure semantics
on_denied_tool_call: structured_error_in_receipt
on_write_outside_allowed_path: structured_error_in_receipt
```

## Validation requirements (w16-1 quality gate)

The variant schema validator must:
1. Reject a variant document missing `variant_id`, `per_pr_gates`, `feature_end_gates`, `pr_size_limit`, `auto_merge`, `capability_tokens`, or `cross_domain`.
2. Accept the proposed `business-light.yaml` end-to-end.
3. Refuse a request from a `business-light` orchestrator to dispatch a worker whose permissions resolve to `coding-strict` (cross-variant dispatch is forbidden by default).
4. Log every variant resolution to `decisions.ndjson` with `scope: governance_variant_resolved`.

## Backward-compat invariant

Existing `coding-strict` orchestrators (e.g. tech-lead, the existing T0 stack) are NOT affected. The variant registry is additive. Phase 9 schema is unchanged. Only the registry's variant set grows.
