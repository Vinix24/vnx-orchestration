#!/usr/bin/env python3
"""
Code Snippet Extractor for VNX Intelligence System
Version: 8.0.2 (Phase 2)
Purpose: Extract high-quality code patterns and populate FTS5 search database
"""

import sqlite3
import ast
import re
import sys
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import json

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

# VNX Base Configuration
PATHS = ensure_env()
VNX_BASE = Path(PATHS["VNX_HOME"])
PROJECT_ROOT = Path(PATHS["PROJECT_ROOT"])
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"

# Snippet quality thresholds
MIN_QUALITY_SCORE = 70  # Only extract from high-quality files
MIN_FUNCTION_LENGTH = 5  # Minimum lines for a snippet
MAX_FUNCTION_LENGTH = 50  # Maximum lines for a snippet
MIN_SNIPPET_SCORE = 60  # Minimum score to store snippet

# Pattern categories
PATTERN_CATEGORIES = {
    'error_handling': ['try', 'except', 'raise', 'error', 'exception'],
    'validation': ['validate', 'check', 'verify', 'assert'],
    'caching': ['cache', 'memoize', 'cached', 'lru_cache'],
    'retry': ['retry', 'attempt', 'backoff'],
    'logging': ['log', 'logger', 'debug', 'info', 'warning', 'error'],
    'database': ['query', 'execute', 'fetch', 'commit', 'rollback'],
    'async': ['async', 'await', 'asyncio'],
    'testing': ['test', 'mock', 'assert', 'fixture'],
    'parsing': ['parse', 'extract', 'transform'],
    'optimization': ['optimize', 'cache', 'efficient', 'performance']
}

# Color codes
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    RESET = '\033[0m'


def log(level: str, message: str):
    """Log message with timestamp and color coding"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    color_map = {
        'INFO': Colors.BLUE,
        'SUCCESS': Colors.GREEN,
        'WARNING': Colors.YELLOW,
        'ERROR': Colors.RED,
        'EXTRACT': Colors.CYAN
    }

    color = color_map.get(level, Colors.RESET)
    print(f"[{timestamp}] {color}[{level}]{Colors.RESET} {message}")


class SnippetAnalyzer:
    """Analyze code snippets for quality and categorization"""

    @staticmethod
    def calculate_snippet_quality(func_node: ast.FunctionDef, source_lines: List[str]) -> float:
        """Calculate quality score for a code snippet (0-100)"""
        score = 50.0  # Base score

        # Positive indicators
        has_docstring = ast.get_docstring(func_node) is not None
        if has_docstring:
            docstring = ast.get_docstring(func_node)
            if len(docstring) > 20:
                score += 15  # Good documentation
            else:
                score += 5  # Minimal documentation

        # Type hints
        has_return_annotation = func_node.returns is not None
        has_arg_annotations = any(arg.annotation for arg in func_node.args.args)
        if has_return_annotation:
            score += 5
        if has_arg_annotations:
            score += 5

        # Error handling
        has_try_except = any(isinstance(node, ast.Try) for node in ast.walk(func_node))
        if has_try_except:
            score += 10

        # Length appropriateness
        func_length = func_node.end_lineno - func_node.lineno
        if MIN_FUNCTION_LENGTH <= func_length <= 30:
            score += 10  # Good length
        elif func_length > MAX_FUNCTION_LENGTH:
            score -= 10  # Too long

        # Complexity (simple heuristic)
        complexity = sum(1 for node in ast.walk(func_node) if isinstance(node, (ast.If, ast.For, ast.While)))
        if complexity <= 3:
            score += 5  # Low complexity is good for snippets
        elif complexity > 5:
            score -= 10  # Too complex

        # Naming convention
        if func_node.name.startswith('_') and not func_node.name.startswith('__'):
            score -= 5  # Private methods less useful as examples
        elif re.match(r'^[a-z_][a-z0-9_]*$', func_node.name):
            score += 5  # Good naming

        return min(100.0, max(0.0, score))

    @staticmethod
    def categorize_snippet(func_node: ast.FunctionDef, source_code: str) -> List[str]:
        """Categorize snippet based on content and patterns"""
        categories = []
        func_name = func_node.name.lower()
        source_lower = source_code.lower()

        for category, keywords in PATTERN_CATEGORIES.items():
            if any(keyword in func_name or keyword in source_lower for keyword in keywords):
                categories.append(category)

        # Add general categories based on context
        if 'storage' in source_lower or 'database' in source_lower:
            categories.append('storage')
        if 'crawler' in source_lower or 'crawl' in source_lower:
            categories.append('crawler')
        if 'extract' in source_lower:
            categories.append('extraction')

        return list(set(categories)) or ['general']

    @staticmethod
    def extract_dependencies(func_node: ast.FunctionDef, full_source: str) -> List[str]:
        """Extract imports and dependencies used by the function"""
        dependencies = set()

        # Parse full source to get imports
        try:
            tree = ast.parse(full_source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        dependencies.add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        dependencies.add(node.module)

        except:
            pass

        # Also check for common framework references in function
        func_source = ast.get_source_segment(full_source, func_node)
        if func_source:
            for framework in ['crawl4ai', 'supabase', 'fastapi', 'pydantic', 'asyncio']:
                if framework in func_source.lower():
                    dependencies.add(framework)

        return sorted(list(dependencies))

    @staticmethod
    def generate_description(func_node: ast.FunctionDef, categories: List[str]) -> str:
        """Generate a description for the snippet"""
        # Try to use docstring first
        docstring = ast.get_docstring(func_node)
        if docstring:
            # Use first line of docstring
            first_line = docstring.split('\n')[0].strip()
            if first_line:
                return first_line

        # Generate from function name and categories
        func_name = func_node.name.replace('_', ' ')
        category_str = ', '.join(categories[:2])  # Use top 2 categories

        return f"{func_name.capitalize()} - {category_str} utility"


class SnippetExtractor:
    """Extract code snippets from quality Python files"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = None
        self.stats = {
            'files_processed': 0,
            'snippets_extracted': 0,
            'snippets_stored': 0,
            'errors': 0
        }

    def connect(self):
        """Connect to database"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()

    @staticmethod
    def _get_file_commit_hash(file_path: str) -> Optional[str]:
        """Get the latest git commit hash for a file."""
        try:
            result = subprocess.run(
                ['git', 'log', '-1', '--format=%H', '--', file_path],
                capture_output=True, text=True, timeout=5,
                cwd=str(PROJECT_ROOT)
            )
            commit_hash = result.stdout.strip()
            return commit_hash if result.returncode == 0 and commit_hash else None
        except Exception:
            return None

    def get_quality_files(self) -> List[Dict]:
        """Get files with quality score >= MIN_QUALITY_SCORE"""
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT file_path, relative_path, complexity_score, language, framework
            FROM vnx_code_quality
            WHERE complexity_score <= ? AND language = 'python'
            ORDER BY complexity_score ASC
        """, (MIN_QUALITY_SCORE,))

        return [dict(row) for row in cursor.fetchall()]

    def extract_functions_from_file(self, file_path: str) -> List[Tuple[ast.FunctionDef, str, List[str]]]:
        """Extract function definitions from a Python file"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
                source_lines = source.splitlines()

            tree = ast.parse(source, filename=file_path)
            functions = []

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    # Skip private methods (but allow __init__ and other magic methods)
                    if node.name.startswith('_') and not node.name.startswith('__'):
                        continue

                    # Get function source
                    func_source = ast.get_source_segment(source, node)
                    if not func_source:
                        continue

                    # Check length constraints
                    func_length = node.end_lineno - node.lineno
                    if func_length < MIN_FUNCTION_LENGTH or func_length > MAX_FUNCTION_LENGTH:
                        continue

                    functions.append((node, func_source, source_lines))

            return functions

        except Exception as e:
            log('ERROR', f'Failed to extract from {file_path}: {e}')
            return []

    def store_snippet(self, snippet_data: Dict) -> bool:
        """Store snippet in database"""
        try:
            cursor = self.conn.cursor()

            # Insert into FTS5 table
            cursor.execute("""
                INSERT INTO code_snippets (
                    title, description, code, file_path, line_range,
                    tags, language, framework, dependencies,
                    quality_score, usage_count, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                snippet_data['title'],
                snippet_data['description'],
                snippet_data['code'],
                snippet_data['file_path'],
                snippet_data['line_range'],
                snippet_data['tags'],
                snippet_data['language'],
                snippet_data['framework'],
                snippet_data['dependencies'],
                snippet_data['quality_score'],
                0  # Initial usage count
            ))

            snippet_rowid = cursor.lastrowid

            # Get commit hash for citation tracking
            commit_hash = self._get_file_commit_hash(snippet_data['file_path'])
            now = datetime.now().isoformat()

            # Compute stable pattern hash for O(1) usage lookup
            hash_base = f"{snippet_data['title']}|{snippet_data['file_path']}|{snippet_data['line_range']}"
            pattern_hash = hashlib.sha1(hash_base.encode("utf-8")).hexdigest()

            # Insert metadata with citation fields
            cursor.execute("""
                INSERT INTO snippet_metadata (
                    snippet_rowid, file_path, line_start, line_end,
                    quality_score, usage_count,
                    source_commit_hash, pattern_hash, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snippet_rowid,
                snippet_data['file_path'],
                snippet_data['line_start'],
                snippet_data['line_end'],
                snippet_data['quality_score'],
                0,
                commit_hash,
                pattern_hash,
                now
            ))

            self.conn.commit()
            return True

        except Exception as e:
            log('ERROR', f'Failed to store snippet: {e}')
            return False

    def process_file(self, file_data: Dict) -> int:
        """Process a single file and extract snippets"""
        file_path = file_data['file_path']
        log('EXTRACT', f"Processing: {file_data['relative_path']}")

        functions = self.extract_functions_from_file(file_path)
        if not functions:
            return 0

        snippets_stored = 0
        analyzer = SnippetAnalyzer()

        for func_node, func_source, source_lines in functions:
            # Calculate snippet quality
            quality_score = analyzer.calculate_snippet_quality(func_node, source_lines)

            if quality_score < MIN_SNIPPET_SCORE:
                continue  # Skip low-quality snippets

            # Categorize snippet
            categories = analyzer.categorize_snippet(func_node, func_source)

            # Extract dependencies
            with open(file_path, 'r') as f:
                full_source = f.read()
            dependencies = analyzer.extract_dependencies(func_node, full_source)

            # Generate description
            description = analyzer.generate_description(func_node, categories)

            # Prepare snippet data
            snippet_data = {
                'title': func_node.name,
                'description': description,
                'code': func_source,
                'file_path': file_path,
                'line_range': f"{func_node.lineno}-{func_node.end_lineno}",
                'line_start': func_node.lineno,
                'line_end': func_node.end_lineno,
                'tags': ', '.join(categories),
                'language': 'python',
                'framework': file_data.get('framework') or 'none',
                'dependencies': ', '.join(dependencies),
                'quality_score': quality_score
            }

            # Store snippet
            if self.store_snippet(snippet_data):
                snippets_stored += 1
                self.stats['snippets_extracted'] += 1
                log('SUCCESS', f"  ✅ Extracted: {func_node.name} (quality: {quality_score:.1f})")

        self.stats['files_processed'] += 1
        self.stats['snippets_stored'] += snippets_stored

        return snippets_stored

    def run_extraction(self) -> bool:
        """Run snippet extraction process"""
        log('INFO', 'Starting snippet extraction...')
        start_time = datetime.now()

        try:
            # Get quality files
            quality_files = self.get_quality_files()
            log('INFO', f'Found {len(quality_files)} quality files (complexity ≤ {MIN_QUALITY_SCORE})')

            if not quality_files:
                log('WARNING', 'No quality files found for snippet extraction')
                return False

            # Process each file
            for i, file_data in enumerate(quality_files, 1):
                log('EXTRACT', f'[{i}/{len(quality_files)}] {file_data["relative_path"]}')

                try:
                    snippets_count = self.process_file(file_data)
                    if snippets_count > 0:
                        log('INFO', f'  → Extracted {snippets_count} snippet(s)')
                except Exception as e:
                    log('ERROR', f'  ✗ Failed: {e}')
                    self.stats['errors'] += 1

            # Calculate duration
            duration = (datetime.now() - start_time).total_seconds()

            # Print summary
            print(f"\n{Colors.GREEN}{'='*70}")
            print(f"Snippet Extraction Complete!")
            print(f"{'='*70}{Colors.RESET}\n")

            print(f"Files Processed: {self.stats['files_processed']}")
            print(f"Snippets Extracted: {self.stats['snippets_extracted']}")
            print(f"Snippets Stored: {self.stats['snippets_stored']}")
            print(f"Errors: {self.stats['errors']}")
            print(f"Duration: {duration:.2f} seconds")
            print(f"\n{Colors.CYAN}Database: {DB_PATH}{Colors.RESET}\n")

            return True

        except Exception as e:
            log('ERROR', f'Extraction failed: {e}')
            return False


def main():
    """Main execution"""
    print(f"\n{Colors.BLUE}{'='*70}")
    print(f"VNX Code Snippet Extractor")
    print(f"Version: 8.0.2 (Phase 2)")
    print(f"{'='*70}{Colors.RESET}\n")

    # Verify database exists
    if not DB_PATH.exists():
        log('ERROR', f'Quality database not found: {DB_PATH}')
        log('INFO', 'Run code_quality_scanner.py first to populate quality metrics')
        return 1

    # Create extractor
    extractor = SnippetExtractor(DB_PATH)

    # Connect to database
    extractor.connect()

    try:
        # Run extraction
        success = extractor.run_extraction()
        return 0 if success else 1

    finally:
        extractor.close()


if __name__ == "__main__":
    exit(main())
