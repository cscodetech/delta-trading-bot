"""
Railway entry point — runs both the trading bot and dashboard in one process.
"""

import threading
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("railway")


def run_bot():
    """Run the trading bot in a background thread."""
    try:
        from bot import TradingBot
        bot = TradingBot()
        bot.run()
    except Exception as e:
        log.error(f"Bot crashed: {e}", exc_info=True)


def run_dashboard():
    """Run the Flask dashboard (blocking)."""
    from dashboard import app, monitor
    monitor.start()
    port = int(os.getenv("PORT", 5050))
    log.info(f"Starting dashboard on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True, name="TradingBot")
    bot_thread.start()
    log.info("Trading bot started in background thread")

    # Run dashboard in main thread (Railway exposes this port)
    run_dashboard()
