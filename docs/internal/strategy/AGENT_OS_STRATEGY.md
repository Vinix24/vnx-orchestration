# Agent OS Strategy

**Status**: Internal Reference  
**Scope**: Strategic vision, domain expansion model, public/private layering  
**Audience**: T0 orchestrator, architects, operators expanding VNX beyond the coding domain  
**Last Updated**: 2026-05-26

---

## 1. Purpose

This document defines the strategic vision for VNX as a shared substrate across
multiple agent domains, the governance model for expanding beyond the coding-first
core, and the structural boundaries that keep that expansion safe and reversible.

It is the companion document to:
- `docs/_archive/contracts/AGENT_OS_LIFT_IN_CONTRACT.md` — the implementable
  substrate boundary contract (Feature 19)
- `docs/manifesto/ROADMAP.md` — the public roadmap with feature and wave history

---

## 2. Core Principle

VNX started as a coding orchestration runtime. That origin is an asset, not a
constraint. The coding domain provides a proven, battle-tested substrate with
strong governance, real receipts, and high test coverage. Every subsequent domain
builds on top of that substrate — it does not replace it.

The expansion model is:

1. Prove governance properties in the coding domain first.
2. Extract stable abstractions into a reusable substrate layer.
3. Add new domains by declaring a capability profile and governance profile — not
   by forking or reimplementing the substrate.

Domain expansion does not weaken the coding domain. New profiles operate at their
own governance tier without inheriting coding-strict guarantees they have not earned.

---

## 3. Domain Expansion Model

### 3.1 Defined Domains

| Domain | Governance Profile | Status | Scope Model |
|--------|-------------------|--------|-------------|
| `coding` | `coding_strict` | Production | Git worktrees |
| `business` | `business_light` | Pilot (post-Feature 20) | Folder-scoped |
| `regulated` | `regulated_strict` | Planned (post-Feature 21) | Sandbox |

### 3.2 Governance Profiles Are Not "Light vs Heavy"

The `business_light` label does not mean weaker governance. It means a different
governance emphasis:

- **Coding-strict** is heavy on code-review gates (Codex + Gemini), PR-based
  change management, worktree isolation, and test evidence.
- **Business-light** is heavy on cost tracking, uptime monitoring, and
  content-approval workflows. It uses review-by-exception rather than mandatory
  gate passes on every dispatch.
- **Regulated-strict** adds audit bundle requirements, extended retention, and
  explicit operator escalation for all closure decisions.

Each profile is a different governance surface tuned to domain risk. The substrate
enforces whatever the declared profile requires — it does not make judgments about
which profile is more serious.

### 3.3 Coding-First Wedge Is the Credibility Model

The coding domain is the adoption wedge and the credibility anchor. When VNX is
described as governance-first, the evidence is the coding runtime: 1,100+ receipts,
86%+ test pass rate, deterministic SPC-grade quality metrics, and open-source
codebase at `github.com/Vinix24/vnx-orchestration`.

Business and regulated domains inherit this credibility only by using the same
substrate under their own governance profile — not by proximity.

---

## 4. Three-Tier Public/Private Layering

VNX operates across three structural tiers with distinct ownership, disclosure
posture, and stability expectations.

### 4.1 Tier Definitions

| Tier | Name | Visibility | Description |
|------|------|-----------|-------------|
| 1 | Engine | Public (now) | `vnx-orchestration` open-source runtime and substrate |
| 2 | Business OS Framework | Private (now), public-option after burn-in | Reusable patterns for business-domain orchestration |
| 3 | Business Data + Client Config | Always private | Supabase data, secrets, client configurations |

**Tier 1: Engine (public)**

The open-source runtime on `github.com/Vinix24/vnx-orchestration`. This is the
substrate layer: dispatch lifecycle, lease management, receipt pipeline, session
tracking, open items, intelligence injection, governance enforcement, runtime
adapters, capability profiles. Everything in this tier is designed for community
adoption and has no domain-specific assumptions.

Tier 1 is the adoption wedge. It demonstrates governance-first AI orchestration
without requiring any business context. Credibility comes from the codebase itself:
working code, test coverage, NDJSON receipts, and public history.

**Tier 2: Business OS Framework (private now, public-option after burn-in)**

The reusable patterns for how business-domain orchestration layers on top of the
governance-first runtime. This includes governance profiles for business and
regulated domains, architecture decision records for business-specific design
choices, orchestration patterns for content approval and cost gating, and
agent directories (`agents/blog-writer/`, `agents/linkedin-writer/`, etc.).

Tier 2 is deferred open-source, following the same coding-first wedge approach
VNX itself used: build traction and real production evidence before exposing the
design to external scrutiny. Premature publication creates reputational risk on
unproven patterns and makes refactoring more expensive once it acquires external
users.

Tier 2 can only be cleanly separated for future publication if it has never been
entangled with Tier 1 or Tier 3. See Section 4.2.

**Tier 3: Business Data + Client Config (always private)**

Supabase data, leads, client configurations, n8n endpoints, secrets, and any
personally identifiable data. This tier never becomes public. No architectural
decision changes this. The local-first constraint on VNX receipts (`docs/manifesto/
OPEN_METHOD.md`) reflects the same principle applied to the data layer: receipts
stay local, business data stays in Supabase, neither crosses to the other.

### 4.2 Tier Separation Is a Structural Requirement, Not a Policy Choice

The viability of ever publishing Tier 2 depends entirely on whether Tier 2 is
kept clean of Tier 1 internals and Tier 3 data from the start.

If business-domain orchestration logic forks or monkey-patches the engine (Tier 1),
extracting a clean Tier 2 for publication becomes architecturally impossible —
the business patterns are entangled with implementation details that cannot be
disclosed. The same contamination happens in reverse if Tier 2 development directly
references Tier 3 secrets or client data.

The central-install model enforces this boundary structurally. When VNX is installed
centrally via `install-central.sh` with a read-only `VNX_HOME`, project-level code
cannot fork or modify the engine. The engine is consumed as a distribution artifact,
not as editable source within the business workspace. This makes the engine/framework
separation a hard runtime property, not a naming convention.

The parallel to the data layer is exact:

> VNX receipts stay local. They do not go to Supabase.

Applied to the code/config axis:

> Tier 1 engine code stays in the open-source repo. It does not get modified inside
> Tier 2 business workspaces.

Both constraints protect the same property: clean tier boundaries that can be
audited and maintained as the system grows.

### 4.3 Decision Rule for Tier Assignment

When adding new functionality, apply this decision rule:

| Is it reusable by any future domain? | Is it business-specific? | Does it involve secrets/data? | Tier |
|--------------------------------------|--------------------------|-------------------------------|------|
| Yes | No | No | 1 — engine |
| No | Yes | No | 2 — framework |
| Any | Any | Yes | 3 — private data |

When in doubt between Tier 1 and Tier 2, default to Tier 2 until proven reusable.
Promoting a pattern from Tier 2 to Tier 1 is a clean direction (abstraction). The
reverse — demoting Tier 1 with business assumptions — is an architectural regression.

---

## 5. Shared Substrate Design

The substrate (Tier 1) is the layer that all domains share without reimplementation.
The detailed boundary definition is in `AGENT_OS_LIFT_IN_CONTRACT.md`. The key
invariants for strategy purposes:

- **S-1**: No substrate change may break existing coding domain behavior.
  Coding domain tests are the regression gate for every substrate evolution.
- **S-2**: No domain-specific logic may enter substrate modules. Domain logic
  lives in domain-layer files or Tier 2 framework patterns.
- **S-3**: The substrate must be usable by a new domain with zero coding-specific
  knowledge. Only a declared capability profile and governance profile are required.
- **S-4**: Domain imports from the substrate are expected. Substrate imports from
  any domain layer are forbidden.

These are not design preferences. They are the conditions under which multi-domain
expansion remains reversible and auditable.

---

## 6. Governance Across Domains

### 6.1 Governance Profiles Are Declared, Not Inferred

A domain cannot operate without registering a capability profile. The profile
declares what governance the domain requires — gate types, closure rules, retention
policy, scope isolation model, evidence requirements. The substrate enforces whatever
the profile specifies.

This design makes governance intent explicit and machine-verifiable. A future
operator reading the profile knows exactly what was required during a given session,
without reconstructing it from receipts.

### 6.2 Cross-Domain Contamination Is Forbidden

A coding dispatch cannot be executed by a business worker. Domain isolation is
enforced at the dispatch level (see `AGENT_OS_LIFT_IN_CONTRACT.md` §6.2, invariant
G-3). Each domain operates in its own dispatch namespace.

This constraint makes multi-domain operation compositional rather than entangled:
adding a new domain does not change routing behavior for existing domains.

### 6.3 Governance Profiles Do Not Self-Mutate

No governance profile allows automatic instruction edits. `policy_mutation_blocked`
is `True` for all profiles by default. Relaxing this requires an explicit operator
decision recorded in the audit trail. This applies to Tier 2 business profiles
as much as it does to the coding-strict profile.

---

## 7. Activation Sequence for New Domains

A non-coding domain is not activated by writing code. It is activated by completing
this sequence:

1. Define a capability profile with all required fields validated.
2. Define a governance profile with passing conformance tests.
3. Implement and test the scope isolation model for the domain.
4. Implement any domain-specific gates required by the profile.
5. Operator explicitly enables the domain via profile registration.
6. Activation decision is recorded in the audit trail.

Any domain that skips these steps is not a registered domain — it is an ad hoc
configuration with no substrate guarantees. The Feature 20 pilot
(`business_light` + folder-scoped orchestration) is the reference implementation
of this sequence for the business domain.

---

## 8. Relationship to Open-Source Strategy

VNX's open-source publication strategy mirrors the domain expansion model: prove
it works internally before exposing it externally.

The coding runtime was published once it had production receipts, real test
coverage, and a stable governance contract. The same bar applies to the Business
OS Framework before it becomes a Tier 2 candidate for publication. Publishing
underbaked business patterns would undermine the credibility that the Tier 1
engine establishes.

The three-tier model protects this:

- Tier 1 is always the public credibility anchor.
- Tier 2 is published only when it has the same quality signal as Tier 1 at
  publication time — real production patterns, documented governance profiles,
  and auditable evidence.
- Tier 3 is never published and contains no design information that needs to be
  public.

---

## 9. Anti-Goals

| Anti-Goal | Reason |
|-----------|--------|
| Fork the engine inside a business workspace | Destroys Tier 1/2 separation; blocks future Tier 2 publication |
| Merge Tier 3 secrets into Tier 1 or Tier 2 artifacts | Violates local-first data constraint; creates compliance risk |
| Publish Tier 2 before burn-in evidence exists | Same mistake as shipping an unproven governance profile as `regulated_strict` |
| Use proximity to Tier 1 as a substitute for earned governance properties | Business domains inherit the substrate, not the coding domain's credibility |
| Add business logic to substrate modules | Invariant S-2 violation; blocks future domain additions |
| Treat governance profiles as cosmetic labels | Profiles are machine-enforced contracts, not documentation markers |

---

## 10. Document Relationships

| Document | Role |
|----------|------|
| This document | Strategic vision and tier model |
| `AGENT_OS_LIFT_IN_CONTRACT.md` | Implementable substrate boundary (Feature 19) |
| `ROADMAP.md` | Public roadmap with feature and wave history |
| `GOVERNANCE_ARCHITECTURE.md` | Decision framework and gate enforcement |
| Feature 20 plan | First business_light pilot implementation |
| Feature 21 plan | Regulated-strict profile and audit bundle |
