"""
Railway entry point — runs the Flask dashboard (multi-user).

Notes:
- Start the trading bot per-user from the Dashboard UI (it runs as a subprocess with BOT_USER_ID).
- Auto-close monitor is OFF by default and can be enabled per-user from the UI.
"""

import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("railway")


def main():
    from dashboard import app
    try:
        from waitress import serve
    except Exception:
        serve = None

    port = int(os.getenv("PORT", "5050"))
    log.info(f"Starting dashboard on port {port}")

    if serve:
        serve(app, host="0.0.0.0", port=port, threads=8)
    else:
        app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
