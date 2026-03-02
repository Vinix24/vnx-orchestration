#!/usr/bin/env python3
"""
Doc Section Extractor for VNX Intelligence System
Version: 1.0.0
Purpose: Extract high-quality documentation sections from markdown files
         and populate FTS5 search database alongside code snippets.

Configurable via VNX_DOCS_DIRS environment variable (comma-separated paths,
relative to PROJECT_ROOT or absolute). Feature is inactive when not configured.
"""

import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
VNX_BASE = Path(PATHS["VNX_HOME"])
PROJECT_ROOT = Path(PATHS["PROJECT_ROOT"])
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"

MIN_SECTION_LINES = 3
MIN_QUALITY_SCORE = 40

DOC_CATEGORY_RANGES: Dict[Tuple[int, int], str] = {
    (0, 10): "governance",
    (10, 20): "architecture",
    (20, 30): "api",
    (30, 50): "implementation",
    (50, 60): "configuration",
    (60, 70): "operations",
    (70, 80): "business",
    (80, 100): "deployment",
}

SECTION_TITLE_TAGS: Dict[str, str] = {
    "api": "api",
    "endpoint": "api",
    "rest": "api",
    "sse": "sse-streaming",
    "browser": "browser-pool",
    "crawler": "crawler",
    "storage": "storage",
    "monitor": "monitoring",
    "deploy": "deployment",
    "security": "security",
    "kvk": "kvk-validation",
    "extractor": "extraction",
    "test": "testing",
    "plugin": "plugin",
    "email": "email-service",
}

SUBDIRECTORY_CATEGORIES: Dict[str, str] = {
    "production": "operations",
    "refactorquickscan": "api",
    "architecture": "architecture",
    "deploy": "deployment",
    "config": "configuration",
    "business": "business",
}


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


def log(level: str, message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color_map = {
        "INFO": Colors.BLUE,
        "SUCCESS": Colors.GREEN,
        "WARN": Colors.YELLOW,
        "ERROR": Colors.RED,
        "EXTRACT": Colors.CYAN,
    }
    color = color_map.get(level, Colors.RESET)
    print(f"[{timestamp}] {color}[{level}]{Colors.RESET} {message}")


def _resolve_docs_dirs() -> List[Path]:
    """Resolve documentation directories from VNX_DOCS_DIRS env var."""
    raw = os.environ.get("VNX_DOCS_DIRS", "")
    if not raw:
        return []
    dirs = []
    for entry in raw.split(","):
        p = Path(entry.strip())
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.is_dir():
            dirs.append(p)
        else:
            log("WARN", f"VNX_DOCS_DIRS entry not found: {p}")
    return dirs


@dataclass
class DocFrontmatter:
    title: str = ""
    status: str = "current"
    owner: str = ""
    last_updated: str = ""
    summary: str = ""
    links: Dict[str, str] = field(default_factory=dict)


@dataclass
class DocSection:
    title: str
    body: str
    line_start: int
    line_end: int
    has_code_blocks: bool
    has_tables: bool
    code_block_count: int
    cross_ref_count: int


def parse_frontmatter(content: str) -> Optional[DocFrontmatter]:
    """Parse YAML frontmatter delimited by --- lines."""
    if not content.startswith("---"):
        return None
    lines = content.split("\n")
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return None

    try:
        import yaml

        fm_text = "\n".join(lines[1:end_idx])
        data = yaml.safe_load(fm_text)
        if not isinstance(data, dict):
            return None
        fm = DocFrontmatter(
            title=str(data.get("title", "")),
            status=str(data.get("status", "current")),
            owner=str(data.get("owner", "")),
            last_updated=str(data.get("last_updated", "")),
            summary=str(data.get("summary", "")),
            links=data.get("links", {}),
        )
        return fm
    except Exception:
        return None


def split_sections(content: str, frontmatter: Optional[DocFrontmatter]) -> List[DocSection]:
    """Split markdown content on ## headings into sections."""
    lines = content.split("\n")

    # Skip frontmatter lines
    start_line = 0
    if content.startswith("---"):
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                start_line = i + 1
                break

    sections: List[DocSection] = []
    current_title = ""
    current_lines: List[str] = []
    section_start = start_line + 1  # 1-indexed

    for i in range(start_line, len(lines)):
        line = lines[i]
        if line.startswith("## "):
            # Save previous section if non-empty
            if current_title and current_lines:
                body = "\n".join(current_lines).strip()
                body_line_count = len([ln for ln in body.split("\n") if ln.strip()])
                if body_line_count >= MIN_SECTION_LINES:
                    sections.append(_build_section(current_title, body, section_start, i))
            current_title = line[3:].strip()
            current_lines = []
            section_start = i + 1  # 1-indexed
        else:
            current_lines.append(line)

    # Last section
    if current_title and current_lines:
        body = "\n".join(current_lines).strip()
        body_line_count = len([ln for ln in body.split("\n") if ln.strip()])
        if body_line_count >= MIN_SECTION_LINES:
            sections.append(_build_section(current_title, body, section_start, len(lines)))

    return sections


def _build_section(title: str, body: str, line_start: int, line_end: int) -> DocSection:
    code_blocks = re.findall(r"```", body)
    code_block_count = len(code_blocks) // 2
    has_tables = bool(re.search(r"\|.*\|.*\|", body))
    cross_refs = re.findall(r"\[.*?\]\(.*?\.md\)", body)
    return DocSection(
        title=title,
        body=body,
        line_start=line_start,
        line_end=line_end,
        has_code_blocks=code_block_count > 0,
        has_tables=has_tables,
        code_block_count=code_block_count,
        cross_ref_count=len(cross_refs),
    )


def categorize_doc(file_path: Path, frontmatter: Optional[DocFrontmatter]) -> str:
    """Determine category from filename prefix number, frontmatter, or subdirectory."""
    name = file_path.name
    match = re.match(r"^(\d+)", name)
    if match:
        num = int(match.group(1))
        for (lo, hi), category in DOC_CATEGORY_RANGES.items():
            if lo <= num < hi:
                return category

    # Fallback: subdirectory name
    parent_name = file_path.parent.name.lower()
    if parent_name in SUBDIRECTORY_CATEGORIES:
        return SUBDIRECTORY_CATEGORIES[parent_name]

    # Fallback: frontmatter owner
    if frontmatter and frontmatter.owner:
        owner_lower = frontmatter.owner.lower()
        for key, cat in SUBDIRECTORY_CATEGORIES.items():
            if key in owner_lower:
                return cat

    return "general"


def extract_tags(section: DocSection, category: str) -> List[str]:
    """Extract tags from section title and content."""
    tags = {"documentation", category}
    title_lower = section.title.lower()
    for keyword, tag in SECTION_TITLE_TAGS.items():
        if keyword in title_lower:
            tags.add(tag)
    return sorted(tags)


def score_doc_section(
    section: DocSection,
    frontmatter: Optional[DocFrontmatter],
) -> float:
    """Calculate quality score for a documentation section (0-100)."""
    score = 50.0

    if section.has_code_blocks:
        score += 10
    if section.code_block_count >= 2:
        score += 5
    if section.has_tables:
        score += 8

    word_count = len(section.body.split())
    if 50 <= word_count <= 500:
        score += 10
    elif word_count > 500:
        score += 5
    if word_count < 20:
        score -= 15

    if section.cross_ref_count >= 1:
        score += 5
    if section.cross_ref_count >= 3:
        score += 5

    if frontmatter and frontmatter.summary:
        score += 5

    # Status penalties
    if frontmatter:
        status = frontmatter.status.lower()
        if status in ("archived", "deprecated"):
            score *= 0.5
        elif status == "draft":
            score *= 0.75

    return min(100.0, max(0.0, score))


def _generate_description(section: DocSection, frontmatter: Optional[DocFrontmatter]) -> str:
    """Build a description from frontmatter summary + first sentence of body."""
    parts = []
    if frontmatter and frontmatter.summary:
        parts.append(frontmatter.summary.strip())
    # First non-empty, non-heading line
    for line in section.body.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("|") and not stripped.startswith("```"):
            parts.append(stripped[:200])
            break
    return " — ".join(parts) if parts else section.title


def _extract_cross_ref_files(section: DocSection) -> List[str]:
    """Extract filenames of cross-referenced markdown docs."""
    refs = re.findall(r"\[.*?\]\((.*?\.md)\)", section.body)
    return sorted(set(refs))


class DocSectionExtractor:
    """Extract documentation sections from markdown files into FTS5 database."""

    def __init__(self, db_path: Path, docs_dirs: List[Path]):
        self.db_path = db_path
        self.docs_dirs = docs_dirs
        self.conn: Optional[sqlite3.Connection] = None
        self.stats = {
            "files_processed": 0,
            "sections_extracted": 0,
            "sections_stored": 0,
            "files_skipped_unchanged": 0,
            "errors": 0,
        }

    def connect(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        if self.conn:
            self.conn.close()

    @staticmethod
    def _get_file_commit_hash(file_path: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H", "--", file_path],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(PROJECT_ROOT),
            )
            commit_hash = result.stdout.strip()
            return commit_hash if result.returncode == 0 and commit_hash else None
        except Exception:
            return None

    def _is_unchanged(self, file_path: str) -> bool:
        """Check if file has same commit hash as last extraction."""
        if not self.conn:
            return False
        try:
            cursor = self.conn.execute(
                "SELECT source_commit_hash FROM snippet_metadata WHERE file_path = ? LIMIT 1",
                (file_path,),
            )
            row = cursor.fetchone()
            if not row or not row["source_commit_hash"]:
                return False
            current_hash = self._get_file_commit_hash(file_path)
            return current_hash is not None and current_hash == row["source_commit_hash"]
        except Exception:
            return False

    def _clear_stale_sections(self, file_path: str):
        """Remove existing markdown sections for a file before re-extraction."""
        if not self.conn:
            return
        try:
            # Get rowids from snippet_metadata for this file's markdown entries
            cursor = self.conn.execute(
                "SELECT snippet_rowid FROM snippet_metadata WHERE file_path = ?",
                (file_path,),
            )
            rowids = [row["snippet_rowid"] for row in cursor.fetchall()]

            if rowids:
                # Verify these are markdown entries in FTS5 before deleting
                for rowid in rowids:
                    fts_row = self.conn.execute(
                        "SELECT language FROM code_snippets WHERE rowid = ?",
                        (rowid,),
                    ).fetchone()
                    if fts_row and fts_row["language"] == "markdown":
                        self.conn.execute(
                            "DELETE FROM code_snippets WHERE rowid = ?",
                            (rowid,),
                        )
                        self.conn.execute(
                            "DELETE FROM snippet_metadata WHERE snippet_rowid = ?",
                            (rowid,),
                        )
            self.conn.commit()
        except Exception as e:
            log("ERROR", f"Failed to clear stale sections for {file_path}: {e}")

    def find_markdown_files(self) -> List[Path]:
        """Find all markdown files in configured docs directories, skipping archive/."""
        files = []
        for docs_dir in self.docs_dirs:
            for md_file in sorted(docs_dir.rglob("*.md")):
                # Skip archive directories
                if "archive" in md_file.parts:
                    continue
                files.append(md_file)
        return files

    def store_section(self, section_data: Dict) -> bool:
        """Store a documentation section in FTS5 + snippet_metadata."""
        if not self.conn:
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO code_snippets (
                    title, description, code, file_path, line_range,
                    tags, language, framework, dependencies,
                    quality_score, usage_count, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    section_data["title"],
                    section_data["description"],
                    section_data["code"],
                    section_data["file_path"],
                    section_data["line_range"],
                    section_data["tags"],
                    section_data["language"],
                    section_data["framework"],
                    section_data["dependencies"],
                    section_data["quality_score"],
                    0,
                ),
            )
            snippet_rowid = cursor.lastrowid

            commit_hash = self._get_file_commit_hash(section_data["file_path"])
            now = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT INTO snippet_metadata (
                    snippet_rowid, file_path, line_start, line_end,
                    quality_score, usage_count,
                    source_commit_hash, extracted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snippet_rowid,
                    section_data["file_path"],
                    section_data["line_start"],
                    section_data["line_end"],
                    section_data["quality_score"],
                    0,
                    commit_hash,
                    now,
                ),
            )
            self.conn.commit()
            return True
        except Exception as e:
            log("ERROR", f"Failed to store section: {e}")
            return False

    def process_file(self, file_path: Path) -> int:
        """Process a single markdown file and extract sections. Returns sections stored."""
        str_path = str(file_path)

        # Idempotency: skip unchanged files
        if self._is_unchanged(str_path):
            self.stats["files_skipped_unchanged"] += 1
            return 0

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            log("ERROR", f"Failed to read {file_path}: {e}")
            self.stats["errors"] += 1
            return 0

        # Clear stale sections for this file before re-extraction
        self._clear_stale_sections(str_path)

        frontmatter = parse_frontmatter(content)
        sections = split_sections(content, frontmatter)
        category = categorize_doc(file_path, frontmatter)

        sections_stored = 0
        for section in sections:
            quality = score_doc_section(section, frontmatter)
            if quality < MIN_QUALITY_SCORE:
                continue

            tags = extract_tags(section, category)
            description = _generate_description(section, frontmatter)
            cross_refs = _extract_cross_ref_files(section)

            section_data = {
                "title": section.title,
                "description": description,
                "code": section.body,
                "file_path": str_path,
                "line_range": f"{section.line_start}-{section.line_end}",
                "line_start": section.line_start,
                "line_end": section.line_end,
                "tags": ", ".join(tags),
                "language": "markdown",
                "framework": category,
                "dependencies": ", ".join(cross_refs),
                "quality_score": quality,
            }

            if self.store_section(section_data):
                sections_stored += 1
                self.stats["sections_extracted"] += 1

        self.stats["files_processed"] += 1
        self.stats["sections_stored"] += sections_stored
        return sections_stored

    def run_extraction(self) -> bool:
        """Orchestrate full extraction pipeline."""
        if not self.docs_dirs:
            log("INFO", "No docs directories configured (VNX_DOCS_DIRS not set)")
            return True

        log("INFO", f"Starting doc section extraction from {len(self.docs_dirs)} directory(ies)...")
        start_time = datetime.now()

        md_files = self.find_markdown_files()
        log("INFO", f"Found {len(md_files)} markdown files")

        if not md_files:
            log("WARN", "No markdown files found in configured directories")
            return True

        for i, md_file in enumerate(md_files, 1):
            rel_path = md_file.name
            try:
                count = self.process_file(md_file)
                if count > 0:
                    log("EXTRACT", f"[{i}/{len(md_files)}] {rel_path} -> {count} section(s)")
            except Exception as e:
                log("ERROR", f"[{i}/{len(md_files)}] {rel_path} failed: {e}")
                self.stats["errors"] += 1

        duration = (datetime.now() - start_time).total_seconds()
        print(f"\n{Colors.GREEN}{'=' * 70}")
        print("Doc Section Extraction Complete!")
        print(f"{'=' * 70}{Colors.RESET}\n")
        print(f"Files Processed: {self.stats['files_processed']}")
        print(f"Files Skipped (unchanged): {self.stats['files_skipped_unchanged']}")
        print(f"Sections Extracted: {self.stats['sections_extracted']}")
        print(f"Sections Stored: {self.stats['sections_stored']}")
        print(f"Errors: {self.stats['errors']}")
        print(f"Duration: {duration:.2f} seconds")
        print(f"\n{Colors.CYAN}Database: {self.db_path}{Colors.RESET}\n")
        return True


def main():
    print(f"\n{Colors.BLUE}{'=' * 70}")
    print("VNX Doc Section Extractor")
    print("Version: 1.0.0")
    print(f"{'=' * 70}{Colors.RESET}\n")

    if not DB_PATH.exists():
        log("ERROR", f"Quality database not found: {DB_PATH}")
        log("INFO", "Run code_quality_scanner.py first to populate quality metrics")
        return 1

    docs_dirs = _resolve_docs_dirs()
    if not docs_dirs:
        log("INFO", "VNX_DOCS_DIRS not set — doc extraction disabled")
        return 0

    log("INFO", f"Docs directories: {[str(d) for d in docs_dirs]}")

    extractor = DocSectionExtractor(DB_PATH, docs_dirs)
    extractor.connect()
    try:
        success = extractor.run_extraction()
        return 0 if success else 1
    finally:
        extractor.close()


if __name__ == "__main__":
    exit(main())
