#!/usr/bin/env python3
"""
Code Quality Scanner for VNX Intelligence System
Version: 8.0.2 (Phase 2)
Purpose: Scan Python files and populate quality intelligence database
"""

import sqlite3
import os
import sys
import ast
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import fnmatch

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

# Scan configuration
SCAN_DIRECTORIES = [
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "tests",
    VNX_BASE / "scripts"
]

# Explicit exclude globs for archived/backup paths
EXCLUDE_GLOBS = [
    "archive*",
    "archived*",
    "backup*"
]

ALLOWLIST_ENV = "VNX_QUALITY_SCAN_ALLOWLIST"
ALLOWLIST_FILE_ENV = "VNX_QUALITY_SCAN_ALLOWLIST_FILE"

# Quality thresholds
COMPLEXITY_THRESHOLDS = {
    'low': 10,      # Simple functions
    'moderate': 20,  # Moderate complexity
    'high': 30,     # High complexity - needs attention
    'critical': 50  # Critical - requires refactoring
}

MAX_FUNCTION_LENGTH = 50  # lines
MAX_FILE_LENGTH = 500     # lines
MAX_NESTING_DEPTH = 4     # levels

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
        'SCAN': Colors.CYAN
    }

    color = color_map.get(level, Colors.RESET)
    print(f"[{timestamp}] {color}[{level}]{Colors.RESET} {message}")


def _load_allowlist_globs() -> List[str]:
    """Load allowlist globs from env or file (one glob per line)."""
    allowlist: List[str] = []

    env_value = os.environ.get(ALLOWLIST_ENV, "")
    if env_value:
        allowlist.extend([item.strip() for item in env_value.split(",") if item.strip()])

    file_value = os.environ.get(ALLOWLIST_FILE_ENV, "")
    if file_value:
        allowlist_path = Path(file_value).expanduser().resolve()
        if allowlist_path.exists():
            allowlist.extend([
                line.strip()
                for line in allowlist_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ])
        else:
            log('WARNING', f'Allowlist file not found: {allowlist_path}')

    return allowlist


def _matches_exclude_globs(path: Path, exclude_globs: List[str]) -> bool:
    """Match against exclude globs on any path component."""
    if not exclude_globs:
        return False

    for part in path.parts:
        lower_part = part.lower()
        for glob in exclude_globs:
            if fnmatch.fnmatch(lower_part, glob.lower()):
                return True

    return False


def _matches_allowlist_globs(path: Path, allowlist_globs: List[str], scan_root: Path) -> bool:
    """Match allowlist globs against relative path or basename."""
    if not allowlist_globs:
        return True

    try:
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        try:
            relative_path = path.relative_to(scan_root).as_posix()
        except ValueError:
            relative_path = path.as_posix()

    basename = path.name

    for glob in allowlist_globs:
        pattern = glob.strip()
        if not pattern:
            continue
        if "/" in pattern:
            if fnmatch.fnmatch(relative_path, pattern):
                return True
        else:
            if fnmatch.fnmatch(basename, pattern):
                return True

    return False


class CodeMetricsCalculator:
    """Calculate code quality metrics using AST analysis"""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.tree = None
        self.source_lines = []

    def parse_file(self) -> bool:
        """Parse Python file into AST"""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                source = f.read()
                self.source_lines = source.splitlines()
                self.tree = ast.parse(source, filename=str(self.file_path))
            return True
        except Exception as e:
            log('ERROR', f'Failed to parse {self.file_path}: {e}')
            return False

    def calculate_cyclomatic_complexity(self, node: ast.AST) -> int:
        """Calculate cyclomatic complexity for a node"""
        complexity = 1  # Base complexity

        for child in ast.walk(node):
            # Decision points that increase complexity
            if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, (ast.ListComp, ast.DictComp, ast.SetComp)):
                complexity += 1

        return complexity

    def calculate_nesting_depth(self, node: ast.AST, current_depth: int = 0) -> int:
        """Calculate maximum nesting depth"""
        max_depth = current_depth

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.With, ast.Try)):
                child_depth = self.calculate_nesting_depth(child, current_depth + 1)
                max_depth = max(max_depth, child_depth)

        return max_depth

    def count_line_types(self) -> Tuple[int, int, int]:
        """Count code, comment, and blank lines"""
        code_lines = 0
        comment_lines = 0
        blank_lines = 0

        in_multiline_string = False

        for line in self.source_lines:
            stripped = line.strip()

            if not stripped:
                blank_lines += 1
            elif stripped.startswith('#'):
                comment_lines += 1
            elif '"""' in stripped or "'''" in stripped:
                # Toggle multiline string state
                in_multiline_string = not in_multiline_string
                comment_lines += 1
            elif in_multiline_string:
                comment_lines += 1
            else:
                code_lines += 1

        return code_lines, comment_lines, blank_lines

    def analyze_functions(self) -> List[Dict]:
        """Analyze all functions in the file"""
        functions = []

        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                func_info = {
                    'name': node.name,
                    'line_start': node.lineno,
                    'line_end': node.end_lineno,
                    'length': node.end_lineno - node.lineno + 1,
                    'complexity': self.calculate_cyclomatic_complexity(node),
                    'nesting_depth': self.calculate_nesting_depth(node),
                    'has_docstring': ast.get_docstring(node) is not None,
                    'args_count': len(node.args.args)
                }
                functions.append(func_info)

        return functions

    def analyze_classes(self) -> List[Dict]:
        """Analyze all classes in the file"""
        classes = []

        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef):
                class_info = {
                    'name': node.name,
                    'line_start': node.lineno,
                    'line_end': node.end_lineno,
                    'has_docstring': ast.get_docstring(node) is not None,
                    'method_count': sum(1 for n in node.body if isinstance(n, ast.FunctionDef))
                }
                classes.append(class_info)

        return classes

    def count_imports(self) -> int:
        """Count import statements"""
        import_count = 0

        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_count += 1

        return import_count

    def calculate_file_metrics(self) -> Optional[Dict]:
        """Calculate comprehensive file metrics"""
        if not self.parse_file():
            return None

        # Line counts
        code_lines, comment_lines, blank_lines = self.count_line_types()
        total_lines = len(self.source_lines)

        # Structural analysis
        functions = self.analyze_functions()
        classes = self.analyze_classes()
        import_count = self.count_imports()

        # Complexity metrics
        max_function_length = max([f['length'] for f in functions], default=0)
        max_nesting_depth = max([f['nesting_depth'] for f in functions], default=0)
        avg_complexity = sum([f['complexity'] for f in functions]) / len(functions) if functions else 0

        # Complexity score (0-100, higher is worse)
        complexity_score = min(100, (
            (avg_complexity / 10 * 30) +  # 30% weight on avg complexity
            (max_function_length / MAX_FUNCTION_LENGTH * 25) +  # 25% weight on function length
            (max_nesting_depth / MAX_NESTING_DEPTH * 20) +  # 20% weight on nesting
            (total_lines / MAX_FILE_LENGTH * 15) +  # 15% weight on file size
            (import_count / 20 * 10)  # 10% weight on imports
        ))

        # Quality warnings
        warnings = []
        critical_issues = 0
        warning_issues = 0
        info_issues = 0

        if complexity_score > COMPLEXITY_THRESHOLDS['critical']:
            warnings.append({'type': 'critical', 'message': f'Critical complexity score: {complexity_score:.1f}'})
            critical_issues += 1
        elif complexity_score > COMPLEXITY_THRESHOLDS['high']:
            warnings.append({'type': 'warning', 'message': f'High complexity score: {complexity_score:.1f}'})
            warning_issues += 1

        if max_function_length > MAX_FUNCTION_LENGTH:
            warnings.append({'type': 'warning', 'message': f'Long function detected: {max_function_length} lines'})
            warning_issues += 1

        if max_nesting_depth > MAX_NESTING_DEPTH:
            warnings.append({'type': 'warning', 'message': f'Deep nesting detected: {max_nesting_depth} levels'})
            warning_issues += 1

        if total_lines > MAX_FILE_LENGTH:
            warnings.append({'type': 'info', 'message': f'Large file: {total_lines} lines (consider splitting)'})
            info_issues += 1

        # Calculate docstring coverage
        functions_with_docs = sum(1 for f in functions if f['has_docstring'])
        classes_with_docs = sum(1 for c in classes if c['has_docstring'])
        total_documentable = len(functions) + len(classes)
        docstring_coverage = (functions_with_docs + classes_with_docs) / total_documentable * 100 if total_documentable > 0 else 0

        # Track assignment suggestion (simplified for Phase 2)
        suggested_track = None
        track_confidence = 0.0

        if 'storage' in str(self.file_path).lower() or 'database' in str(self.file_path).lower():
            suggested_track = 'A'
            track_confidence = 0.8
        elif complexity_score > COMPLEXITY_THRESHOLDS['high']:
            suggested_track = 'B'
            track_confidence = 0.7
        elif 'test' not in str(self.file_path).lower() and complexity_score < COMPLEXITY_THRESHOLDS['low']:
            suggested_track = 'C'
            track_confidence = 0.5

        return {
            'file_path': str(self.file_path),
            'project_root': str(PROJECT_ROOT),
            'relative_path': str(self.file_path.relative_to(PROJECT_ROOT)),
            'line_count': total_lines,
            'code_lines': code_lines,
            'comment_lines': comment_lines,
            'blank_lines': blank_lines,
            'complexity_score': round(complexity_score, 2),
            'cyclomatic_complexity': int(avg_complexity),
            'cognitive_complexity': max_nesting_depth,
            'function_count': len(functions),
            'class_count': len(classes),
            'import_count': import_count,
            'max_function_length': max_function_length,
            'max_nesting_depth': max_nesting_depth,
            'has_tests': 'test' in str(self.file_path).lower(),
            'test_coverage': 0.0,  # Would need coverage tool integration
            'has_docstrings': docstring_coverage > 0,
            'docstring_coverage': round(docstring_coverage, 2),
            'quality_warnings': json.dumps(warnings),
            'critical_issues': critical_issues,
            'warning_issues': warning_issues,
            'info_issues': info_issues,
            'suggested_track': suggested_track,
            'track_confidence': track_confidence,
            'language': 'python',
            'framework': self._detect_framework(),
            'last_modified': datetime.fromtimestamp(self.file_path.stat().st_mtime).isoformat(),
            'scan_version': '1.0'
        }

    def _detect_framework(self) -> Optional[str]:
        """Detect framework from imports"""
        frameworks = {
            'crawl4ai': ['crawl4ai'],
            'supabase': ['supabase'],
            'fastapi': ['fastapi'],
            'flask': ['flask'],
            'django': ['django'],
            'pytest': ['pytest']
        }

        source = '\n'.join(self.source_lines).lower()

        for framework, keywords in frameworks.items():
            if any(keyword in source for keyword in keywords):
                return framework

        return None


class QualityDatabaseManager:
    """Manage quality intelligence database operations"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = None

    def connect(self):
        """Connect to database"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()

    def store_quality_metrics(self, metrics: Dict) -> bool:
        """Store or update quality metrics for a file"""
        try:
            cursor = self.conn.cursor()

            # Check if file already exists
            cursor.execute("SELECT id FROM vnx_code_quality WHERE file_path = ?", (metrics['file_path'],))
            existing = cursor.fetchone()

            if existing:
                # Update existing record
                cursor.execute("""
                    UPDATE vnx_code_quality SET
                        project_root = ?,
                        relative_path = ?,
                        line_count = ?,
                        code_lines = ?,
                        comment_lines = ?,
                        blank_lines = ?,
                        complexity_score = ?,
                        cyclomatic_complexity = ?,
                        cognitive_complexity = ?,
                        function_count = ?,
                        class_count = ?,
                        import_count = ?,
                        max_function_length = ?,
                        max_nesting_depth = ?,
                        has_tests = ?,
                        test_coverage = ?,
                        has_docstrings = ?,
                        docstring_coverage = ?,
                        quality_warnings = ?,
                        critical_issues = ?,
                        warning_issues = ?,
                        info_issues = ?,
                        suggested_track = ?,
                        track_confidence = ?,
                        language = ?,
                        framework = ?,
                        last_scan = CURRENT_TIMESTAMP,
                        last_modified = ?,
                        scan_version = ?
                    WHERE file_path = ?
                """, (
                    metrics['project_root'],
                    metrics['relative_path'],
                    metrics['line_count'],
                    metrics['code_lines'],
                    metrics['comment_lines'],
                    metrics['blank_lines'],
                    metrics['complexity_score'],
                    metrics['cyclomatic_complexity'],
                    metrics['cognitive_complexity'],
                    metrics['function_count'],
                    metrics['class_count'],
                    metrics['import_count'],
                    metrics['max_function_length'],
                    metrics['max_nesting_depth'],
                    metrics['has_tests'],
                    metrics['test_coverage'],
                    metrics['has_docstrings'],
                    metrics['docstring_coverage'],
                    metrics['quality_warnings'],
                    metrics['critical_issues'],
                    metrics['warning_issues'],
                    metrics['info_issues'],
                    metrics['suggested_track'],
                    metrics['track_confidence'],
                    metrics['language'],
                    metrics['framework'],
                    metrics['last_modified'],
                    metrics['scan_version'],
                    metrics['file_path']
                ))
            else:
                # Insert new record
                cursor.execute("""
                    INSERT INTO vnx_code_quality (
                        file_path, project_root, relative_path,
                        line_count, code_lines, comment_lines, blank_lines,
                        complexity_score, cyclomatic_complexity, cognitive_complexity,
                        function_count, class_count, import_count,
                        max_function_length, max_nesting_depth,
                        has_tests, test_coverage, has_docstrings, docstring_coverage,
                        quality_warnings, critical_issues, warning_issues, info_issues,
                        suggested_track, track_confidence,
                        language, framework, last_modified, scan_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    metrics['file_path'],
                    metrics['project_root'],
                    metrics['relative_path'],
                    metrics['line_count'],
                    metrics['code_lines'],
                    metrics['comment_lines'],
                    metrics['blank_lines'],
                    metrics['complexity_score'],
                    metrics['cyclomatic_complexity'],
                    metrics['cognitive_complexity'],
                    metrics['function_count'],
                    metrics['class_count'],
                    metrics['import_count'],
                    metrics['max_function_length'],
                    metrics['max_nesting_depth'],
                    metrics['has_tests'],
                    metrics['test_coverage'],
                    metrics['has_docstrings'],
                    metrics['docstring_coverage'],
                    metrics['quality_warnings'],
                    metrics['critical_issues'],
                    metrics['warning_issues'],
                    metrics['info_issues'],
                    metrics['suggested_track'],
                    metrics['track_confidence'],
                    metrics['language'],
                    metrics['framework'],
                    metrics['last_modified'],
                    metrics['scan_version']
                ))

            self.conn.commit()
            return True

        except Exception as e:
            log('ERROR', f'Failed to store metrics: {e}')
            return False

    def record_scan_history(self, scan_type: str, files_scanned: int, files_changed: int, issues_found: int, duration: float) -> int:
        """Record scan history entry"""
        try:
            cursor = self.conn.cursor()

            cursor.execute("""
                INSERT INTO scan_history (
                    scan_type, files_scanned, files_changed, issues_found,
                    scan_duration_seconds, status
                ) VALUES (?, ?, ?, ?, ?, 'completed')
            """, (scan_type, files_scanned, files_changed, issues_found, duration))

            self.conn.commit()
            return cursor.lastrowid

        except Exception as e:
            log('ERROR', f'Failed to record scan history: {e}')
            return -1


class QualityScanner:
    """Main quality scanner orchestrator"""

    def __init__(
        self,
        scan_dirs: List[Path],
        db_path: Path,
        exclude_globs: Optional[List[str]] = None,
        allowlist_globs: Optional[List[str]] = None
    ):
        self.scan_dirs = scan_dirs
        self.db_manager = QualityDatabaseManager(db_path)
        self.exclude_globs = exclude_globs if exclude_globs is not None else EXCLUDE_GLOBS
        self.allowlist_globs = allowlist_globs if allowlist_globs is not None else _load_allowlist_globs()
        self.stats = {
            'files_scanned': 0,
            'files_changed': 0,
            'issues_found': 0,
            'errors': 0
        }

    def find_python_files(self) -> List[Path]:
        """Find all Python files in scan directories"""
        python_files = []

        for scan_dir in self.scan_dirs:
            if not scan_dir.exists():
                log('WARNING', f'Scan directory does not exist: {scan_dir}')
                continue

            for py_file in scan_dir.rglob('*.py'):
                # Skip __pycache__ and .venv directories
                if '__pycache__' in str(py_file) or '.venv' in str(py_file):
                    continue
                if _matches_exclude_globs(py_file, self.exclude_globs):
                    continue
                if not _matches_allowlist_globs(py_file, self.allowlist_globs, scan_dir):
                    continue

                python_files.append(py_file)

        return python_files

    def scan_file(self, file_path: Path) -> Optional[Dict]:
        """Scan a single Python file"""
        try:
            calculator = CodeMetricsCalculator(file_path)
            metrics = calculator.calculate_file_metrics()

            if metrics:
                self.stats['files_scanned'] += 1

                # Count issues
                self.stats['issues_found'] += metrics['critical_issues']
                self.stats['issues_found'] += metrics['warning_issues']

                return metrics

            return None

        except Exception as e:
            log('ERROR', f'Failed to scan {file_path}: {e}')
            self.stats['errors'] += 1
            return None

    def run_scan(self, scan_type: str = 'full') -> bool:
        """Run complete quality scan"""
        log('INFO', f'Starting {scan_type} quality scan...')
        start_time = datetime.now()

        # Connect to database
        self.db_manager.connect()

        try:
            # Find all Python files
            if self.allowlist_globs:
                log('INFO', f'Allowlist enabled ({len(self.allowlist_globs)} patterns)')
            python_files = self.find_python_files()
            log('INFO', f'Found {len(python_files)} Python files to scan')

            # Scan each file
            for i, file_path in enumerate(python_files, 1):
                log('SCAN', f'[{i}/{len(python_files)}] Scanning: {file_path.relative_to(PROJECT_ROOT)}')

                metrics = self.scan_file(file_path)

                if metrics:
                    # Store in database
                    if self.db_manager.store_quality_metrics(metrics):
                        self.stats['files_changed'] += 1

                    # Log issues
                    if metrics['critical_issues'] > 0:
                        log('ERROR', f"  ⚠️  {metrics['critical_issues']} critical issue(s)")
                    elif metrics['warning_issues'] > 0:
                        log('WARNING', f"  ⚠️  {metrics['warning_issues']} warning(s)")
                    else:
                        log('SUCCESS', f"  ✅ Quality score: {metrics['complexity_score']:.1f}/100")

            # Calculate scan duration
            duration = (datetime.now() - start_time).total_seconds()

            # Record scan history
            self.db_manager.record_scan_history(
                scan_type,
                self.stats['files_scanned'],
                self.stats['files_changed'],
                self.stats['issues_found'],
                duration
            )

            # Print summary
            print(f"\n{Colors.GREEN}{'='*70}")
            print(f"Quality Scan Complete!")
            print(f"{'='*70}{Colors.RESET}\n")

            print(f"Files Scanned: {self.stats['files_scanned']}")
            print(f"Files Updated: {self.stats['files_changed']}")
            print(f"Issues Found: {self.stats['issues_found']}")
            print(f"Errors: {self.stats['errors']}")
            print(f"Duration: {duration:.2f} seconds")
            print(f"\n{Colors.CYAN}Database: {DB_PATH}{Colors.RESET}\n")

            return True

        except Exception as e:
            log('ERROR', f'Scan failed: {e}')
            return False

        finally:
            self.db_manager.close()


def main():
    """Main execution"""
    print(f"\n{Colors.BLUE}{'='*70}")
    print(f"VNX Code Quality Scanner")
    print(f"Version: 8.0.2 (Phase 2)")
    print(f"{'='*70}{Colors.RESET}\n")

    # Verify database exists
    if not DB_PATH.exists():
        log('ERROR', f'Quality database not found: {DB_PATH}')
        log('INFO', 'Run quality_db_init.py first to initialize the database')
        sys.exit(1)

    # Create scanner
    scanner = QualityScanner(SCAN_DIRECTORIES, DB_PATH)

    # Run scan
    success = scanner.run_scan(scan_type='full')

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
