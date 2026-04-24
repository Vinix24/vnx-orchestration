"""
Intelligence Hygiene
====================
Daily hygiene operations: database optimization, pattern quality checks,
freshness verification, tag refresh, and cleanup.
"""

import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class HygieneRunner:
    """Runs daily intelligence hygiene operations."""

    def __init__(
        self,
        gatherer,
        project_root: Path,
        vnx_dir: Path,
        refresh_daily: bool,
        find_state_file: Callable,
    ) -> None:
        self.gatherer = gatherer
        self.project_root = Path(project_root)
        self.vnx_dir = Path(vnx_dir)
        self.refresh_daily = refresh_daily
        self._find_state_file = find_state_file

    def _count_available_patterns(self) -> int:
        """Count total patterns available in database."""
        try:
            if not self.gatherer.quality_db:
                db_path = self._find_state_file("quality_intelligence.db")
                if db_path and db_path.exists():
                    import sqlite3
                    self.gatherer.quality_db = sqlite3.connect(str(db_path))
                    logger.info(f"Loaded quality database from {db_path}")
                else:
                    logger.error("Database not found in active state roots")
                    return 0

            if self.gatherer.quality_db:
                # Count high-quality snippets (PR #8 Fix)
                cursor = self.gatherer.quality_db.execute(
                    "SELECT COUNT(*) FROM code_snippets WHERE quality_score > 80"
                )
                count = cursor.fetchone()[0]
                return count
            return 0
        except Exception as e:
            logger.error(f"Error counting patterns: {e}")
            return 0

    def _optimize_database(self):
        """Optimize SQLite database."""
        try:
            if self.gatherer.quality_db:
                self.gatherer.quality_db.execute("VACUUM")
                self.gatherer.quality_db.execute("ANALYZE")
                logger.info("Database optimization complete")
        except Exception as e:
            logger.error(f"Database optimization failed: {e}")

    def _refresh_quality_intelligence(self):
        """Run quality scanner + snippet extractor to refresh patterns."""
        try:
            base_dir = str(self.vnx_dir)
            scanner = os.path.join(base_dir, "scripts", "code_quality_scanner.py")
            extractor = os.path.join(base_dir, "scripts", "code_snippet_extractor.py")
            if os.path.exists(scanner):
                logger.info("🔄 Refreshing quality intelligence database...")
                subprocess.run(["python3", scanner], check=False)
            if os.path.exists(extractor):
                subprocess.run(["python3", extractor], check=False)
            doc_extractor = os.path.join(base_dir, "scripts", "doc_section_extractor.py")
            if os.path.exists(doc_extractor):
                subprocess.run(["python3", doc_extractor], check=False)
            logger.info("✅ Intelligence refresh complete")
        except Exception as e:
            logger.error(f"❌ Intelligence refresh failed: {e}")

    def _verify_pattern_quality(self):
        """Verify pattern quality metrics."""
        try:
            if self.gatherer.quality_db:
                cursor = self.gatherer.quality_db.execute("""
                    SELECT
                        COUNT(*) as total,
                        AVG(quality_score) as avg_quality,
                        COUNT(CASE WHEN quality_score >= 85 THEN 1 END) as high_quality
                    FROM code_snippets
                """)
                result = cursor.fetchone()

                logger.info(
                    f"Pattern quality: {result['total']} total, "
                    f"{result['avg_quality']:.1f} avg quality, "
                    f"{result['high_quality']} high quality (≥85)"
                )
        except Exception as e:
            logger.error(f"Pattern quality check failed: {e}")

    def _cleanup_old_data(self):
        """Cleanup old data from database."""
        try:
            if self.gatherer.quality_db:
                # Remove old prevention rules with low confidence (older than 30 days)
                cutoff = (datetime.now() - timedelta(days=30)).isoformat()
                self.gatherer.quality_db.execute("""
                    DELETE FROM prevention_rules
                    WHERE confidence < 0.5 AND created_at < ?
                """, (cutoff,))

                # Commit changes
                self.gatherer.quality_db.commit()
                logger.info("Old data cleanup complete")
        except Exception as e:
            logger.error(f"Data cleanup failed: {e}")

    def _verify_pattern_freshness_bulk(self):
        """Bulk verification of snippet citations against current git state.

        Iterates all snippet_metadata rows with source_commit_hash,
        compares with the current commit for each file, and updates verified_at.
        Stale snippets get their quality_score penalized.
        """
        if not self.gatherer.quality_db:
            return

        try:
            cursor = self.gatherer.quality_db.execute('''
                SELECT id, file_path, source_commit_hash
                FROM snippet_metadata
                WHERE source_commit_hash IS NOT NULL
            ''')
            rows = cursor.fetchall()

            if not rows:
                logger.info("No snippets with commit hashes to verify")
                return

            now = datetime.now().isoformat()
            verified = 0
            stale = 0

            for row in rows:
                file_path = row['file_path']
                stored_hash = row['source_commit_hash']

                try:
                    result = subprocess.run(
                        ['git', 'log', '-1', '--format=%H', '--', file_path],
                        capture_output=True, text=True, timeout=5,
                        cwd=str(self.project_root)
                    )
                    current_hash = result.stdout.strip() if result.returncode == 0 else None

                    if current_hash and current_hash != stored_hash:
                        stale += 1
                        # Penalize stale snippet quality
                        self.gatherer.quality_db.execute('''
                            UPDATE snippet_metadata
                            SET verified_at = ?, quality_score = MAX(0, quality_score * 0.8)
                            WHERE id = ?
                        ''', (now, row['id']))
                    else:
                        self.gatherer.quality_db.execute('''
                            UPDATE snippet_metadata
                            SET verified_at = ?
                            WHERE id = ?
                        ''', (now, row['id']))

                    verified += 1

                except Exception:
                    continue

            self.gatherer.quality_db.commit()
            logger.info(f"Freshness check: {verified} verified, {stale} stale out of {len(rows)} snippets")

        except Exception as e:
            logger.error(f"Bulk freshness verification failed: {e}")

    def _refresh_tags_from_completed_dispatches(self):
        """Scan completed dispatches from the last 24h and feed extracted tags into tag intelligence.

        For each dispatch, extracts tags from metadata (Priority, Gate, Role, Reason)
        and instruction text (compound tag detection), then runs them through
        analyze_multi_tag_patterns() for pattern detection and prevention rule generation.
        """
        from tag_intelligence import TagIntelligenceEngine

        dispatches_dir = self.vnx_dir / "dispatches" / "completed"
        if not dispatches_dir.is_dir():
            logger.info("No completed dispatches directory found")
            return

        cutoff = (datetime.now() - timedelta(days=1)).timestamp()
        tag_engine = TagIntelligenceEngine()
        processed = 0

        for dispatch_file in dispatches_dir.glob("*.md"):
            try:
                if dispatch_file.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue

            tags = tag_engine.extract_tags_from_dispatch(dispatch_file)
            if not tags:
                continue

            normalized = tag_engine.normalize_tags(tags)
            if normalized:
                tag_engine.analyze_multi_tag_patterns(
                    list(normalized),
                    phase=None,
                    terminal=None,
                    outcome="completed",
                )
                processed += 1

        tag_engine.close()
        logger.info(f"Tag refresh: processed {processed} completed dispatches")

    def daily_hygiene(self):
        """Run daily hygiene operations (all steps except learning cycle and cache rankings)."""
        logger.info("🧹 Starting daily hygiene operations...")

        try:
            if self.refresh_daily:
                self._refresh_quality_intelligence()

            self._optimize_database()
            self._verify_pattern_quality()
            self._verify_pattern_freshness_bulk()
            self._refresh_tags_from_completed_dispatches()
            self._cleanup_old_data()

            logger.info("✅ Daily hygiene complete")

        except Exception as e:
            logger.error(f"❌ Daily hygiene failed: {e}")
