"""
Flask HTTP server wrapping the BotManager.

Serves two audiences:
  1. Main backend (Flask API on :5000) — sends commands over HTTP
  2. CLI utility (cli.py) — same HTTP API from the terminal

Run:
    python -m bot_manager.server
"""

from __future__ import annotations

import functools
import logging
import os
import signal
import sys
from datetime import datetime

from flask import Flask, jsonify, request

from bot_manager.config import Config
from bot_manager.manager import BotManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("server")

app = Flask(__name__)
mgr: BotManager | None = None


# -----------------------------------------------------------------------
# Auth middleware (simple shared secret)
# -----------------------------------------------------------------------

def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Manager-Key", "")
        if token != Config.MANAGER_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


def _ok(data: dict | list, code: int = 200):
    return jsonify({"ok": True, "data": data}), code


def _err(message: str, code: int = 400):
    return jsonify({"ok": False, "error": message}), code


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------

@app.get("/api/health")
def health():
    return _ok({
        "status": "running",
        "pid": os.getpid(),
        "workers": mgr.get_overview() if mgr else {},
    })


@app.get("/api/workers")
@require_auth
def list_workers():
    return _ok(mgr.list_workers(), 200)


@app.get("/api/workers/<int:user_id>")
@require_auth
def get_worker(user_id: int):
    info = mgr.get_worker_status(user_id)
    if info is None:
        return _err(f"Worker для user_id={user_id} не найден", 404)
    return _ok(info)


@app.post("/api/workers/<int:user_id>/start")
@require_auth
def start_worker(user_id: int):
    try:
        result = mgr.start_bot(user_id)
        return _ok(result)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc), 422)
    except Exception as exc:
        log.exception("start_bot failed for user_id=%s", user_id)
        return _err(str(exc), 500)


@app.post("/api/workers/<int:user_id>/stop")
@require_auth
def stop_worker(user_id: int):
    try:
        result = mgr.stop_bot(user_id)
        return _ok(result)
    except Exception as exc:
        log.exception("stop_bot failed for user_id=%s", user_id)
        return _err(str(exc), 500)


@app.post("/api/workers/<int:user_id>/restart")
@require_auth
def restart_worker(user_id: int):
    try:
        result = mgr.restart_bot(user_id)
        return _ok(result)
    except (ValueError, PermissionError) as exc:
        return _err(str(exc), 422)
    except Exception as exc:
        log.exception("restart_bot failed for user_id=%s", user_id)
        return _err(str(exc), 500)


@app.post("/api/workers/<int:user_id>/close-positions")
@require_auth
def close_positions(user_id: int):
    try:
        result = mgr.close_positions(user_id)
        return _ok(result)
    except RuntimeError as exc:
        return _err(str(exc), 422)
    except Exception as exc:
        log.exception("close_positions failed for user_id=%s", user_id)
        return _err(str(exc), 500)


@app.get("/api/logs/<int:user_id>")
@require_auth
def get_logs(user_id: int):
    from bot_manager.db.connection import Database
    from bot_manager.db import queries as Q

    limit = request.args.get("limit", 50, type=int)
    db = Database()
    rows = db.execute(Q.SELECT_EVENTS, (user_id, limit))
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = v.isoformat()
    return _ok(rows)


@app.post("/api/shutdown")
@require_auth
def shutdown_manager():
    """Graceful shutdown via API (alternative to SIGTERM)."""
    def _do_shutdown():
        import time
        time.sleep(0.5)
        mgr.shutdown()
        os.kill(os.getpid(), signal.SIGTERM)

    import threading
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return _ok({"status": "shutting_down"})


# -----------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------

def create_app() -> Flask:
    global mgr
    mgr = BotManager()

    recovered = mgr.recover()
    log.info("Recovery complete: %d bots restored", len(recovered))

    mgr.start_background()

    def handle_sigterm(signum, frame):
        log.info("SIGTERM received — shutting down")
        mgr.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    return app


if __name__ == "__main__":
    application = create_app()
    log.info(
        "Manager server starting on %s:%s",
        Config.MANAGER_HOST,
        Config.MANAGER_PORT,
    )
    application.run(
        host=Config.MANAGER_HOST,
        port=Config.MANAGER_PORT,
        debug=False,
        use_reloader=False,
    )
