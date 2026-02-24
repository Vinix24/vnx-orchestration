#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
# VNX Demo Setup Script
# ══════════════════════════════════════════════════════════════════════
#
# This script demonstrates the full VNX orchestration lifecycle:
#
# 1. Creates a realistic LeadFlow SaaS project (FastAPI + Python)
# 2. Seeds a FEATURE_PLAN.md with 6 PRs across 3 parallel tracks:
#    - Track A (T1): Lead Scoring Engine, Dashboard API
#    - Track B (T2): AI Config & Model Registry, Multi-Provider Dispatch
#    - Track C (T3): WebSocket Updates, Email Campaigns
# 3. PRs have realistic dependency chains, gates, and constraints:
#    - PR-1 is ready to implement (no dependencies)
#    - PR-2 depends on PR-1 (blocked until PR-1 completes)
#    - PR-3 depends on PR-2 (blocked, with architecture warning)
#    - PR-4 depends on PR-2 (blocked)
#    - PR-5 depends on PR-1 (blocked until PR-1 completes)
#    - PR-6 is in Planning gate (not yet ready for implementation)
# 4. Quality gates enforce: 500 lines warning, 800 lines blocker
# 5. Clones VNX from GitHub and runs `vnx init`
# 6. Initializes the PR queue with 37 open items (blockers + warnings)
#
# Usage:
#   bash setup_demo.sh [target_dir]
#
# Default target: ~/Development/vnx_demo
#
# After setup, run:
#   cd <target>/leadflow && .claude/vnx-system/bin/vnx start
#
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="${1:-$HOME/Development/vnx_demo}"
PROJECT_DIR="$DEMO_DIR/leadflow"
VNX_REPO="https://github.com/Vinix24/vnx-orchestration.git"

echo "══════════════════════════════════════════════════════════════"
echo "  VNX Orchestration Demo Setup"
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "  This creates a LeadFlow SaaS project with 6 PRs across"
echo "  3 parallel tracks, then bootstraps VNX orchestration."
echo ""
echo "  Target: $PROJECT_DIR"
echo ""

# ── Step 1: Clean project directory ────────────────────────────────────
echo "[1/8] Preparing project directory..."
rm -rf "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/src"/{api,services,models,utils}
mkdir -p "$PROJECT_DIR/tests"
mkdir -p "$PROJECT_DIR/config"
echo "  Created: $PROJECT_DIR"

# ── Step 2: Seed LeadFlow SaaS codebase ───────────────────────────────
echo "[2/8] Seeding LeadFlow SaaS codebase..."

cat > "$PROJECT_DIR/src/api/main.py" << 'PYEOF'
"""LeadFlow API - Lead management SaaS"""
from fastapi import FastAPI

app = FastAPI(title="LeadFlow", version="0.1.0")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/leads")
async def list_leads():
    return {"leads": [], "total": 0}
PYEOF

cat > "$PROJECT_DIR/src/services/lead_service.py" << 'PYEOF'
"""Lead management service"""
class LeadService:
    def __init__(self):
        self.leads = []

    def create_lead(self, data: dict) -> dict:
        lead = {"id": len(self.leads) + 1, **data}
        self.leads.append(lead)
        return lead

    def get_leads(self) -> list:
        return self.leads
PYEOF

cat > "$PROJECT_DIR/src/models/lead.py" << 'PYEOF'
"""Lead data model"""
from dataclasses import dataclass
from typing import Optional

@dataclass
class Lead:
    id: int
    email: str
    name: str
    company: Optional[str] = None
    score: int = 0
PYEOF

cat > "$PROJECT_DIR/src/utils/validators.py" << 'PYEOF'
"""Input validation utilities"""
import re

def validate_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))
PYEOF

cat > "$PROJECT_DIR/tests/test_lead_service.py" << 'PYEOF'
"""Tests for lead service"""
from src.services.lead_service import LeadService

def test_create_lead():
    service = LeadService()
    lead = service.create_lead({"email": "test@example.com", "name": "Test"})
    assert lead["id"] == 1
    assert lead["email"] == "test@example.com"
PYEOF

cat > "$PROJECT_DIR/requirements.txt" << 'EOF'
fastapi>=0.104.0
uvicorn>=0.24.0
pydantic>=2.0.0
EOF

echo "  Seeded: API, services, models, tests"

# ── Quality advisory trap (demo-only) ────────────────────────────────
# Seeds a ~555-line Python file that exceeds the 500-line warning threshold.
# Purpose: prove quality advisory fires during demo. NOT a production pattern.
cat > "$PROJECT_DIR/src/services/lead_scoring_engine.py" << 'PYEOF'
"""Lead Scoring Engine - Core scoring implementation.

Handles behavioral, firmographic, demographic and engagement scoring
dimensions with configurable weights, 0-100 normalization, and full
audit trail for compliance and debugging.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────────

class ScoreDimension(Enum):
    BEHAVIORAL = "behavioral"
    FIRMOGRAPHIC = "firmographic"
    DEMOGRAPHIC = "demographic"
    ENGAGEMENT = "engagement"


class ScoreTier(Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    DISQUALIFIED = "disqualified"


class EventType(Enum):
    PAGE_VIEW = "page_view"
    FORM_SUBMIT = "form_submit"
    EMAIL_OPEN = "email_open"
    EMAIL_CLICK = "email_click"
    DEMO_REQUEST = "demo_request"
    PRICING_VIEW = "pricing_view"
    DOWNLOAD = "download"
    WEBINAR_ATTEND = "webinar_attend"
    CHAT_INITIATE = "chat_initiate"
    RETURN_VISIT = "return_visit"


# ── Data Classes ───────────────────────────────────────────────────────

@dataclass
class ScoringWeight:
    dimension: ScoreDimension
    event_type: str
    base_points: float
    decay_factor: float = 0.95
    max_occurrences: int = 10
    recency_boost: float = 1.2


@dataclass
class LeadEvent:
    event_type: EventType
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    session_id: Optional[str] = None


@dataclass
class DimensionScore:
    dimension: ScoreDimension
    raw_score: float
    normalized_score: float
    contributing_events: int
    last_event_at: Optional[datetime] = None


@dataclass
class LeadScore:
    lead_id: str
    total_score: float
    tier: ScoreTier
    dimensions: dict[str, DimensionScore]
    computed_at: datetime
    version: str = "1.0"
    audit_trail: list[dict] = field(default_factory=list)


@dataclass
class FirmographicData:
    company_name: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    annual_revenue: Optional[float] = None
    country: Optional[str] = None
    technology_stack: list[str] = field(default_factory=list)


@dataclass
class DemographicData:
    title: Optional[str] = None
    department: Optional[str] = None
    seniority: Optional[str] = None
    linkedin_url: Optional[str] = None


# ── Scoring Configuration ──────────────────────────────────────────────

DEFAULT_WEIGHTS: dict[str, ScoringWeight] = {
    "page_view": ScoringWeight(
        dimension=ScoreDimension.BEHAVIORAL,
        event_type="page_view",
        base_points=1.0,
        decay_factor=0.9,
        max_occurrences=20,
    ),
    "form_submit": ScoringWeight(
        dimension=ScoreDimension.BEHAVIORAL,
        event_type="form_submit",
        base_points=15.0,
        decay_factor=0.98,
        max_occurrences=5,
    ),
    "email_open": ScoringWeight(
        dimension=ScoreDimension.ENGAGEMENT,
        event_type="email_open",
        base_points=3.0,
        decay_factor=0.92,
        max_occurrences=15,
    ),
    "email_click": ScoringWeight(
        dimension=ScoreDimension.ENGAGEMENT,
        event_type="email_click",
        base_points=8.0,
        decay_factor=0.95,
        max_occurrences=10,
    ),
    "demo_request": ScoringWeight(
        dimension=ScoreDimension.BEHAVIORAL,
        event_type="demo_request",
        base_points=50.0,
        decay_factor=1.0,
        max_occurrences=2,
    ),
    "pricing_view": ScoringWeight(
        dimension=ScoreDimension.BEHAVIORAL,
        event_type="pricing_view",
        base_points=20.0,
        decay_factor=0.97,
        max_occurrences=5,
    ),
    "download": ScoringWeight(
        dimension=ScoreDimension.ENGAGEMENT,
        event_type="download",
        base_points=12.0,
        decay_factor=0.96,
        max_occurrences=8,
    ),
    "webinar_attend": ScoringWeight(
        dimension=ScoreDimension.ENGAGEMENT,
        event_type="webinar_attend",
        base_points=25.0,
        decay_factor=0.99,
        max_occurrences=5,
    ),
    "chat_initiate": ScoringWeight(
        dimension=ScoreDimension.BEHAVIORAL,
        event_type="chat_initiate",
        base_points=10.0,
        decay_factor=0.93,
        max_occurrences=8,
    ),
    "return_visit": ScoringWeight(
        dimension=ScoreDimension.BEHAVIORAL,
        event_type="return_visit",
        base_points=5.0,
        decay_factor=0.91,
        max_occurrences=15,
    ),
}

TIER_THRESHOLDS = {
    ScoreTier.HOT: 80.0,
    ScoreTier.WARM: 40.0,
    ScoreTier.COLD: 10.0,
    ScoreTier.DISQUALIFIED: 0.0,
}

FIRMOGRAPHIC_SCORES = {
    "enterprise": {"min_employees": 500, "points": 30.0},
    "mid_market": {"min_employees": 100, "points": 20.0},
    "smb": {"min_employees": 10, "points": 10.0},
    "startup": {"min_employees": 1, "points": 5.0},
}

INDUSTRY_MULTIPLIERS = {
    "technology": 1.3,
    "finance": 1.2,
    "healthcare": 1.1,
    "manufacturing": 1.0,
    "retail": 0.9,
    "education": 0.8,
}

SENIORITY_SCORES = {
    "c_level": 25.0,
    "vp": 20.0,
    "director": 15.0,
    "manager": 10.0,
    "individual_contributor": 5.0,
}


# ── Scoring Engine ─────────────────────────────────────────────────────

class LeadScoringEngine:
    """Multi-dimensional lead scoring with decay, normalization, and audit."""

    def __init__(
        self,
        weights: Optional[dict[str, ScoringWeight]] = None,
        tier_thresholds: Optional[dict[ScoreTier, float]] = None,
    ):
        self.weights = weights or DEFAULT_WEIGHTS
        self.tier_thresholds = tier_thresholds or TIER_THRESHOLDS
        self._audit_log: list[dict] = []

    def compute_score(
        self,
        lead_id: str,
        events: list[LeadEvent],
        firmographic: Optional[FirmographicData] = None,
        demographic: Optional[DemographicData] = None,
    ) -> LeadScore:
        self._audit_log = []
        now = datetime.now(timezone.utc)

        behavioral = self._score_behavioral(events, now)
        engagement = self._score_engagement(events, now)
        firmo = self._score_firmographic(firmographic)
        demo = self._score_demographic(demographic)

        dimensions = {
            ScoreDimension.BEHAVIORAL.value: behavioral,
            ScoreDimension.ENGAGEMENT.value: engagement,
            ScoreDimension.FIRMOGRAPHIC.value: firmo,
            ScoreDimension.DEMOGRAPHIC.value: demo,
        }

        total = self._normalize_total(
            behavioral.normalized_score
            + engagement.normalized_score
            + firmo.normalized_score
            + demo.normalized_score
        )

        tier = self._classify_tier(total)

        self._audit("final_score", {
            "lead_id": lead_id,
            "total": total,
            "tier": tier.value,
        })

        return LeadScore(
            lead_id=lead_id,
            total_score=total,
            tier=tier,
            dimensions=dimensions,
            computed_at=now,
            audit_trail=list(self._audit_log),
        )

    def _score_behavioral(
        self, events: list[LeadEvent], now: datetime
    ) -> DimensionScore:
        behavioral_events = [
            e for e in events
            if self.weights.get(e.event_type.value, ScoringWeight(
                ScoreDimension.BEHAVIORAL, "", 0
            )).dimension == ScoreDimension.BEHAVIORAL
        ]
        raw = self._compute_dimension_raw(behavioral_events, now)
        normalized = self._sigmoid_normalize(raw, midpoint=50.0, steepness=0.08)
        last_at = max((e.timestamp for e in behavioral_events), default=None)

        self._audit("behavioral_score", {
            "raw": raw, "normalized": normalized,
            "event_count": len(behavioral_events),
        })

        return DimensionScore(
            dimension=ScoreDimension.BEHAVIORAL,
            raw_score=raw,
            normalized_score=normalized,
            contributing_events=len(behavioral_events),
            last_event_at=last_at,
        )

    def _score_engagement(
        self, events: list[LeadEvent], now: datetime
    ) -> DimensionScore:
        engagement_events = [
            e for e in events
            if self.weights.get(e.event_type.value, ScoringWeight(
                ScoreDimension.ENGAGEMENT, "", 0
            )).dimension == ScoreDimension.ENGAGEMENT
        ]
        raw = self._compute_dimension_raw(engagement_events, now)
        normalized = self._sigmoid_normalize(raw, midpoint=40.0, steepness=0.1)
        last_at = max((e.timestamp for e in engagement_events), default=None)

        self._audit("engagement_score", {
            "raw": raw, "normalized": normalized,
            "event_count": len(engagement_events),
        })

        return DimensionScore(
            dimension=ScoreDimension.ENGAGEMENT,
            raw_score=raw,
            normalized_score=normalized,
            contributing_events=len(engagement_events),
            last_event_at=last_at,
        )

    def _score_firmographic(
        self, data: Optional[FirmographicData]
    ) -> DimensionScore:
        if data is None:
            return DimensionScore(
                dimension=ScoreDimension.FIRMOGRAPHIC,
                raw_score=0.0,
                normalized_score=0.0,
                contributing_events=0,
            )

        raw = 0.0
        factors = 0

        if data.employee_count is not None:
            for tier_name, config in sorted(
                FIRMOGRAPHIC_SCORES.items(),
                key=lambda x: x[1]["min_employees"],
                reverse=True,
            ):
                if data.employee_count >= config["min_employees"]:
                    raw += config["points"]
                    factors += 1
                    break

        if data.industry:
            multiplier = INDUSTRY_MULTIPLIERS.get(
                data.industry.lower(), 1.0
            )
            raw *= multiplier
            factors += 1

        if data.annual_revenue is not None:
            revenue_score = min(20.0, math.log10(max(1, data.annual_revenue)) * 3)
            raw += revenue_score
            factors += 1

        if data.technology_stack:
            tech_bonus = min(10.0, len(data.technology_stack) * 2.0)
            raw += tech_bonus
            factors += 1

        normalized = self._sigmoid_normalize(raw, midpoint=30.0, steepness=0.12)

        self._audit("firmographic_score", {
            "raw": raw, "normalized": normalized, "factors": factors,
        })

        return DimensionScore(
            dimension=ScoreDimension.FIRMOGRAPHIC,
            raw_score=raw,
            normalized_score=normalized,
            contributing_events=factors,
        )

    def _score_demographic(
        self, data: Optional[DemographicData]
    ) -> DimensionScore:
        if data is None:
            return DimensionScore(
                dimension=ScoreDimension.DEMOGRAPHIC,
                raw_score=0.0,
                normalized_score=0.0,
                contributing_events=0,
            )

        raw = 0.0
        factors = 0

        if data.seniority:
            seniority_key = data.seniority.lower().replace(" ", "_")
            raw += SENIORITY_SCORES.get(seniority_key, 3.0)
            factors += 1

        if data.title:
            title_lower = data.title.lower()
            if any(kw in title_lower for kw in ("cto", "ceo", "cfo", "coo")):
                raw += 10.0
            elif any(kw in title_lower for kw in ("vp", "vice president")):
                raw += 7.0
            elif "director" in title_lower:
                raw += 5.0
            elif "manager" in title_lower:
                raw += 3.0
            factors += 1

        if data.department:
            dept_lower = data.department.lower()
            if dept_lower in ("engineering", "product", "it"):
                raw += 5.0
            elif dept_lower in ("marketing", "sales"):
                raw += 4.0
            elif dept_lower in ("operations", "finance"):
                raw += 3.0
            factors += 1

        if data.linkedin_url:
            raw += 2.0
            factors += 1

        normalized = self._sigmoid_normalize(raw, midpoint=20.0, steepness=0.15)

        self._audit("demographic_score", {
            "raw": raw, "normalized": normalized, "factors": factors,
        })

        return DimensionScore(
            dimension=ScoreDimension.DEMOGRAPHIC,
            raw_score=raw,
            normalized_score=normalized,
            contributing_events=factors,
        )

    def _compute_dimension_raw(
        self, events: list[LeadEvent], now: datetime
    ) -> float:
        occurrence_counts: dict[str, int] = {}
        total = 0.0

        for event in sorted(events, key=lambda e: e.timestamp, reverse=True):
            event_key = event.event_type.value
            weight = self.weights.get(event_key)
            if weight is None:
                continue

            count = occurrence_counts.get(event_key, 0)
            if count >= weight.max_occurrences:
                continue
            occurrence_counts[event_key] = count + 1

            age_hours = max(0, (now - event.timestamp).total_seconds() / 3600)
            decay = weight.decay_factor ** (age_hours / 24)
            recency = weight.recency_boost if age_hours < 24 else 1.0
            points = weight.base_points * decay * recency

            total += points

        return total

    def _sigmoid_normalize(
        self, value: float, midpoint: float = 50.0, steepness: float = 0.1
    ) -> float:
        normalized = 100.0 / (1.0 + math.exp(-steepness * (value - midpoint)))
        return round(min(100.0, max(0.0, normalized)), 2)

    def _normalize_total(self, raw_total: float) -> float:
        return round(min(100.0, max(0.0, raw_total / 4.0)), 2)

    def _classify_tier(self, score: float) -> ScoreTier:
        for tier in (ScoreTier.HOT, ScoreTier.WARM, ScoreTier.COLD):
            if score >= self.tier_thresholds[tier]:
                return tier
        return ScoreTier.DISQUALIFIED

    def _audit(self, step: str, data: dict) -> None:
        self._audit_log.append({
            "step": step,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        })


# ── Batch Processing ───────────────────────────────────────────────────

class BatchScoringProcessor:
    """Process multiple leads in batch with summary statistics."""

    def __init__(self, engine: Optional[LeadScoringEngine] = None):
        self.engine = engine or LeadScoringEngine()
        self.results: list[LeadScore] = []

    def process_batch(
        self,
        leads: list[dict[str, Any]],
    ) -> list[LeadScore]:
        self.results = []
        for lead_data in leads:
            lead_id = lead_data["lead_id"]
            events = lead_data.get("events", [])
            firmographic = lead_data.get("firmographic")
            demographic = lead_data.get("demographic")
            score = self.engine.compute_score(
                lead_id, events, firmographic, demographic
            )
            self.results.append(score)
        return self.results

    def get_summary(self) -> dict[str, Any]:
        if not self.results:
            return {"total_leads": 0, "tier_distribution": {}}

        tier_counts: dict[str, int] = {}
        total_score = 0.0
        for result in self.results:
            tier_key = result.tier.value
            tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1
            total_score += result.total_score

        return {
            "total_leads": len(self.results),
            "average_score": round(total_score / len(self.results), 2),
            "tier_distribution": tier_counts,
            "hot_leads": [
                r.lead_id for r in self.results if r.tier == ScoreTier.HOT
            ],
        }


# ── Score History Tracker ──────────────────────────────────────────────

class ScoreHistoryTracker:
    """Track score changes over time for trend analysis."""

    def __init__(self):
        self._history: dict[str, list[dict]] = {}

    def record(self, score: LeadScore) -> None:
        if score.lead_id not in self._history:
            self._history[score.lead_id] = []
        self._history[score.lead_id].append({
            "score": score.total_score,
            "tier": score.tier.value,
            "timestamp": score.computed_at.isoformat(),
        })

    def get_trend(self, lead_id: str) -> Optional[str]:
        history = self._history.get(lead_id, [])
        if len(history) < 2:
            return None
        recent = history[-1]["score"]
        previous = history[-2]["score"]
        if recent > previous + 5:
            return "improving"
        elif recent < previous - 5:
            return "declining"
        return "stable"

    def get_history(self, lead_id: str) -> list[dict]:
        return list(self._history.get(lead_id, []))
PYEOF

echo "  Seeded: Quality advisory trap file (lead_scoring_engine.py)"

# ── Step 3: Configuration files ────────────────────────────────────────
echo "[3/8] Creating configuration files..."

cat > "$PROJECT_DIR/config/settings.py" << 'PYEOF'
"""Application settings"""
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///leadflow.db")
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
PYEOF

cat > "$PROJECT_DIR/.env.example" << 'EOF'
DATABASE_URL=sqlite:///leadflow.db
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=false
EOF

cat > "$PROJECT_DIR/.gitignore" << 'EOF'
__pycache__/
*.pyc
.env
.venv/
*.db
.vnx-data/
node_modules/
EOF

echo "  Created: settings, .env.example, .gitignore"

# ── Step 4: Project CLAUDE.md ──────────────────────────────────────────
echo "[4/8] Creating CLAUDE.md..."

cat > "$PROJECT_DIR/CLAUDE.md" << 'EOF'
# LeadFlow SaaS

## Project Overview
LeadFlow is a lead management SaaS application built with FastAPI.

## Architecture
- **API Layer**: FastAPI with async endpoints
- **Services**: Business logic layer
- **Models**: Data models with Pydantic/dataclasses
- **Utils**: Shared utilities and validators

## Development
```bash
pip install -r requirements.txt
uvicorn src.api.main:app --reload --port 8000
```

## Testing
```bash
pytest tests/
```

## Git Workflow
- Work directly on `main` branch. Do NOT create feature branches.
- Commit directly to main — no PRs needed for this project.

## Key Constraints
- Python 3.10+
- FastAPI for API layer
- SQLite for development, PostgreSQL for production
- Email validation required for all lead inputs
EOF

echo "  Created: CLAUDE.md"

# Prevent global MCP servers from being enabled in the demo
# settings.local.json with empty enabledMcpjsonServers overrides any user defaults
mkdir -p "$PROJECT_DIR/.claude"
cat > "$PROJECT_DIR/.claude/settings.local.json" << 'EOF'
{
  "enabledMcpjsonServers": []
}
EOF
echo "  Created: settings.local.json (MCP servers disabled for demo)"

# ── Step 5: Feature plan with quality gates ────────────────────────────
echo "[5/8] Creating FEATURE_PLAN.md with dependencies, warnings & quality gates..."

# Copy from the demo/ directory if available, otherwise generate inline
if [ -f "$SCRIPT_DIR/FEATURE_PLAN.md" ]; then
  cp "$SCRIPT_DIR/FEATURE_PLAN.md" "$PROJECT_DIR/FEATURE_PLAN.md"
  echo "  Copied: FEATURE_PLAN.md from demo/ template"
else
  echo "  WARNING: demo/FEATURE_PLAN.md not found, generating inline..."
  # Fallback: minimal feature plan
  cat > "$PROJECT_DIR/FEATURE_PLAN.md" << 'EOF'
# Feature: AI-Powered Lead Scoring

## PR-1: AI Config & Model Registry (Track B)
**Gate**: Implementation
**Priority**: P1
**Track**: B
Dependencies: []
**Scope**: Create AI model configuration and registry

### Quality Gate
- [ ] All tests pass for ai_config and ai_registry modules
- [ ] No Python file exceeds 500 lines (warning) or 800 lines (blocker)
EOF
fi

echo ""
echo "  PR Dependency Graph:"
echo "    PR-1 (Track B) ─── ready to implement"
echo "      ├── PR-2 (Track A) ─── blocked by PR-1 [WARNING: config-only weights]"
echo "      │     ├── PR-3 (Track C) ─── blocked by PR-2 [WARNING: auth required]"
echo "      │     ├── PR-4 (Track A) ─── blocked by PR-2"
echo "      │     └── PR-6 (Track C) ─── blocked by PR-2+PR-4 [GATE: Planning only]"
echo "      └── PR-5 (Track B) ─── blocked by PR-1 [WARNING: env-only API keys]"
echo ""

# ── Step 6: Git initialization ─────────────────────────────────────────
echo "[6/8] Initializing git repository..."

cd "$PROJECT_DIR"
git init -q
git add -A
git commit -q -m "Initial commit: LeadFlow SaaS project"
echo "  Git repo initialized with initial commit"

# ── Step 7: VNX installation ──────────────────────────────────────────
echo "[7/8] Installing VNX orchestration system from GitHub..."

mkdir -p "$PROJECT_DIR/.claude"
git clone -q "$VNX_REPO" "$PROJECT_DIR/.claude/vnx-system"
echo "  Cloned VNX from: $VNX_REPO"

# Run vnx init
cd "$PROJECT_DIR"
bash .claude/vnx-system/bin/vnx init
echo "  VNX initialized (terminals, skills, hooks, database)"

# Verify T0 orchestration assets were unpacked from current shipped templates/skills.
if ! grep -q "T0 orchestration uses \`CLAUDE.md\` only." "$PROJECT_DIR/.claude/terminals/T0/CLAUDE.md"; then
  echo "  ERROR: T0 CLAUDE.md is not the expected current template."
  exit 1
fi
if ! grep -q "T0 orchestration itself uses \`CLAUDE.md\` only." "$PROJECT_DIR/.claude/skills/t0-orchestrator/SKILL.md"; then
  echo "  ERROR: t0-orchestrator SKILL.md is not the expected current skill."
  exit 1
fi
echo "  Verified: T0 CLAUDE.md + t0-orchestrator skill are current"

# Provider profiles are selected interactively at `vnx start`.
# No hardcoded provider — user picks claude-only, claude-codex,
# claude-gemini, or full-multi from the startup menu.

# ── Step 8: Initialize feature queue ───────────────────────────────────
echo "[8/8] Initializing feature queue from FEATURE_PLAN.md..."

cd "$PROJECT_DIR/.claude/vnx-system/scripts"
if [ -f "pr_queue_manager.py" ]; then
  python3 pr_queue_manager.py init-feature "$PROJECT_DIR/FEATURE_PLAN.md" || true

  # Auto-promote PR-1 (the only PR without dependencies)
  local_dispatch="$(ls "$PROJECT_DIR/.vnx-data/dispatches/staging/" 2>/dev/null | head -1)"
  if [ -n "$local_dispatch" ]; then
    local_dispatch_name="${local_dispatch%.md}"
    python3 pr_queue_manager.py promote "$local_dispatch_name" 2>/dev/null || true
  fi

  echo ""
  python3 pr_queue_manager.py status 2>/dev/null || true
fi

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  Demo Setup Complete!"
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "  What was created:"
echo "    - LeadFlow SaaS project at: $PROJECT_DIR"
echo "    - 6 PRs queued across 3 parallel tracks (A/B/C)"
echo "    - PR-1 promoted to 'ready' (first to be dispatched)"
echo "    - PR-2 to PR-6 blocked by dependencies"
echo "    - Quality advisory trap: lead_scoring_engine.py (555 lines > 500 warning)"
echo "    - 37 quality gate open items (blockers + warnings)"
echo "    - 4 terminal templates (T0 orchestrator + T1/T2/T3 workers)"
echo "    - Skills deployed for Claude (.claude/skills/), Codex (.agents/skills/), Gemini (.gemini/skills/)"
echo "    - SessionStart hooks for automatic context loading"
echo ""
echo "  Quality thresholds enforced:"
echo "    - Python files: 500 lines = warning, 800 lines = blocker"
echo "    - Shell scripts: 300 lines = warning, 500 lines = blocker"
echo "    - Functions: 40 lines = warning, 70 lines = blocker"
echo ""
echo "  How it works:"
echo "    1. T0 reads the queue and dispatches PR-1 to Track B (T2)"
echo "    2. T2 implements PR-1 (AI Config & Model Registry)"
echo "    3. T2 writes a completion report to unified_reports/"
echo "    4. Receipt processor detects the report and notifies T0"
echo "    5. T0 receives the receipt, marks PR-1 complete"
echo "    6. T0 promotes PR-2 and PR-5 (their dependency is now met)"
echo "    7. T0 dispatches PR-2 to Track A (T1) and PR-5 to Track B (T2)"
echo "    8. Both tracks work in parallel!"
echo ""
echo "  To launch:"
echo "    cd $PROJECT_DIR"
echo "    .claude/vnx-system/bin/vnx start"
echo ""
echo "  Provider profiles (choose at startup):"
echo "    claude-only   — All Claude Code (default)"
echo "    claude-codex  — T1: Codex CLI"
echo "    claude-gemini — T1: Gemini CLI"
echo "    full-multi    — T1: Codex CLI, T2: Gemini CLI"
echo ""
echo "    Or: .claude/vnx-system/bin/vnx start --profile claude-codex"
echo ""
echo "  Controls:"
echo "    Ctrl+G ......... Open dispatch queue popup"
echo "    Ctrl+B D ....... Detach (keeps running)"
echo "    Mouse .......... Click to switch panes"
echo ""
