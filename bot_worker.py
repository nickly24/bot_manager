"""
Bot Worker — entry point for a per-user subprocess.

Launched by BotManager via:
    python bot_worker.py --user-id 42

Lifecycle:
  1. Parse args, set up signal handlers
  2. Initialize TradingEngine (load config, keys, connect to OKX)
  3. Enter async main loop (WebSocket tickers → spread → trade)
  4. On SIGTERM  → save state, exit gracefully
  5. On SIGUSR1 → close all positions
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

# Ensure bot_manager package is importable when run as subprocess
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker:%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("bot_worker")


def main() -> None:
    parser = argparse.ArgumentParser(description="PairTrading Bot Worker")
    parser.add_argument("--user-id", type=int, required=True)
    args = parser.parse_args()

    user_id = args.user_id
    log.info("Starting worker for user_id=%s", user_id)

    from bot_manager.trading.engine import TradingEngine

    engine = TradingEngine(user_id)

    # ---- Signal handlers ------------------------------------------------

    def on_sigterm(signum, frame):
        log.info("SIGTERM received — saving state and exiting")
        engine.save_state_and_stop()
        sys.exit(0)

    def on_sigusr1(signum, frame):
        log.info("SIGUSR1 received — requesting close positions")
        engine.request_close_positions()

    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGUSR1, on_sigusr1)

    # ---- Init & run -----------------------------------------------------

    try:
        engine.initialize()
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — saving state")
        engine.save_state_and_stop()
        sys.exit(0)
    except Exception:
        log.exception("Fatal error in worker user_id=%s", user_id)
        engine.save_state_and_stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
