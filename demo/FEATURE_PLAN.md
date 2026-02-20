# Feature: AI-Powered Lead Scoring & Multi-Model Architecture

## Overview
Implement AI-powered lead scoring with configurable model registry supporting
multiple AI providers (OpenAI, Anthropic, local models).

## Track Assignment
- **Track A (T1)**: Scoring logic and analytics (PR-2, PR-4)
- **Track B (T2)**: AI infrastructure and config (PR-1, PR-5)
- **Track C (T3)**: Real-time features and integrations (PR-3, PR-6)

## Quality Standards
- Python files: warning at 500 lines, blocker at 800 lines
- Shell scripts: warning at 300 lines, blocker at 500 lines
- Functions: warning at 40 lines, blocker at 70 lines
- All tests must pass before marking complete
- No zombie processes or resource leaks

---

## PR-1: AI Config & Model Registry (Track B)
**Gate**: Implementation
**Priority**: P1
**Track**: B
Dependencies: []
**Scope**: Create centralized AI model configuration and registry
**Files**: `src/services/ai_config.py`, `src/models/ai_registry.py`, `config/ai_models.yaml`
**Open Items**:
- OI-101: Define model registry schema with provider abstraction
- OI-102: Implement config validation with fallback defaults
- OI-103: Add model capability tagging (scoring, classification, embedding)

### Quality Gate
- [ ] All tests pass for ai_config and ai_registry modules
- [ ] No Python file exceeds 500 lines (warning) or 800 lines (blocker)
- [ ] No function exceeds 40 lines (warning) or 70 lines (blocker)
- [ ] Config validation covers all provider types with fallback defaults
- [ ] No hardcoded API keys or secrets in source files

---

## PR-2: Lead Scoring Engine (Track A)
**Gate**: Implementation
**Priority**: P1
**Track**: A
Dependencies: [PR-1]
**Warning**: Scoring weights must be configurable via config, not hardcoded. Use the model registry from PR-1 for AI-assisted scoring.
**Scope**: Update the existing lead scoring engine to load weights from YAML config and integrate with the AI model registry from PR-1. The current implementation in `lead_scoring_engine.py` has hardcoded weights that must be replaced with config-driven values.
**Files**: `src/services/lead_scoring_engine.py`, `src/models/score.py`, `config/scoring_weights.yaml`
**Open Items**:
- OI-201: Update `src/services/lead_scoring_engine.py` to load weights from `config/scoring_weights.yaml` instead of hardcoded values
- OI-202: Add score normalization (0-100 scale) to the existing engine
- OI-203: Extend the scoring audit trail with timestamp and reason tracking
- OI-204: Add batch scoring method to `LeadScoringEngine` that processes a list of leads with per-lead error handling, progress callbacks, and aggregated statistics (min/max/avg/median scores, tier distribution, failure count)

### Quality Gate
- [ ] All tests pass for scoring_engine including edge cases
- [ ] No function exceeds 40 lines (warning) or 70 lines (blocker)
- [ ] Score normalization produces values in 0-100 range for all inputs
- [ ] No hardcoded scoring weights - all configurable via YAML
- [ ] Audit trail records every score change with timestamp and reason

---

## PR-3: Real-time Score Updates via WebSocket (Track C)
**Gate**: Implementation
**Priority**: P2
**Track**: C
Dependencies: [PR-2]
**Warning**: WebSocket connections MUST be authenticated. Do not allow unauthenticated score streaming. Rate limit to 10 updates/sec per client.
**Architecture Note**: This PR requires careful design of the event broadcasting pattern. Consider using Redis pub/sub for multi-instance deployments.
**Scope**: WebSocket endpoint for live score updates
**Files**: `src/api/websocket.py`, `src/services/score_stream.py`
**Open Items**:
- OI-301: WebSocket connection manager with authentication
- OI-302: Score change event broadcasting
- OI-303: Client reconnection handling with score replay

### Quality Gate
- [ ] All tests pass including E2E WebSocket connection tests
- [ ] No Python file exceeds 500 lines (warning) or 800 lines (blocker)
- [ ] No function exceeds 40 lines (warning) or 70 lines (blocker)
- [ ] 100% of WebSocket connections require authentication token
- [ ] No unauthenticated score streaming possible
- [ ] Rate limiting enforced at 10 updates/sec per client
- [ ] Clean shutdown with no zombie WebSocket connections
- [ ] Memory usage stable under sustained connections (no resource leaks)

---

## PR-4: Scoring Dashboard API (Track A)
**Gate**: Implementation
**Priority**: P2
**Track**: A
Dependencies: [PR-2]
**Scope**: REST endpoints for scoring analytics and reporting
**Files**: `src/api/dashboard.py`, `src/services/analytics.py`
**Open Items**:
- OI-401: Aggregate scoring statistics endpoint
- OI-402: Lead funnel conversion metrics
- OI-403: Time-series score trend analysis

### Quality Gate
- [ ] All tests pass for dashboard and analytics endpoints
- [ ] No Python file exceeds 500 lines (warning) or 800 lines (blocker)
- [ ] No function exceeds 40 lines (warning) or 70 lines (blocker)
- [ ] API response time <= 200ms for aggregate queries
- [ ] No regression in existing lead API endpoints

---

## PR-5: Multi-Provider Dispatch (Track B)
**Gate**: Implementation
**Priority**: P2
**Track**: B
Dependencies: [PR-1]
**Warning**: Provider API keys must come from environment variables, never from config files. Include cost tracking from day one - retrofitting is expensive.
**Scope**: Route AI requests to optimal provider based on task type
**Files**: `src/services/ai_dispatch.py`, `src/models/dispatch_config.py`
**Open Items**:
- OI-501: Provider routing strategy (cost, latency, capability)
- OI-502: Fallback chain when primary provider fails
- OI-503: Usage tracking and cost allocation per provider

### Quality Gate
- [ ] All tests pass including provider fallback scenarios
- [ ] No Python file exceeds 500 lines (warning) or 800 lines (blocker)
- [ ] No function exceeds 40 lines (warning) or 70 lines (blocker)
- [ ] 100% of API keys loaded from environment variables
- [ ] No secrets in config files or source code
- [ ] Cost tracking records every AI request with provider and token count
- [ ] Fallback chain handles provider timeout within 5 seconds

---

## PR-6: Email Campaign Integration (Track C)
**Gate**: Planning
**Priority**: P3
**Track**: C
Dependencies: [PR-2, PR-4]
**Warning**: This PR is still in Planning gate. Do NOT start implementation until architecture review is complete. Email sending requires compliance review for GDPR/CAN-SPAM.
**Scope**: Connect scoring to automated email campaigns
**Files**: `src/services/campaign_service.py`, `src/api/campaigns.py`
**Open Items**:
- OI-601: Score threshold triggers for campaign enrollment
- OI-602: Campaign template selection based on lead segment
- OI-603: A/B testing framework for email variants

### Quality Gate
- [ ] All tests pass including E2E campaign enrollment flow
- [ ] No Python file exceeds 500 lines (warning) or 800 lines (blocker)
- [ ] No function exceeds 40 lines (warning) or 70 lines (blocker)
- [ ] 100% GDPR compliance for email consent tracking
- [ ] Unsubscribe mechanism functional in all campaign templates
- [ ] No email sent without explicit user consent record
