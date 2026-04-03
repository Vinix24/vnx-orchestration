#!/usr/bin/env python3
"""
VNX Intelligence Daemon
=======================
Continuous intelligence extraction with hourly pattern updates and health monitoring.
Integrates with VNX supervisor for lifecycle management.

Author: T-MANAGER
Date: 2026-01-19
Version: 1.0.0
"""

import os
import re
import sys
import json
import time
import signal
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List

# Add scripts directory to path for imports
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))
sys.path.insert(0, str(script_dir / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")
try:
    from python_singleton import enforce_python_singleton
except Exception as exc:
    raise SystemExit(f"Failed to load python_singleton helper: {exc}")

try:
    from gather_intelligence import T0IntelligenceGatherer
    from learning_loop import LearningLoop
    from cached_intelligence import CachedIntelligence
    from tag_intelligence import TagIntelligenceEngine
except ImportError as e:
    print(f"ERROR: Could not import required modules: {e}", file=sys.stderr)
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)
ROLLBACK_ENV_FLAG = "VNX_STATE_SIMPLIFICATION_ROLLBACK"


def _env_flag(name: str) -> Optional[bool]:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _rollback_mode_enabled() -> bool:
    rollback = _env_flag(ROLLBACK_ENV_FLAG)
    if rollback is None:
        rollback = _env_flag("VNX_STATE_DUAL_WRITE_LEGACY")
    return bool(rollback)


class GovernanceDigestRunner:
    """Periodic governance digest builder.

    Reads governance signals from the runtime state directory, calls the F18
    extractors and digest builder, and writes the result to
    ``state_dir/governance_digest.json`` every ``interval`` seconds.

    Interval defaults to 300 s (5 min) and is configurable via the
    ``VNX_DIGEST_INTERVAL`` environment variable.
    """

    OUTPUT_FILENAME = "governance_digest.json"
    RUNNER_VERSION = "1.0"

    def __init__(self, state_dir: Path, interval: int = 300) -> None:
        self.state_dir = Path(state_dir)
        self.interval = max(1, interval)
        self.digest_path = self.state_dir / self.OUTPUT_FILENAME
        self.last_run: Optional[datetime] = None

    @classmethod
    def from_env(cls, state_dir: Path) -> "GovernanceDigestRunner":
        """Construct runner reading interval from VNX_DIGEST_INTERVAL (default 300)."""
        try:
            interval = int(os.environ.get("VNX_DIGEST_INTERVAL", "300"))
        except (ValueError, TypeError):
            interval = 300
        return cls(state_dir, interval)

    # ── Scheduling ──────────────────────────────────────────────────────────

    def should_run(self) -> bool:
        """Return True if the interval has elapsed since the last run."""
        if self.last_run is None:
            return True
        elapsed = (datetime.now() - self.last_run).total_seconds()
        return elapsed >= self.interval

    # ── Signal loading ───────────────────────────────────────────────────────

    def _load_gate_results(self) -> List[Dict]:
        """Extract gate result records from t0_receipts.ndjson."""
        results: List[Dict] = []
        receipts_path = self.state_dir / "t0_receipts.ndjson"
        if not receipts_path.exists():
            return results

        gate_event_map = {
            "task_complete": "pass",
            "task_success": "pass",
            "gate_pass": "pass",
            "task_failed": "fail",
            "gate_fail": "fail",
            "gate_failure": "fail",
        }

        try:
            with open(receipts_path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        receipt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    gate = receipt.get("gate") or receipt.get("gate_id", "")
                    event_type = receipt.get("event_type", "")
                    if not gate or event_type not in gate_event_map:
                        continue

                    results.append({
                        "gate_id": gate,
                        "status": gate_event_map[event_type],
                        "feature_id": receipt.get("feature_id", ""),
                        "pr_id": receipt.get("pr", receipt.get("pr_id", "")),
                        "dispatch_id": receipt.get("dispatch_id", ""),
                        "reason": receipt.get("error", receipt.get("reason", "")),
                    })
        except Exception as exc:
            logger.warning("GovernanceDigestRunner: could not read receipts: %s", exc)

        return results

    def _load_queue_anomalies(self) -> List[Dict]:
        """Extract queue anomaly records from t0_receipts.ndjson."""
        anomalies: List[Dict] = []
        receipts_path = self.state_dir / "t0_receipts.ndjson"
        if not receipts_path.exists():
            return anomalies

        anomaly_types = frozenset({
            "delivery_failure", "reconcile_error", "ack_timeout",
            "dead_letter", "queue_stall",
        })

        try:
            with open(receipts_path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        receipt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if receipt.get("event_type") in anomaly_types:
                        anomalies.append(receipt)
        except Exception as exc:
            logger.warning("GovernanceDigestRunner: could not read anomalies: %s", exc)

        return anomalies

    # ── Core execution ───────────────────────────────────────────────────────

    def run_once(self) -> bool:
        """Execute one digest cycle.

        1. Load signals from state files.
        2. Call collect_governance_signals() (F18 extractor).
        3. Call build_digest() (F18 digest builder).
        4. Write output to state_dir/governance_digest.json.

        Returns True on success, False on failure.
        """
        try:
            from governance_signal_extractor import collect_governance_signals
            from retrospective_digest import build_digest
        except ImportError as exc:
            logger.error("GovernanceDigestRunner: cannot import F18 modules: %s", exc)
            return False

        try:
            gate_results = self._load_gate_results()
            queue_anomalies = self._load_queue_anomalies()

            signals = collect_governance_signals(
                gate_results=gate_results or None,
                queue_anomalies=queue_anomalies or None,
            )

            digest = build_digest(signals)

            output: Dict = {
                "runner_version": self.RUNNER_VERSION,
                "state_dir": str(self.state_dir),
                "interval_seconds": self.interval,
                "source_records": {
                    "gate_results": len(gate_results),
                    "queue_anomalies": len(queue_anomalies),
                },
                **digest.to_dict(),
            }

            self._write_json_atomic(output)
            self.last_run = datetime.now()
            logger.info(
                "GovernanceDigestRunner: digest written (%d signals, %d recurring patterns)",
                len(signals),
                len(digest.recurring_patterns),
            )
            return True

        except Exception as exc:
            logger.error("GovernanceDigestRunner.run_once failed: %s", exc)
            return False

    def _write_json_atomic(self, payload: Dict) -> None:
        """Write payload to digest_path via a temporary file (atomic replace)."""
        import tempfile
        self.digest_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.digest_path.parent, delete=False, suffix=".tmp"
        ) as tmp:
            json.dump(payload, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, self.digest_path)


class IntelligenceDaemon:
    """Continuous intelligence extraction daemon with health monitoring"""

    def __init__(self):
        """Initialize daemon with paths and configuration"""
        paths = ensure_env()
        self.project_root = Path(paths["PROJECT_ROOT"]).expanduser().resolve()
        self.vnx_dir = Path(paths["VNX_HOME"])
        self.state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
        self.legacy_state_dir = (self.vnx_dir / "state").resolve()
        self.dashboard_file = self.state_dir / "dashboard_status.json"
        self.legacy_dashboard_file = self.legacy_state_dir / "dashboard_status.json"
        self.intelligence_health_file = self.state_dir / "intelligence_health.json"
        self.legacy_intelligence_health_file = self.legacy_state_dir / "intelligence_health.json"
        self.dashboard_write_enabled = os.getenv("VNX_INTELLIGENCE_DASHBOARD_WRITE", "0") == "1"
        self.rollback_mode = _rollback_mode_enabled()

        self.compat_state_dirs: List[Path] = [self.state_dir]
        if self.rollback_mode:
            for state_dir in [self.legacy_state_dir, self.project_root / ".vnx-data" / "state"]:
                if state_dir not in self.compat_state_dirs:
                    self.compat_state_dirs.append(state_dir)
            logger.warning(
                "[CUTOVER] Rollback mode enabled (%s=1). Legacy state reads and mirror writes are active.",
                ROLLBACK_ENV_FLAG,
            )

        # Intelligence components
        self.gatherer = T0IntelligenceGatherer()
        self.learning_loop = LearningLoop()
        self.cached_intelligence = CachedIntelligence()

        # Daemon state
        self.running = True
        self.last_extraction = None
        self.last_daily_hygiene = None
        self.last_learning_cycle = None
        self.extraction_interval = 3600  # 1 hour in seconds
        self.daily_hygiene_hour = 18  # 18:00 (6 PM)
        self.learning_cycle_hour = 18  # Run learning at same time as hygiene
        self.refresh_daily = os.getenv("VNX_DAILY_INTEL_REFRESH", "1") == "1"

        # Health tracking
        self.health_status = {
            'status': 'starting',
            'last_extraction': None,
            'patterns_available': 0,
            'extraction_errors': 0,
            'uptime_seconds': 0,
            'last_health_update': None
        }

        # Governance digest runner (5-min cadence by default)
        self.digest_runner = GovernanceDigestRunner.from_env(self.state_dir)

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info("Intelligence Daemon initialized (rollback_mode=%s)", self.rollback_mode)

    def _find_state_file(self, filename: str) -> Optional[Path]:
        """Find a state file from canonical root, with optional rollback compatibility."""
        for state_dir in self.compat_state_dirs:
            candidate = state_dir / filename
            if candidate.exists():
                return candidate
        return None

    def _write_json_atomic(self, destination: Path, payload: Dict) -> None:
        import tempfile

        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, delete=False, suffix='.tmp') as tmp:
            json.dump(payload, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, destination)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False

    def should_extract_hourly(self) -> bool:
        """Check if hourly extraction is due"""
        if not self.last_extraction:
            return True

        elapsed = (datetime.now() - self.last_extraction).total_seconds()
        return elapsed >= self.extraction_interval

    def should_run_daily_hygiene(self) -> bool:
        """Check if daily hygiene is due (runs at 18:00)"""
        now = datetime.now()

        # Skip if already ran today
        if self.last_daily_hygiene:
            if self.last_daily_hygiene.date() == now.date():
                return False

        # Check if current hour matches target hour
        return now.hour == self.daily_hygiene_hour

    def hourly_extraction(self):
        """Run hourly intelligence extraction"""
        logger.info("🔄 Starting hourly intelligence extraction...")

        try:
            # Count patterns available
            pattern_count = self._count_available_patterns()

            # Update last extraction time
            self.last_extraction = datetime.now()

            # Update health status
            self.health_status['status'] = 'healthy'
            self.health_status['last_extraction'] = self.last_extraction.isoformat()
            self.health_status['patterns_available'] = pattern_count
            self.health_status['extraction_errors'] = 0

            logger.info(f"✅ Hourly extraction complete: {pattern_count} patterns available")

        except Exception as e:
            logger.error(f"❌ Hourly extraction failed: {e}")
            self.health_status['extraction_errors'] += 1
            self.health_status['status'] = 'degraded' if self.health_status['extraction_errors'] < 3 else 'unhealthy'

    def daily_hygiene(self):
        """Run daily hygiene operations at 18:00"""
        logger.info("🧹 Starting daily hygiene operations...")

        try:
            # Refresh intelligence database (quality scan + snippet extraction)
            if self.refresh_daily:
                self._refresh_quality_intelligence()

            # Database optimization
            self._optimize_database()

            # Pattern quality check
            self._verify_pattern_quality()

            # Bulk citation freshness verification
            self._verify_pattern_freshness_bulk()

            # Tag auto-refresh from completed dispatches
            self._refresh_tags_from_completed_dispatches()

            # Cleanup old data
            self._cleanup_old_data()

            # Run learning cycle
            self.run_learning_cycle()

            # Update cache rankings
            self.cached_intelligence.update_pattern_rankings()

            # Update last hygiene time
            self.last_daily_hygiene = datetime.now()

            logger.info("✅ Daily hygiene complete")

        except Exception as e:
            logger.error(f"❌ Daily hygiene failed: {e}")

    def _count_available_patterns(self) -> int:
        """Count total patterns available in database"""
        try:
            # Check if database is loaded, if not, try to load it
            if not self.gatherer.quality_db:
                # Try to load the database
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
        """Optimize SQLite database"""
        try:
            if self.gatherer.quality_db:
                self.gatherer.quality_db.execute("VACUUM")
                self.gatherer.quality_db.execute("ANALYZE")
                logger.info("Database optimization complete")
        except Exception as e:
            logger.error(f"Database optimization failed: {e}")

    def _refresh_quality_intelligence(self):
        """Run quality scanner + snippet extractor to refresh patterns"""
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
        """Verify pattern quality metrics"""
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
        """Cleanup old data from database"""
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

    def run_learning_cycle(self):
        """Run the learning loop to update pattern confidence"""
        logger.info("🔄 Starting learning cycle...")

        try:
            # Run the daily learning cycle
            report = self.learning_loop.daily_learning_cycle()

            # Update health status with learning metrics
            self.health_status['learning_stats'] = report.get('statistics', {})
            self.health_status['pattern_metrics'] = report.get('pattern_metrics', {})

            # Update last learning cycle time
            self.last_learning_cycle = datetime.now()

            logger.info(f"✅ Learning cycle complete: {report['statistics'].get('confidence_adjustments', 0)} confidence adjustments made")

        except Exception as e:
            logger.error(f"❌ Learning cycle failed: {e}")

    # ── PR Auto-Discovery ──────────────────────────────────────────
    # No config file needed. PRs are discovered from:
    #   1. Track gate names matching pattern gate_pr{N}_{description}
    #   2. Receipt history (task_complete events referencing PRs)
    #   3. Dispatch IDs containing PR references
    # ──────────────────────────────────────────────────────────────

    _PR_GATE_RE = re.compile(r"gate_pr(\d+)_(.*)", re.IGNORECASE)
    _PR_REF_RE = re.compile(r"PR[- ]?(\d+)", re.IGNORECASE)

    def _auto_discover_prs(self, brief: dict) -> dict:
        """Auto-discover PRs from tracks, track history, receipts, and dispatches.

        Returns dict: { pr_num: { id, num, description, gate_trigger, receipt_done } }
        """
        discovered = {}  # pr_num (int) -> pr_info dict

        def _register_gate(gate: str):
            """Register a PR from a gate name if it matches the pattern."""
            m = self._PR_GATE_RE.match(gate)
            if m:
                pr_num = int(m.group(1))
                desc = m.group(2).replace("_", " ").strip().title()
                if pr_num not in discovered:
                    discovered[pr_num] = {
                        "id": f"PR{pr_num}",
                        "num": pr_num,
                        "description": desc,
                        "gate_trigger": gate,
                        "receipt_done": False,
                    }

        # ── Source 1: Current track gates ──
        for track_id, track_data in brief.get("tracks", {}).items():
            _register_gate(track_data.get("current_gate", ""))

        # ── Source 1b: Track history from progress_state.yaml ──
        progress_file = self._find_state_file("progress_state.yaml")
        if progress_file and progress_file.exists():
            try:
                import yaml
                with open(progress_file, "r") as f:
                    progress = yaml.safe_load(f) or {}
                for track_id, track_data in progress.get("tracks", {}).items():
                    _register_gate(track_data.get("current_gate", ""))
                    for entry in track_data.get("history", []):
                        _register_gate(entry.get("gate", ""))
            except Exception as e:
                logger.error(f"Error reading progress_state.yaml: {e}")

        # ── Source 2: Receipt history ──
        receipts_file = self._find_state_file("t0_receipts.ndjson")
        if receipts_file and receipts_file.exists():
            try:
                with open(receipts_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            receipt = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Extract PR numbers from multiple fields
                        searchable = " ".join(
                            str(receipt.get(k, ""))
                            for k in ("type", "title", "gate", "task_id", "dispatch_id")
                        )
                        for m in self._PR_REF_RE.finditer(searchable):
                            pr_num = int(m.group(1))
                            is_done = (
                                receipt.get("event_type") == "task_complete"
                                and receipt.get("status") in ("success", "unknown")
                            )

                            if pr_num not in discovered:
                                # Build description from receipt title if available
                                title = receipt.get("title", "")
                                desc = self._PR_REF_RE.sub("", title).strip(" -–—:").strip()
                                if not desc:
                                    desc = f"PR {pr_num}"
                                discovered[pr_num] = {
                                    "id": f"PR{pr_num}",
                                    "num": pr_num,
                                    "description": desc,
                                    "gate_trigger": f"gate_pr{pr_num}_unknown",
                                    "receipt_done": is_done,
                                }
                            elif is_done:
                                discovered[pr_num]["receipt_done"] = True

                            # Enrich gate_trigger from receipt gate field if we only had a stub
                            receipt_gate = receipt.get("gate", "")
                            gm = self._PR_GATE_RE.match(receipt_gate)
                            if gm and int(gm.group(1)) == pr_num:
                                discovered[pr_num]["gate_trigger"] = receipt_gate
                                if not discovered[pr_num]["description"] or discovered[pr_num]["description"] == f"PR {pr_num}":
                                    discovered[pr_num]["description"] = gm.group(2).replace("_", " ").strip().title()

            except Exception as e:
                logger.error(f"Error scanning receipts for PR discovery: {e}")

        # ── Source 3: Dispatch IDs in brief ──
        for receipt in brief.get("recent_receipts", []):
            dispatch_id = receipt.get("dispatch_id", "")
            for m in self._PR_REF_RE.finditer(dispatch_id):
                pr_num = int(m.group(1))
                if pr_num not in discovered:
                    discovered[pr_num] = {
                        "id": f"PR{pr_num}",
                        "num": pr_num,
                        "description": f"PR {pr_num}",
                        "gate_trigger": f"gate_pr{pr_num}_unknown",
                        "receipt_done": False,
                    }

        return discovered

    def _determine_pr_statuses(self, discovered: dict, tracks: dict) -> dict:
        """Determine status for all discovered PRs.

        Priority:
        1. Live track gate match (working → in_progress, idle → done)
        2. Receipt history completion → done
        3. Dependency inference (if higher PR active, lower numbered deps are done)
        4. Default → pending
        """
        status_map = {}

        # First pass: track data + receipt signals
        for pr_num, pr_info in discovered.items():
            pr_id = pr_info["id"]
            gate = pr_info["gate_trigger"]
            status_map[pr_id] = "pending"

            # Check live track data (highest priority)
            for track_id, track_data in tracks.items():
                current_gate = track_data.get("current_gate", "")
                track_status = track_data.get("status", "")

                if current_gate == gate:
                    if track_status in ("working", "active"):
                        status_map[pr_id] = "in_progress"
                    elif track_status == "idle":
                        status_map[pr_id] = "done"
                    break

            # Fallback: receipt history
            if status_map[pr_id] == "pending" and pr_info.get("receipt_done"):
                status_map[pr_id] = "done"

        # Second pass: sequential inference
        # If PR N is in_progress or done, all PRs with lower numbers
        # that share the same track lineage are implicitly done
        sorted_nums = sorted(discovered.keys())
        for pr_num in sorted_nums:
            pr_id = discovered[pr_num]["id"]
            if status_map[pr_id] in ("in_progress", "done"):
                # Mark all lower-numbered PRs on the same track lineage as done
                gate = discovered[pr_num]["gate_trigger"]
                # Find which track this PR is on
                pr_track = None
                for track_id, track_data in tracks.items():
                    if track_data.get("current_gate", "") == gate:
                        pr_track = track_id
                        break

                for lower_num in sorted_nums:
                    if lower_num >= pr_num:
                        break
                    lower_id = discovered[lower_num]["id"]
                    lower_gate = discovered[lower_num]["gate_trigger"]

                    # Same track lineage: check if the lower PR's gate was on the same track
                    if pr_track:
                        # If this track is currently on a higher PR, lower ones are done
                        if status_map[lower_id] == "pending":
                            # Check if the lower gate belongs to the same track pattern
                            lower_gate_match = self._PR_GATE_RE.match(lower_gate)
                            current_gate_match = self._PR_GATE_RE.match(gate)
                            if lower_gate_match and current_gate_match:
                                status_map[lower_id] = "done"

        return status_map

    def _build_pr_queue(self, brief: dict, existing_dashboard: dict) -> dict:
        """Build PR queue from auto-discovered data with persistence.

        Merges newly discovered PRs with previously persisted registry so
        PRs that were on a track gate in the past are not lost when the
        track moves forward.
        """
        tracks = brief.get("tracks", {})

        # Auto-discover PRs from current data sources
        discovered = self._auto_discover_prs(brief)

        # ── Merge with persisted registry ──
        # The registry lives in dashboard._pr_registry and accumulates
        # all PRs ever discovered. New data enriches but never removes.
        registry = existing_dashboard.get("_pr_registry", {})

        # Import persisted entries (keyed by pr_num as string)
        for num_str, pr_info in registry.items():
            pr_num = int(num_str)
            if pr_num not in discovered:
                discovered[pr_num] = pr_info
            else:
                # Enrich: keep better description if the new one is generic
                if discovered[pr_num]["description"] == f"PR {pr_num}":
                    discovered[pr_num]["description"] = pr_info.get("description", discovered[pr_num]["description"])
                # Keep receipt_done if it was ever true
                if pr_info.get("receipt_done"):
                    discovered[pr_num]["receipt_done"] = True

        if not discovered:
            return {
                "active_feature": "Active Development",
                "total_prs": 0,
                "completed_prs": 0,
                "progress_percent": 0,
                "prs": [],
                "_pr_registry": {},
            }

        # Determine statuses
        status_map = self._determine_pr_statuses(discovered, tracks)

        # Build sorted PR list
        all_prs = []
        sorted_nums = sorted(discovered.keys())

        for pr_num in sorted_nums:
            pr_info = discovered[pr_num]
            pr_id = pr_info["id"]
            status = status_map.get(pr_id, "pending")

            # Auto-infer sequential dependencies: PR N depends on nearest lower PR
            deps = []
            if pr_num > 1:
                prev_num = None
                for n in sorted_nums:
                    if n < pr_num:
                        prev_num = n
                    else:
                        break
                if prev_num is not None and prev_num in discovered:
                    deps = [discovered[prev_num]["id"]]

            # Blocked: pending + dependencies not done
            blocked = False
            if status == "pending" and deps:
                blocked = any(status_map.get(d) != "done" for d in deps)

            all_prs.append({
                "id": pr_id,
                "description": pr_info["description"],
                "status": status,
                "deps": deps,
                "blocked": blocked,
            })

        completed_count = sum(1 for pr in all_prs if pr["status"] == "done")
        total_count = len(all_prs)

        # Persist registry for next cycle (keyed by pr_num string for JSON compat)
        new_registry = {str(k): v for k, v in discovered.items()}

        return {
            "active_feature": "Active Development",
            "total_prs": total_count,
            "completed_prs": completed_count,
            "progress_percent": int((completed_count / total_count * 100)) if total_count > 0 else 0,
            "prs": all_prs,
            "_pr_registry": new_registry,
        }

    def write_health_status(self):
        """Legacy dashboard projection (disabled by default for single-writer ownership)."""
        if not self.dashboard_write_enabled:
            return
        try:
            # Load current dashboard
            dashboard = {}
            dashboard_source = self._find_state_file("dashboard_status.json")
            if dashboard_source and dashboard_source.exists():
                with open(dashboard_source, 'r') as f:
                    dashboard = json.load(f)

            # Load t0_brief.json for terminal and track data
            t0_brief_file = self._find_state_file("t0_brief.json")

            if t0_brief_file and t0_brief_file.exists():
                with open(t0_brief_file, 'r') as f:
                    brief = json.load(f)

                # Update terminals from brief
                terminals = {"T0": {
                    "status": "active",
                    "gate": "ORCHESTRATION",
                    "type": "ORCHESTRATOR",
                    "ready": True
                }}

                for tid, tdata in brief.get("terminals", {}).items():
                    terminals[tid] = {
                        "status": "active" if tdata.get("status") in ["working", "idle"] else "offline",
                        "gate": tdata.get("track", ""),
                        "current_task": tdata.get("current_task", ""),
                        "ready": tdata.get("ready", False),
                        "type": "WORKER"
                    }
                dashboard["terminals"] = terminals

                # Build PR queue via auto-discovery (persists registry in dashboard)
                pr_result = self._build_pr_queue(brief, dashboard)
                # Store registry separately, keep pr_queue clean for dashboard
                dashboard["_pr_registry"] = pr_result.pop("_pr_registry", {})
                dashboard["pr_queue"] = pr_result

                # Build open items from terminals with PR correlation
                open_items = []
                pr_queue = dashboard["pr_queue"]
                # Build gate→PR lookup from discovered PRs
                gate_to_pr_id = {}
                for pr in pr_queue.get("prs", []):
                    # Find gate from tracks that match this PR
                    for track_id, track_data in brief.get("tracks", {}).items():
                        gate = track_data.get("current_gate", "")
                        m = self._PR_GATE_RE.match(gate)
                        if m and f"PR{m.group(1)}" == pr["id"]:
                            gate_to_pr_id[gate] = pr["id"]

                for tid, tdata in brief.get("terminals", {}).items():
                    if tdata.get("current_task"):
                        severity = "warning" if tdata.get("status") == "working" else "info"
                        # Correlate task with PR via gate matching
                        pr_id = None
                        track_id = tdata.get("track", "")
                        if track_id:
                            track_info = brief.get("tracks", {}).get(track_id, {})
                            current_gate = track_info.get("current_gate", "")
                            pr_id = gate_to_pr_id.get(current_gate)

                        open_items.append({
                            "id": tdata["current_task"],
                            "title": f"{tdata.get('current_task', 'Task')} ({tid})",
                            "severity": severity,
                            "pr_id": pr_id
                        })

                dashboard["open_items"] = {
                    "open_count": len(open_items),
                    "summary": {
                        "open_count": len(open_items),
                        "blocker_count": 0,
                        "warn_count": sum(1 for item in open_items if item["severity"] == "warning"),
                        "info_count": sum(1 for item in open_items if item["severity"] == "info")
                    },
                    "top_blockers": [],
                    "open_items": open_items
                }

                # Copy other fields from brief
                dashboard["tracks"] = brief.get("tracks", {})
                dashboard["recent_receipts"] = brief.get("recent_receipts", [])
                dashboard["queues"] = brief.get("queues", {})

            # Update intelligence section
            dashboard['intelligence_daemon'] = {
                'status': self.health_status['status'],
                'last_extraction': self.health_status['last_extraction'],
                'patterns_available': self.health_status['patterns_available'],
                'extraction_errors': self.health_status['extraction_errors'],
                'uptime_seconds': self.health_status['uptime_seconds'],
                'last_update': datetime.now().isoformat()
            }

            # Write canonical first; mirror legacy only in rollback mode.
            self._write_json_atomic(self.dashboard_file, dashboard)
            if self.rollback_mode and self.legacy_dashboard_file != self.dashboard_file:
                self._write_json_atomic(self.legacy_dashboard_file, dashboard)

            self.health_status['last_health_update'] = datetime.now()

        except Exception as e:
            logger.error(f"Failed to write health status: {e}")

    def write_intelligence_health(self):
        """Write to dedicated intelligence health file (PR #8 Fix - avoid dashboard races)"""
        import os

        health_data = {
            'timestamp': datetime.now().isoformat(),
            'daemon_running': True,
            'daemon_pid': os.getpid(),
            'patterns_available': self.health_status.get('patterns_available', 0),
            'last_extraction': self.health_status.get('last_extraction', 'never'),
            'extraction_errors': self.health_status.get('extraction_errors', 0),
            'uptime_seconds': self.health_status.get('uptime_seconds', 0),
            'status': self.health_status.get('status', 'unknown')
        }

        try:
            self._write_json_atomic(self.intelligence_health_file, health_data)
            if self.rollback_mode and self.legacy_intelligence_health_file != self.intelligence_health_file:
                self._write_json_atomic(self.legacy_intelligence_health_file, health_data)

        except Exception as e:
            logger.error(f"Failed to write intelligence health file: {e}")

    def run(self):
        """Main daemon loop"""
        logger.info("=" * 60)
        logger.info("VNX Intelligence Daemon - STARTED")
        logger.info("=" * 60)
        logger.info(f"Hourly extraction interval: {self.extraction_interval}s")
        logger.info(f"Daily hygiene time: {self.daily_hygiene_hour}:00")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 60)

        # Initial extraction on startup
        self.hourly_extraction()
        self.write_intelligence_health()  # PR #8 Fix - dedicated health file
        self.write_health_status()

        start_time = datetime.now()

        while self.running:
            try:
                # Update uptime
                self.health_status['uptime_seconds'] = int((datetime.now() - start_time).total_seconds())

                # Hourly extraction
                if self.should_extract_hourly():
                    self.hourly_extraction()

                # Daily hygiene at 18:00
                if self.should_run_daily_hygiene():
                    self.daily_hygiene()

                # Governance digest (every VNX_DIGEST_INTERVAL seconds, default 5 min)
                if self.digest_runner.should_run():
                    self.digest_runner.run_once()

                # Health reporting (every minute)
                self.write_intelligence_health()  # Write to dedicated file (PR #8 Fix)
                self.write_health_status()  # Update dashboard every cycle for live sync

                # Sleep for 60 seconds
                time.sleep(60)

            except Exception as e:
                logger.error(f"Error in daemon loop: {e}")
                self.health_status['status'] = 'error'
                time.sleep(60)  # Continue after error

        # Graceful shutdown
        logger.info("Intelligence Daemon shutting down...")
        self.health_status['status'] = 'stopped'
        self.write_health_status()

        # Close database connection
        if self.gatherer.quality_db:
            self.gatherer.quality_db.close()

        logger.info("Shutdown complete")


def main():
    """Entry point for intelligence daemon"""
    paths = ensure_env()
    singleton_lock = enforce_python_singleton(
        "intelligence_daemon",
        paths["VNX_LOCKS_DIR"],
        paths["VNX_PIDS_DIR"],
        logger.info,
    )
    if singleton_lock is None:
        return

    daemon = IntelligenceDaemon()
    daemon.run()


if __name__ == '__main__':
    main()
