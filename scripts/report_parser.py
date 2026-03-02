#!/usr/bin/env python3
"""
VNX Report Parser for Enhanced Receipt Generation
=================================================
Extracts structured data from markdown reports to enhance receipts with
tags, root causes, dependencies, and recommendations for T0 intelligence.

Author: T-MANAGER
Date: 2025-09-25
Version: 1.0
"""

import re
import os
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime


class ReportParser:
    """Extract structured intelligence from markdown reports"""

    def __init__(self):
        """Initialize parser with regex patterns"""
        script_dir = Path(__file__).resolve().parent
        sys.path.insert(0, str(script_dir / "lib"))
        try:
            from vnx_paths import ensure_env
        except Exception as exc:
            raise SystemExit(f"Failed to load vnx_paths: {exc}")

        paths = ensure_env()
        self.vnx_home = Path(paths["VNX_HOME"])
        self.dispatch_completed_dir = Path(paths["VNX_DISPATCH_DIR"]) / "completed"

        self.tag_patterns = {
            'issues': re.compile(r'\*\*Issue Tags\*\*:\s*(.+)'),
            'components': re.compile(r'\*\*Component Tags\*\*:\s*(.+)'),
            'solutions': re.compile(r'\*\*Solution Tags\*\*:\s*(.+)')
        }
        self.header_pattern = re.compile(r'^#{1,3}\s+(.+)$', re.MULTILINE)
        self.metadata_pattern = re.compile(r'\*\*([^*]+)\*\*:\s*(.+)')

        # New patterns for enhanced fields
        self.confidence_pattern = re.compile(r'\*\*Confidence\*\*:\s*([0-9.]+)')
        self.task_id_pattern = re.compile(r'\*\*Task ID\*\*:\s*(.+)')
        self.dispatch_id_pattern = re.compile(r'\*\*Dispatch ID\*\*:\s*(.+)')

        # INTELLIGENCE INTEGRATION (PR #8): Extract intelligence data from dispatch
        self.intelligence_pattern = re.compile(r'\[INTELLIGENCE_DATA\]\npattern_count:\s*(\d+)\nprevention_rules:\s*(\d+)\nquality_context:\s*(.+)')
        self.quality_context_pattern = re.compile(r'quality_context:\s*(.+)')

    def parse_report(self, markdown_path: str) -> Dict[str, Any]:
        """
        Parse a complete markdown report into enhanced receipt format

        Args:
            markdown_path: Path to markdown report file

        Returns:
            Dictionary with extracted fields for enhanced receipt
        """
        # Store filename for track letter detection in extract_metadata
        self._current_filename = Path(markdown_path).name

        try:
            with open(markdown_path, 'r') as f:
                content = f.read()
        except FileNotFoundError:
            return {'error': f'Report not found: {markdown_path}'}
        except Exception as e:
            return {'error': f'Error reading report: {str(e)}'}

        # Extract all components
        extracted = {
            'metadata': self.extract_metadata(content),
            'tags': self.extract_tags(content),
            'root_cause': self.extract_root_cause(content),
            'dependencies': self.extract_dependencies(content),
            'recommendations': self.extract_recommendations(content),
            'metrics': self.extract_metrics(content),
            'validation': self.extract_validation(content),
            'intelligence': self.extract_intelligence(content),  # PR #8: Extract quality_context
            'used_pattern_hashes': self.extract_used_pattern_hashes(content)
        }

        # PR #8 Fix: If no intelligence in report, try to cross-reference from dispatch
        if not extracted['intelligence'].get('quality_context'):
            # Extract dispatch_id from metadata
            dispatch_id = extracted.get('metadata', {}).get('dispatch_id', '')
            if dispatch_id:
                dispatch_intelligence = self.extract_intelligence_from_dispatch(dispatch_id)
                if dispatch_intelligence.get('quality_context'):
                    extracted['intelligence'] = dispatch_intelligence

        # Build enhanced receipt structure
        return self._build_enhanced_receipt(extracted, markdown_path)

    def extract_metadata(self, content: str) -> Dict[str, str]:
        """Extract all metadata fields from report header"""
        metadata = {}

        def _normalize_meta_key(key: str) -> str:
            return key.strip().lower().replace("-", "_").replace(" ", "_")

        def _clean_scalar(value: Any, default: str = "unknown") -> str:
            if value is None:
                return default
            text = str(value).strip()
            if not text:
                return default
            first_line = text.splitlines()[0].strip()
            return first_line if first_line else default

        # Extract all metadata fields
        for match in self.metadata_pattern.finditer(content[:2000]):  # Check first 2000 chars
            key = _normalize_meta_key(match.group(1))
            value = match.group(2).strip()
            metadata[key] = value

        # PR #8 Fix: Extract YAML metadata block including dispatch_id
        yaml_match = re.search(r'```yaml\n(.*?)\n```', content[:2000], re.DOTALL)
        if yaml_match:
            yaml_content = yaml_match.group(1)
            # Parse simple YAML key:value pairs (no full YAML parser needed)
            for line in yaml_content.split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = _normalize_meta_key(key)
                    value = value.strip()
                    metadata[key] = value

        # Dispatch assignment table fallback:
        # | **Dispatch-ID** | 20260218-151417-... |
        if not metadata.get("dispatch_id"):
            table_dispatch_match = re.search(
                r'^\|\s*\*\*Dispatch-ID\*\*\s*\|\s*([^|]+?)\s*\|',
                content[:3000],
                re.MULTILINE,
            )
            if table_dispatch_match:
                metadata["dispatch_id"] = table_dispatch_match.group(1).strip()

        # Plain-text fallback for legacy/variant formatting.
        if not metadata.get("dispatch_id"):
            plain_dispatch_match = re.search(
                r'^\s*Dispatch-ID:\s*(\S+)\s*$',
                content[:3000],
                re.MULTILINE | re.IGNORECASE,
            )
            if plain_dispatch_match:
                metadata["dispatch_id"] = plain_dispatch_match.group(1).strip()

        # Normalize unknown-like values so fallback logic can run consistently.
        if str(metadata.get("dispatch_id", "")).strip().lower() in {"", "unknown", "none", "null"}:
            metadata.pop("dispatch_id", None)

        # Phase 1B: Extract session_id (multiple patterns)
        # Pattern 1: **Session**: abc-def-123 (markdown bold) - accepts alphanumeric, hyphens, underscores
        session_match = re.search(r'\*\*Session\*\*:\s*([a-zA-Z0-9\-_]+)', content[:2000], re.IGNORECASE)
        if session_match and 'session_id' not in metadata:
            metadata['session_id'] = session_match.group(1)

        # Pattern 2: Session: abc-def-123 (plain text) - accepts alphanumeric, hyphens, underscores
        if 'session_id' not in metadata:
            session_match = re.search(r'^Session:\s*([a-zA-Z0-9\-_]+)', content[:2000], re.MULTILINE | re.IGNORECASE)
            if session_match:
                metadata['session_id'] = session_match.group(1)

        # Extract title and type from first header
        title_match = re.search(r'^#\s+([^:]+):\s*(.+)$', content, re.MULTILINE)
        if title_match:
            metadata['type'] = title_match.group(1).strip().upper()
            metadata['title'] = title_match.group(2).strip()

        # Extract confidence as float
        if 'confidence' in metadata:
            try:
                metadata['confidence'] = float(metadata['confidence'])
            except ValueError:
                metadata['confidence'] = 0.50  # Default if invalid

        # Track letter → terminal ID mapping
        _track_to_terminal = {'A': 'T1', 'B': 'T2', 'C': 'T3'}

        # Resolve terminal: check all metadata values for T1/T2/T3/T-MANAGER pattern
        if 'terminal' not in metadata:
            for val in metadata.values():
                if isinstance(val, str):
                    m = re.match(r'(T[1-3]|T-MANAGER)\b', val)
                    if m:
                        metadata['terminal'] = m.group(1)
                        break
                    # Also accept "Track A/B/C" as terminal identifier
                    m = re.match(r'Track\s+([ABC])\b', val)
                    if m:
                        metadata['terminal'] = _track_to_terminal[m.group(1)]
                        break

        # Last resort: scan first 500 chars for terminal pattern in any bold field
        if 'terminal' not in metadata:
            header_match = re.search(r'\*\*\w+\*\*:\s*(T[1-3]|T-MANAGER)\b', content[:500])
            if not header_match:
                header_match = re.search(r'\*\*\w+\*\*:\s*Track\s+([ABC])\b', content[:500])
                if header_match:
                    metadata['terminal'] = _track_to_terminal[header_match.group(1)]
            if header_match and 'terminal' not in metadata:
                metadata['terminal'] = header_match.group(1)

        # Normalize terminal value: "T2 (Track B)" → "T2"
        if 'terminal' in metadata:
            m = re.match(r'(T[1-3]|T-MANAGER)\b', metadata['terminal'])
            if m:
                metadata['terminal'] = m.group(1)
            else:
                # Handle "Track B" style values
                m = re.match(r'Track\s+([ABC])\b', metadata['terminal'])
                if m:
                    metadata['terminal'] = _track_to_terminal[m.group(1)]

        # Filename-based terminal detection (fallback for track letter convention)
        if 'terminal' not in metadata:
            filename = ''
            # Will be set by parse_report caller; check for _current_filename attr
            if hasattr(self, '_current_filename') and self._current_filename:
                filename = self._current_filename
            if filename:
                track_match = re.match(r'^\d{8}-\d{6}-([A-C])-', filename)
                if track_match:
                    metadata['terminal'] = _track_to_terminal[track_match.group(1)]

        # Ensure required fields have defaults
        metadata.setdefault('terminal', 'unknown')
        metadata.setdefault('gate', 'unknown')
        metadata.setdefault('status', 'unknown')
        metadata.setdefault('confidence', 0.50)
        metadata.setdefault('task_id', 'unknown')
        metadata.setdefault('dispatch_id', 'unknown')
        metadata['type'] = _clean_scalar(metadata.get('type', 'UNKNOWN'), default='UNKNOWN').upper()
        metadata['title'] = _clean_scalar(metadata.get('title', 'No title'), default='No title')
        metadata['status'] = _clean_scalar(metadata.get('status', 'unknown'), default='unknown')
        metadata['gate'] = _clean_scalar(metadata.get('gate', 'unknown'), default='unknown')
        metadata['task_id'] = _clean_scalar(metadata.get('task_id', 'unknown'), default='unknown')
        metadata['dispatch_id'] = _clean_scalar(metadata.get('dispatch_id', 'unknown'), default='unknown')

        # Map Terminal to track for receipt compatibility
        if 'terminal' in metadata:
            terminal = metadata['terminal']
            track_map = {'T1': 'A', 'T2': 'B', 'T3': 'C'}
            metadata['track'] = track_map.get(terminal, terminal)

        return metadata

    def extract_tags(self, content: str) -> Dict[str, List[str]]:
        """Extract categorized tags from report"""
        tags = {
            'issues': [],
            'components': [],
            'solutions': []
        }

        for category, pattern in self.tag_patterns.items():
            match = pattern.search(content)
            if match:
                # Split tags and clean them
                raw_tags = match.group(1).split()
                tags[category] = [tag.strip() for tag in raw_tags if tag.startswith('#')]

        return tags

    def extract_root_cause(self, content: str) -> Dict[str, Any]:
        """Extract root cause analysis section"""
        root_cause = {
            'identified': None,
            'confidence': 0.0,
            'evidence': []
        }

        # Find root cause section
        root_cause_section = self._extract_section(content, 'Root Cause')
        if not root_cause_section:
            return root_cause

        # Extract main cause (usually first paragraph after header)
        lines = root_cause_section.strip().split('\n')
        for i, line in enumerate(lines):
            if line and not line.startswith('#') and not line.startswith('*'):
                root_cause['identified'] = line.strip()
                break

        # Look for evidence bullets
        evidence_pattern = re.compile(r'^[-*]\s+(.+)$', re.MULTILINE)
        evidence_matches = evidence_pattern.findall(root_cause_section)
        root_cause['evidence'] = evidence_matches[:5]  # Limit to 5 pieces of evidence

        # Estimate confidence based on evidence count
        if root_cause['identified']:
            root_cause['confidence'] = min(0.95, 0.70 + (len(root_cause['evidence']) * 0.05))

        return root_cause

    def extract_dependencies(self, content: str) -> Dict[str, Any]:
        """Extract cross-track dependencies and impacts"""
        dependencies = {
            'tracks': [],
            'components': [],
            'blocking': False,
            'risk_level': 'unknown'
        }

        # Find dependencies section
        dep_section = self._extract_section(content, 'Dependenc')
        if not dep_section:
            # Also check for "Cross-Track Impact" or similar
            dep_section = self._extract_section(content, 'Impact')

        if dep_section:
            # Extract track mentions
            track_pattern = re.compile(r'Track\s+([ABC])', re.IGNORECASE)
            tracks = track_pattern.findall(dep_section)
            dependencies['tracks'] = list(set(tracks))

            # Extract file/component mentions
            file_pattern = re.compile(r'([a-z_]+(?:/[a-z_]+)*\.py)', re.IGNORECASE)
            components = file_pattern.findall(dep_section)
            dependencies['components'] = list(set(components))[:5]  # Limit to 5

            # Check for blocking keywords
            if re.search(r'\b(block|blocking|blocked|critical)\b', dep_section, re.IGNORECASE):
                dependencies['blocking'] = True

            # Determine risk level
            if re.search(r'\b(high risk|critical|severe)\b', dep_section, re.IGNORECASE):
                dependencies['risk_level'] = 'high'
            elif re.search(r'\b(medium risk|moderate)\b', dep_section, re.IGNORECASE):
                dependencies['risk_level'] = 'medium'
            elif re.search(r'\b(low risk|minimal|safe)\b', dep_section, re.IGNORECASE):
                dependencies['risk_level'] = 'low'

        return dependencies

    def extract_recommendations(self, content: str) -> Dict[str, List[str]]:
        """Extract recommendations and next steps"""
        recommendations = {
            'immediate': [],
            'next_phase': [],
            'warnings': []
        }

        # Find recommendations/next steps section
        rec_section = self._extract_section(content, 'Recommendation')
        if not rec_section:
            rec_section = self._extract_section(content, 'Next Step')

        if rec_section:
            # Extract bullet points
            bullet_pattern = re.compile(r'^[-*]\s+(.+)$', re.MULTILINE)
            bullets = bullet_pattern.findall(rec_section)

            for bullet in bullets:
                # Categorize based on keywords
                if re.search(r'\b(immediate|urgent|now|asap)\b', bullet, re.IGNORECASE):
                    recommendations['immediate'].append(bullet[:200])
                elif re.search(r'\b(warning|caution|risk|careful)\b', bullet, re.IGNORECASE):
                    recommendations['warnings'].append(bullet[:200])
                else:
                    recommendations['next_phase'].append(bullet[:200])

            # Limit lists
            recommendations['immediate'] = recommendations['immediate'][:3]
            recommendations['next_phase'] = recommendations['next_phase'][:3]
            recommendations['warnings'] = recommendations['warnings'][:3]

        return recommendations

    def extract_metrics(self, content: str) -> Dict[str, Any]:
        """Extract categorized metrics from report"""
        metrics = {
            'performance': {},
            'quality': {},
            'business': {}
        }

        # Find metrics section
        metrics_section = self._extract_section(content, 'Metrics')
        if not metrics_section:
            # Try alternative sections
            metrics_section = self._extract_section(content, 'Results')

        if metrics_section:
            # Extract Performance metrics
            perf_patterns = {
                'processing_time_ms': re.compile(r'Processing time:\s*(\d+(?:\.\d+)?)\s*ms', re.IGNORECASE),
                'memory_mb': re.compile(r'Memory usage:\s*(\d+(?:\.\d+)?)\s*MB', re.IGNORECASE),
                'cpu_percent': re.compile(r'CPU usage:\s*(\d+(?:\.\d+)?)\s*%', re.IGNORECASE),
                'response_time_ms': re.compile(r'Response time:\s*(\d+(?:\.\d+)?)\s*ms', re.IGNORECASE)
            }

            for key, pattern in perf_patterns.items():
                match = pattern.search(metrics_section)
                if match:
                    try:
                        metrics['performance'][key] = float(match.group(1))
                    except ValueError:
                        pass

            # Extract Quality metrics
            quality_patterns = {
                'tests_passed': re.compile(r'Tests passed:\s*(\d+)', re.IGNORECASE),
                'tests_failed': re.compile(r'Tests failed:\s*(\d+)', re.IGNORECASE),
                'coverage_percent': re.compile(r'Coverage:\s*(\d+(?:\.\d+)?)\s*%', re.IGNORECASE),
                'lines_changed': re.compile(r'Lines changed:\s*(\d+)', re.IGNORECASE)
            }

            for key, pattern in quality_patterns.items():
                match = pattern.search(metrics_section)
                if match:
                    try:
                        if 'percent' in key:
                            metrics['quality'][key] = float(match.group(1))
                        else:
                            metrics['quality'][key] = int(match.group(1))
                    except ValueError:
                        pass

            # Extract Business metrics
            business_patterns = {
                'records_processed': re.compile(r'Records processed:\s*(\d+)', re.IGNORECASE),
                'success_rate_percent': re.compile(r'Success rate:\s*(\d+(?:\.\d+)?)\s*%', re.IGNORECASE),
                'errors_encountered': re.compile(r'Errors encountered:\s*(\d+)', re.IGNORECASE)
            }

            for key, pattern in business_patterns.items():
                match = pattern.search(metrics_section)
                if match:
                    try:
                        if 'percent' in key:
                            metrics['business'][key] = float(match.group(1))
                        else:
                            metrics['business'][key] = int(match.group(1))
                    except ValueError:
                        pass

        # Also search for metrics in entire content if section not found
        if not any(metrics.values()):
            # Fallback to searching entire content with simpler patterns
            simple_patterns = {
                'memory_mb': re.compile(r'(\d+(?:\.\d+)?)\s*MB', re.IGNORECASE),
                'tests': re.compile(r'(\d+)\s*tests?\s*pass', re.IGNORECASE),
                'performance_ms': re.compile(r'(\d+(?:\.\d+)?)\s*ms\b')
            }

            for key, pattern in simple_patterns.items():
                match = pattern.search(content)
                if match:
                    try:
                        value = float(match.group(1))
                        if 'memory' in key:
                            metrics['performance']['memory_mb'] = value
                        elif 'test' in key:
                            metrics['quality']['tests_passed'] = int(value)
                        elif 'performance' in key:
                            metrics['performance']['processing_time_ms'] = value
                    except ValueError:
                        pass

        return metrics

    def extract_validation(self, content: str) -> Dict[str, Any]:
        """Extract validation and testing results"""
        validation = {
            'tests_passed': 0,
            'tests_failed': 0,
            'quality_gates': [],
            'gates_passed': 0,
            'gates_failed': 0
        }

        # Find validation/testing section
        val_section = self._extract_section(content, 'Validation')
        if not val_section:
            val_section = self._extract_section(content, 'Test')

        if val_section:
            # Extract test counts
            passed_match = re.search(r'(\d+)\s*(?:tests?\s*)?pass', val_section, re.IGNORECASE)
            failed_match = re.search(r'(\d+)\s*(?:tests?\s*)?fail', val_section, re.IGNORECASE)

            if passed_match:
                validation['tests_passed'] = int(passed_match.group(1))
            if failed_match:
                validation['tests_failed'] = int(failed_match.group(1))

            # Look for quality gates
            gates = ['syntax', 'types', 'lint', 'security', 'performance', 'coverage']
            for gate in gates:
                if re.search(rf'\b{gate}\b', val_section, re.IGNORECASE):
                    validation['quality_gates'].append(gate)
                    # Check if passed or failed
                    if re.search(rf'{gate}.*?(pass|✅|success)', val_section, re.IGNORECASE):
                        validation['gates_passed'] += 1
                    elif re.search(rf'{gate}.*?(fail|❌|error)', val_section, re.IGNORECASE):
                        validation['gates_failed'] += 1

        return validation

    def _extract_section(self, content: str, section_name: str) -> Optional[str]:
        """Extract a specific section from markdown content"""
        # Find section header
        pattern = re.compile(rf'^#{1,3}\s*{section_name}.*?$', re.MULTILINE | re.IGNORECASE)
        match = pattern.search(content)

        if not match:
            return None

        start = match.end()

        # Find next section header of same or higher level
        level = len(re.match(r'^(#{1,3})', match.group()).group(1))
        next_section = re.compile(rf'^#{{{1},{level}}}\s+', re.MULTILINE)
        next_match = next_section.search(content, start)

        if next_match:
            end = next_match.start()
        else:
            end = len(content)

        return content[start:end].strip()

    def extract_intelligence(self, content: str) -> Dict[str, Any]:
        """
        Extract intelligence data added by dispatcher (PR #8)

        Returns:
            Dictionary with pattern_count, prevention_rules, and quality_context
        """
        intelligence = {
            'pattern_count': 0,
            'prevention_rules': 0,
            'quality_context': {}
        }

        # Look for INTELLIGENCE_DATA section
        intel_match = self.intelligence_pattern.search(content)
        if intel_match:
            try:
                intelligence['pattern_count'] = int(intel_match.group(1))
                intelligence['prevention_rules'] = int(intel_match.group(2))
                # Parse quality_context JSON
                quality_context_str = intel_match.group(3).strip()
                if quality_context_str and quality_context_str != '{}':
                    intelligence['quality_context'] = json.loads(quality_context_str)
            except (ValueError, json.JSONDecodeError) as e:
                # Log but continue - don't fail on intelligence extraction
                pass

        return intelligence

    def extract_used_pattern_hashes(self, content: str) -> List[str]:
        """Extract pattern hashes used by the agent (optional, for usage tracking)"""
        hashes: List[str] = []

        # YAML or JSON-like list (used_pattern_hashes: [abc, def])
        list_match = re.search(r'used_pattern_hashes\s*:\s*\[(.*?)\]', content, re.IGNORECASE | re.DOTALL)
        if list_match:
            hashes.extend(re.findall(r'[a-f0-9]{8,40}', list_match.group(1), re.IGNORECASE))

        # Plaintext line (Patterns Used: <hash1>, <hash2>)
        for line in content.splitlines():
            if 'patterns used' in line.lower() or 'pattern_hash' in line.lower():
                hashes.extend(re.findall(r'[a-f0-9]{8,40}', line, re.IGNORECASE))

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for h in hashes:
            key = h.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(key)

        return unique

    def extract_intelligence_from_dispatch(self, dispatch_id: str) -> Dict[str, Any]:
        """
        Extract intelligence from original dispatch file using dispatch_id
        (PR #8 Fix - cross-reference dispatches when reports lack intelligence)
        """
        intelligence = {
            'pattern_count': 0,
            'prevention_rules': 0,
            'quality_context': {}
        }

        if not dispatch_id:
            return intelligence

        try:
            # Search for dispatch by ID in completed directory
            dispatch_dir = self.dispatch_completed_dir

            # Look for dispatches with matching dispatch_id in metadata
            for dispatch_file in dispatch_dir.glob("*.md"):
                with open(dispatch_file, 'r') as f:
                    content = f.read()

                # Extract dispatch metadata to find exact match
                # Look for: dispatch_id: <id> or **ID**: <id> or **dispatch_id**: <id> (with optional markdown)
                import re
                id_pattern = re.compile(r'\*{0,2}(?:dispatch_id|Dispatch ID|ID)\*{0,2}:\s*(\S+)', re.IGNORECASE)
                id_match = id_pattern.search(content)

                if id_match and id_match.group(1) == dispatch_id:
                    # Found exact dispatch, extract INTELLIGENCE_DATA
                    intel_match = self.intelligence_pattern.search(content)
                    if intel_match:
                        intelligence['pattern_count'] = int(intel_match.group(1))
                        intelligence['prevention_rules'] = int(intel_match.group(2))
                        quality_context_str = intel_match.group(3).strip()
                        if quality_context_str and quality_context_str != '{}':
                            intelligence['quality_context'] = json.loads(quality_context_str)
                    break

        except Exception as e:
            # Log but don't fail on cross-reference failure
            pass

        return intelligence

    def _build_enhanced_receipt(self, extracted: Dict, report_path: str) -> Dict[str, Any]:
        """Build enhanced receipt structure from extracted data"""
        metadata = extracted.get('metadata', {})

        # Start with comprehensive structure including all new fields
        receipt = {
            'event_type': 'task_complete',  # Primary field for structured processing
            'event': 'task_complete',  # Legacy compatibility field
            'timestamp': datetime.utcnow().isoformat(),  # FIX: Use UTC, not local time
            'terminal': metadata.get('terminal', 'unknown'),
            'track': metadata.get('track'),  # Include track field for terminal routing
            'type': metadata.get('type', 'UNKNOWN'),
            'gate': metadata.get('gate', 'unknown'),
            'status': metadata.get('status', 'unknown'),
            'confidence': metadata.get('confidence', 0.50),
            'task_id': metadata.get('task_id', 'unknown'),
            'dispatch_id': metadata.get('dispatch_id', 'unknown'),
            'session_id': metadata.get('session_id'),  # Phase 2: Session tracking for cost attribution
            'report_path': report_path,
            'report_file': Path(report_path).name,  # Add filename for easier tracking
            'title': metadata.get('title', 'No title')
        }

        # Add extracted intelligence
        if extracted['tags'] and any(extracted['tags'].values()):
            receipt['tags'] = extracted['tags']

        if extracted['root_cause']['identified']:
            receipt['root_cause'] = extracted['root_cause']

        if extracted['dependencies']:
            receipt['dependencies'] = extracted['dependencies']

        if any(extracted['recommendations'].values()):
            receipt['recommendations'] = extracted['recommendations']

        # Add structured metrics
        if any(extracted['metrics'].values()):
            receipt['metrics'] = extracted['metrics']

        if extracted['validation'] and (extracted['validation']['tests_passed'] or
                                       extracted['validation']['tests_failed'] or
                                       extracted['validation']['quality_gates']):
            receipt['validation'] = extracted['validation']

        # INTELLIGENCE INTEGRATION (PR #8): Add quality_context to receipt
        if extracted.get('intelligence'):
            intelligence_data = extracted['intelligence']
            if intelligence_data.get('pattern_count', 0) > 0:
                receipt['pattern_count'] = intelligence_data['pattern_count']
            if intelligence_data.get('prevention_rules', 0) > 0:
                receipt['prevention_rules'] = intelligence_data['prevention_rules']
            if intelligence_data.get('quality_context') and intelligence_data['quality_context'] != {}:
                receipt['quality_context'] = intelligence_data['quality_context']

        if extracted.get('used_pattern_hashes'):
            receipt['used_pattern_hashes'] = extracted['used_pattern_hashes']

        # Use provided confidence or calculate based on completeness
        if metadata.get('confidence', 0) > 0:
            receipt['confidence'] = metadata['confidence']
        else:
            # Calculate confidence based on data completeness
            field_count = sum([
                bool(receipt.get('tags')),
                bool(receipt.get('root_cause')),
                bool(receipt.get('dependencies')),
                bool(receipt.get('recommendations')),
                bool(receipt.get('metrics')),
                bool(receipt.get('validation'))
            ])
            receipt['confidence'] = min(0.98, 0.50 + (field_count * 0.08))

        # Mark if this is legacy format (missing required fields)
        required_fields = ['task_id', 'dispatch_id', 'confidence']
        missing_fields = [f for f in required_fields if receipt.get(f) in ['unknown', None, 0]]
        if missing_fields:
            receipt['legacy_format'] = True
            receipt['missing_fields'] = missing_fields

        return receipt


def main():
    """Test the parser with a sample report or process command-line argument"""
    parser = ReportParser()

    # Check if report path provided as command-line argument
    if len(sys.argv) > 1:
        test_report = sys.argv[1]
    else:
        # Fallback to test report for manual testing
        test_report = str(Path(os.environ.get("VNX_REPORTS_DIR", parser.dispatch_completed_dir.parent.parent / "unified_reports")) / '20250925-175000-T3-IMPL-phase2-storage-integration.md')

    if Path(test_report).exists():
        result = parser.parse_report(test_report)

        # Output clean JSON only (no headers for automated processing)
        print(json.dumps(result))

        # Size info to stderr for debugging (won't interfere with JSON output)
        receipt_json = json.dumps(result)
        sys.stderr.write(f"\nReceipt size: {len(receipt_json)} bytes\n")
        sys.stderr.write(f"Target: <2000 bytes (2KB)\n")

        if len(receipt_json) > 2000:
            sys.stderr.write("⚠️ Receipt exceeds target size - consider trimming fields\n")
    else:
        # Error messages to stderr so they don't interfere with JSON output
        sys.stderr.write(f"Report not found: {test_report}\n")
        sys.stderr.write("\nTrying to find any recent report...\n")

        # Try to find any recent report
        reports_dir = Path(os.environ.get("VNX_REPORTS_DIR", parser.dispatch_completed_dir.parent.parent / "unified_reports"))
        if reports_dir.exists():
            reports = sorted(reports_dir.glob('*.md'), reverse=True)
            if reports:
                sys.stderr.write(f"Testing with: {reports[0]}\n")
                result = parser.parse_report(str(reports[0]))
                print(json.dumps(result))  # Clean JSON to stdout


if __name__ == '__main__':
    main()
