#!/usr/bin/env python3
"""
Heartbeat ACK Monitor Daemon - Production Mode
==============================================
Runs as a background daemon listening for dispatch notifications
and automatically generating ACK receipts via heartbeat detection.

Usage: python3 heartbeat_ack_monitor_daemon.py [--receipts /path/to/receipts.ndjson]
"""

import sys
import os
import logging
from pathlib import Path

# Add script directory to path to import heartbeat_ack_monitor and vnx_paths
script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir))
sys.path.insert(0, str(script_dir / "lib"))

try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

from heartbeat_ack_monitor import HeartbeatACKMonitor

paths = ensure_env()
log_dir = Path(paths["VNX_LOGS_DIR"])
log_dir.mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_dir / "heartbeat_ack_daemon.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


def main():
    """Run heartbeat monitor as production daemon with socket server"""

    # Set environment variables for production mode
    if 'RECEIPT_FILE' not in os.environ:
        os.environ['RECEIPT_FILE'] = str(Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson")

    if 'SHADOW_MODE' not in os.environ:
        os.environ['SHADOW_MODE'] = 'false'

    logger.info("=" * 60)
    logger.info("Heartbeat ACK Monitor Daemon - PRODUCTION MODE")
    logger.info("=" * 60)
    logger.info(f"Receipts file: {os.environ['RECEIPT_FILE']}")
    logger.info(f"Shadow mode: {os.environ['SHADOW_MODE']}")

    # Create monitor instance (reads config from environment)
    monitor = HeartbeatACKMonitor()

    # Start socket server for dispatch notifications
    socket_path = '/tmp/heartbeat_ack_monitor.sock'
    monitor.start_socket_server(socket_path)

    logger.info("[DAEMON] Socket server started, ready to receive dispatch notifications")
    logger.info("[DAEMON] Dispatcher will notify via: python3 notify_dispatch.py")
    logger.info("[DAEMON] Press Ctrl+C to stop")

    try:
        # Keep daemon running
        import signal
        import time

        def signal_handler(sig, frame):
            logger.info("[DAEMON] Received shutdown signal, exiting...")
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Daemon main loop - just sleep and let socket server handle requests
        while True:
            time.sleep(60)  # Wake up periodically to check for signals
            logger.debug("[DAEMON] Heartbeat - daemon alive")

    except KeyboardInterrupt:
        logger.info("[DAEMON] Keyboard interrupt, shutting down...")
    except Exception as e:
        logger.error(f"[DAEMON] Unexpected error: {e}")
        raise


if __name__ == '__main__':
    main()
