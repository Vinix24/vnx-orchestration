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


# OI-1101: model-name shape validation. Real model names (sonnet, opus,
# kimi-k3, deepseek-v4-pro, claude-haiku-4-5-20251001) share three properties:
# no spaces, no backticks, bounded length. The longest known model name across
# the wave7 registry is claude-haiku-4-5-20251001 (26 chars); 64 gives generous
# headroom while rejecting prose-sized strings (70+ chars observed in the wild).
_MODEL_NAME_MAX_LENGTH = 64


def _is_inside_inline_code(text: str, pos: int) -> bool:
    """Return True when character position *pos* in *text* is inside a
    backtick-delimited inline code span.

    Counts single backticks before *pos*, excluding triple-backtick code blocks.
    An odd count means we are inside an inline code span.
    """
    before = text[:pos]
    # Remove triple-backtick fenced code blocks so they don't contribute
    # to the inline-backtick count.
    without_blocks = re.sub(r'```.*?```', '', before, flags=re.DOTALL)
    return without_blocks.count('`') % 2 == 1


def _is_valid_model_name(value: str) -> bool:
    """Return True when *value* looks like a real model identifier.

    A valid model name contains no spaces, no backticks, is non-empty, and
    does not exceed ``_MODEL_NAME_MAX_LENGTH`` characters.
    """
    v = value.strip()
    if not v:
        return False
    if ' ' in v:
        return False
    if '`' in v:
        return False
    if len(v) > _MODEL_NAME_MAX_LENGTH:
        return False
    return True


def _resolve_work_timestamp(metadata: Dict[str, str], report_path: str) -> Optional[str]:
    """Resolve the time the work was done, not the time the report was processed.

    OI-1092: a receipt processed today for work done last week must carry last
    week's date. A processed-now timestamp silently corrupts every time series
    that groups by date. Resolution order:

      1. Explicit date field in the report (``datum``, ``date``, ``timestamp``,
         ``recorded_at``) — already captured by ``extract_metadata``.
      2. File mtime of the report on disk.
      3. Fail-closed: return None when neither is determinable, so the caller
         can refuse to emit a receipt rather than write one with a false date.

    Returns an ISO-8601 timestamp string (UTC) or None.
    """
    # Tier 1: explicit date field from the report metadata.
    for key in ('datum', 'date', 'timestamp', 'recorded_at', 'created_at'):
        raw = str(metadata.get(key) or '').strip()
        if not raw or raw.lower() in ('unknown', 'none', 'null', 'n/a', ''):
            continue
        try:
            # Accept common ISO-8601 variants; normalise to full UTC form.
            from dateutil.parser import parse as _parse_dt
        except ImportError:
            _parse_dt = None
        if _parse_dt is not None:
            try:
                dt = _parse_dt(raw)
                if dt.tzinfo is None:
                    from datetime import timezone
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:  # vnx-silent-except: fallback tier — a dateutil parse miss drops to the fromisoformat tier, then the file-mtime tier, and finally the explicit fail-closed return
                pass
        # Fallback: try fromisoformat for strict ISO-8601.
        try:
            from datetime import datetime as _datetime, timezone as _timezone
            dt = _datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_timezone.utc)
            return dt.isoformat()
        except Exception:  # vnx-silent-except: fallback tier — a fromisoformat parse miss drops to the file-mtime tier and, if that fails too, the explicit fail-closed return
            pass

    # Tier 2: file modification time.
    try:
        from datetime import datetime as _datetime, timezone as _timezone
        mtime = os.path.getmtime(report_path)
        dt = _datetime.fromtimestamp(mtime, tz=_timezone.utc)
        return dt.isoformat()
    except OSError:
        pass

    # Tier 3: fail-closed — no receipt with a false date.
    return None


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

        return self._parse_content(content, markdown_path)

    def parse_by_dispatch_id(
        self, dispatch_id: str, data_dir: "Optional[Path]" = None
    ) -> Dict[str, Any]:
        """Resolve and parse a report from a dispatch_id.

        Uses the shared report_path resolver (``resolve_report_path``) so the
        correct report is found even when multiple filename forms exist on disk.

        Args:
            dispatch_id: The dispatch identifier string.
            data_dir:    Override ``VNX_DATA_DIR`` (resolved from env when *None*).

        Returns:
            Dictionary with extracted fields (same shape as ``parse_report``),
            plus ``_ambiguous_report`` and ``_report_path_candidates`` when
            the resolver found multiple candidates.
        """
        try:
            from report_path import resolve_report_path
        except ImportError:
            return {'error': 'report_path resolver not available'}

        resolved = resolve_report_path(dispatch_id, data_dir=data_dir)
        if resolved is None:
            return {'error': f'No report found for dispatch: {dispatch_id}'}

        result = self.parse_report(str(resolved.path))
        if resolved.ambiguous:
            result['_ambiguous_report'] = True
            result['_report_path_candidates'] = [
                {"path": str(c), "size": resolved.candidate_sizes[str(c)]}
                for c in resolved.candidates_found
            ]
        return result

    def _parse_content(self, content: str, markdown_path: str) -> Dict[str, Any]:
        """Parse report content (already read from disk) into enhanced receipt format."""

        # Extract all components. ADR-035 §3.3/§9 PR-5: tags/root_cause/
        # dependencies/metrics/used_pattern_hashes are dead weight (zero
        # receipt-field readers, verified) and no longer extracted;
        # recommendations/intelligence (pattern_count/quality_context) are
        # live readers (§3.2) and stay.
        extracted = {
            'metadata': self.extract_metadata(content),
            'recommendations': self.extract_recommendations(content),
            'validation': self.extract_validation(content),
            'intelligence': self.extract_intelligence(content),  # PR #8: Extract quality_context
        }

        # Governance (audit #10): validate the worker report body. A success-claim with an invalid
        # body must NOT enter the audit trail as a clean task_complete. Fail-soft: any import/validate
        # error leaves the receipt's behaviour unchanged (treated as valid).
        extracted['_body_contract_valid'] = True
        try:
            _lib = str(Path(__file__).resolve().parent / "lib")
            if _lib not in sys.path:
                sys.path.insert(0, _lib)
            from report_body_contract import validate_body as _validate_body
            extracted['_body_contract_valid'] = bool(_validate_body(content).valid)
        except Exception:
            extracted['_body_contract_valid'] = True

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

        # OI-1086: parse `---` YAML frontmatter FIRST (dispatch-envelope
        # reports stamp provider/model/role there — e.g. the headless
        # plan-gate seats carry `model: kimi-k3` in frontmatter and no
        # `**Model**:` body field at all). Parsed before the bold-field scan
        # so an explicit body field always wins over the frontmatter —
        # measured on the 2026-08-03/04 envelope series, which carries
        # frontmatter `model: unknown` alongside a real `**Model**: sonnet`
        # in the body. Unknown-like frontmatter scalars are skipped for the
        # same reason: a placeholder must never shadow a real body value.
        # Only top-level scalars are read; nested blocks (route_decision,
        # token_usage) are not receipt metadata.
        _UNKNOWN_LIKE = {'unknown', 'none', 'null', 'n/a', 'na', 'unset', '-'}
        fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if fm_match:
            for line in fm_match.group(1).split('\n'):
                if ':' not in line or line[0] in (' ', '\t', '#'):
                    continue
                key, value = line.split(':', 1)
                key = _normalize_meta_key(key)
                value = value.strip()
                if not key or not value:
                    continue
                if value.lower() in _UNKNOWN_LIKE:
                    continue
                metadata[key] = value

        # Extract all metadata fields. OI-1101: skip matches inside inline
        # code spans (backtick-delimited) — a `**Model**: value` mention
        # inside prose or code is not a real identity declaration.
        # Also apply shape validation for model values extracted here.
        for match in self.metadata_pattern.finditer(content[:2000]):  # Check first 2000 chars
            if _is_inside_inline_code(content, match.start()):
                continue
            key = _normalize_meta_key(match.group(1))
            value = match.group(2).strip()
            if key == 'model' and not _is_valid_model_name(value):
                continue
            metadata[key] = value

        # OI-1086 fallback: reports that embed their full dispatch
        # instruction (the "# Dispatch <id> / ## Instruction" convention)
        # carry the door-stamped **Model**/**Provider** block MID-FILE,
        # outside the 2000-char header window above — measured on the
        # 2026-08-04 m-series (e.g. 20260804-m00-oi917.md, block at line
        # 196/282). First match wins and only fills keys the header scan did
        # not already provide, so a prose mention later in the body can
        # never override a real header value, and a header value always
        # beats an embedded-instruction copy.
        #
        # OI-1101: provider-lane reports echo the full dispatch instruction
        # in their body. A prose sentence that mentions the model field by
        # name (e.g. "the **Model**: field names the provider") is matched
        # by the regex and produces a 70-character prose string as the model
        # value. Two guards: (a) skip matches inside inline code spans
        # (backtick-delimited), (b) reject values that don't look like real
        # model names (spaces, backticks, or longer than 64 characters).
        for _key, _pat in (
            ('model', r'\*\*Model\*\*:\s*(.+)'),
            ('provider', r'\*\*Provider\*\*:\s*(.+)'),
        ):
            if not str(metadata.get(_key) or '').strip():
                for m in re.finditer(_pat, content):
                    _value = m.group(1).strip()
                    if _is_inside_inline_code(content, m.start()):
                        continue
                    if _key == 'model' and not _is_valid_model_name(_value):
                        continue
                    metadata[_key] = _value
                    break

        # Plain-text header fallback (same convention as the Dispatch-ID
        # plain-text fallback below): reports like the 2026-08-04 triage/m-
        # series stamp a bare `Model: X` / `Provider: Y` block at the top.
        # Case-sensitive on purpose: lowercase `model:` is the frontmatter
        # form handled above, and anchoring to the header window keeps prose
        # mentions out. Only fills keys still absent.
        for _key, _pat in (
            ('model', r'^\s*Model:\s*(\S+)\s*$'),
            ('provider', r'^\s*Provider:\s*(\S+)\s*$'),
        ):
            if not str(metadata.get(_key) or '').strip():
                m = re.search(_pat, content[:3000], re.MULTILINE)
                if m and m.group(1).strip().lower() not in _UNKNOWN_LIKE:
                    metadata[_key] = m.group(1).strip()

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

        # Filename-based dispatch_id fallback: when content parsing yielded nothing,
        # derive dispatch_id from the report filename by stripping known suffixes.
        # Example: '20260527-132804-pip-repackage-namespace_report.md'
        #       -> '20260527-132804-pip-repackage-namespace'
        # Only applied when dispatch_id is still absent after content extraction.
        if not metadata.get("dispatch_id") and hasattr(self, '_current_filename') and self._current_filename:
            _fn = self._current_filename
            for _suffix in ('_report.md', '_report.txt', '_report', '.md', '.txt'):
                if _fn.endswith(_suffix):
                    _fn = _fn[:-len(_suffix)]
                    break
            if _fn and _fn.lower() not in {'unknown', 'none', 'null', ''}:
                metadata['dispatch_id'] = _fn

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
        # Find section header. Note: {{1,3}} (double-braced) is required — a
        # bare {1,3} inside an f-string is parsed as the tuple literal (1, 3),
        # not the regex quantifier, and silently never matches any heading.
        pattern = re.compile(rf'^#{{1,3}}\s*{section_name}.*?$', re.MULTILINE | re.IGNORECASE)
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

        # Governance (audit #10): a receipt that CLAIMS success must satisfy the report body contract.
        # A success-claim with an invalid body is recorded as report_contract_invalid (status
        # contract_invalid) so it cannot enter the audit trail as a verified task_complete. Reports
        # that do not claim success (gate verdicts, partial, unknown) keep their event_type.
        _contract_valid = extracted.get('_body_contract_valid', True)
        _status = str(metadata.get('status', 'unknown')).lower()
        _claims_success = _status in ('success', 'done', 'complete', 'completed', 'pass', 'passed')
        if _claims_success and not _contract_valid:
            _event_type = 'report_contract_invalid'
            _receipt_status = 'contract_invalid'
        else:
            _event_type = 'task_complete'
            _receipt_status = metadata.get('status', 'unknown')

        # OI-1092: the receipt timestamp must be the time the WORK was done,
        # not the time the report was PROCESSED. A backlog of 103 previously
        # refused reports processed today would otherwise write 103 receipts
        # with today's date for work done 3–7 August — worse than the gap
        # itself, because a missing receipt is visibly absent while a receipt
        # with the wrong date silently corrupts every time series.
        #
        # Resolution order:
        #   1. Explicit date field in the report (datum, date, timestamp, recorded_at)
        #   2. File mtime of the report on disk
        #   3. Fail-closed: neither available → no receipt emitted
        _work_ts = _resolve_work_timestamp(metadata, report_path)
        if _work_ts is None:
            return {'error': f'Cannot resolve work timestamp for: {report_path}'}

        # Start with comprehensive structure including all new fields
        receipt = {
            'event_type': _event_type,  # Primary field for structured processing
            'event': _event_type,  # Legacy compatibility field
            # Receipt-quality PR-3: report-derived receipts are dispatch-lane
            # outcomes (closed-set kind; role propagates via PR-1/2 lanes).
            'receipt_kind': 'dispatch',
            'timestamp': _work_ts,
            'terminal': metadata.get('terminal', 'unknown'),
            'track': metadata.get('track'),  # Include track field for terminal routing
            'type': metadata.get('type', 'UNKNOWN'),
            'gate': metadata.get('gate', 'unknown'),
            'status': _receipt_status,
            'contract_valid': _contract_valid,  # governance visibility (audit #10)
            'task_id': metadata.get('task_id', 'unknown'),
            'dispatch_id': metadata.get('dispatch_id', 'unknown'),
            'session_id': metadata.get('session_id'),  # Phase 2: Session tracking for cost attribution
            'report_path': report_path,
            'report_file': Path(report_path).name,  # Add filename for easier tracking
            'title': metadata.get('title', 'No title')
        }

        # OI-1086: lift model/provider from extracted metadata onto the receipt.
        # extract_metadata() already captures the `**Model**:`/`**Provider**:`
        # bold fields, but the hand-picked key list above used to drop them —
        # which made every report-derived receipt (receipt_kind='dispatch',
        # no exempt source) fail append_receipt's fail-closed missing_model
        # check (#1338) and put the receipt processor into a permanent
        # reject-retry loop.
        #
        # Missing-field contract: when the report carries no model/provider,
        # the keys are OMITTED from the receipt — never written as '' or
        # 'unknown'. For the fail-closed validator an absent key, an empty
        # string and 'unknown' are equivalent (all refused); omitting is the
        # honest form because it does not imply a known-but-empty value, and
        # it keeps the refusal reason ("carries no real model") accurate.
        _model = str(metadata.get('model') or '').strip()
        if _model:
            receipt['model'] = _model
        _provider = str(metadata.get('provider') or '').strip()
        if _provider:
            receipt['provider'] = _provider

        if any(extracted['recommendations'].values()):
            receipt['recommendations'] = extracted['recommendations']

        # ADR-035 §3.1/§9 PR-5: promote the raw extract_validation() output to
        # the canonical verification{} shape — the same shape
        # dispatch_envelope.py::_verification_from_report builds for Path 1's
        # envelope sub-path, so compute_verdict reads one consistent
        # verification.method vocabulary regardless of which write path
        # produced the receipt. Always present (never omitted): an absent
        # verification{} reads to compute_verdict as evidence_complete=True
        # (method=None isn't in INCOMPLETE_EVIDENCE_METHODS), which is wrong —
        # "unknown" is the honest default when no evidence was found (§3.1).
        raw_validation = extracted.get('validation') or {}
        tests_passed = int(raw_validation.get('tests_passed') or 0)
        tests_failed = int(raw_validation.get('tests_failed') or 0)
        tests_run = tests_passed + tests_failed
        if tests_run > 0:
            verification_method = 'pytest'
        elif raw_validation.get('quality_gates'):
            verification_method = 'manual'
        else:
            verification_method = 'unknown'
        receipt['verification'] = {
            'method': verification_method,
            'tests_run': tests_run if tests_run > 0 else None,
            'tests_passed': tests_passed if tests_run > 0 else None,
            'tests_failed': tests_failed if tests_run > 0 else None,
            'command': None,
            'pr_ref': None,
            'push_verified': None,
            'spec_deviation': None,
        }

        # INTELLIGENCE INTEGRATION (PR #8): Add quality_context to receipt
        # (pattern_count/quality_context are live readers, §3.2; prevention_rules
        # is dead weight, §3.3, and is no longer promoted onto the receipt).
        if extracted.get('intelligence'):
            intelligence_data = extracted['intelligence']
            if intelligence_data.get('pattern_count', 0) > 0:
                receipt['pattern_count'] = intelligence_data['pattern_count']
            if intelligence_data.get('quality_context') and intelligence_data['quality_context'] != {}:
                receipt['quality_context'] = intelligence_data['quality_context']

        # Mark if this is legacy format (missing required fields). `confidence`
        # dropped from this check (§3.3): the field itself no longer exists on
        # the receipt, so checking for it would spuriously flag every receipt.
        required_fields = ['task_id', 'dispatch_id']
        missing_fields = [f for f in required_fields if receipt.get(f) in ['unknown', None, 0]]
        if missing_fields:
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
