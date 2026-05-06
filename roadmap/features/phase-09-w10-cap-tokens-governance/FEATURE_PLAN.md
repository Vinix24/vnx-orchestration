# Feature: Phase 09 — W10 Capability Tokens And Governance Variants

**Status**: Draft
**Priority**: P0
**Branch**: `feat/w10-cap-tokens-governance`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Introduce ed25519-signed capability tokens that authorize every dispatch in the system, plus pluggable governance variants (strict / business-light / content-only). Replace ambient operator approval with cryptographically attenuated capability tokens. Drives PRD-VNX-UH-001 §FR-5 (cap tokens) and §FR-6 (governance variants). This phase is the security foundation for sub-orchestrators (W12) and folder-based agents (W14).

## Dependency Flow
```text
PR-0 (no dependencies on this feature, but depends on W9)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-0 -> PR-3
PR-3 -> PR-4
PR-2, PR-3, PR-4 -> PR-5
PR-5 -> PR-6
```

## PR-0: Ed25519 Key Management And Trust Anchor Store
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: []

### Description
Establish key management for the entire cap-token system. Operator root key (the trust anchor) is generated once, stored encrypted at rest, and rotatable. Per-orchestrator and per-worker signing keys are derived/signed by the operator root. Trust-anchor store retains historical anchors so old tokens still verify after rotation.

### Scope
- `scripts/lib/cap_keys.py` — keypair generation, encryption-at-rest (using OS keychain on macOS, age/file on Linux)
- `.vnx-data/state/keys/` directory with strict mode 0700; key files mode 0600
- `trust_anchors.json` — append-only, every historical operator public key recorded with timestamp
- Key rotation CLI: `python3 scripts/cap_token_cli.py rotate-operator-key`
- Audit log of every key generation, rotation, and revocation

### Success Criteria
- Operator root key generated, encrypted at rest, never written in plaintext
- Per-terminal signing keys derived and stored in keys directory
- Trust anchor file contains every historical public key
- Rotate operation produces new active key without losing history
- Filesystem permissions enforced (0700 dir, 0600 files)

### Quality Gate
`gate_pr0_key_mgmt`:
- [ ] Operator root key never appears in plaintext on disk
- [ ] Trust anchor file is append-only (rotation does not remove old anchors)
- [ ] Permissions are 0700 on dir, 0600 on key files
- [ ] Key rotation CLI succeeds and audit log records the event
- [ ] Recovery test: deleted active key signaled clearly, no silent fallback

## PR-1: Capability Token Schema And Signer
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-0]

### Description
Define the cap-token JSON schema (issuer, subject, scope, caveats, expiry, dispatch_id, parent_token_hash, signature). Implement signer that mints tokens against the operator root key (and later, against per-orchestrator derived keys). Tokens are immutable after signing.

### Scope
- `scripts/lib/cap_token.py` — `CapToken` dataclass, JSON serialization, canonical encoding for signing
- Token schema: issuer (key id), subject (terminal/agent id), scope (capabilities), caveats (attenuation rules), expiry (unix ts), dispatch_id, parent_token_hash (chain), signature (ed25519)
- Canonical JSON encoding (sorted keys, no whitespace) for deterministic signing
- Token signer: `CapTokenSigner.mint(scope, caveats, expiry, parent=None)`

### Success Criteria
- Token round-trips: serialize → deserialize → signature still verifies
- Canonical encoding produces byte-identical output across runs
- Schema rejects malformed tokens at parse time (no untrusted-input crashes)
- Tokens with expired timestamp are rejected at parse layer (defense in depth)

### Quality Gate
`gate_pr1_token_schema`:
- [ ] Round-trip test for 1000 random valid tokens passes
- [ ] Malformed token corpus (mutation fuzz) all rejected
- [ ] Canonical encoding deterministic across runs and platforms (linux + macOS)
- [ ] Expired token rejected at parse, not only at verify

## PR-2: Attenuation And Verifier
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.25 day
**Dependencies**: [PR-1]

### Description
Implement caveat attenuation: a holder can mint a child token whose scope is a strict subset of its parent. Implement verifier that walks the token chain to operator root, validates each signature against the historical trust anchor, and rejects if any link is broken or any caveat is expanded.

### Scope
- `CapTokenAttenuator.derive(parent_token, narrower_scope, narrower_caveats)`
- `CapTokenVerifier.verify(token, trust_anchors)` — walks parent chain, validates all signatures
- Replay-cache: dispatch_id seen recently → reject
- Structured error types: `BrokenChain`, `ExpiredToken`, `ScopeExpansion`, `BadSignature`, `Replay`, `UnknownAnchor`
- Performance budget: 1ms per verification on M-series local hardware

### Success Criteria
- Attenuation: child cannot expand scope; verifier rejects expansion
- Verifier walks N-deep chain (tested up to depth 8) and validates correctly
- Replay-cache rejects same dispatch_id within TTL
- Errors are structured (not string-matched), each type distinct

### Quality Gate
`gate_pr2_attenuation_verifier`:
- [ ] Scope-expansion attempt rejected
- [ ] Broken-chain (missing parent token) rejected
- [ ] 8-deep valid chain verifies in under 5ms
- [ ] Replay attack rejected
- [ ] All error types have structured shape, no string matching

## PR-3: Governance YAML Loader And Variant Schema
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-0]

### Description
Define governance variants (strict, business-light, content-only) and the YAML schema that declares them. Each variant specifies required gates, allowed agent kinds, max worker count, allowed providers. Loader validates and caches variants. Drives PRD-VNX-UH-001 §FR-6.

### Scope
- `governance/variants/strict.yaml`, `business-light.yaml`, `content-only.yaml`
- Schema: `required_gates`, `allowed_kinds`, `max_workers`, `allowed_providers`, `cap_token_required`
- Loader: `GovernanceVariantRegistry.load_all()` with strict YAML validation
- Variant lookup keyed by orchestrator agent folder

### Success Criteria
- Three variant files exist and load cleanly
- Loader rejects unknown keys (typo defense)
- Variant cache invalidates on file mtime change
- Variant declares minimal viable gate stack (strict requires gemini+codex+claude_github; content-only requires only gemini)

### Quality Gate
`gate_pr3_governance_loader`:
- [ ] All three variants load without error
- [ ] Unknown YAML key rejected with helpful error
- [ ] Cache invalidation works on file mtime change
- [ ] Variant→gate-stack mapping is deterministic

## PR-4: Gate Stack Resolver
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Dependencies**: [PR-3]

### Description
Build resolver that takes a dispatch and selects the gate stack via the orchestrator's governance variant. Replaces hard-coded gate selection. Resolver must be deterministic, auditable, and emit a receipt event when a gate is skipped because the variant did not require it.

### Scope
- `scripts/lib/gate_stack_resolver.py`
- `resolve(dispatch) -> List[GateSpec]` returning ordered list
- Receipt event `gate_skipped_by_variant` whenever a gate is skipped (audit trail)
- Override hook: operator can force-add gates per dispatch (never remove)

### Success Criteria
- Strict variant always requires gemini+codex+claude_github
- Business-light variant requires gemini+codex only
- Content-only variant requires gemini only
- Skipped gates produce receipt events
- Operator override can add but not remove gates

### Quality Gate
`gate_pr4_gate_resolver`:
- [ ] All three variants produce expected gate list
- [ ] Skipped gate produces audit receipt
- [ ] Operator override cannot remove a required gate (hard constraint)
- [ ] Resolver is deterministic across runs

## PR-5: Integration With Dispatcher (Cap Token Required Per Dispatch)
**Track**: C
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.5 day
**Dependencies**: [PR-2, PR-3, PR-4]

### Description
Wire dispatcher to require a valid cap token on every dispatch. Operator approval mints the root token at promote time. Sub-orchestrators (W12) attenuate from there. Dispatches without valid token are rejected. Enables both cap-token enforcement and gate-stack selection.

### Scope
- Dispatcher: require `cap_token` field on every promoted dispatch (after grace period flag)
- Promote step: operator approval triggers token mint with operator root key
- Verifier hook in dispatcher: reject dispatches whose token fails verification
- Grace flag: `VNX_CAP_TOKENS_ENFORCED=true|false` for staged rollout
- Receipts: every dispatch records its cap-token hash chain

### Success Criteria
- Dispatch without cap token (in enforced mode) rejected with structured error
- Dispatch with valid cap token routed normally
- Receipt records token hash and parent hash chain
- Grace flag allows soft rollout (warn only) before full enforcement
- Backwards-compat path: existing dispatches still work in non-enforced mode

### Quality Gate
`gate_pr5_dispatcher_integration`:
- [ ] Enforced mode: missing cap token → reject
- [ ] Enforced mode: valid cap token → route normally
- [ ] Receipt records token hash and full parent chain
- [ ] Grace mode: warns but does not block
- [ ] Existing dispatch flow regression-free in non-enforced mode

## PR-6: End-To-End Tests, Forge / Replay / Attenuation / Rotation / Failover Suite
**Track**: B
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.5 day
**Dependencies**: [PR-5]

### Description
Comprehensive cryptographic / security regression suite. Includes adversarial attempts: forge attacks, replay attacks, scope expansion, anchor migration, provider failover trust continuity, and the 1000 verifications per second NFR-4 performance target.

### Scope
- Forge attempt suite
- Replay attack suite
- Attenuation suite (narrowing succeeds, expansion fails)
- Trust anchor migration suite (rotated key, old tokens still verify against historical anchor; new tokens require new key)
- Provider failover trust continuity suite
- Performance: 1000 verifies / sec target

### Success Criteria
- All adversarial cases produce correct rejection with the correct structured error type
- All legitimate attenuation cases pass
- Anchor migration test passes both halves (old still works, new required)
- Performance test holds under load
- Failover test: orchestrator switches Claude→Codex mid-mission, cap-token chain still verifies

### Quality Gate
`gate_pr6_security_e2e`:
- [ ] Forge attempt test: worker without signing key tries to forge an upstream-approved dispatch → verifier rejects with `BadSignature`
- [ ] Replay attack test: replay an old token after dispatch_id is in cache → reject with `Replay`
- [ ] Attenuation test (narrow): sub-orchestrator narrows scope (caveats), dispatch accepted
- [ ] Attenuation test (expand): sub-orchestrator tries to expand scope → reject with `ScopeExpansion`
- [ ] Trust anchor migration test: rotate operator's key → existing tokens with old key still verify against historical anchor; new tokens require new key
- [ ] Provider-failover-preserves-trust test: orchestrator switches Claude→Codex during mission → cap-token chain still verifies (operator's signing key persists across provider switch)
- [ ] Performance test: 1000 token-verifications per second on reference hardware (matches NFR-4)
- [ ] CODEX GATE on this PR is mandatory feature-end gate
- [ ] CLAUDE_GITHUB_OPTIONAL on this PR is mandatory triple-gate (security-sensitive feature end)

## Test Plan (Phase-Level — Security Critical)

### Adversarial Tests (must all reject correctly)
- **Forge**: a worker mints a token with its own keypair claiming operator issuance. Verifier walks chain, finds unknown public key at root, rejects with `UnknownAnchor`.
- **Replay**: capture a valid token, resubmit after first dispatch completes. Verifier consults replay-cache, rejects with `Replay`.
- **Scope expansion**: orchestrator with scope `{Read, Edit}` mints child token with scope `{Read, Edit, Bash}`. Verifier rejects with `ScopeExpansion`.
- **Truncated chain**: child token references a parent_token_hash that is not present. Verifier rejects with `BrokenChain`.
- **Expired token**: well-formed signature but expiry in past. Verifier rejects with `ExpiredToken`.
- **Bit-flipped signature**: random byte mutation on signature field. Verifier rejects with `BadSignature`.

### Legitimate Behavior Tests
- Narrow caveats: child = parent ∩ narrower → accept
- Same caveats: child mirrors parent → accept (functionally a delegate)
- Multi-hop chain (depth 8): operator → main → tech-lead → worker → all verify

### Trust Anchor Migration
- Rotate operator key → trust_anchors.json now contains [old_pub, new_pub]
- Old token with old issuer key → verifies against historical anchor
- New token must be signed with new key, attempting old key produces `BadSignature`
- Audit log records rotation timestamp

### Failover / Continuity Test
- Mission begins with Claude orchestrator holding cap token
- Provider failover (W7.5) swaps to Codex orchestrator mid-stream
- Operator's signing key persists across provider switch (key is local to project, not provider)
- New child tokens minted by Codex still chain to operator anchor → verify

### Performance / NFR-4
- Generate 100 tokens, verify each 10× → 1000 verifications, must complete under 1 second on reference (M-series Mac, single thread)
- Profile under contention: 4 concurrent verifier threads, 1000 each → no lock starvation

### Integration With Governance Variants
- Strict variant + dispatch with insufficient gate stack → reject before dispatch
- Business-light variant + dispatch with codex gate skipped → accept, audit receipt logged
- Content-only variant + cap token claiming code-domain scope → reject (variant scope mismatch)

### Permission / Filesystem Hardening
- Key directory mode != 0700 on startup → loader refuses to start, prints remediation
- Key file mode != 0600 → same
- Trust anchor file overwritten externally → checksum mismatch detected on next load
