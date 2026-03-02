#!/usr/bin/env python3
"""
VNX Intelligence Gatherer with Agent Validation and Tag Intelligence
Provides intelligence services for T0 orchestration including agent validation,
pattern matching, tag intelligence, and quality context enrichment.
"""

import json
import os
import sqlite3
import re
import hashlib
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from cli_output import emit_json, emit_human, parse_human_flag

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from agent_directory_loader import load_agent_directory as _load_agent_directory

EXIT_OK = 0
EXIT_VALIDATION = 10
EXIT_IO = 20
EXIT_DEPENDENCY = 30


def _env_flag(name: str) -> Optional[bool]:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _rollback_mode_enabled() -> bool:
    rollback = _env_flag("VNX_STATE_SIMPLIFICATION_ROLLBACK")
    if rollback is None:
        rollback = _env_flag("VNX_STATE_DUAL_WRITE_LEGACY")
    return bool(rollback)

# Import tag intelligence engine
try:
    from tag_intelligence import TagIntelligenceEngine
except ImportError:
    # Fallback if tag_intelligence not available
    TagIntelligenceEngine = None


class T0IntelligenceGatherer:
    """T0's intelligence query system with agent validation and pattern matching"""

    def __init__(self):
        """Initialize intelligence gatherer with agent directory, pattern database, and tag intelligence"""
        script_dir = Path(__file__).resolve().parent
        sys.path.insert(0, str(script_dir / "lib"))
        try:
            from vnx_paths import ensure_env
        except Exception as exc:
            raise SystemExit(f"Failed to load vnx_paths: {exc}")

        paths = ensure_env()
        self.vnx_path = Path(paths["VNX_HOME"])
        self.project_root = Path(paths["PROJECT_ROOT"])
        self.agent_directory = self.load_agent_directory()

        # Initialize quality intelligence database
        # Primary: VNX_STATE_DIR (canonical runtime state)
        # Optional fallback to legacy only when rollback mode is enabled.
        self.quality_db_path = Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"
        if not self.quality_db_path.exists() and _rollback_mode_enabled():
            legacy_db = Path(paths["VNX_HOME"]) / "state" / "quality_intelligence.db"
            if legacy_db.exists():
                self.quality_db_path = legacy_db
        self.quality_db = None
        if self.quality_db_path.exists():
            try:
                self.quality_db = sqlite3.connect(self.quality_db_path)
                self.quality_db.row_factory = sqlite3.Row
            except Exception as e:
                print(f"⚠️ Warning: Could not connect to quality database: {e}")
                self.quality_db = None

        # Initialize tag intelligence engine
        self.tag_engine = None
        if TagIntelligenceEngine:
            try:
                self.tag_engine = TagIntelligenceEngine(self.quality_db_path)
            except Exception as e:
                print(f"⚠️ Warning: Could not initialize tag intelligence: {e}")
                self.tag_engine = None

    def load_agent_directory(self) -> List[str]:
        """Load valid skills from skills.yaml (V8 uses skills, not agent templates)"""
        return _load_agent_directory(self.vnx_path, self.project_root)

    def validate_agent(self, agent_name: str) -> Dict[str, Any]:
        """Validate skill/agent exists in directory (V8 validates against skills)"""
        if not agent_name:
            # No agent specified is valid (T0 can dispatch without agent)
            return {"valid": True, "agent": None}

        # Normalize the name (convert underscores to hyphens)
        normalized_name = agent_name.replace('_', '-')

        if normalized_name not in self.agent_directory:
            return {
                "valid": False,
                "error": f"Skill/Role '{agent_name}' not found in skills directory",
                "available_skills": self.agent_directory,
                "suggestion": self.suggest_closest_agent(agent_name)
            }

        return {"valid": True, "agent": normalized_name}

    def suggest_closest_agent(self, requested_agent: str) -> str:
        """Suggest closest matching agent name based on keywords"""
        requested_lower = requested_agent.lower()

        # Validation and testing keywords → quality-engineer
        validation_keywords = ['valid', 'test', 'quality', 'check', 'verify',
                              'assess', 'review', 'inspect', 'qa', 'audit']
        if any(kw in requested_lower for kw in validation_keywords):
            return 'quality-engineer'

        # Debugging keywords
        elif 'debug' in requested_lower or 'troubleshoot' in requested_lower:
            return 'debugger'

        # Refactoring keywords
        elif 'refactor' in requested_lower or 'cleanup' in requested_lower:
            return 'python-optimizer'

        # Performance keywords
        elif 'perf' in requested_lower or 'optim' in requested_lower:
            return 'performance-profiler'

        # Security keywords
        elif 'security' in requested_lower or 'vuln' in requested_lower:
            return 'security-engineer'

        # Architecture keywords
        elif 'architect' in requested_lower or 'design' in requested_lower:
            return 'architect'

        # Analysis keywords
        elif 'analyst' in requested_lower or 'analyz' in requested_lower:
            return 'data-analyst'

        # Integration keywords
        elif 'integration' in requested_lower or 'api' in requested_lower:
            return 'api-developer'

        # Senior/review keywords
        elif 'senior' in requested_lower or 'lead' in requested_lower:
            return 'reviewer'

        # Default to backend-developer for implementation tasks
        else:
            return 'backend-developer'

    def gather_for_dispatch(
        self,
        task_description: str,
        terminal: str,
        agent: Optional[str] = None,
        gate: Optional[str] = None
    ) -> Dict[str, Any]:
        """Main intelligence gathering for T0 dispatch creation"""

        # CRITICAL: Validate agent first
        if agent:
            agent_validation = self.validate_agent(agent)
            if not agent_validation["valid"]:
                return {
                    "dispatch_blocked": True,
                    "error": agent_validation["error"],
                    "available_agents": agent_validation["available_skills"],
                    "suggested_agent": agent_validation["suggestion"],
                    "timestamp": datetime.now().isoformat()
                }

        # Extract and analyze tags (PR #3)
        extracted_tags = self.extract_tags_from_description(task_description)
        task_paths = self.extract_task_paths(task_description)
        path_tags = self._extract_path_tags(task_paths)
        if path_tags:
            extracted_tags = list(set(extracted_tags + path_tags))

        # Agent validation passed, gather intelligence
        suggested_patterns = self.query_relevant_patterns(
            task_description,
            gate=gate,
            task_paths=task_paths,
            preferred_tags=extracted_tags
        )
        tag_analysis = self.analyze_tags_for_task(extracted_tags, phase=gate, terminal=terminal)
        prevention_rules = self.query_prevention_rules(task_description, extracted_tags)

        intelligence = {
            "agent_validated": True,
            "agent": agent,
            "terminal": terminal,
            "task": task_description,
            "timestamp": datetime.now().isoformat(),
            "task_paths": task_paths,
            "gate": gate,

            # Pattern matching (PR #2)
            "suggested_patterns": suggested_patterns,
            "pattern_count": len(suggested_patterns),

            # Tag intelligence (PR #3)
            "prevention_rules": prevention_rules,
            "prevention_rule_count": len(prevention_rules),
            "extracted_tags": extracted_tags,
            "tag_analysis": tag_analysis,

            # Mined data from reports (PR #4)
            "relevant_reports": self.find_similar_reports(task_description),
            "known_issues": self.query_antipatterns(task_description, limit=3),
            "success_prediction": None,  # PR #7
            "file_warnings": [],         # PR #2

            # Pattern hashes for feedback loop (agents report back which they used)
            "offered_pattern_hashes": [
                p['pattern_hash'] for p in suggested_patterns if p.get('pattern_hash')
            ],

            # Quality context with patterns and tags
            "quality_context": {
                "intelligence_version": "1.4.0",
                "agent_validated": True,
                "patterns_available": len(suggested_patterns) > 0,
                "pattern_count": len(suggested_patterns),
                "offered_pattern_hashes": [
                    p['pattern_hash'] for p in suggested_patterns if p.get('pattern_hash')
                ],
                "tags_analyzed": tag_analysis.get("analyzed", False),
                "tag_combination": tag_analysis.get("tag_combination", []),
                "prevention_rules_available": len(prevention_rules) > 0,
                "prevention_rule_count": len(prevention_rules),
                "reports_mined": True,  # Now active with PR #4
                "mined_context": self.get_mined_quality_context(task_description)
            }
        }

        return intelligence

    def extract_keywords(self, text: str) -> List[str]:
        """Extract meaningful keywords from task description"""
        # Remove common words and extract technical terms
        stopwords = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
                    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
                    'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'to', 'for',
                    'in', 'on', 'at', 'by', 'with', 'from', 'up', 'out', 'if', 'then',
                    'review', 'create', 'update', 'verify', 'ensure', 'document', 'implement',
                    'add', 'run', 'build', 'test', 'tests'}

        # First extract technical terms with hyphens/underscores
        technical_terms = re.findall(r'\b[a-zA-Z0-9]+(?:[_-][a-zA-Z0-9]+)+\b', text)
        keywords = [t.lower() for t in technical_terms]

        # Then extract regular words (but not if they're part of technical terms)
        text_without_technical = text
        for term in technical_terms:
            text_without_technical = text_without_technical.replace(term, ' ')

        words = re.findall(r'\b[a-z]+\b', text_without_technical.lower())
        keywords.extend([w for w in words if w not in stopwords and len(w) > 2])

        return list(set(keywords))

    def extract_task_paths(self, text: str) -> List[str]:
        """Extract file paths from task description"""
        # Build path prefix pattern from known dirs + VNX_DOCS_DIRS entries
        prefixes = ['src', 'tests', 'docs', '\\.claude']
        docs_dirs_raw = os.environ.get("VNX_DOCS_DIRS", "")
        if docs_dirs_raw:
            for entry in docs_dirs_raw.split(","):
                name = Path(entry.strip()).name
                if name and name not in prefixes:
                    prefixes.append(re.escape(name))
        path_pattern = r'(?:' + '|'.join(prefixes) + r')/(?:[^\\s`),]+)'
        paths = [p.strip() for p in re.findall(path_pattern, text)]

        # Also capture bare filenames (e.g. SEOScanForm.tsx, scan_orchestrator_service.py)
        # This improves path-based matching when dispatches mention files without full paths.
        filename_pattern = r'\\b[a-zA-Z0-9_\\-]+\\.(?:py|ts|tsx|js|jsx|md|json|yaml|yml)\\b'
        for name in re.findall(filename_pattern, text):
            if name not in paths:
                paths.append(name)
        return paths

    def _get_preferred_language(self, task_paths: List[str], keywords: List[str]) -> Optional[str]:
        """Determine preferred language based on task context.
        Returns: "python", "markdown", or None (search all).
        """
        doc_keywords = {
            'documentation', 'docs', 'document', 'markdown',
            'guide', 'runbook', 'architecture-doc', 'api-doc',
            'content', 'marketing', 'sales', 'deployment-guide',
        }
        if any(k in keywords for k in doc_keywords):
            return "markdown"

        if not task_paths:
            return None

        exts = {Path(p.strip('`')).suffix.lower() for p in task_paths if p}
        if not exts:
            return None

        if exts.issubset({'.md', '.markdown', '.txt'}):
            return "markdown"
        if exts.issubset({'.py'}):
            return "python"
        return None

    def _should_skip_code_patterns(self, task_paths: List[str], keywords: List[str]) -> bool:
        """Skip code pattern lookup for non-code tasks (docs/UI) unless explicitly Python-related."""
        # Frontend/UI keyword detection even if no explicit file paths
        frontend_keywords = {
            'frontend', 'react', 'tsx', 'jsx', 'ui', 'ux', 'seoscanform',
            'marketing-magic-circle', 'nextjs', 'next', 'tailwind', 'component'
        }
        if any(k in keywords for k in frontend_keywords):
            # Frontend snippets are now seeded; allow pattern lookup for UI tasks
            return False
        if not task_paths:
            return False
        exts = {Path(p.strip('`')).suffix.lower() for p in task_paths if p}
        if not exts:
            return False
        non_code_exts = {'.md', '.markdown', '.txt', '.html', '.htm', '.css', '.js', '.ts', '.tsx', '.json', '.yaml', '.yml'}
        if exts.issubset(non_code_exts):
            # Allow override if Python explicitly mentioned
            if 'python' in keywords or 'py' in keywords:
                return False
            return True
        return False

    def _path_hints(self, task_paths: List[str]) -> Dict[str, Any]:
        hints = set()
        extensions = set()
        for raw in task_paths:
            cleaned = re.sub(r'[\\),.;]+$', '', raw.strip('`'))
            path = Path(cleaned)
            if path.suffix:
                extensions.add(path.suffix.lower())
            if path.name:
                hints.add(path.name.lower())
            parent = str(path.parent).lower()
            if parent and parent not in ('.', '/'):
                hints.add(parent)
        return {"hints": hints, "extensions": extensions}

    def _is_testing_gate(self, gate: Optional[str]) -> bool:
        if not gate:
            return False
        return gate.lower() in {
            'testing', 'review', 'validation', 'quality', 'quality_gate',
            'integration', 'integration-testing', 'qa'
        }

    def _extract_component_tags(self, task_paths: List[str], preferred_tags: Optional[List[str]] = None) -> List[str]:
        """Extract component tags from file paths to narrow search space"""
        component_tags = []

        # Map file paths to component tags
        for path in task_paths:
            path_lower = path.lower()
            if 'api' in path_lower or 'controllers' in path_lower:
                component_tags.append('api')
            if 'crawler' in path_lower or 'extractor' in path_lower:
                component_tags.append('crawler')
            if 'storage' in path_lower or 'database' in path_lower:
                component_tags.append('storage')
            if 'test' in path_lower:
                component_tags.append('testing')
            if 'sse' in path_lower or 'streaming' in path_lower:
                component_tags.append('sse')

        # Add preferred tags if provided
        if preferred_tags:
            component_tags.extend([t for t in preferred_tags if t in ['crawler', 'storage', 'api', 'extraction', 'sse']])

        return list(set(component_tags))  # Unique tags only

    def _extract_path_tags(self, task_paths: List[str]) -> List[str]:
        """Extract enriched file-path tags for preferred tag overlap"""
        tags = set()
        for path in task_paths:
            path_lower = path.lower()
            if 'src/crawler' in path_lower:
                tags.add('crawler-component')
            if 'src/storage' in path_lower:
                tags.add('storage-component')
            if 'src/frontend' in path_lower:
                tags.add('frontend-component')
            if '/tests/' in path_lower or path_lower.startswith('tests/') or 'test_' in path_lower:
                tags.add('testing')
        return list(tags)

    def _search_specific_paths(self, task_paths: List[str], keywords: List[str], limit: int) -> List[Dict]:
        """Search patterns from specific files mentioned in task"""
        patterns = []
        seen = set()

        for task_path in task_paths[:3]:  # Limit to first 3 paths
            # Extract filename or directory from path
            path_parts = Path(task_path).parts
            search_term = path_parts[-1] if path_parts else task_path

            query = """
            SELECT rowid as snippet_id, title, description, code, file_path, line_range,
                   tags, quality_score, usage_count, last_updated, language
            FROM code_snippets
            WHERE file_path LIKE ?
            AND quality_score >= 80
            ORDER BY quality_score DESC
            LIMIT ?
            """
            try:
                cursor = self.quality_db.execute(query, (f"%{search_term}%", limit * 2))
                for row in cursor:
                    pattern = dict(row)
                    key = (pattern.get('title'), pattern.get('file_path'), pattern.get('line_range'))
                    if key not in seen:
                        seen.add(key)
                        patterns.append(pattern)
            except Exception:
                continue

        return patterns

    def _finalize_patterns(self, patterns: List[Dict], keywords: List[str],
                          preferred_tags: Optional[List[str]], task_paths: Optional[List[str]],
                          gate: Optional[str], limit: int) -> List[Dict]:
        """Common pattern finalization logic"""
        # Hard filters to avoid low-value injections
        preferred_tags = [t.lower() for t in (preferred_tags or [])]
        task_paths = task_paths or []
        path_hints = self._path_hints(task_paths)["hints"] if task_paths else []

        def _tag_set(pattern: Dict) -> List[str]:
            tags = str(pattern.get('tags', '')).lower()
            tag_list = [t.strip() for t in re.split(r'[,\\s]+', tags) if t.strip()]
            return tag_list

        for pattern in patterns:
            # Calculate relevance score
            pattern['relevance_score'] = self.score_pattern_relevance(
                pattern, keywords, preferred_tags=preferred_tags,
                task_paths=task_paths, gate=gate
            )

            # Compute stable hash for usage tracking
            pattern_hash = f"{pattern.get('title','')}|{pattern.get('file_path','')}|{pattern.get('line_range','')}"
            pattern['pattern_hash'] = hashlib.sha1(pattern_hash.encode("utf-8")).hexdigest()

            # Clean up code snippet for display
            if pattern.get('code'):
                code = pattern['code']
                if len(code) > 1000:
                    code = code[:997] + "..."
                pattern['code'] = code

        # Register offered patterns in pattern_usage for feedback loop tracking
        self._register_offered_patterns(patterns)

        # Verify pattern freshness via citation commit hashes
        for pattern in patterns:
            self._verify_pattern_freshness(pattern)

        # Hard filters: enforce actionable relevance (lightweight)
        filtered = []
        for pattern in patterns:
            tags = _tag_set(pattern)

            # Rule: exclude generic-only patterns
            if not tags or (len(tags) == 1 and tags[0] == 'general'):
                continue

            # Rule: require at least 1 preferred tag overlap when preferred tags exist
            if preferred_tags:
                overlap = sum(1 for t in preferred_tags if t in tags)
                if overlap < 1:
                    continue

            # Soft rule: prefer file path overlap when task paths are provided
            if path_hints:
                file_path = str(pattern.get('file_path', '')).lower()
                if any(hint in file_path for hint in path_hints):
                    pattern['relevance_score'] = pattern.get('relevance_score', 0) + 0.2

            filtered.append(pattern)

        # Sort by relevance and return top N
        filtered.sort(key=lambda x: x['relevance_score'], reverse=True)

        # Return top patterns based on relevance threshold
        if filtered and filtered[0].get('relevance_score', 0) >= 0.6:
            return filtered[:limit]
        if filtered:
            return filtered[:1]
        # Fallback: if filters remove all patterns, return best-scoring original pattern
        patterns.sort(key=lambda x: x['relevance_score'], reverse=True)
        return patterns[:1] if patterns else []

    def _register_offered_patterns(self, patterns: List[Dict]):
        """Register offered patterns in pattern_usage table for feedback loop tracking.

        Called at dispatch time so the learning loop can later detect which patterns
        were offered but never used (ignored).
        """
        if not self.quality_db:
            return

        now = datetime.now().isoformat()
        for pattern in patterns:
            pattern_hash = pattern.get('pattern_hash', '')
            if not pattern_hash:
                continue

            try:
                self.quality_db.execute('''
                    INSERT INTO pattern_usage
                        (pattern_id, pattern_title, pattern_hash, last_offered, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pattern_id) DO UPDATE SET
                        last_offered = excluded.last_offered,
                        updated_at = excluded.updated_at
                ''', (
                    pattern_hash,
                    str(pattern.get('title', ''))[:200],
                    pattern_hash,
                    now, now, now
                ))
            except Exception:
                pass

        try:
            self.quality_db.commit()
        except Exception:
            pass

    def _verify_pattern_freshness(self, pattern: Dict):
        """Verify pattern freshness by comparing source_commit_hash with current file state.

        Adds a 'citation' dict to the pattern with commit hash and staleness info.
        """
        if not self.quality_db:
            return

        file_path = pattern.get('file_path', '')
        if not file_path:
            return

        try:
            cursor = self.quality_db.execute('''
                SELECT source_commit_hash, verified_at
                FROM snippet_metadata
                WHERE file_path = ?
                  AND source_commit_hash IS NOT NULL
                LIMIT 1
            ''', (file_path,))
            row = cursor.fetchone()

            if not row:
                return

            stored_hash = row['source_commit_hash']
            verified_at = row['verified_at']

            # Get current commit hash for the file
            import subprocess
            result = subprocess.run(
                ['git', 'log', '-1', '--format=%H', '--', file_path],
                capture_output=True, text=True, timeout=5,
                cwd=str(self.project_root)
            )
            current_hash = result.stdout.strip() if result.returncode == 0 else None

            is_stale = current_hash is not None and current_hash != stored_hash

            pattern['citation'] = {
                'source_commit': stored_hash[:12] if stored_hash else None,
                'current_commit': current_hash[:12] if current_hash else None,
                'stale': is_stale,
                'verified_at': verified_at
            }

        except Exception:
            pass

    def score_pattern_relevance(
        self,
        pattern: Dict,
        keywords: List[str],
        preferred_tags: Optional[List[str]] = None,
        task_paths: Optional[List[str]] = None,
        gate: Optional[str] = None
    ) -> float:
        """Enhanced scoring with gate awareness, path relevance, recency, and tag matching"""
        score = 0.0

        # Check title matches (highest weight)
        title = str(pattern.get('title', '')).lower()
        for keyword in keywords:
            if keyword in title:
                score += 0.4

        # Check description matches
        description = str(pattern.get('description', '')).lower()
        for keyword in keywords:
            if keyword in description:
                score += 0.3

        # Check tags matches (improved tag scoring)
        tags = str(pattern.get('tags', '')).lower()
        tag_list = [t.strip() for t in re.split(r'[,\\s]+', tags) if t.strip()]
        tag_set = set(tag_list)
        tag_boost = 0.0
        for keyword in keywords:
            if keyword in tags:
                tag_boost += 0.2
        # Boost for specific/compound tags vs generic ones
        if any(t in tag_set for t in ('sse', 'sse-streaming', 'pipeline', 'browser-pool')):
            tag_boost *= 1.5
        score += min(tag_boost, 0.4)  # Cap tag contribution

        # Preferred tag boost (from task tags)
        preferred_tags = [t.lower() for t in (preferred_tags or [])]
        tag_hits = 0
        if preferred_tags:
            tag_hits = sum(1 for t in preferred_tags if t in tag_set)
            if tag_hits:
                score += min(0.3, 0.1 * tag_hits)
            else:
                score *= 0.6

        # File path relevance (new) - boost for relevant paths
        file_path = str(pattern.get('file_path', '')).lower()
        if any(kw in file_path for kw in keywords):
            score += 0.15
        # Boost for production/real-world test files
        if 'production' in file_path or 'real_world' in file_path or 'sme' in file_path:
            score += 0.1

        # Task path hints - boost if exact file/dir mentioned, penalize if not
        path_hit = False
        if task_paths:
            hints = self._path_hints(task_paths)
            if any(hint in file_path for hint in hints["hints"]):
                path_hit = True
                score += 0.3
            else:
                score *= 0.4

        # Extra boost when both file-path overlap AND tag overlap are present
        if path_hit and tag_hits:
            score += 0.25

        # Gate-aware testing penalty/boost
        is_test_gate = self._is_testing_gate(gate)
        if '/tests/' in file_path or 'test_' in Path(file_path).name.lower():
            score *= 1.3 if is_test_gate else 0.15

        # Quality score weighting (patterns with higher quality)
        quality = float(pattern.get('quality_score', 70)) / 100.0
        score *= (0.8 + 0.2 * quality)  # 80-100% multiplier based on quality

        # Usage count penalty (prefer less-used patterns for diversity)
        usage_count = int(pattern.get('usage_count', 0))
        if usage_count > 5:
            score *= 0.9  # Slight penalty for overused patterns

        # Recency boost (prefer newer patterns) - use last_updated if available
        last_updated = pattern.get('last_updated')
        if last_updated:
            try:
                updated = datetime.fromisoformat(str(last_updated).strip())
                days_old = (datetime.now() - updated).days
                if days_old <= 14:
                    score *= 1.35
                elif days_old <= 30:
                    score *= 1.2
                elif days_old <= 90:
                    score *= 1.0
                else:
                    score *= 0.8
            except ValueError:
                pass

        # Cap overall score and ensure minimum
        score = max(0.01, min(score, 2.0))  # Keep between 0.01 and 2.0

        return score

    def query_relevant_patterns(
        self,
        task_description: str,
        limit: int = 5,
        gate: Optional[str] = None,
        task_paths: Optional[List[str]] = None,
        preferred_tags: Optional[List[str]] = None
    ) -> List[Dict]:
        """Query relevant code patterns from 1,143 available patterns"""
        if not self.quality_db:
            return []

        try:
            # Extract keywords for matching
            keywords = self.extract_keywords(task_description)
            task_paths = task_paths or []

            # Language-aware filtering: doc tasks get markdown, code tasks get python
            preferred_lang = self._get_preferred_language(task_paths, keywords)

            # Frontend/UI/doc tasks should not pull python snippets by default
            if self._should_skip_code_patterns(task_paths, keywords) and not self._is_testing_gate(gate):
                if preferred_lang != "markdown":
                    return []
                # Doc task: allow through, FTS5 query will filter on language="markdown"

            # BAND-AID FIX 1: Component tag pre-filtering
            # Extract component from task paths to narrow search space
            component_tags = self._extract_component_tags(task_paths, preferred_tags)

            # BAND-AID FIX 2: File path priority search
            # If specific files mentioned, search those files FIRST
            if task_paths and len(task_paths) <= 3:  # Specific file references
                patterns_from_paths = self._search_specific_paths(task_paths, keywords, limit)
                if len(patterns_from_paths) >= 2:  # Found enough relevant patterns
                    return self._finalize_patterns(patterns_from_paths, keywords, preferred_tags, task_paths, gate, limit)

            if not keywords:
                # If no keywords, return high-quality general patterns
                if component_tags:
                    # Filter by component tags
                    tag_filter = ' OR '.join([f'tags:"{tag}"' for tag in component_tags])
                    query = """
                    SELECT rowid as snippet_id, title, description, code, file_path, line_range,
                           tags, quality_score, usage_count, last_updated, language
                    FROM code_snippets
                    WHERE code_snippets MATCH ?
                    AND quality_score >= 85
                    ORDER BY quality_score DESC, usage_count DESC
                    LIMIT ?
                    """
                    cursor = self.quality_db.execute(query, (tag_filter, limit))
                else:
                    query = """
                    SELECT rowid as snippet_id, title, description, code, file_path, line_range,
                           tags, quality_score, usage_count, last_updated, language
                    FROM code_snippets
                    WHERE quality_score >= 85
                    ORDER BY quality_score DESC, usage_count DESC
                    LIMIT ?
                    """
                    cursor = self.quality_db.execute(query, (limit,))
            else:
                # Build FTS5 match query - quote each term to avoid column reference errors
                # (e.g., "1-5" would be interpreted as columns 1 through 5 without quotes)
                quoted_keywords = [f'"{k}"' for k in keywords if k and len(k) > 1]

                # BAND-AID FIX 3: Add component tags to search terms for better filtering
                if component_tags:
                    # Boost component-specific patterns
                    quoted_keywords.extend([f'tags:"{tag}"' for tag in component_tags[:2]])

                # Language filter: restrict to preferred language when set
                if preferred_lang:
                    quoted_keywords.append(f'language:"{preferred_lang}"')

                match_terms = ' OR '.join(quoted_keywords)

                # Lower quality threshold for markdown (docs score lower by design)
                min_quality = 40 if preferred_lang == "markdown" else 85

                query = """
                SELECT rowid as snippet_id, title, description, code, file_path, line_range,
                       tags, quality_score, usage_count, last_updated, language
                FROM code_snippets
                WHERE code_snippets MATCH ?
                AND quality_score >= ?
                ORDER BY rank, quality_score DESC
                LIMIT ?
                """
                cursor = self.quality_db.execute(query, (match_terms, min_quality, limit * 3))

            # Collect unique patterns from cursor
            patterns = []
            seen = set()
            for row in cursor:
                pattern = dict(row)
                key = (pattern.get('title'), pattern.get('file_path'), pattern.get('line_range'))
                if key not in seen:
                    seen.add(key)
                    patterns.append(pattern)

            # Add path-hint matches as a fallback
            if task_paths:
                hints = self._path_hints(task_paths)["hints"]
                for hint in list(hints)[:6]:
                    if not hint:
                        continue
                    path_query = """
                    SELECT rowid as snippet_id, title, description, code, file_path, line_range,
                           tags, quality_score, usage_count, last_updated, language
                    FROM code_snippets
                    WHERE file_path LIKE ?
                    AND quality_score >= 85
                    ORDER BY quality_score DESC
                    LIMIT ?
                    """
                    cursor = self.quality_db.execute(path_query, (f"%{hint}%", limit))
                    for row in cursor:
                        pattern = dict(row)
                        key = (pattern.get('title'), pattern.get('file_path'), pattern.get('line_range'))
                        if key not in seen:
                            seen.add(key)
                            patterns.append(pattern)

            # Finalize patterns with scoring and filtering
            return self._finalize_patterns(patterns, keywords, preferred_tags, task_paths, gate, limit)

        except Exception as e:
            print(f"⚠️ Warning: Pattern query failed: {e}")
            return []

    def query_prevention_rules(self, task_description: str, tags: Optional[List[str]] = None) -> List[Dict]:
        """Generate prevention rules based on task context and known patterns"""
        rules = []
        desc_lower = task_description.lower()

        # Extract keywords to understand task context
        keywords = self.extract_keywords(task_description)

        # Rule 1: SSE/Streaming related warnings
        if any(kw in desc_lower for kw in ['sse', 'streaming', 'server-sent', 'events']):
            rules.append({
                'rule': 'SSE Pipeline Memory Management',
                'warning': 'Ensure proper cleanup of SSE connections to prevent memory leaks',
                'recommendation': 'Implement connection timeout handlers and cleanup in finally blocks',
                'confidence': 0.9,
                'pattern_refs': ['test_sse_performance', 'test_progressive_sse']
            })

        # Rule 2: Browser/Crawler warnings
        if any(kw in desc_lower for kw in ['browser', 'crawler', 'playwright', 'chromium']):
            rules.append({
                'rule': 'Browser Process Cleanup',
                'warning': 'Always kill Chromium processes after crawler operations to prevent memory accumulation',
                'recommendation': 'Use browser_pool.py and chromium_killer.py for proper cleanup',
                'confidence': 0.95,
                'pattern_refs': ['browser_pool_validation', 'chromium_memory_optimization']
            })

        # Rule 3: Authentication/Security warnings
        if any(kw in desc_lower for kw in ['auth', 'login', 'security', 'jwt', 'token']):
            rules.append({
                'rule': 'Authentication Security',
                'warning': 'Implement proper JWT validation and secure token storage',
                'recommendation': 'Use environment variables for secrets, validate JWT expiry',
                'confidence': 0.85,
                'pattern_refs': ['auth_middleware', 'jwt_validation']
            })

        # Rule 4: Database/Storage warnings
        if any(kw in desc_lower for kw in ['database', 'storage', 'query', 'supabase', 'sql']):
            rules.append({
                'rule': 'Database Performance',
                'warning': 'Avoid N+1 queries and ensure proper indexing for performance',
                'recommendation': 'Use batch operations and connection pooling, target p95 <50ms',
                'confidence': 0.8,
                'pattern_refs': ['storage_optimizer', 'query_performance']
            })

        # Rule 5: Testing related warnings
        if any(kw in desc_lower for kw in ['test', 'validate', 'qa', 'coverage']):
            rules.append({
                'rule': 'Test Coverage Requirements',
                'warning': 'Ensure ≥80% unit test coverage and ≥70% integration test coverage',
                'recommendation': 'Include edge cases, error scenarios, and performance tests',
                'confidence': 0.75,
                'pattern_refs': ['test_coverage_validation', 'test_edge_cases']
            })

        # Rule 6: Production deployment warnings
        if any(kw in desc_lower for kw in ['production', 'deploy', 'release', 'live']):
            rules.append({
                'rule': 'Production Safety',
                'warning': 'Validate memory usage <500MB and p95 response times before production',
                'recommendation': 'Run production validation tests and monitor resource usage',
                'confidence': 0.9,
                'pattern_refs': ['production_validation', 'memory_monitoring']
            })

        # Rule 7: SME/B2B specific warnings
        if any(kw in desc_lower for kw in ['sme', 'b2b', 'business', 'enterprise']):
            rules.append({
                'rule': 'SME B2B Requirements',
                'warning': 'Ensure Dutch compliance with KvK/BTW validation and decimal formats',
                'recommendation': 'Validate business numbers, use proper decimal formatting for Dutch market',
                'confidence': 0.7,
                'pattern_refs': ['dutch_compliance', 'b2b_validation']
            })

        # Rule 8: Performance optimization warnings
        if any(kw in desc_lower for kw in ['optimize', 'performance', 'speed', 'bottleneck']):
            rules.append({
                'rule': 'Performance Optimization',
                'warning': 'Profile before optimizing - avoid premature optimization',
                'recommendation': 'Use proper profiling tools, focus on critical path optimizations',
                'confidence': 0.8,
                'pattern_refs': ['performance_profiling', 'optimization_strategy']
            })

        # Rule 9: API development warnings
        if any(kw in desc_lower for kw in ['api', 'endpoint', 'rest', 'graphql']):
            rules.append({
                'rule': 'API Design Best Practices',
                'warning': 'Implement proper error handling, rate limiting, and API versioning',
                'recommendation': 'Use consistent response formats, implement proper status codes',
                'confidence': 0.75,
                'pattern_refs': ['api_design', 'error_handling']
            })

        # Rule 10: Refactoring warnings
        if any(kw in desc_lower for kw in ['refactor', 'cleanup', 'technical debt', 'improve']):
            rules.append({
                'rule': 'Refactoring Safety',
                'warning': 'Ensure comprehensive tests exist before major refactoring',
                'recommendation': 'Create a refactoring plan, preserve functionality, use feature flags',
                'confidence': 0.85,
                'pattern_refs': ['safe_refactoring', 'feature_flags']
            })

        return rules

    def extract_tags_from_description(self, description: str) -> List[str]:
        """Extract specific, compound tags from task description for better pattern matching"""
        tags = []
        desc_lower = description.lower()

        # Phase detection with specific contexts
        if any(word in desc_lower for word in ['design', 'plan', 'architect']):
            tags.append('design-phase')
            if 'system' in desc_lower:
                tags.append('system-architecture')
            if 'api' in desc_lower:
                tags.append('api-design')

        if any(word in desc_lower for word in ['implement', 'code', 'develop', 'build']):
            tags.append('implementation-phase')
            if 'feature' in desc_lower:
                tags.append('feature-implementation')
            if 'fix' in desc_lower:
                tags.append('bug-fix-implementation')

        if any(word in desc_lower for word in ['test', 'validate', 'qa', 'coverage']):
            tags.append('testing-phase')
            if 'unit' in desc_lower:
                tags.append('unit-testing')
            if 'integration' in desc_lower:
                tags.append('integration-testing')
            if 'e2e' in desc_lower or 'end-to-end' in desc_lower:
                tags.append('e2e-testing')
            if 'production' in desc_lower:
                tags.append('production-validation')

        if any(word in desc_lower for word in ['production', 'deploy', 'release', 'live']):
            tags.append('production-phase')
            if 'rollback' in desc_lower:
                tags.append('production-rollback')

        # Component detection with specific subsystems
        if any(word in desc_lower for word in ['crawler', 'scrape', 'web', 'crawl4ai']):
            tags.append('crawler-component')
            if 'vnx' in desc_lower:
                tags.append('vnx-crawler')
            if 'hybrid' in desc_lower:
                tags.append('hybrid-crawler')
            if 'browser' in desc_lower or 'chromium' in desc_lower:
                tags.append('browser-pool')

        if any(word in desc_lower for word in ['storage', 'database', 'persist', 'supabase']):
            tags.append('storage-component')
            if 'query' in desc_lower:
                tags.append('storage-query')
            if 'optimization' in desc_lower or 'optimize' in desc_lower:
                tags.append('storage-optimization')

        if any(word in desc_lower for word in ['api', 'endpoint', 'controller', 'route']):
            tags.append('api-component')
            if 'quickscan' in desc_lower:
                tags.append('quickscan-api')
            if 'sse' in desc_lower or 'server-sent' in desc_lower:
                tags.append('sse-pipeline')

        # Specific feature tags
        if 'sse' in desc_lower or 'streaming' in desc_lower:
            tags.append('sse-streaming')
            if 'pipeline' in desc_lower:
                tags.append('sse-pipeline')
            if 'performance' in desc_lower:
                tags.append('sse-performance')

        if 'quickscan' in desc_lower:
            tags.append('quickscan')
            if 'preview' in desc_lower:
                tags.append('quickscan-preview')
            if 'extended' in desc_lower:
                tags.append('quickscan-extended')

        if 'sme' in desc_lower or 'b2b' in desc_lower:
            tags.append('sme-b2b')
            if 'test' in desc_lower:
                tags.append('sme-b2b-testing')
            if 'dutch' in desc_lower or 'kvk' in desc_lower or 'btw' in desc_lower:
                tags.append('dutch-compliance')

        # Issue detection with specific contexts
        if any(word in desc_lower for word in ['validation', 'invalid', 'schema', 'validate']):
            tags.append('validation')
            if 'error' in desc_lower:
                tags.append('validation-error')
            if 'pipeline' in desc_lower:
                tags.append('validation-pipeline')

        if any(word in desc_lower for word in ['performance', 'slow', 'optimize', 'bottleneck']):
            tags.append('performance')
            if 'issue' in desc_lower or 'problem' in desc_lower:
                tags.append('performance-issue')
            if 'optimization' in desc_lower:
                tags.append('performance-optimization')
            if 'memory' in desc_lower:
                tags.append('memory-optimization')

        if any(word in desc_lower for word in ['memory', 'leak', 'oom']):
            tags.append('memory')
            if 'leak' in desc_lower:
                tags.append('memory-leak')
            if 'optimization' in desc_lower:
                tags.append('memory-optimization')
            if 'browser' in desc_lower or 'chromium' in desc_lower:
                tags.append('browser-memory')

        if any(word in desc_lower for word in ['race', 'concurrency', 'thread', 'parallel']):
            tags.append('concurrency')
            if 'race' in desc_lower:
                tags.append('race-condition')
            if 'deadlock' in desc_lower:
                tags.append('deadlock')

        # Technical debt and refactoring
        if any(word in desc_lower for word in ['refactor', 'cleanup', 'technical debt', 'improve']):
            tags.append('refactoring')
            if 'technical debt' in desc_lower:
                tags.append('technical-debt')
            if 'cleanup' in desc_lower:
                tags.append('code-cleanup')

        # Security related
        if any(word in desc_lower for word in ['security', 'auth', 'jwt', 'token', 'encryption']):
            tags.append('security')
            if 'auth' in desc_lower or 'jwt' in desc_lower:
                tags.append('authentication')
            if 'vulnerability' in desc_lower:
                tags.append('security-vulnerability')

        # Dutch market specific
        if any(word in desc_lower for word in ['dutch', 'netherlands', 'kvk', 'btw', 'nl']):
            tags.append('dutch-market')
            if 'kvk' in desc_lower or 'btw' in desc_lower:
                tags.append('dutch-compliance')
            if 'decimal' in desc_lower or 'format' in desc_lower:
                tags.append('dutch-formatting')

        # Severity detection (keep at the end for priority)
        if any(word in desc_lower for word in ['critical', 'blocker', 'urgent', 'emergency']):
            tags.append('critical-severity')
        elif any(word in desc_lower for word in ['high', 'important', 'major']):
            tags.append('high-priority')
        elif any(word in desc_lower for word in ['medium', 'moderate']):
            tags.append('medium-priority')
        else:
            tags.append('normal-priority')

        return list(set(tags))  # Remove duplicates

    def analyze_tags_for_task(
        self,
        tags: List[str],
        phase: Optional[str] = None,
        terminal: Optional[str] = None
    ) -> Dict[str, Any]:
        """Analyze tags for a task and return intelligence"""
        if not self.tag_engine:
            return {"analyzed": False, "reason": "tag_engine_not_available"}

        try:
            # Analyze tag combination
            result = self.tag_engine.analyze_multi_tag_patterns(
                tags=tags,
                phase=phase,
                terminal=terminal,
                outcome=None  # Outcome tracked later after task completion
            )

            return result
        except Exception as e:
            print(f"⚠️ Warning: Tag analysis failed: {e}")
            return {"analyzed": False, "error": str(e)}

    def find_similar_reports(self, task_description: str) -> List[Dict]:
        """Find similar reports from mined data"""
        if not self.quality_db:
            return []

        try:
            # Extract keywords from task description
            keywords = self.extract_keywords(task_description)
            tags = self.extract_tags_from_description(task_description)

            # Query report_findings table for similar reports
            query = '''
                SELECT report_path, task_type, summary, tags_found,
                       patterns_found, antipatterns_found, prevention_rules_found,
                       report_date, terminal
                FROM report_findings
                WHERE summary LIKE ? OR tags_found LIKE ?
                ORDER BY extracted_at DESC
                LIMIT 5
            '''

            keyword_pattern = '%' + '%'.join(keywords) + '%'
            tag_pattern = '%' + '%'.join(tags) + '%'

            cursor = self.quality_db.execute(query, (keyword_pattern, tag_pattern))
            reports = []

            for row in cursor:
                reports.append({
                    'report_path': row['report_path'],
                    'task_type': row['task_type'],
                    'summary': row['summary'],
                    'tags': json.loads(row['tags_found']) if row['tags_found'] else [],
                    'patterns_found': row['patterns_found'],
                    'antipatterns_found': row['antipatterns_found'],
                    'prevention_rules_found': row['prevention_rules_found'],
                    'report_date': row['report_date'],
                    'terminal': row['terminal']
                })

            return reports

        except Exception as e:
            print(f"⚠️ Warning: Could not find similar reports: {e}")
            return []

    def query_antipatterns(self, task_description: str, limit: int = 5) -> List[Dict]:
        """Query relevant antipatterns from mined data"""
        if not self.quality_db:
            return []

        try:
            keywords = self.extract_keywords(task_description)
            keyword_pattern = '%' + '%'.join(keywords) + '%'

            query = '''
                SELECT title, description, category, severity,
                       occurrence_count
                FROM antipatterns
                WHERE title LIKE ? OR description LIKE ?
                ORDER BY occurrence_count DESC, last_seen DESC
                LIMIT ?
            '''

            cursor = self.quality_db.execute(query, (keyword_pattern, keyword_pattern, limit))
            antipatterns = []

            for row in cursor:
                antipatterns.append({
                    'pattern': row['title'],
                    'description': row['description'],
                    'category': row['category'],
                    'severity': row['severity'],
                    'occurrence_count': row['occurrence_count'],
                    'has_prevention_rule': False
                })

            return antipatterns

        except Exception as e:
            print(f"⚠️ Warning: Could not query antipatterns: {e}")
            return []

    def get_mined_quality_context(self, task_description: str) -> str:
        """Generate quality context from mined report data"""
        if not self.quality_db:
            return ""

        context_parts = []

        # Add relevant antipatterns
        antipatterns = self.query_antipatterns(task_description, limit=3)
        if antipatterns:
            context_parts.append("Known antipatterns to avoid:")
            for ap in antipatterns:
                context_parts.append(f"  - {ap['pattern'][:80]} (severity: {ap['severity']}, occurrences: {ap['occurrence_count']})")

        # Add relevant prevention rules
        try:
            keywords = self.extract_keywords(task_description)
            keyword_pattern = '%' + '%'.join(keywords) + '%'

            cursor = self.quality_db.execute('''
                SELECT description, recommendation, confidence
                FROM prevention_rules
                WHERE description LIKE ? OR recommendation LIKE ?
                ORDER BY confidence DESC
                LIMIT 3
            ''', (keyword_pattern, keyword_pattern))

            rules = cursor.fetchall()
            if rules:
                context_parts.append("\nRelevant prevention rules:")
                for rule in rules:
                    context_parts.append(f"  - {rule['description'][:40]} → {rule['recommendation'][:40]}")

        except Exception as e:
            print(f"⚠️ Warning: Could not query prevention rules: {e}")

        # Add similar report references
        similar_reports = self.find_similar_reports(task_description)
        if similar_reports:
            context_parts.append("\nSimilar past reports:")
            for report in similar_reports[:2]:
                context_parts.append(f"  - {report.get('task_type', 'Unknown')}: {report.get('summary', '')[:60]}")

        return '\n'.join(context_parts) if context_parts else ""


def main():
    """CLI interface for testing intelligence gatherer"""
    import sys

    gatherer = T0IntelligenceGatherer()

    human, argv = parse_human_flag(sys.argv[1:])

    if len(argv) > 0:
        command = argv[0]

        if command == "list-agents":
            if human:
                emit_human("Valid agents:")
                for agent in gatherer.agent_directory:
                    emit_human(f"  - {agent}")
            else:
                emit_json({"agents": gatherer.agent_directory})
            return EXIT_OK

        elif command == "validate":
            if len(argv) < 2:
                if human:
                    emit_human("Usage: gather_intelligence.py validate <agent-name> [--human]")
                else:
                    emit_json({"error": "Missing agent name", "usage": "validate <agent-name> [--human]"})
                return EXIT_VALIDATION

            agent_name = argv[1]
            result = gatherer.validate_agent(agent_name)
            if human:
                if result.get("valid"):
                    emit_human(f"Valid agent: {result.get('agent') or agent_name}")
                else:
                    emit_human(f"Invalid agent: {agent_name}")
                    emit_human(f"Error: {result.get('error')}")
                    emit_human(f"Suggestion: {result.get('suggestion')}")
            else:
                emit_json(result)
            return EXIT_OK if result.get("valid") else EXIT_VALIDATION

        elif command == "gather":
            if len(argv) < 3:
                if human:
                    emit_human("Usage: gather_intelligence.py gather <task> <terminal> [agent] [gate] [--human]")
                else:
                    emit_json({"error": "Missing required arguments", "usage": "gather <task> <terminal> [agent] [gate] [--human]"})
                return EXIT_VALIDATION

            task = argv[1]
            terminal = argv[2]
            agent = argv[3] if len(argv) > 3 else None
            gate = argv[4] if len(argv) > 4 else None

            result = gatherer.gather_for_dispatch(task, terminal, agent, gate)
            if human:
                emit_human(f"Dispatch blocked: {result.get('dispatch_blocked', False)}")
                if result.get("error"):
                    emit_human(f"Error: {result.get('error')}")
                emit_human(f"Pattern count: {result.get('pattern_count', 0)}")
                emit_human(f"Prevention rules: {result.get('prevention_rule_count', 0)}")
            else:
                emit_json(result)
            return EXIT_OK if not result.get("dispatch_blocked") else EXIT_VALIDATION

        elif command == "patterns":
            if len(argv) < 2:
                if human:
                    emit_human("Usage: gather_intelligence.py patterns <task-description> [--human]")
                else:
                    emit_json({"error": "Missing task description", "usage": "patterns <task-description> [--human]"})
                return EXIT_VALIDATION

            task = ' '.join(argv[1:])
            patterns = gatherer.query_relevant_patterns(task)

            if human:
                emit_human(f"\nFound {len(patterns)} relevant patterns for: '{task}'\n")
                for i, pattern in enumerate(patterns):
                    emit_human(f"Pattern {i+1}:")
                    emit_human(f"  Title: {pattern.get('title', 'N/A')}")
                    emit_human(f"  Quality: {pattern.get('quality_score', 0)}%")
                    emit_human(f"  Relevance: {pattern.get('relevance_score', 0):.2f}")
                    emit_human(f"  Tags: {pattern.get('tags', '')}")
                    emit_human(f"  File: {pattern.get('file_path', '')}")
                    if pattern.get('description'):
                        emit_human(f"  Description: {pattern['description'][:100]}...")
                    emit_human("")
            else:
                emit_json({"query": task, "patterns": patterns})
            return EXIT_OK

        elif command == "keywords":
            if len(argv) < 2:
                if human:
                    emit_human("Usage: gather_intelligence.py keywords <text> [--human]")
                else:
                    emit_json({"error": "Missing text", "usage": "keywords <text> [--human]"})
                return EXIT_VALIDATION

            text = ' '.join(argv[1:])
            keywords = gatherer.extract_keywords(text)
            if human:
                emit_human(f"Keywords extracted: {keywords}")
            else:
                emit_json({"text": text, "keywords": keywords})
            return EXIT_OK

        elif command == "tags":
            if len(argv) < 2:
                if human:
                    emit_human("Usage: gather_intelligence.py tags <task-description> [--human]")
                else:
                    emit_json({"error": "Missing task description", "usage": "tags <task-description> [--human]"})
                return EXIT_VALIDATION

            task = ' '.join(argv[1:])
            tags = gatherer.extract_tags_from_description(task)
            if gatherer.tag_engine:
                analysis = gatherer.analyze_tags_for_task(tags)
            else:
                analysis = None

            if human:
                emit_human(f"Tags extracted: {tags}")
                if analysis is not None:
                    emit_human("\nTag Analysis:")
                    emit_human(json.dumps(analysis, indent=2))
            else:
                emit_json({"task": task, "tags": tags, "analysis": analysis})
            return EXIT_OK

        elif command == "prevention":
            if len(argv) < 2:
                if human:
                    emit_human("Usage: gather_intelligence.py prevention <task-description> [--human]")
                else:
                    emit_json({"error": "Missing task description", "usage": "prevention <task-description> [--human]"})
                return EXIT_VALIDATION

            task = ' '.join(argv[1:])
            rules = gatherer.query_prevention_rules(task)
            if human:
                emit_human(f"\nFound {len(rules)} prevention rules:\n")
                for rule in rules:
                    emit_human(f"Tags: {', '.join(rule['tag_combination'])}")
                    emit_human(f"Type: {rule['rule_type']}")
                    emit_human(f"Confidence: {rule['confidence']:.2f}")
                    emit_human(f"Recommendation: {rule['recommendation']}")
                    emit_human("")
            else:
                emit_json({"task": task, "prevention_rules": rules})
            return EXIT_OK

        else:
            if human:
                emit_human(f"Unknown command: {command}")
                emit_human("Available commands: list-agents, validate, gather, patterns, keywords, tags, prevention")
            else:
                emit_json({
                    "error": f"Unknown command: {command}",
                    "available_commands": ["list-agents", "validate", "gather", "patterns", "keywords", "tags", "prevention"],
                })
            return EXIT_VALIDATION
    else:
        if gatherer.quality_db:
            try:
                pattern_count = gatherer.quality_db.execute("SELECT COUNT(*) FROM code_snippets").fetchone()[0]
            except Exception:
                pattern_count = None
        else:
            pattern_count = None

        if gatherer.tag_engine:
            try:
                stats = gatherer.tag_engine.get_statistics()
            except Exception:
                stats = {}
        else:
            stats = {}

        if human:
            emit_human("VNX Intelligence Gatherer v1.4.0")
            emit_human(f"Loaded agents: {len(gatherer.agent_directory)}")
            emit_human(f"Database connected: {gatherer.quality_db is not None}")
            emit_human(f"Tag engine available: {gatherer.tag_engine is not None}")
            if pattern_count is not None:
                emit_human(f"Patterns available: {pattern_count}")
            if stats:
                emit_human(f"Tag combinations: {stats.get('total_combinations', 0)}")
                emit_human(f"Prevention rules: {stats.get('total_rules', 0)}")
            emit_human("\nCommands: list-agents, validate, gather, patterns, keywords, tags, prevention")
        else:
            emit_json({
                "version": "1.4.0",
                "loaded_agents": len(gatherer.agent_directory),
                "database_connected": gatherer.quality_db is not None,
                "tag_engine_available": gatherer.tag_engine is not None,
                "patterns_available": pattern_count,
                "tag_statistics": stats or None,
                "commands": ["list-agents", "validate", "gather", "patterns", "keywords", "tags", "prevention"],
            })
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
