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
import json
import time
import signal
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List

# Add scripts directory to path for imports
script_dir = Path(__file__).parent
import sys
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
    from pr_discovery import PRDiscovery
    from intelligence_dashboard import DashboardBuilder
    from intelligence_hygiene import HygieneRunner
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
    """Periodic governance digest builder — writes governance_digest.json every ``interval`` seconds.
    Reads F18 extractors. Interval from VNX_DIGEST_INTERVAL env var (default 300 s).
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

    def should_run(self) -> bool:
        """Return True if the interval has elapsed since the last run."""
        if self.last_run is None:
            return True
        elapsed = (datetime.now() - self.last_run).total_seconds()
        return elapsed >= self.interval

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
            "task_timeout": "fail",
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

                    # Handle review_gate_result events with embedded status
                    if event_type == "review_gate_result":
                        if not gate:
                            continue
                        embedded_status = receipt.get("status", "")
                        if embedded_status in ("pass", "passed", "success"):
                            mapped_status = "pass"
                        elif embedded_status in ("fail", "failed"):
                            mapped_status = "fail"
                        else:
                            continue
                        results.append({
                            "gate_id": gate,
                            "status": mapped_status,
                            "feature_id": receipt.get("feature_id", ""),
                            "pr_id": receipt.get("pr", receipt.get("pr_id", "")),
                            "dispatch_id": receipt.get("dispatch_id", ""),
                            "reason": receipt.get("error", receipt.get("reason", receipt.get("summary", ""))),
                        })
                        continue

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

    def run_once(self) -> bool:
        """Execute one digest cycle: load signals, collect, build, write JSON.
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

            # Persist signals to quality_intelligence.db so intelligence_selector
            # can query populated tables (bridges governance → DB).
            try:
                from intelligence_persist import persist_signals_to_db
                db_path = self.state_dir / "quality_intelligence.db"
                if db_path.exists() and signals:
                    persist_result = persist_signals_to_db(signals, db_path)
                    logger.info(
                        "GovernanceDigestRunner: persisted signals to DB "
                        "(patterns=%d, antipatterns=%d, metadata=%d)",
                        persist_result["patterns_upserted"],
                        persist_result["antipatterns_upserted"],
                        persist_result["metadata_updated"],
                    )
            except Exception as exc:
                logger.warning("GovernanceDigestRunner: signal persistence failed (non-fatal): %s", exc)

            digest = build_digest(signals)

            self.last_run = datetime.now()
            logger.info(
                "GovernanceDigestRunner: processed %d signals, %d recurring patterns",
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
        with tempfile.NamedTemporaryFile(mode="w", dir=self.digest_path.parent, delete=False, suffix=".tmp") as tmp:
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
        self.rollback_mode = _rollback_mode_enabled()
        self.dashboard_write_enabled = os.getenv("VNX_INTELLIGENCE_DASHBOARD_WRITE", "0") == "1"
        self.refresh_daily = os.getenv("VNX_DAILY_INTEL_REFRESH", "1") == "1"

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

        # Health tracking
        self.health_status = {
            'status': 'starting',
            'last_extraction': None,
            'patterns_available': 0,
            'extraction_errors': 0,
            'uptime_seconds': 0,
            'last_health_update': None
        }

        # Decomposed components
        self.pr_discovery = PRDiscovery(self.compat_state_dirs)
        self.dashboard_builder = DashboardBuilder(
            state_dir=self.state_dir,
            legacy_state_dir=self.legacy_state_dir,
            rollback_mode=self.rollback_mode,
            dashboard_write_enabled=self.dashboard_write_enabled,
            pr_discovery=self.pr_discovery,
            find_state_file=self._find_state_file,
            health_status=self.health_status,
        )
        self.hygiene_runner = HygieneRunner(
            gatherer=self.gatherer,
            project_root=self.project_root,
            vnx_dir=self.vnx_dir,
            refresh_daily=self.refresh_daily,
            find_state_file=self._find_state_file,
        )

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
        if self.last_daily_hygiene:
            if self.last_daily_hygiene.date() == now.date():
                return False
        return now.hour == self.daily_hygiene_hour

    def hourly_extraction(self):
        """Run hourly intelligence extraction"""
        logger.info("🔄 Starting hourly intelligence extraction...")

        try:
            pattern_count = self.hygiene_runner._count_available_patterns()

            self.last_extraction = datetime.now()
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
        self.hygiene_runner.daily_hygiene()
        self.run_learning_cycle()
        self.cached_intelligence.update_pattern_rankings()
        self.last_daily_hygiene = datetime.now()

    def run_learning_cycle(self):
        """Run the learning loop to update pattern confidence"""
        logger.info("🔄 Starting learning cycle...")

        try:
            report = self.learning_loop.daily_learning_cycle()
            self.health_status['learning_stats'] = report.get('statistics', {})
            self.health_status['pattern_metrics'] = report.get('pattern_metrics', {})
            self.last_learning_cycle = datetime.now()
            logger.info(f"✅ Learning cycle complete: {report['statistics'].get('confidence_adjustments', 0)} confidence adjustments made")

        except Exception as e:
            logger.error(f"❌ Learning cycle failed: {e}")

    def write_health_status(self):
        """Delegate to dashboard builder."""
        self.dashboard_builder.write_health_status()

    def write_intelligence_health(self):
        """Delegate to dashboard builder."""
        self.dashboard_builder.write_intelligence_health()

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
        self.dashboard_builder.write_intelligence_health()  # PR #8 Fix - dedicated health file
        self.dashboard_builder.write_health_status()

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
                self.dashboard_builder.write_intelligence_health()  # Write to dedicated file (PR #8 Fix)
                self.dashboard_builder.write_health_status()  # Update dashboard every cycle for live sync

                try:
                    from health_beacon import HealthBeacon
                    _hb_paths = ensure_env()
                    HealthBeacon(
                        Path(_hb_paths["VNX_DATA_DIR"]),
                        "intelligence_daemon",
                        expected_interval_seconds=300,
                    ).heartbeat(
                        status="ok",
                        details={
                            "uptime_seconds": self.health_status.get("uptime_seconds", 0),
                            "daemon_status": self.health_status.get("status", "running"),
                        },
                    )
                except Exception:
                    pass

                # Sleep for 60 seconds
                time.sleep(60)

            except Exception as e:
                logger.error(f"Error in daemon loop: {e}")
                self.health_status['status'] = 'error'
                try:
                    from health_beacon import HealthBeacon
                    _hb_paths = ensure_env()
                    HealthBeacon(
                        Path(_hb_paths["VNX_DATA_DIR"]),
                        "intelligence_daemon",
                        expected_interval_seconds=300,
                    ).heartbeat(status="fail", details={"error": str(e)})
                except Exception:
                    pass
                time.sleep(60)  # Continue after error

        # Graceful shutdown
        logger.info("Intelligence Daemon shutting down...")
        self.health_status['status'] = 'stopped'
        self.dashboard_builder.write_health_status()

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
