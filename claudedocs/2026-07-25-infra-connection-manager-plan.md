# Feature Plan: Infra Connection-Manager (governed provisioning agent)

**Status**: DRAFT — for plan-gate panel deliberation
**Date**: 2026-07-25
**Author**: dispatch-D-df331345 (planning agent)
**Priority**: P1
**Track**: B

## Business Problem

Every new project (last case: oncueassistant.com, 2026-07) requires hand-wiring infrastructure across vendor dashboards: DNS records at TransIP, domain attachment + env vars in Vercel, Workers/D1/Turnstile in Cloudflare, sending-domain verification in Resend, secrets in GCP. This is multi-hour, error-prone, un-receipted manual work that sits entirely outside the VNX governance perimeter. What breaks if not done: every future project repeats the same dashboard-clicking, with no audit trail, no idempotency, and no rollback record. The trigger class of work is "provision project infra" — it should be a governed dispatch, not a human clicking session.

The one decision the panel must deliberate hard is **where credentials live centrally and how they are injected at run time without ever entering the VNX audit trail** (§3). Vincent is explicitly skeptical of the macOS Keychain and asked whether API tokens, OAuth, or something else is right.

## Success Metrics

- Provisioning a new project's baseline infra (DNS + domain + deploy target + sending domain) is one governed dispatch chain, not a manual dashboard session.
- 0 secret bytes in any receipt, NDJSON line, unified report, or log (verified by a redaction gate with tests).
- Every destructive/outward-facing infra op passes a human confirmation gate ("laatste setje altijd menselijk").
- Every op is idempotent and supports `--dry-run`; re-running a provisioning dispatch is a no-op, not a duplicate-record incident.

---

## 1. Systems inventory (read from Vincent's real context, deduped)

Inventory sourced from `~/.claude/profile.md` (portfolio/technical-setup), the VNX repo, and Vincent's explicit naming. n8n is listed for completeness but **excluded** — decommissioned 2026-06-21 per profile.

**14 active systems:**

| # | System | Used for | CLI / API | Auth model |
|---|--------|----------|-----------|------------|
| 1 | **TransIP** | Registrar + DNS (all domains) | REST API v6; official Terraform provider; no first-class CLI | API key + private key pair; token with configurable expiry + IP allowlist; per-token label scoping |
| 2 | **Vercel** | Frontend hosting/deploys, domains, env vars | `vercel` CLI + REST API v2 | Scoped access tokens (account/team-scoped, per-token permission scope); OAuth for third-party |
| 3 | **Cloudflare** | Workers, D1, Turnstile, edge | `wrangler` CLI + REST API v4 | Scoped API tokens (per-resource, per-permission — strongly preferred) vs legacy global API key (avoid) |
| 4 | **Resend** | Transactional email, sending domains | REST API + SDKs (no official CLI) | API keys with permission level (full / sending-only) and domain scoping |
| 5 | **GCP** | Vertex AI, Secret Manager, possibly Cloud Run | `gcloud` CLI + client libraries | Service account JSON keys (user-managed) or OAuth user creds via `gcloud auth`; Workload Identity Federation for non-local |
| 6 | **GitHub** | Repos, PRs, `gh` automation; SSH commit signing for VNX attestation | `gh` CLI + REST/GraphQL API | Fine-grained PAT (per-repo, per-permission) or `gh auth login` OAuth token; separate ed25519(-sk) SSH signing key per `docs/governance/KEY_PROVISIONING.md` |
| 7 | **Supabase** | Mission Control DB (business data, audit columns) | Supabase CLI + Management API + Postgres direct | Personal access token (Management API); per-project service-role + anon JWT keys; DB connection strings |
| 8 | **Mollie** | Payments (SEOcrawler SaaS, VNX Digital) | REST API v2 (no CLI) | API keys (live_/test_ prefixed, per-profile); OAuth for platform apps |
| 9 | **HubSpot** | CRM; Fireflies → HubSpot auto-flow | REST API v3 (no CLI) | Private-app access token (scoped) or OAuth 2.0 app |
| 10 | **Fireflies** | Meeting notetaker feeding HubSpot | GraphQL API | Single API key per account |
| 11 | **OpenRouter** | Multi-provider LLM routing (panel, VNX lanes) | OpenAI-compatible REST API | API key, per-key credit limits |
| 12 | **Anthropic / Claude Code** | Primary agent provider | Anthropic API + Claude Code subscription | API key, or subscription OAuth (CLI-managed, not API-addressable) |
| 13 | **Kimi CLI (Moonshot)** | Agent provider lane | Kimi CLI + API | API key / OAuth (CLI-managed) |
| 14 | **Perplexity** | Research lane (sonar) | REST API | API key |

**Excluded / noted:**
- **n8n** — decommissioned 2026-06-21 (profile). No credential needed; remove any residual stored keys during PR-1 store setup.
- **DeepSeek** — if still active as a lane, same shape as OpenRouter (API key, OpenAI-compatible). Panel to confirm whether it is live; treated as optional inventory item, not a blocker.
- **Codex CLI (OpenAI)** — auth is CLI-managed OAuth (ChatGPT account); not API-addressable by this skill, out of scope for managed credentials.
- **Gemini CLI** — covered by GCP credentials (item 5) or a Google AI Studio API key; if the latter, it joins the inventory as a plain API key.

### Auth-model pattern across the inventory

Three patterns cover everything:
1. **Scoped API token** (TransIP, Vercel, Cloudflare, Resend, Mollie, HubSpot, OpenRouter, Perplexity, Fireflies, Supabase PAT) — the dominant model, and the one this design standardizes on.
2. **Service account / key pair** (GCP JSON key; TransIP's key+private-key is structurally this too; GitHub SSH signing key, already governed separately by KEY_PROVISIONING.md).
3. **CLI-managed OAuth** (`gh`, `gcloud` user auth, Claude/Codex/Kimi CLI subscriptions) — refresh tokens held by the vendor CLI itself. The skill shells out to the CLI and inherits its session; these are NOT pulled into the central store (refresh-token custody stays with the vendor CLI's own secure storage).

---

## 2. Design goals mapped to Vincent's stated priorities

| Priority (profile) | Design consequence |
|---|---|
| Governance + audit trail (financial background, ISO/ISAE) | Every infra action is a receipted dispatch; the credential *reference* (never the value) is part of the receipt. Rotation and access events are auditable at the store level. |
| Least privilege | Per-service, per-project scoped tokens with minimal scopes; no global keys (Cloudflare global key explicitly rejected). |
| Rotation | Store-native rotation reminders + one-command re-issue runbook per provider; token expiry preferred where the vendor supports it (TransIP). |
| Human-in-the-loop ("laatste setje altijd menselijk") | Hard confirmation gate on destructive/outward-facing ops; dry-run output is the confirmation artifact. |
| Deterministisch + generatief | Ops are deterministic API calls with deterministic idempotency checks; the agent only sequences and interprets them. |
| Secrets never in receipts/NDJSON | Env-at-spawn injection + redaction gate; see §3 and §5. |
| Two machines (MacBook + Mac Mini) | Central store must be cross-machine — this alone disqualifies a per-machine store. |

---

## 3. THE central decision: credential store + run-time injection

### Options weighed

**A. macOS Keychain (`security` CLI)**
- Pros: on-box, OS-native, Touch ID unlock possible, no new dependency.
- Cons (decisive): per-machine — Vincent runs MacBook AND Mac Mini, so the store forks silently and rotation means touching two machines; scripting is awkward (`security find-generic-password` UX, ACL prompts, item naming conventions); no native audit log of *which process read what when*; no sharing/escrow story; rotation is manual per machine; keychain items are invisible to any central policy. Auditability — Vincent's stokpaardje — is weakest here.
- Verdict: **rejected as primary**. Acceptable only as a local cache for an unlock token, not as the system of record.

**B. Password-manager CLI — 1Password `op` (or Bitwarden `bw`)**
- Pros: single source of truth synced across both Macs; biometric unlock interactively, service-account token non-interactively; **item-level audit log** (who/what/when accessed — matches ISAE audit-trail instinct); per-vault access control; `op inject` / `op run` do exactly env-at-spawn injection without writing secrets to disk; secret references (`op://vault/item/field`) are safe to commit — the receipt can store the reference and it reveals nothing; rotation = replace one item, both machines current instantly; sharing possible if a collaborator (e.g. Theun) ever needs one scoped item.
- Cons: paid subscription (already sunk cost if Vincent uses 1Password — panel to confirm; Bitwarden `bw` is the free fallback with the same injection pattern but weaker audit UX); non-interactive use needs a service-account token or an `op` session, which itself needs custody (bootstrap secret problem — see below).
- Verdict: **recommended primary**.

**C. Dedicated secrets manager (Vault / GCP Secret Manager / Doppler / Infisical)**
- Pros: strongest rotation policy engines, dynamic secrets, access policies, audit; GCP Secret Manager is already in Vincent's stack (item 5).
- Cons: operationally heavy for a solopreneur — Vault is self-hosted infra to maintain; Doppler/Infisical add a new vendor + subscription for a team-of-one; GCP Secret Manager is pay-per-access with IAM complexity and no biometric unlock for interactive work; all solve team problems Vincent doesn't have. Over-engineering violates keep-it-simple.
- Verdict: **rejected for now**; named as the migration path if client projects ever need shared, policy-driven secret access. GCP Secret Manager stays in the inventory as a *target* system the skill can deploy secrets INTO (for Cloud Run workloads), not as the store OF credentials.

**D. Per-service scoped tokens vs OAuth (token strategy — orthogonal to store choice)**
- Scoped API tokens: least-privilege, instant revocation, per-project blast radius, no refresh-flow complexity. Supported by 10 of 14 systems.
- OAuth (user OAuth / refresh tokens): required only where the vendor forces it (HubSpot public apps, Mollie platform apps). Refresh tokens are long-lived credentials that must be stored and rotated — strictly worse than scoped tokens when a token option exists.
- Verdict: **scoped API tokens as the default; OAuth only where the vendor offers no scoped-token path**; CLI-managed OAuth (gh, gcloud, LLM CLIs) stays with the vendor CLI and is never pulled into the store.

**E. Plaintext `.env`**
- Verdict: **rejected** outright beyond throwaway local dev. Unencrypted, accidentally-committable, invisible to audit, no rotation story. Non-starter given the governance bar.

### RECOMMENDATION (one line)

**1Password as the central credential store, holding per-service scoped API tokens (OAuth only where forced), injected env-at-spawn via `op run` by a VNX credential broker with a strict env allowlist — secret values never persisted, never shelled through argv, never written to receipts.**

### Run-time injection mechanics (how VNX does it)

1. Every credential lives as a 1Password item. The repo and dispatches reference it only as a secret reference: `op://vnx-infra/transip-prod/api_key`.
2. A **credential broker** (`scripts/lib/credential_broker.py`) is the only code path allowed to materialize a secret. Given a dispatch's declared credential requirements (a manifest in the dispatch: which systems, which ops), it:
   - resolves references via `op read` / builds an env template;
   - spawns the worker process with `op run --env-file <generated template>` semantics — the secret exists only in the child process environment;
   - enforces an **env allowlist**: only declared variable names may be injected (e.g. `TRANSIP_API_KEY`, `VERCEL_TOKEN`); anything else is refused;
   - scrubs the injected variables from the child's captured stdout/stderr before any of it reaches reports or receipts (defense in depth alongside the redaction gate, §5).
3. Bootstrap secret custody: interactive runs use biometric unlock (`op` desktop-app integration). Headless/background dispatches require a 1Password service-account token, whose own custody is one operator-provisioned item in the macOS Keychain (Keychain demoted to what it's good at: one local bootstrap secret, not the system of record). This follows the existing KEY_PROVISIONING.md pattern: operator provisions, workers never self-provision.
4. Resolver hook tie-in (addresses the logged critical antipattern — pre-dispatch resolver refusing `unknown:unknown` receipts): the broker resolves ALL credential references *before* dispatch is emitted. If any reference fails to resolve (missing item, wrong vault, locked store), the dispatch is refused at the door with a named error — never a runtime failure surfacing as an unknown receipt.
5. Receipt content: the receipt records `credential_refs: ["op://vnx-infra/transip-prod/api_key"]` (references are non-sensitive) and `credentials_injected: ["TRANSIP_API_KEY"]` (variable names only). Values: never.

---

## 4. Skill design: `infra-connection-manager`

New VNX skill at `skills/infra-connection-manager/` with a matching agent profile. Per-system operations exposed as tools, each implemented as a thin deterministic adapter over the vendor CLI/API, each driven by the credential broker.

### Operation surface (v1)

| Tool | System | Op | Mutating? |
|---|---|---|---|
| `dns.list-records` | TransIP | Read DNS zone | no |
| `dns.set-record` | TransIP | Create/update DNS record (idempotent by name+type+value) | **yes — gated** |
| `vercel.list-domains` | Vercel | Read project domains | no |
| `vercel.add-domain` | Vercel | Attach domain to project | **yes — gated** |
| `vercel.set-env` | Vercel | Set project env var | **yes — gated** |
| `cloudflare.deploy-worker` | Cloudflare | `wrangler deploy` | **yes — gated** |
| `cloudflare.d1-query` | Cloudflare | D1 read query | no |
| `resend.verify-domain` | Resend | Add + verify sending domain, returns DNS records to set | **yes — gated** |
| `gcp.deploy` | GCP | Cloud Run deploy (if used) | **yes — gated** |
| `gcp.secret-set` | GCP | Write secret into GCP Secret Manager for a workload | **yes — gated** |
| `github.repo-create` | GitHub | Create repo from template | **yes — gated** |
| `supabase.status` | Supabase | Project health/read | no |
| `mollie.profile-check` | Mollie | Read-only profile/key sanity | no |

Read ops are ungated; mutating ops pass the HITL gate (§5). v1 covers the oncueassistant trigger class end-to-end: TransIP + Vercel + Resend + Cloudflare. HubSpot/Fireflies/Mollie/LLM providers get adapters in later slices (their provisioning is not per-project-infra critical path).

### Per-op contract (every tool must implement)

- **Idempotency**: op first reads current state; if desired state already holds, returns `already_converged` and changes nothing. Re-running a provisioning chain is safe.
- **Dry-run**: `--dry-run` returns the exact diff of API calls it *would* make. The dry-run diff is the artifact shown at the HITL gate.
- **Structured result**: every op returns `{status, op, system, dry_run, diff, changed, credential_ref}` — receipt-ready, value-free.
- **Error taxonomy**: named errors (`auth_failed`, `rate_limited`, `conflict_existing`, `vendor_error`) — never bare unknowns (antipattern compliance).

### Dispatch/receipt integration

- Each infra action is a normal governed dispatch (single-block, T1/T2/T3 per existing routing rules) with the skill's ops as its declared tool scope.
- The dispatch manifest declares `requires_credentials: [transip, vercel]` — the broker (§3) resolves and injects at spawn; the receipt logs refs + variable names, never values.
- Provisioning a whole project is a dispatch *chain* (sequenced ops with receipts per step), so First-Pass Yield and rework metrics apply to infra work exactly as to code work.

---

## 5. Human-in-the-loop + governance model

**Confirmation gate.** Mutating/outward-facing ops (DNS changes, deploys, domain moves, env/secret writes) cannot execute autonomously. Flow:
1. Agent runs the op with `--dry-run` → produces the diff.
2. T0 presents the diff to Vincent; execution requires explicit human confirmation (matches "laatste setje altijd menselijk").
3. The confirmation itself is receipted (who confirmed, what diff, when) — the audit trail shows the human decision point, satisfying the financial-governance bar.
4. Read ops and converged no-ops run ungated.

**Redaction gate (hard rule: no secrets in receipts/NDJSON).** Three layers:
1. *Structural*: secrets exist only in child-process env, never in files, argv, or prompt text (§3).
2. *Broker-side scrub*: captured output is scrubbed of injected values before it can reach a report.
3. *Receipt-side scan*: a pre-write scanner in the shared receipt append path (`append_receipt_internals`) refuses any receipt whose payload matches known token shapes (regexes per provider: `re_` Resend, `live_`/`test_` Mollie, TransIP key format, GCP service-account JSON markers, plus entropy heuristic) or any exact injected value. Fail-closed: a match blocks the write and opens an open-item. This generalizes the existing pre-dispatch resolver-hook antipattern from identity fields to secret material.

**Scope discipline.** Worker file-write scope and permission settings (docs/core/12_PERMISSION_SETTINGS.md) apply unchanged; the skill's adapters run through the broker, never around it. Credential refs in receipts are safe-to-log by construction (`op://` URIs disclose vault/item names only — panel to confirm this disclosure level is acceptable; alternative: opaque alias IDs).

---

## 6. PR decomposition (dependency order)

### PR-1: Credential-strategy ADR + credential broker foundation
**Track**: B | **Skill**: vnx-manager | **Risk**: High (security-sensitive) | **Complexity**: Medium
Dependencies: []
- Land the ADR ratifying §3 (store choice, token strategy, injection mechanics, rejected alternatives) under `docs/governance/`.
- `scripts/lib/credential_broker.py`: `op` reference resolution, env allowlist, env-at-spawn spawn wrapper, output scrubbing, pre-dispatch resolve-all-refuse-unknown behavior.
- `tests/` unit coverage: allowlist enforcement, resolve-failure refusal, scrub correctness. Mock `op` at the subprocess boundary (test doubles at the boundary are legitimate; no stub business logic).
- Operator runbook: vault layout, per-provider token provisioning with minimal scopes, rotation procedure, n8n residual-key cleanup.

### PR-2: Receipt redaction gate
**Track**: B | **Skill**: backend-developer | **Risk**: Medium | Depends on: PR-1
- Pre-write secret scanner in the shared receipt append path; provider token-shape regexes + injected-value matching; fail-closed with open-item on match.
- Tests: synthetic receipts with planted token shapes are refused; clean receipts pass; `op://` refs pass.

### PR-3: Skill scaffold + op contract
**Track**: B | **Skill**: vnx-manager | **Risk**: Low | Depends on: PR-1, PR-2
- `skills/infra-connection-manager/SKILL.md` + agent profile + tools manifest.
- Adapter base implementing the per-op contract (idempotency, dry-run, structured result, error taxonomy) with one read-only reference adapter (`dns.list-records`) proving the pattern end-to-end through broker + receipt.

### PR-4: TransIP + Vercel + Resend adapters (the trigger case)
**Track**: B | **Skill**: api-developer | **Risk**: High (mutating, outward-facing) | Depends on: PR-3
- `dns.set-record`, `vercel.add-domain`, `vercel.set-env`, `resend.verify-domain` — each idempotent with dry-run; integration-tested against vendor sandbox/test resources where available.

### PR-5: Cloudflare + GCP + GitHub adapters
**Track**: B | **Skill**: api-developer | **Risk**: High | Depends on: PR-3
- `cloudflare.deploy-worker`, `cloudflare.d1-query`, `gcp.deploy`, `gcp.secret-set`, `github.repo-create`, plus read ops for Supabase/Mollie.

### PR-6: HITL confirmation gate + project-provisioning chain + certification
**Track**: B | **Skill**: vnx-manager + quality-engineer | **Risk**: Medium | Depends on: PR-4, PR-5
- Confirmation-gate wiring in T0 (dry-run diff presentation, confirmation receipt).
- `provision-new-project` dispatch chain template (repo → domain → DNS → deploy → sending domain → secrets), replicating the oncueassistant.com wiring as its certification case.
- Final certification: update BUSINESS planning changelog + project status doc; docs update (DISPATCH_RULES, skill registry, governance index).

### Dependency flow
```
PR-1 -> PR-2 -> PR-3 -> PR-4 ─┐
                    └> PR-5 ─┴> PR-6
```

---

## 7. Risk assessment

| Risk | Impact | Mitigation |
|---|---|---|
| Secret leaks into receipt/report/log | Critical | Three-layer defense (structural, broker scrub, fail-closed receipt scan); PR-2 ships before any mutating adapter |
| Broker becomes a god-privilege component | High | Env allowlist per dispatch; broker resolves only declared refs; all resolutions logged (refs only); CODEOWNERS review on `credential_broker.py` |
| Service-account bootstrap token compromised | High | Single bootstrap item in local Keychain, 0600-equivalent custody, operator-provisioned per KEY_PROVISIONING pattern; rotate on suspicion; 1Password audit log gives detection |
| 1Password outage blocks infra dispatches | Medium | Read ops degrade gracefully; mutating ops refuse-closed at the pre-dispatch resolver (named error, not unknown receipt); manual fallback runbook documented |
| Vendor API drift breaks adapters | Medium | Adapters are thin; error taxonomy surfaces `vendor_error` distinctly; per-adapter contract tests |
| Agent executes destructive op unconfirmed | Critical | HITL gate is structural (mutating ops have no autonomous path), not a prompt instruction |
| Panel judges `op://` refs in receipts as too revealing | Low | Fallback: opaque alias IDs mapped in the broker manifest |

## 8. Open questions for the panel

1. Confirm 1Password is Vincent's active password manager (else Bitwarden `bw` fallback — same design, weaker audit UX).
2. Accept `op://` secret references in receipts, or require opaque aliases?
3. Is DeepSeek a live lane (inventory completeness)?
4. Service-account token for headless dispatches: acceptable, or should headless infra dispatches be disallowed entirely (interactive-only for mutating infra work)? Lean: allow, with the bootstrap custody model in §3.
5. v1 adapter scope: confirm HubSpot/Fireflies/Mollie/LLM-provider adapters are later-slice, not v1.

## Final checklist
- [x] Systems inventory from real context (14 active systems, n8n excluded as decommissioned)
- [x] One recommended credential strategy with rationale + rejected alternatives
- [x] Injection mechanics: env-at-spawn, never persisted, never in receipts
- [x] Skill/tool design with idempotency + dry-run contract
- [x] HITL gate matching "laatste setje altijd menselijk"
- [x] PR decomposition, acyclic dependency graph
- [x] Critical antipattern (unknown:unknown receipt resolver) addressed in broker design
