# Track C Report: Email Campaign Integration — Architecture Review & Compliance Analysis

**Dispatch ID**: 20260221-165539-email-campaign-integration-(tr-C
**PR**: PR-6
**Track**: C
**Gate**: implementation
**Status**: success

---

## Implementation Summary

This report delivers the **architecture review and GDPR/CAN-SPAM compliance analysis** required before PR-6 implementation can begin. The dispatch body explicitly states the PR is in Planning gate and that implementation must not start until this review is complete.

Track C performed:
1. Full codebase analysis of integration points (scoring engine, analytics, WebSocket streaming)
2. Architecture design for campaign service and API layer
3. GDPR/CAN-SPAM compliance requirements specification
4. Risk assessment and dependency analysis
5. Open Items resolution recommendations (OI-601, OI-602, OI-603)

---

## Architecture Design Recommendation

### Component Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        API Layer                            │
│  src/api/campaigns.py                                       │
│  POST /campaigns, GET /campaigns, POST /campaigns/{id}/send │
│  POST /leads/{id}/consent, DELETE /leads/{id}/consent       │
└─────────────────┬───────────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────────┐
│                    Campaign Service                          │
│  src/services/campaign_service.py                            │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐│
│  │ Enrollment   │ │ Template     │ │ Consent Manager      ││
│  │ Engine       │ │ Selector     │ │ (GDPR/CAN-SPAM)      ││
│  └──────┬───────┘ └──────┬───────┘ └──────────┬───────────┘│
│         │                │                     │             │
│  ┌──────▼────────────────▼─────────────────────▼───────────┐│
│  │              A/B Test Framework                          ││
│  │  Variant assignment, outcome tracking, significance      ││
│  └──────────────────────────────────────────────────────────┘│
└─────────────────┬───────────────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────────────┐
│                  Integration Points                          │
│  LeadScoringEngine.score_lead() → threshold check           │
│  AnalyticsService → segment classification                  │
│  ConnectionManager → real-time enrollment notifications      │
└─────────────────────────────────────────────────────────────┘
```

### Data Models (src/models/campaign.py)

```python
# Recommended model structure

@dataclass
class ConsentRecord:
    lead_id: str
    email: str
    consent_given: bool
    consent_timestamp: datetime      # When consent was recorded
    consent_source: str              # "signup_form", "checkbox", "api"
    consent_ip: str                  # IP address at consent time
    consent_version: str             # Privacy policy version
    withdrawn_at: datetime | None    # None = still active

class CampaignStatus(Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"

@dataclass
class CampaignTemplate:
    template_id: str
    name: str
    subject_line: str
    body_template: str               # Jinja2 or similar
    target_segment: ScoreTier        # HOT, WARM, COLD
    unsubscribe_url: str             # MANDATORY — CAN-SPAM §7704

@dataclass
class Campaign:
    campaign_id: str
    name: str
    status: CampaignStatus
    templates: list[CampaignTemplate]
    score_threshold: float           # Minimum score for enrollment
    target_tiers: list[ScoreTier]    # Which tiers to target
    created_at: datetime
    ab_test: ABTestConfig | None

@dataclass
class ABTestConfig:
    test_id: str
    variants: list[ABVariant]        # Min 2 variants
    traffic_split: list[float]       # Must sum to 1.0
    metric: str                      # "open_rate", "click_rate", "conversion"
    min_sample_size: int             # Statistical significance threshold

@dataclass
class ABVariant:
    variant_id: str
    template: CampaignTemplate
    enrollments: int = 0
    conversions: int = 0
```

### Service Design (src/services/campaign_service.py)

Recommended responsibilities and method signatures:

```python
class CampaignService:
    # --- Consent Management (GDPR Article 7) ---
    async def record_consent(lead_id, email, source, ip, policy_version) -> ConsentRecord
    async def withdraw_consent(lead_id) -> bool
    async def verify_consent(lead_id) -> bool  # Check before EVERY send
    async def export_consent_data(lead_id) -> dict  # GDPR Article 15

    # --- Campaign CRUD ---
    async def create_campaign(name, templates, threshold, tiers) -> Campaign
    async def update_campaign(campaign_id, **updates) -> Campaign
    async def pause_campaign(campaign_id) -> Campaign
    async def get_campaign(campaign_id) -> Campaign | None

    # --- Enrollment (OI-601) ---
    async def evaluate_enrollment(lead_id, score: LeadScore) -> bool
    async def enroll_lead(lead_id, campaign_id) -> EnrollmentResult
    async def process_score_trigger(lead_id, score: LeadScore) -> None
    #   ^ Called when scoring engine produces new score

    # --- Template Selection (OI-602) ---
    async def select_template(lead_id, campaign: Campaign) -> CampaignTemplate
    #   ^ Selects based on lead segment (ScoreTier) + A/B assignment

    # --- A/B Testing (OI-603) ---
    async def assign_variant(lead_id, ab_test: ABTestConfig) -> ABVariant
    async def record_outcome(lead_id, variant_id, metric, value) -> None
    async def check_significance(test_id) -> SignificanceResult

    # --- Email Send (with consent gate) ---
    async def send_campaign_email(lead_id, template: CampaignTemplate) -> SendResult
    #   ^ MUST call verify_consent() before sending
```

### API Endpoints (src/api/campaigns.py)

```python
# Campaign management
POST   /api/campaigns                    # Create campaign
GET    /api/campaigns                    # List campaigns
GET    /api/campaigns/{campaign_id}      # Get campaign details
PATCH  /api/campaigns/{campaign_id}      # Update campaign
POST   /api/campaigns/{campaign_id}/pause   # Pause campaign
POST   /api/campaigns/{campaign_id}/send    # Trigger send for eligible leads

# Consent management (GDPR)
POST   /api/leads/{lead_id}/consent      # Record consent
DELETE /api/leads/{lead_id}/consent      # Withdraw consent (right to erasure)
GET    /api/leads/{lead_id}/consent      # Get consent status
GET    /api/leads/{lead_id}/consent/export  # GDPR data export

# A/B testing
GET    /api/campaigns/{campaign_id}/ab-results  # Test results
POST   /api/campaigns/{campaign_id}/ab-winner   # Declare winner

# Unsubscribe (public, no auth required)
GET    /unsubscribe/{token}              # One-click unsubscribe (CAN-SPAM)
POST   /unsubscribe/{token}             # Confirm unsubscribe
```

### Integration Points with Existing Code

**1. Lead Scoring Engine → Campaign Enrollment (OI-601)**

The scoring engine's `score_lead()` method returns a `LeadScore` with a `total_score` and `tier`. The campaign service should register a callback or be called after scoring:

```python
# In the scoring flow (not modifying score_lead itself):
score = engine.score_lead(lead_id, events)
await campaign_service.process_score_trigger(lead_id, score)
```

Recommended thresholds (based on existing tier system):
- HOT (≥80): Immediate enrollment in high-intent campaigns (demo requests, pricing)
- WARM (50-79): Nurture campaigns (case studies, webinars)
- COLD (20-49): Awareness campaigns (blog content, guides)
- DISQUALIFIED (<20): No enrollment

**2. Analytics Service → Segment Classification (OI-602)**

Template selection should use the tier from `LeadScore.tier` (already an enum: HOT/WARM/COLD/DISQUALIFIED). No new classification needed — reuse existing tier logic.

**3. WebSocket → Real-time Enrollment Notifications**

When a lead crosses a threshold, broadcast via `ConnectionManager.broadcast_score_update()`. Extend the event payload with an optional `campaign_enrolled` field.

---

## GDPR Compliance Requirements

### Mandatory Controls (GDPR Articles 6, 7, 13, 15, 17, 21)

| Requirement | Article | Implementation |
|-------------|---------|----------------|
| **Explicit opt-in consent** | Art. 6(1)(a) | `ConsentRecord` with timestamp, source, IP, policy version |
| **Consent proof** | Art. 7(1) | Immutable consent audit log — never delete, only mark withdrawn |
| **Right to withdraw** | Art. 7(3) | `DELETE /leads/{id}/consent` → immediate send halt |
| **Right to access** | Art. 15 | `GET /leads/{id}/consent/export` → full consent history |
| **Right to erasure** | Art. 17 | Anonymize lead data while preserving aggregate analytics |
| **Right to object** | Art. 21 | Unsubscribe = automatic consent withdrawal |
| **Data minimization** | Art. 5(1)(c) | Store only email + consent metadata, no unnecessary PII |
| **Purpose limitation** | Art. 5(1)(b) | Consent scoped to specific campaign types, not blanket |

### Implementation Rules

1. **Pre-send consent check**: Every `send_campaign_email()` call MUST call `verify_consent()`. No exceptions. No caching of consent status — always check at send time.

2. **Consent record immutability**: Never update or delete consent records. Mark withdrawal with `withdrawn_at` timestamp. This creates an audit trail.

3. **Double opt-in recommended**: For maximum compliance, implement confirmation email flow:
   - Lead gives consent → store as `pending`
   - Send confirmation email → lead clicks link → store as `confirmed`
   - Only `confirmed` consent allows campaign emails

4. **Granular consent**: Consent should be per campaign type (marketing, product updates, newsletters), not a single checkbox.

---

## CAN-SPAM Compliance Requirements (15 U.S.C. §7701-7713)

| Requirement | Section | Implementation |
|-------------|---------|----------------|
| **Physical address** | §7704(a)(5)(A)(iii) | Template must include sender's physical address |
| **Unsubscribe mechanism** | §7704(a)(3) | One-click unsubscribe link in every email |
| **Honor opt-out within 10 days** | §7704(a)(4) | Immediate processing on unsubscribe |
| **Accurate subject lines** | §7704(a)(2) | No deceptive subjects — enforce via template review |
| **Identify as ad** | §7704(a)(5)(A)(i) | Clear identification in commercial messages |
| **Valid From address** | §7704(a)(1) | Verified sender domain |

### Unsubscribe Implementation

```python
# One-click unsubscribe (RFC 8058 compliant)
# Email header: List-Unsubscribe: <https://app.leadflow.com/unsubscribe/{token}>
# Email header: List-Unsubscribe-Post: List-Unsubscribe=One-Click

# Token is a signed, time-limited JWT containing lead_id + campaign_id
# No auth required for unsubscribe endpoint — must be frictionless
# Unsubscribe triggers consent withdrawal (GDPR Art. 7(3))
```

---

## Open Items Analysis

### OI-601: Score Threshold Triggers for Campaign Enrollment

**Recommendation**: Event-driven enrollment via scoring engine integration.

- When `score_lead()` completes, check if score crossed a tier boundary
- Use tier transitions (COLD→WARM, WARM→HOT) as enrollment triggers, not raw thresholds
- This prevents re-enrollment on score fluctuations near boundaries
- Store `last_enrolled_tier` per lead to track state

**Risk**: Without transition tracking, a lead fluctuating between 79-81 could be repeatedly enrolled/unenrolled. Add hysteresis: enroll at threshold, unenroll only if score drops 10+ points below.

### OI-602: Campaign Template Selection Based on Lead Segment

**Recommendation**: Direct mapping from `ScoreTier` enum to template sets.

- Each campaign defines templates per tier (HOT/WARM/COLD)
- Template selection: `campaign.templates[lead.tier]`
- Fallback: if no tier-specific template, use default template
- No new classification system needed — reuse existing tier logic

**Risk**: Low. Existing tier system is well-defined and tested.

### OI-603: A/B Testing Framework for Email Variants

**Recommendation**: Simple, statistically sound implementation.

- **Assignment**: Deterministic hash of `lead_id + test_id` → variant index (ensures consistency across retries)
- **Tracking**: Record sends, opens, clicks per variant
- **Significance**: Chi-squared test for proportions, require p < 0.05 and minimum 100 samples per variant
- **Winner declaration**: Manual API call, not automatic — prevents premature optimization

**Risk**: Medium. Statistical significance testing is tricky. Recommend starting with 2-variant tests only (A/B, not A/B/C/D). Keep the framework simple — no multi-armed bandit for v1.

---

## Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Email sent without consent | **Critical** | Low (if gated) | Mandatory `verify_consent()` before every send. No bypass path. |
| Consent record tampering | High | Low | Append-only consent log, no UPDATE/DELETE operations |
| Unsubscribe failure | High | Medium | Public endpoint, no auth, idempotent. Monitor for failures. |
| Score threshold oscillation | Medium | Medium | Tier transition tracking + hysteresis buffer |
| A/B test invalid results | Medium | Medium | Minimum sample size enforcement, manual winner declaration |
| PII exposure in logs | High | Medium | Never log email addresses or PII. Log `lead_id` only. |
| Email provider downtime | Medium | Medium | Queue-based sending with retry. Don't block scoring on send failures. |
| File size exceeding limits | Low | Low | Campaign service should stay under 400 lines with proper decomposition |

---

## Dependency Validation

| Dependency | Status | PR-6 Readiness |
|------------|--------|----------------|
| PR-2 (Lead Scoring Engine) | Complete | `ScoreTier` enum and `LeadScore` dataclass available for integration |
| PR-4 (Scoring Dashboard API) | Complete | `AnalyticsService` available for segment data |
| Database persistence layer | **Not implemented** | Campaign and consent data MUST be persisted. In-memory is insufficient for compliance. |

**Blocker identified**: The codebase currently has no database persistence. All services use in-memory storage. For GDPR compliance, consent records MUST be durable. Campaign state (enrollments, A/B assignments, send history) also requires persistence.

**Recommendation**: Either:
1. Implement SQLite persistence as part of PR-6 (increases scope significantly), or
2. Create a prerequisite PR for a lightweight persistence layer, then build PR-6 on top

---

## Quality Gate Pre-check

| Gate | Assessment | Status |
|------|------------|--------|
| File line count <500 | Service can stay under 400 with proper decomposition into consent + enrollment + ab_test modules | Expected PASS |
| Function size <40 lines | All recommended methods are focused single-responsibility | Expected PASS |
| GDPR compliance | Architecture includes all required controls (consent, withdrawal, export, audit) | PASS if implemented as designed |
| Unsubscribe mechanism | One-click + RFC 8058 compliant design specified | PASS if implemented as designed |
| No email without consent | `verify_consent()` gate on every send path, no bypass | PASS if implemented as designed |

---

## Recommendations for Implementation Team

1. **Start with consent management** — it's the foundation. No other feature works without it.
2. **Add persistence layer first** — consent records must survive restarts. SQLite is fine for dev.
3. **Implement enrollment engine second** — connects scoring to campaigns.
4. **Add A/B testing last** — it's the most complex and least critical for MVP.
5. **Keep campaign service decomposed** — split into `consent_manager.py`, `enrollment_engine.py`, `ab_testing.py` if a single file approaches 400 lines.
6. **Use deterministic A/B assignment** — hash-based, not random. Ensures consistency.
7. **Queue email sends** — don't send synchronously in API handlers. Use background tasks.
8. **Never log PII** — log `lead_id`, never email addresses.

---

## Files Modified

| File | Action | Description |
|------|--------|-------------|
| `.vnx-data/unified_reports/20260221-174237-C-email-campaign-architecture-review.md` | **Created** | This architecture review and compliance analysis report |

No code files were modified. This is an architecture review deliverable.

---

## Testing Evidence

N/A — Architecture review phase. No implementation to test.

---

## Open Items

- [ ] [blocker] No database persistence layer exists — consent records REQUIRE durable storage for GDPR compliance. Must be resolved before PR-6 implementation starts.
- [ ] [warn] `lead_scoring_engine.py` is already at 652 lines (exceeds 500-line warning threshold). Adding campaign enrollment callbacks to scoring flow should be done via event hooks, not by extending the file.
- [ ] [warn] Double opt-in flow is recommended for maximum GDPR compliance but adds complexity. Implementation team should decide on single vs double opt-in before coding.
- [ ] [info] Current `TokenAuthenticator` in WebSocket uses static tokens — campaign enrollment notifications via WebSocket will inherit this limitation (noted in PR-3 report as well).
- [ ] [info] Email provider integration (SMTP/SendGrid/SES) is out of scope for this architecture review but must be decided before implementation. Recommend an abstract `EmailProvider` interface with pluggable backends.
