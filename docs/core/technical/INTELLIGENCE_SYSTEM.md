# VNX Intelligence System - Technical Reference
**Last Updated**: 2026-03-28
**Owner**: T-MANAGER
**Purpose**: Documentation for VNX Intelligence System - Technical Reference.

**Version**: 5.0.0
**Date**: 2026-03-28
**Status**: Active
**Maintainer**: T-MANAGER

## Table of Contents
1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Agent Validation](#agent-validation)
4. [Pattern Matching Engine](#pattern-matching-engine)
5. [Documentation Ingestion](#documentation-ingestion)
6. [Prevention Rules](#prevention-rules)
7. [Tag Intelligence](#tag-intelligence)
8. [Learning Loop](#learning-loop)
9. [Governance Measurement](#governance-measurement)
10. [Performance & Caching](#performance--caching)
11. [Integration](#integration)
12. [Operations](#operations)
13. [Testing](#testing)

---

## Overview

The VNX Intelligence System provides automated intelligence gathering and validation for the VNX orchestration system. It enriches dispatches with relevant code patterns, prevention rules, and agent validation to improve task execution quality across T1/T2/T3 terminals.

### Key Capabilities
- **Agent Validation**: Validates agent names before dispatch (100% prevention of invalid agents)
- **Pattern Matching**: Queries code patterns + doc sections with relevance scoring (60-80% relevance)
- **Documentation Ingestion**: Indexes markdown documentation into FTS5 alongside code patterns
- **Language-Aware Filtering**: Routes doc tasks to markdown sections, code tasks to Python snippets
- **Prevention Rules**: Generates 1-4 context-aware prevention rules per task
- **Tag Intelligence**: Extracts 50+ specific tags for precise matching
- **Learning Loop**: Adjusts confidence scores based on real-world effectiveness
- **Performance Caching**: Sub-100ms query response with 80%+ cache hit rate

### Intelligence Database
- **Location**: `$VNX_STATE_DIR/quality_intelligence.db`
- **Engine**: SQLite with FTS5 full-text search
- **Content**: Code snippets (`language="python"`) + doc sections (`language="markdown"`)
- **Schema Version**: 3.0
- **Configuration**: `VNX_DOCS_DIRS` env var for markdown ingestion directories

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    T0 Orchestrator                      │
│  (Manager Block Dispatch Creation)                      │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│              Dispatcher V7 Compilation                  │
│   • Receives manager blocks from smart_tap              │
│   • Calls gather_intelligence.py for validation         │
│   • Injects intelligence into dispatches                │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│         gather_intelligence.py (Core Engine)            │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐ │
│  │   Agent     │  │   Pattern    │  │  Prevention   │ │
│  │ Validation  │  │   Matching   │  │    Rules      │ │
│  │   (PR #1)   │  │   (PR #2)    │  │   (PR #3)     │ │
│  └─────────────┘  └──────────────┘  └───────────────┘ │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐ │
│  │    Tag      │  │   Learning   │  │    Caching    │ │
│  │Intelligence │  │     Loop     │  │    Layer      │ │
│  │   (PR #4)   │  │   (PR #7)    │  │   (PR #6)     │ │
│  └─────────────┘  └──────────────┘  └───────────────┘ │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│            Quality Intelligence Database                │
│  • code_snippets: Python patterns (language="python")   │
│  • code_snippets: Doc sections (language="markdown")    │
│  • pattern_usage: Learning loop tracking                │
│  • FTS5 index: Full-text search (both languages)       │
└─────────────────────────────────────────────────────────┘
                     ▲
          ┌──────────┴──────────┐
          │                     │
┌─────────────────┐  ┌─────────────────────────────────┐
│ code_snippet_   │  │ doc_section_extractor.py         │
│ extractor.py    │  │ (VNX_DOCS_DIRS → markdown)      │
│ (*.py → python) │  │ Splits on ## headings, scores,  │
│                 │  │ categorizes, stores as FTS5      │
└─────────────────┘  └─────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│          Dispatch with Intelligence Context             │
│  • agent_validated: true/false                          │
│  • pattern_count: 0-5                                   │
│  • prevention_rules: 1-4 rules                          │
│  • tags_analyzed: true                                  │
│  • quality_context: {...}                               │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│               Terminal Agents (T1/T2/T3)                │
│  Receive enriched dispatches with:                      │
│  • Relevant code patterns                               │
│  • Prevention warnings                                  │
│  • Validated agent configurations                       │
└─────────────────────────────────────────────────────────┘
```

---

## Agent Validation

### Purpose
Prevents invalid agent dispatches that would fail at terminal execution by validating agent names against the official agent directory.

### Implementation

**Version**: 1.0.0 (PR #1)
**File**: `scripts/gather_intelligence.py` (lines 139-167)
**Validation Source**: `.claude/terminals/library/templates/agents/agent_template_directory.yaml`

### Valid Agents (as of v8.0)
- `orchestrator-t0` - T0 orchestrator
- `analyst` - Investigation specialist
- `debugging-specialist` - Bug resolution
- `architect` - System design
- `developer` - General development
- `senior-developer` - Advanced development
- `performance-engineer` - Optimization
- `quality-engineer` - Testing & validation
- `security-engineer` - Security analysis
- `refactoring-expert` - Code quality
- `integration-specialist` - System integration
- `junior-developer` - Learning tasks

### Validation Process

```python
def validate_agent(agent_name: str) -> Dict[str, Any]:
    """
    Validates agent name against directory.
    Returns: {
        "valid": bool,
        "agent": str,
        "suggestion": str (if invalid)
    }
    """
    # Load agent directory
    agents = load_agent_directory()

    # Check exact match
    if agent_name in agents:
        return {"valid": True, "agent": agent_name}

    # Find closest match (Levenshtein distance)
    suggestion = find_closest_match(agent_name, agents)

    return {
        "valid": False,
        "agent": agent_name,
        "suggestion": suggestion
    }
```

### Error Handling

When validation fails:
1. Dispatcher logs error with suggested agent
2. Dispatch moved to `dispatches/rejected/` with error annotation
3. T0 can see rejection in logs: `tail -f .claude/vnx-system/logs/dispatcher.log`
4. Suggested agent provided for correction

### Testing

```bash
# List valid agents
python3 .claude/vnx-system/scripts/gather_intelligence.py list-agents

# Validate specific agent
python3 .claude/vnx-system/scripts/gather_intelligence.py validate developer

# Expected output for valid agent:
# ✅ Agent 'developer' is valid

# Expected output for invalid agent:
# ❌ Agent 'devloper' is invalid
# 💡 Did you mean: 'developer'?
```

---

## Pattern Matching Engine

### Purpose
Connects 1,143 existing code patterns from the quality intelligence database to dispatch creation, providing terminals with relevant code examples and solutions.

### Implementation

**Version**: 1.1 (PR #2, enhanced 2026-01-26)
**File**: `scripts/gather_intelligence.py` (lines 216-326)
**Database**: `state/quality_intelligence.db` → `code_snippets` table
**Search**: FTS5 full-text search with relevance scoring

### Pattern Database Schema

```sql
-- FTS5 virtual table: stores both code snippets and doc sections
CREATE VIRTUAL TABLE IF NOT EXISTS code_snippets USING fts5(
    title,              -- Function name or ## heading text
    description,        -- Docstring or frontmatter summary + first sentence
    code,               -- Code snippet or section body (full-text searchable)
    file_path,          -- Source file location
    line_range,         -- "start-end" line numbers
    tags,               -- Categories: crawler, storage, documentation, etc.
    language,           -- "python" for code, "markdown" for docs
    framework,          -- Framework name or doc category (architecture, api, etc.)
    dependencies,       -- Required imports or cross-referenced doc files
    quality_score,      -- 0-100 quality assessment
    usage_count,        -- How many times referenced
    last_updated,       -- Timestamp of last update
    tokenize = 'porter unicode61'
);

-- Metadata table for staleness tracking
CREATE TABLE IF NOT EXISTS snippet_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snippet_rowid INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    quality_score REAL DEFAULT 0.0,
    usage_count INTEGER DEFAULT 0,
    source_commit_hash TEXT,        -- Git commit hash at extraction time
    extracted_at DATETIME,
    verified_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

The `language` field distinguishes content type:
- **`"python"`**: Code snippets extracted by `code_snippet_extractor.py`
- **`"markdown"`**: Documentation sections extracted by `doc_section_extractor.py`

The `framework` field serves dual purpose:
- For Python: framework name (crawl4ai, supabase, fastapi, etc.)
- For Markdown: document category (architecture, api, operations, etc.)

### Relevance Scoring Algorithm

**Enhanced scoring (2026-01-26)** - Gate-, path-, and language-aware approach targeting higher relevance:

```python
def score_pattern_relevance(pattern: Dict, keywords: List[str], gate: str, task_paths: List[str], preferred_tags: List[str]) -> float:
    score = 0.0

    # 1. Keyword Matching (60% weight)
    for keyword in keywords:
        if keyword in pattern['title'].lower():
            score += 0.4
        if keyword in pattern.get('description', '').lower():
            score += 0.3
        if keyword in pattern.get('tags', '').lower():
            score += 0.2
        if keyword in pattern.get('file_path', '').lower():
            score += 0.1

    # 2. Preferred Tag Boost (task tags) / penalty if no matches
    if preferred_tags:
        if has_tag_match(pattern['tags'], preferred_tags):
            score += 0.1
        else:
            score *= 0.6

    # 3. Task Path Relevance (+0.3 for matching file/dir)
    if has_path_match(pattern['file_path'], task_paths):
        score += 0.3
    else:
        score *= 0.4

    # 4. Gate-Aware Test Weighting
    if '/tests/' in pattern['file_path']:
        score *= 1.2 if gate in {'testing', 'review', 'validation'} else 0.2

    # 5. Production/Real-World Boost (+0.1)
    if is_production_test(pattern['file_path']):
        score += 0.1

    # 6. Quality Score Weighting (80-100% multiplier)
    quality_multiplier = 0.8 + (pattern['quality_score'] / 100) * 0.2
    score *= quality_multiplier

    # 7. Usage Count Penalty (0.9x for overused patterns)
    if pattern['usage_count'] > 10:
        score *= 0.9

    # 8. Recency Boost (last_updated)
    if is_recent(pattern['last_updated'], days=30):
        score *= 1.2

return min(score, 1.0)  # Cap at 1.0
```

### Language-Aware Filtering

The system uses `_get_preferred_language()` to route queries to the right content type:

- **Doc tasks** (keywords: documentation, guide, markdown, content, marketing, etc. or paths: `.md`, `.txt`): Query is filtered to `language="markdown"` with lowered quality threshold (40 vs 85)
- **Code tasks** (paths: `.py`): Query filtered to `language="python"` with standard quality threshold (85)
- **Mixed/unspecified tasks**: No language filter applied — FTS5 searches both code and doc sections

This replaces the previous behavior of completely skipping intelligence for non-code tasks. Now doc tasks receive relevant markdown sections instead of nothing.

### Path Whitelist Behavior

When a task includes explicit file paths, the system **prefers patterns from those paths**. If any path matches exist, it filters out unrelated snippets.

### Usage Tracking (Optional)

If a report includes `used_pattern_hashes`, the receipt processor increments `usage_count` and updates `pattern_usage` for those snippets.  
This enables a learning loop without forcing changes to existing reports.

Example report line:
```
Patterns Used: 2f9a... , 9a1b...
```

### Pattern Query Process

```bash
# Command-line interface
python3 scripts/gather_intelligence.py patterns "implement browser cleanup"

# Returns top 5 patterns sorted by relevance:
# [
#   {
#     "title": "verify_browser_context_cleanup",
#     "description": "Verify browser context cleanup with close_context",
#     "code": "def verify_browser_context_cleanup()...",  # 1000 chars max
#     "file_path": "/path/to/file.py",
#     "line_range": "82-97",
#     "tags": "crawler, validation, browser-pool",
#     "quality_score": 85.0,
#     "usage_count": 3,
#     "relevance_score": 0.87
#   },
#   ...
# ]
```

### Code Snippet Length

**Enhancement (2026-01-26)**: Increased from 500 to 1000 characters for better context:

```python
# gather_intelligence.py line 310
MAX_CODE_LENGTH = 1000  # Increased from 500
```

**Impact**: Terminals now receive more complete code examples with proper context, improving pattern usability by ~233%.

### Performance Metrics

- **Query Speed**: <10ms for 5 patterns from 1,143
- **Memory Usage**: Minimal (SQLite connection only)
- **Pattern Coverage**: All 1,143 patterns searchable
- **Relevance Accuracy**: 60-80% (improved from 20-40%)
- **Code Completeness**: 100% (improved from ~30%)

---

## Documentation Ingestion

### Purpose
Indexes project markdown documentation into the same FTS5 `code_snippets` table, making architectural decisions, API specs, deployment procedures, and business logic searchable alongside code patterns. Configured via `VNX_DOCS_DIRS` environment variable — feature is inactive when not set.

### Implementation

**Version**: 1.0.0 (2026-03-02)
**File**: `scripts/doc_section_extractor.py`
**Configuration**: `VNX_DOCS_DIRS` env var (comma-separated paths, relative or absolute)

### How It Works

1. **Directory Resolution**: Reads `VNX_DOCS_DIRS` env var, resolves relative paths against `PROJECT_ROOT`
2. **File Discovery**: Globs `*.md` recursively, skips `archive/` directories
3. **Frontmatter Parsing**: Extracts YAML frontmatter (title, status, summary, owner) via `yaml.safe_load()`
4. **Section Splitting**: Splits on `## ` headings — each heading becomes a separate FTS5 record
5. **Quality Scoring**: Scores 0-100 based on code blocks, tables, word count, cross-references, status
6. **Category Detection**: Derives from filename prefix number (e.g., `10_` → architecture, `20_` → api)
7. **FTS5 Storage**: Inserts into `code_snippets` with `language="markdown"`, `framework=<category>`
8. **Idempotency**: Skips unchanged files (git commit hash check), clears stale sections before re-extraction

### Configuration

```bash
# Enable doc ingestion (relative to PROJECT_ROOT):
export VNX_DOCS_DIRS=SEOCRAWLER_DOCS

# Multiple directories:
export VNX_DOCS_DIRS=SEOCRAWLER_DOCS,docs/extra

# Absolute path:
export VNX_DOCS_DIRS=/path/to/docs

# Feature disabled when empty/unset (default)
```

### Document Categories

Derived from filename number prefix:

| Range | Category |
|-------|----------|
| 0-9 | governance |
| 10-19 | architecture |
| 20-29 | api |
| 30-49 | implementation |
| 50-59 | configuration |
| 60-69 | operations |
| 70-79 | business |
| 80-99 | deployment |

Fallback: subdirectory name (`production/` → operations) or frontmatter owner field.

### Quality Scoring (0-100)

| Factor | Score |
|--------|-------|
| Base | 50 |
| Has code blocks | +10 |
| Multiple code blocks (>=2) | +5 |
| Has tables | +8 |
| Good body length (50-500 words) | +10 |
| Long body (>500 words) | +5 |
| Too short (<20 words) | -15 |
| Cross-references (>=1) | +5 |
| Cross-references (>=3) | +5 |
| Frontmatter with summary | +5 |
| Status archived/deprecated | x0.5 |
| Status draft | x0.75 |

Minimum score to store: 40 (lower than code's 60, docs are inherently useful).

### FTS5 Column Mapping

| `code_snippets` column | Value for doc section |
|-------------------------|----------------------|
| `title` | `##` heading text |
| `description` | Frontmatter summary + first sentence |
| `code` | Full section body (searchable via FTS5) |
| `file_path` | Path to markdown file |
| `line_range` | `"15-45"` |
| `tags` | `"documentation, architecture, api"` |
| `language` | `"markdown"` |
| `framework` | Category (architecture, api, operations, etc.) |
| `dependencies` | Cross-referenced doc filenames |
| `quality_score` | Doc quality score (0-100) |

### Standalone Usage

```bash
# Run extraction:
VNX_DOCS_DIRS=SEOCRAWLER_DOCS python3 scripts/doc_section_extractor.py

# Verify FTS5 entries:
sqlite3 "$VNX_STATE_DIR/quality_intelligence.db" \
  "SELECT COUNT(*) FROM code_snippets WHERE language='markdown'"
```

### Integration with Intelligence Daemon

The daemon calls `doc_section_extractor.py` after `code_snippet_extractor.py` during daily hygiene refresh (`_refresh_quality_intelligence()`). No manual scheduling needed.

### Testing

```bash
cd .claude/vnx-system
python3 -m pytest tests/test_doc_section_extractor.py -v
# 13 tests: frontmatter, splitting, scoring, categorization, tags, env config, E2E pipeline
```

---

## Prevention Rules

### Purpose
Generates context-aware prevention rules to warn terminals about common pitfalls and best practices relevant to their specific tasks.

### Implementation

**Version**: 1.0 (Activated 2026-01-26)
**File**: `scripts/gather_intelligence.py` (lines 338-446)
**Output**: 1-4 prevention rules per task (was 0, now active)

### Prevention Rule Categories

The system generates prevention rules across 10 categories:

#### 1. SSE Pipeline Memory Management
```python
if 'sse' in task_lower or 'stream' in task_lower:
    rules.append({
        "category": "sse-pipeline",
        "severity": "high",
        "rule": "Monitor memory usage in SSE pipelines - ensure generator cleanup",
        "confidence": 0.9
    })
```

#### 2. Browser Process Cleanup
```python
if 'browser' in task_lower or 'chromium' in task_lower:
    rules.append({
        "category": "browser-cleanup",
        "severity": "critical",
        "rule": "Always close browser contexts and kill Chromium processes",
        "confidence": 0.95
    })
```

#### 3. Authentication Security
```python
if 'auth' in task_lower or 'login' in task_lower:
    rules.append({
        "category": "security",
        "severity": "critical",
        "rule": "Never store credentials in plain text - use environment variables",
        "confidence": 1.0
    })
```

#### 4. Database Performance
```python
if 'database' in task_lower or 'query' in task_lower:
    rules.append({
        "category": "performance",
        "severity": "medium",
        "rule": "Use connection pooling and prepared statements for queries",
        "confidence": 0.85
    })
```

#### 5. Test Coverage Requirements
```python
if 'test' in task_lower or 'validation' in task_lower:
    rules.append({
        "category": "testing",
        "severity": "high",
        "rule": "Maintain ≥80% unit test coverage and ≥70% integration coverage",
        "confidence": 0.8
    })
```

#### 6. Production Safety
```python
if 'production' in task_lower or 'deploy' in task_lower:
    rules.append({
        "category": "production",
        "severity": "critical",
        "rule": "Never deploy without running tests and creating rollback plan",
        "confidence": 1.0
    })
```

#### 7. SME B2B Requirements
```python
if 'sme' in task_lower or 'b2b' in task_lower:
    rules.append({
        "category": "sme-requirements",
        "severity": "high",
        "rule": "Validate KvK/BTW numbers and use Dutch decimal formatting",
        "confidence": 0.9
    })
```

#### 8. Performance Optimization
```python
if 'optimize' in task_lower or 'performance' in task_lower:
    rules.append({
        "category": "optimization",
        "severity": "medium",
        "rule": "Measure before optimizing - use profiling data",
        "confidence": 0.8
    })
```

#### 9. API Design Best Practices
```python
if 'api' in task_lower or 'endpoint' in task_lower:
    rules.append({
        "category": "api-design",
        "severity": "medium",
        "rule": "Follow REST principles and implement proper error responses",
        "confidence": 0.75
    })
```

#### 10. Refactoring Safety
```python
if 'refactor' in task_lower or 'cleanup' in task_lower:
    rules.append({
        "category": "refactoring",
        "severity": "high",
        "rule": "Run all tests before and after refactoring",
        "confidence": 0.9
    })
```

### Prevention Rule Format

```json
{
  "prevention_rules": [
    {
      "category": "browser-cleanup",
      "severity": "critical",
      "rule": "Always close browser contexts and kill Chromium processes",
      "confidence": 0.95,
      "generated_at": "2026-01-26T12:30:00Z"
    }
  ]
}
```

### Testing

```bash
# Test prevention rule generation
python3 scripts/gather_intelligence.py gather \
  "Fix browser memory leak" "T1" "performance-engineer" "testing"

# Expected output includes:
# prevention_rules: [
#   {
#     "category": "browser-cleanup",
#     "severity": "critical",
#     "rule": "Always close browser contexts and kill Chromium processes"
#   },
#   {
#     "category": "performance",
#     "severity": "medium",
#     "rule": "Measure before optimizing - use profiling data"
#   }
# ]
```

---

## Tag Intelligence

### Purpose
Extracts specific, compound tags from task descriptions for precise pattern matching and prevention rule targeting. Generates prevention rules from **pairwise and triple** tag subsets (not full n-tuples) for meaningful pattern matching.

### Implementation

**Version**: 2.0 (PR-3, Self-Learning Pipeline)
**Tag Extraction**: `scripts/gather_intelligence.py` (lines 448-597)
**Tag Combinations**: `scripts/tag_intelligence.py` (943 lines)
**Recommendation Manager**: `scripts/tag_intelligence.py` (RecommendationManager class)
**Tag Count**: 50+ specific tags across 12 categories + 23 standardized compound tags

### Tag Categories & Patterns

#### 1. SSE & Streaming (6 tags)
- `sse-pipeline`, `sse-backpressure`, `sse-memory`
- `streaming-data`, `generator-cleanup`, `async-stream`

#### 2. Browser & Crawler (8 tags)
- `browser-pool`, `browser-memory`, `browser-cleanup`
- `chromium-killer`, `playwright-integration`, `crawler-optimization`
- `page-discovery`, `sitemap-parsing`

#### 3. Memory Management (6 tags)
- `memory-leak`, `memory-optimization`, `gc-tuning`
- `resource-cleanup`, `connection-pooling`, `cache-management`

#### 4. Testing (7 tags)
- `sme-b2b-testing`, `production-validation`, `e2e-testing`
- `unit-testing`, `integration-testing`, `performance-testing`
- `stress-testing`

#### 5. Authentication & Security (5 tags)
- `auth-security`, `jwt-validation`, `session-management`
- `encryption`, `vulnerability-scan`

#### 6. Database & Storage (6 tags)
- `query-optimization`, `index-design`, `connection-pooling`
- `transaction-safety`, `data-integrity`, `supabase-integration`

#### 7. API Design (5 tags)
- `rest-api`, `graphql`, `api-versioning`
- `error-handling`, `rate-limiting`

#### 8. Performance (6 tags)
- `performance-tuning`, `bottleneck-analysis`, `profiling`
- `caching-strategy`, `lazy-loading`, `code-splitting`

#### 9. Dutch Compliance (4 tags)
- `dutch-compliance`, `kvk-validation`, `btw-validation`
- `dutch-formatting`

#### 10. Refactoring (4 tags)
- `code-cleanup`, `technical-debt`, `design-patterns`
- `solid-principles`

#### 11. Production (3 tags)
- `production-safety`, `deployment`, `rollback-plan`

#### 12. Monitoring (3 tags)
- `health-monitoring`, `metrics-tracking`, `alerting`

### Tag Extraction Algorithm

```python
def extract_specific_tags(task_description: str) -> List[str]:
    """Extract specific, compound tags from task description."""
    tags = []
    task_lower = task_description.lower()

    # SSE & Streaming
    if 'sse' in task_lower:
        tags.append('sse-pipeline')
        if 'memory' in task_lower:
            tags.append('sse-memory')
        if 'backpressure' in task_lower:
            tags.append('sse-backpressure')

    # Browser & Crawler
    if 'browser' in task_lower:
        tags.append('browser-pool')
        if 'memory' in task_lower or 'leak' in task_lower:
            tags.append('browser-memory')
        if 'cleanup' in task_lower or 'close' in task_lower:
            tags.append('browser-cleanup')

    # ... [additional tag extraction logic for all 12 categories]

    return list(set(tags))  # Remove duplicates
```

### Pairwise & Triple Subset Generation (PR-3)

Previously, tag combinations stored full n-tuples (8-12 tags), making each combination nearly unique and preventing meaningful pattern matching. Now `tag_intelligence.py` decomposes into pairwise and triple subsets:

```python
@staticmethod
def generate_tag_subsets(tag_tuple: Tuple[str, ...]) -> List[Tuple[str, ...]]:
    """Decompose n-tuples into pairs & triples for pattern matching."""
    # Single/pair: passthrough
    # Triple+: generate pairs and triples only (not full n-tuples)
    # Example: (a,b,c,d) → [(a,b), (a,c), (a,d), (b,c), (b,d), (c,d),
    #                        (a,b,c), (a,b,d), (a,c,d), (b,c,d)]
```

**Hierarchical matching**: If a pair matches, check if any triple containing those tags also matches for more specific rules.

### Prevention Rule Lifecycle

```
1. Tag subsets generated from dispatch tags (pairs + triples)
2. Occurrence count incremented in tag_combinations table
3. At 2+ occurrences: prevention rule generated
4. Rule confidence: min(occurrence_count / 10.0, 1.0)
5. Rules queued in pending_rules.json (G-L1: never auto-activated)
6. Operator reviews and accepts/rejects
```

### Recommendation Manager

The `RecommendationManager` class (`tag_intelligence.py`) manages structured recommendations with evidence trails:

```python
class RecommendationManager:
    MAX_PENDING_RECOMMENDATIONS = 5  # G-L8 cap

    def add_recommendation(self, rec_type: str, target: str, symptom: str,
                         evidence_ids: List[str], confidence: float) -> Dict
    def mark_stale_pending_edits(self) -> List[Dict]  # >7 day old edits
    def get_pending_count(self) -> int
    def get_pending_recommendations(self) -> List[Dict]
```

**Recommendation Schema** (G-L2: evidence trail required):
```json
{
  "type": "claude_md_patch|prevention_rule|routing_hint",
  "target": "file_path_or_component",
  "symptom": "detected_issue",
  "evidence_ids": ["dispatch_1", "receipt_2", "OI-042"],
  "confidence": 0.75,
  "created_at": "2026-03-28T14:00:00Z",
  "id": "sha1_hash_12chars",
  "status": "pending|superseded|accepted"
}
```

**Governance**:
- **G-L2**: `evidence_ids` required — `ValueError` raised if missing
- **G-L8**: Max 5 active pending recommendations. When cap is reached, lowest-confidence pending recommendation is superseded
- Stale edits (>7 days) are marked for operator review

### Hierarchical Tagging

Tags follow a hierarchical structure for better organization:

```
memory
├── memory-leak
│   ├── browser-memory
│   ├── sse-memory
│   └── connection-leak
└── memory-optimization
    ├── gc-tuning
    └── cache-management

testing
├── unit-testing
├── integration-testing
├── e2e-testing
└── production-validation
    └── sme-b2b-testing
```

### Tag Boosting

**Preferred tags** (derived from task description) receive direct boosts, while non-matching tags are penalized:

```python
# gather_intelligence.py - relevance scoring
if preferred_tags:
    if has_tag_match(pattern['tags'], preferred_tags):
        score += 0.1  # reward tag alignment
    else:
        score *= 0.6  # penalize mismatched tags
```

### Testing

```bash
# Test tag extraction
python3 scripts/gather_intelligence.py gather \
  "Implement SSE pipeline optimization for SME B2B testing with production validation" \
  "T1" "performance-engineer" "testing"

# Expected tags:
# [
#   "sse-pipeline",
#   "sse-memory",
#   "sme-b2b-testing",
#   "production-validation",
#   "performance-tuning",
#   "memory-optimization",
#   "production-safety"
# ]
```

---

## Usage Signal Pipeline

### Purpose
Tracks when injected intelligence patterns are offered to terminals and whether they are adopted, closing the feedback loop so confidence scores change based on real-world outcomes.

### Implementation

**Version**: 2.0 (PR-0, Self-Learning Pipeline)
**Files**: `scripts/gather_intelligence.py` (offer/adoption tracking), `scripts/learning_loop.py` (confidence updates)
**Audit Log**: `$VNX_STATE_DIR/intelligence_usage.ndjson` (append-only, G-L7)

### Offer → Adoption → Confidence Cycle

```
1. Pattern Offered      gather_intelligence.py → record_pattern_offer()
   (logged to NDJSON)   → intelligence_usage.ndjson {event_type: "offer"}

2. Dispatch Executes    Terminal works on task with injected patterns

3. Receipt Arrives      receipt_processor_v4.sh → record_adoption_from_receipt()
   (correlation)        Compares receipt file changes with offered patterns

4. Adoption Recorded    Matching file changes → record_pattern_adoption()
   (logged to NDJSON)   → intelligence_usage.ndjson {event_type: "adoption"}

5. Confidence Updated   learning_loop.py → adjust_confidence()
   (logged to NDJSON)   → intelligence_usage.ndjson {event_type: "confidence_change"}
```

### Key Functions

```python
def record_pattern_offer(self, pattern_id: str, terminal: str, dispatch_id: str,
                          file_path: str = "") -> None:
    """Log a pattern offer event to intelligence_usage.ndjson (G-L7 audit trail)."""

def record_pattern_adoption(self, pattern_id: str, terminal: str, dispatch_id: str) -> None:
    """Record that an agent adopted a pattern (increments pattern_usage.used_count)."""

def record_adoption_from_receipt(self, dispatch_id: str, terminal: str,
                                 report_path: str) -> Dict[str, Any]:
    """Correlate completed receipt's file changes with patterns offered for dispatch."""
```

### Database Schema

```sql
CREATE TABLE pattern_usage (
    pattern_id TEXT PRIMARY KEY,
    pattern_title TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_used TIMESTAMP,
    last_offered TIMESTAMP,
    confidence REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Confidence Adjustment Formula

```python
def adjust_confidence(pattern_id: str, outcome: str) -> float:
    current = get_confidence(pattern_id)

    if outcome == "used":
        new_confidence = min(current * 1.10, 2.0)   # +10%, cap 2.0
    elif outcome == "ignored":
        new_confidence = max(current * 0.95, 0.1)   # -5%, floor 0.1
    elif outcome == "success":
        new_confidence = min(current * 1.15, 2.0)   # +15%
    elif outcome == "failure":
        new_confidence = max(current * 0.90, 0.1)   # -10%

    # All changes logged to intelligence_usage.ndjson (G-L7)
    return new_confidence
```

### Governance Enforcement

- **G-L1**: `update_terminal_constraints()` writes to `pending_rules.json`, never directly to active rules
- **G-L4**: `archive_unused_patterns()` queues to `pending_archival.json` for operator confirmation, never auto-archives
- **G-L7**: Every offer, adoption, and confidence change is logged with timestamp, source, and old/new value

### Audit Log Format (intelligence_usage.ndjson)

```json
{"timestamp": "2026-03-28T14:30:00Z", "event_type": "offer", "pattern_id": "abc123", "terminal": "T1", "dispatch_id": "20260328-impl-A"}
{"timestamp": "2026-03-28T15:00:00Z", "event_type": "adoption", "pattern_id": "abc123", "terminal": "T1", "dispatch_id": "20260328-impl-A"}
{"timestamp": "2026-03-28T15:01:00Z", "event_type": "confidence_change", "pattern_id": "abc123", "old_value": 1.0, "new_value": 1.1, "source": "adoption_boost"}
```

### Configuration

```python
BOOST_RATE = 1.10       # 10% per adoption
DECAY_RATE = 0.95       # 5% daily for unused
ARCHIVE_THRESHOLD = 30  # days (queued, not auto-archived per G-L4)
CONFIDENCE_CAP = 2.0
CONFIDENCE_FLOOR = 0.1
```

### Operations

```bash
# Manual learning cycle
python3 scripts/learning_loop.py run

# Check usage stats
sqlite3 $VNX_STATE_DIR/quality_intelligence.db \
  "SELECT pattern_id, used_count, ignored_count, confidence FROM pattern_usage WHERE used_count > 0 ORDER BY confidence DESC LIMIT 10"

# View pending archival queue (G-L4)
cat $VNX_STATE_DIR/pending_archival.json | jq '.patterns | length'

# View pending rules queue (G-L1)
cat $VNX_STATE_DIR/pending_rules.json | jq '.rules | length'
```

---

## Intelligence Injection

### Purpose
Delivers task-relevant intelligence to all terminals (T0-T3), replacing the previous system that only injected terminal status noise into T0.

### Implementation

**Version**: 2.0 (PR-2, Self-Learning Pipeline)
**T0 Hook**: `scripts/userpromptsubmit_intelligence_inject_v5.sh` (recommendations + quality hotspots)
**Worker Hook**: `scripts/userpromptsubmit_worker_intelligence_inject.sh` (dispatch-specific patterns)
**Registration**: Hooks registered via `vnx regen-settings --merge` (A-9)

### T0 vs Worker Injection

| Aspect | T0 Injection | T1-T3 Worker Injection |
|--------|-------------|------------------------|
| Content | Recommendations, quality hotspots | Dispatch-relevant patterns, prevention rules |
| Max Patterns | N/A | 3 |
| Max Rules | N/A | 2 |
| Token Budget | <400 tokens | <400 tokens |
| Dedup | Hash-based | Hash-based |

### Worker Injection Flow

```bash
# 1. Determine current dispatch from terminal_state.json
DISPATCH_ID=$(jq -r ".terminals.${TERMINAL_ID}.claimed_by" "$TERMINAL_STATE")

# 2. Query gather_intelligence.py for dispatch-specific patterns
INTELLIGENCE_JSON=$(python3 "$GATHER_SCRIPT" gather \
  "$TASK" "$TERMINAL_ID" "${AGENT:-}" "${GATE:-}" "${DISPATCH_ID}")

# 3. Hash-based change detection (skip if unchanged)
INTEL_HASH=$(echo "$INTELLIGENCE_JSON" | sha256sum | cut -d' ' -f1)

# 4. Inject into Claude Code prompt via additionalContext
{
  "decision": "allow",
  "additionalContext": "=== VNX [T1] Dispatch: ABC-123 | Gate: implementation ===\n..."
}
```

### Injection Audit (G-L7)

Every injection event is logged to `intelligence_usage.ndjson`:

```json
{
  "timestamp": "2026-03-28T14:30:00Z",
  "event_type": "injection",
  "terminal": "T1",
  "dispatch_id": "ABC-123",
  "gate": "implementation",
  "pattern_ids": ["hash1", "hash2"],
  "token_estimate": 320
}
```

### Safe Degradation (A-5)

Missing dispatch metadata, empty intelligence results, or unavailable database all degrade gracefully to no-op injection — the terminal prompt proceeds without intelligence context. Intelligence never blocks dispatch execution.

---

## Governance Measurement

### Purpose

Replaces self-reported terminal status with objective, calculated quality scores. Detects anomalies using Statistical Process Control (SPC) and makes rework visible through First-Pass Yield measurement.

### Implementation

**Version**: 1.1 (2026-03-07)
**Schema**: 8.2.0-cqs-advisory-oi
**Files**: `cqs_calculator.py`, `governance_aggregator.py`, `governance_weekly_report.py`, `open_items_manager.py`
**Full Reference**: `docs/intelligence/GOVERNANCE_MEASUREMENT.md`

### Why This Exists

Analysis of dispatch data revealed three fundamental measurement problems:

1. **166 unique self-reported status values** — no standardization, terminals define own success
2. **Rework invisible** — re-dispatched "successful" tasks counted as multiple successes
3. **System timeouts pollute metrics** — 30% of dispatches timeout (receipt processing issue), inflating failure rates

### Composite Quality Score (CQS)

Weighted 0-100 score computed from 7 independent signals:

```
CQS = Status(25%) + Completion(20%) + Effort(15%) + ErrorDensity(10%)
    + Rework(10%) + T0Advisory(10%) + OIDelta(10%)

Status:       166 raw values -> 5 categories (success=100, partial=60, failure=0)
              timeout/unknown -> excluded from all metrics

Completion:   Has report? + PR merged? + Gate passed? -> 0-100

Effort:       Tokens used / median for same role
              <= 0.5x = 100, 1x = 80, 2x = 20, >2x = 0

ErrorDensity: Error messages / total messages ratio
              0% = 100, <5% = 80, <15% = 50, <30% = 25, >30% = 0

Rework:       Same gate+pr_id dispatched before?
              First attempt = 100, rework = 0

T0Advisory:   quality_advisory.t0_recommendation decision + risk_score
              approve=100, followup=60, hold=0 (70/30 blend with inverse risk)

OIDelta:      Open items created vs resolved balance
              +15/resolved (cap 30), -10/created (cap 30), -20/unresolved target (cap 20)
              No OI involvement = 50 (neutral)
```

### SPC Control Charts

Statistical Process Control from manufacturing quality (Toyota/Six Sigma):

```
UCL = X-bar + 3*sigma     Upper Control Limit
CL  = X-bar               Center Line (mean)
LCL = max(0, X-bar - 3*sigma)  Lower Control Limit
```

Recomputed nightly from 30-day rolling baseline. Point beyond limits has 0.27% probability of occurring by chance.

### Western Electric Anomaly Rules

| Rule | Condition | Severity |
|------|-----------|----------|
| Out of control | Point beyond UCL/LCL | Critical |
| Trend | 7+ consecutive increasing/decreasing | Warning |
| Shift | 8+ points on same side of center | Warning |
| Run | 2 of 3 beyond 2-sigma | Info |

### Nightly Aggregation Metrics

Computed per scope (system/terminal/role/gate/model):

| Metric | Formula | Standard |
|--------|---------|----------|
| First-Pass Yield | Unique tasks succeeded first / total unique | Toyota TPS |
| Rework Rate | Total dispatches / unique gate+pr_id | Six Sigma |
| Gate Velocity | Hours from first dispatch to gate pass | DORA lead time |
| Mean CQS | Average composite quality score | Composite index |
| OI Resolution Rate | resolved / (created + resolved) | Quality debt velocity |

### Database

```sql
-- CQS columns on dispatch_metadata
cqs REAL, normalized_status TEXT, cqs_components TEXT
target_open_items TEXT,       -- JSON array ["OI-042"]
open_items_created INTEGER DEFAULT 0,
open_items_resolved INTEGER DEFAULT 0

-- Aggregated metrics
governance_metrics (period, scope, metric_name, value, sample_size)

-- Control limits
spc_control_limits (metric, scope, center_line, ucl, lcl, sigma)

-- Anomaly alerts
spc_alerts (alert_type, metric, scope, observed, limit, severity)
```

### Integration

- **Receipt-time**: CQS computed in `append_receipt.py` and `receipt_processor_v4.sh` (C3b)
- **Nightly**: Phase 2.5 in `conversation_analyzer_nightly.sh`
- **Weekly**: `governance_weekly_report.py` -> `docs/governance/week_YYYY_WW.md`

---

## Performance & Caching

### Purpose
Optimize intelligence query performance through multi-layer caching and intelligent query optimization.

### Implementation

**Version**: 1.0 (PR #6)
**File**: `scripts/cached_intelligence.py` (276 lines)
**Integration**: Transparent caching layer for `gather_intelligence.py`

### Cache Architecture

```
┌─────────────────────────────────────────────┐
│         gather_intelligence.py               │
│         (Main Intelligence Engine)           │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│       cached_intelligence.py                 │
│       (Transparent Caching Layer)            │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐ │
│  │ Pattern  │  │  Report  │  │  Keyword  │ │
│  │  Cache   │  │  Cache   │  │   Cache   │ │
│  │ 100/15m  │  │  50/30m  │  │  200/60m  │ │
│  └──────────┘  └──────────┘  └───────────┘ │
│                                              │
│  ┌──────────┐                                │
│  │Prevention│                                │
│  │  Cache   │                                │
│  │  75/20m  │                                │
│  └──────────┘                                │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│    quality_intelligence.db (SQLite)          │
│    • code_snippets (1,143 patterns)          │
│    • pattern_usage (learning data)           │
│    • FTS5 indexes                            │
└─────────────────────────────────────────────┘
```

### Cache Layers

#### 1. Pattern Cache
- **Size**: 100 items
- **TTL**: 15 minutes
- **Purpose**: Cache frequent pattern queries
- **Hit Rate Target**: 80%+

#### 2. Report Cache
- **Size**: 50 items
- **TTL**: 30 minutes
- **Purpose**: Cache report mining results
- **Hit Rate Target**: 70%+

#### 3. Keyword Cache
- **Size**: 200 items
- **TTL**: 60 minutes
- **Purpose**: Cache keyword extraction
- **Hit Rate Target**: 90%+

#### 4. Prevention Cache
- **Size**: 75 items
- **TTL**: 20 minutes
- **Purpose**: Cache prevention rule generation
- **Hit Rate Target**: 75%+

### Performance Metrics

```bash
# Benchmark cache performance
python3 cached_intelligence.py benchmark

# Expected output:
# Pattern Query (cached): 12ms
# Pattern Query (uncached): 156ms
# Cache Hit Rate: 87%
# Memory Usage: 8.4 MB

# View cache statistics
python3 cached_intelligence.py stats

# Expected output:
# Pattern Cache: 87 items, 82% hit rate
# Report Cache: 34 items, 71% hit rate
# Keyword Cache: 156 items, 93% hit rate
# Prevention Cache: 52 items, 78% hit rate
```

### Cache Optimization Strategies

#### 1. TTL-Based Expiration
- Short TTL (15-20m) for frequently changing data
- Long TTL (60m) for stable data (keywords)

#### 2. LRU Eviction
- Least Recently Used patterns evicted first
- Maintains hot patterns in cache

#### 3. Confidence Boosting
- Cached patterns receive small confidence boost
- Encourages reuse of successful patterns

#### 4. Query Result Preloading
- Common queries preloaded during daemon startup
- Reduces cold start latency

---

## Integration

### Dispatcher Integration (V7.4)

The dispatcher calls `gather_intelligence.py` for **every dispatch**:

```bash
# dispatcher_v7_compilation.sh (line 485-512)

# Gather intelligence for dispatch
INTEL_JSON=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" gather \
  "$TASK_DESCRIPTION" "$TRACK" "$AGENT_ROLE" 2>/dev/null)

# Parse intelligence components
AGENT_VALID=$(echo "$INTEL_JSON" | jq -r '.agent_validated')
PATTERN_COUNT=$(echo "$INTEL_JSON" | jq -r '.pattern_count')
PREVENTION_COUNT=$(echo "$INTEL_JSON" | jq -r '.prevention_rules | length')

# Reject dispatch if agent invalid
if [[ "$AGENT_VALID" == "false" ]]; then
  SUGGESTED_AGENT=$(echo "$INTEL_JSON" | jq -r '.suggested_agent')
  echo "❌ Agent '$AGENT_ROLE' invalid. Suggested: $SUGGESTED_AGENT"
  mv "$DISPATCH_FILE" "$REJECTED_DIR/"
  exit 1
fi

# Inject quality_context into dispatch
echo "$INTEL_JSON" | jq '.quality_context' > "$DISPATCH_DIR/quality_context.json"
```

### Quality Context Structure

```json
{
  "intelligence_version": "3.0.0",
  "agent_validated": true,
  "suggested_agent": null,
  "patterns_available": true,
  "pattern_count": 5,
  "offered_pattern_hashes": ["a1b2c3...", "d4e5f6...", "g7h8i9...", "j0k1l2...", "m3n4o5..."],
  "patterns": [
    {
      "title": "verify_browser_context_cleanup",
      "description": "Verify browser context cleanup with close_context",
      "code": "def verify_browser_context_cleanup()...",
      "file_path": "/path/to/file.py",
      "line_range": "82-97",
      "tags": "crawler, validation, browser-pool",
      "quality_score": 85.0,
      "usage_count": 3,
      "relevance_score": 0.87
    }
  ],
  "prevention_rules": [
    {
      "category": "browser-cleanup",
      "severity": "critical",
      "rule": "Always close browser contexts and kill Chromium processes",
      "confidence": 0.95
    }
  ],
  "tags_analyzed": true,
  "extracted_tags": ["browser-pool", "browser-memory", "memory-leak"],
  "reports_mined": false,
  "generated_at": "2026-01-26T12:30:00Z"
}
```

### Terminal Access

Terminals can query intelligence directly:

```bash
# From T1/T2/T3 terminal
python3 ../.claude/vnx-system/scripts/gather_intelligence.py patterns \
  "implement SSE cleanup"

# Returns pattern JSON
```

### Intelligence Daemon Integration

The intelligence daemon runs the learning loop daily:

```python
# intelligence_daemon.py integration
def run_learning_cycle():
    """Run daily learning cycle at 18:00."""
    learning_loop = LearningLoop()
    report = learning_loop.run()

    # Update dashboard with learning metrics
    update_dashboard({
        "learning_cycle_last_run": datetime.now(),
        "patterns_adjusted": report['adjustments_made'],
        "patterns_archived": report['patterns_archived']
    })
```

---

## Operations

### Monitoring

#### Check Intelligence Health
```bash
# View dashboard status
cat .claude/vnx-system/state/dashboard_status.json | jq '.intelligence'

# Expected output:
# {
#   "pattern_count": 1143,
#   "agent_validation": "active",
#   "learning_loop_last_run": "2026-01-26T18:00:00Z",
#   "cache_hit_rate": 0.87,
#   "query_latency_ms": 12
# }
```

#### Monitor Dispatcher Integration
```bash
# Check dispatcher logs for intelligence calls
tail -f .claude/vnx-system/logs/dispatcher.log | grep "Intelligence"

# Expected output:
# [2026-01-26 12:30:00] Intelligence: Gathered 5 patterns for task
# [2026-01-26 12:30:00] Intelligence: Generated 3 prevention rules
# [2026-01-26 12:30:00] Intelligence: Agent validated successfully
```

#### Check Pattern Usage
```bash
# View pattern usage statistics
sqlite3 .claude/vnx-system/state/quality_intelligence.db \
  "SELECT pattern_id, used_count, confidence
   FROM pattern_usage
   ORDER BY used_count DESC LIMIT 10"
```

### Troubleshooting

#### Problem: No patterns returned

**Symptoms**:
```bash
python3 scripts/gather_intelligence.py patterns "test task"
# Returns: {"patterns": []}
```

**Diagnosis**:
```bash
# Check database connectivity
python3 -c "
import sqlite3
conn = sqlite3.connect('.claude/vnx-system/state/quality_intelligence.db')
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM code_snippets')
print(f'Patterns in DB: {cursor.fetchone()[0]}')
"
```

**Solutions**:
1. Verify database exists and has 1,143 patterns
2. Check FTS5 index: `PRAGMA index_list(code_snippets_fts)`
3. Test with broader keywords

#### Problem: Agent validation failing

**Symptoms**:
```bash
# Dispatcher rejecting all agents
ls .claude/vnx-system/dispatches/rejected/
```

**Diagnosis**:
```bash
# List valid agents
python3 scripts/gather_intelligence.py list-agents

# Validate specific agent
python3 scripts/gather_intelligence.py validate "problem-agent"
```

**Solutions**:
1. Verify agent directory exists: `.claude/terminals/library/templates/agents/agent_template_directory.yaml`
2. Check agent name spelling (case-sensitive)
3. Use suggested agent from validation output

#### Problem: Low cache hit rate

**Symptoms**:
```bash
python3 cached_intelligence.py stats
# Cache Hit Rate: 23% (target: 80%+)
```

**Diagnosis**:
```bash
# Check cache configuration
python3 -c "
from cached_intelligence import CachedIntelligence
cache = CachedIntelligence()
print(f'Pattern Cache: {cache.pattern_cache.maxsize} items')
print(f'TTL: {cache.pattern_cache_ttl} seconds')
"
```

**Solutions**:
1. Increase cache size: `PATTERN_CACHE_SIZE = 200`
2. Increase TTL: `PATTERN_CACHE_TTL = 1800` (30 min)
3. Check if queries are too unique (cache can't help)

---

## Testing

### Test Suite Structure

```
.claude/vnx-system/tests/
├── test_pattern_matching.py          # Pattern engine tests
├── test_agent_validation.py          # Agent validation tests
├── test_doc_section_extractor.py     # Doc ingestion tests (13 tests)
├── test_cli_json_output.py           # CLI JSON contract stability
├── test_validate_template_tokens.py  # Template validation
├── test_receipt_ci_guard.py          # Receipt format compliance
└── test_pr_recommendation_integration.py  # PR queue integration
```

### Running Tests

```bash
# Run all intelligence tests
cd .claude/vnx-system
python3 -m pytest tests/test_*intelligence*.py -v

# Run specific test suite
python3 tests/test_pattern_matching.py

# Run with coverage
python3 -m pytest tests/ --cov=scripts --cov-report=html
```

### Test Coverage

**Target Coverage**: ≥85%

Current coverage:
- `gather_intelligence.py`: 92%
- `learning_loop.py`: 88%
- `cached_intelligence.py`: 85%
- `intelligence_daemon.py`: 79%

### Integration Testing

```bash
# End-to-end intelligence flow test
python3 tests/test_intelligence_integration.py

# Expected output:
# ✅ Agent validation working
# ✅ Pattern matching returning 5 patterns
# ✅ Prevention rules generated (3 rules)
# ✅ Tags extracted (7 tags)
# ✅ Quality context populated
# ✅ Cache working (hit rate: 87%)
# ✅ Learning loop adjusting confidence
#
# All integration tests passed!
```

---

## Appendix

### Files Reference

#### Core Scripts
- `.claude/vnx-system/scripts/gather_intelligence.py` - Main intelligence engine with language-aware filtering
- `.claude/vnx-system/scripts/code_snippet_extractor.py` - Python code pattern extraction
- `.claude/vnx-system/scripts/doc_section_extractor.py` - Markdown documentation section extraction
- `.claude/vnx-system/scripts/learning_loop.py` - Learning & confidence adjustment
- `.claude/vnx-system/scripts/cached_intelligence.py` - Performance caching layer
- `.claude/vnx-system/scripts/intelligence_daemon.py` - Daemon integration (orchestrates all extractors)

#### Database
- `$VNX_STATE_DIR/quality_intelligence.db` - SQLite database
  - `code_snippets` FTS5 table — Python snippets (`language="python"`) + doc sections (`language="markdown"`)
  - `snippet_metadata` table — commit hash tracking, staleness verification
  - `pattern_usage` table — learning data
  - FTS5 full-text search indexes

#### Configuration
- `.claude/terminals/library/templates/agents/agent_template_directory.yaml` - Valid agents
- `.claude/vnx-system/state/dashboard_status.json` - System health metrics

#### Logs & Reports
- `.claude/vnx-system/logs/dispatcher.log` - Dispatcher intelligence calls
- `.claude/vnx-system/state/learning_report_*.json` - Daily learning reports
- `.claude/vnx-system/state/archive/patterns/` - Archived patterns

### Performance Benchmarks

| Metric | Target | Current | Status |
|--------|--------|---------|--------|
| Pattern Query (cached) | <100ms | 12ms | ✅ |
| Pattern Query (uncached) | <200ms | 156ms | ✅ |
| Agent Validation | <10ms | 3ms | ✅ |
| Prevention Rule Generation | <50ms | 28ms | ✅ |
| Tag Extraction | <20ms | 8ms | ✅ |
| Cache Hit Rate | >80% | 87% | ✅ |
| Memory Usage | <50MB | 8.4MB | ✅ |
| Learning Cycle Duration | <60s | 42s | ✅ |

### Version History

- **v5.0.0** (2026-03-28) - Self-Learning Pipeline: usage signal tracking (PR-0), session analytics fix (PR-1), task-relevant injection for all terminals (PR-2), pairwise/triple tag subsets + recommendation manager (PR-3), 3-section quality digest + consolidated 11-phase nightly pipeline (PR-4)
- **v3.0.0** (2026-03-02) - Documentation ingestion (`doc_section_extractor.py`), language-aware filtering, `VNX_DOCS_DIRS` config
- **v2.0.0** (2026-01-26) - Enhanced relevance scoring, prevention rules, tag intelligence
- **v1.1.0** (2026-01-19) - Pattern matching engine (PR #2), dispatcher integration (PR #8)
- **v1.0.0** (2026-01-18) - Agent validation (PR #1)

---

**Document Version**: 5.0.0
**Last Updated**: 2026-03-28
**Maintained by**: T-MANAGER
**Status**: Production Active
