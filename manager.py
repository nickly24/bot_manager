"""
BotManager — core process management logic.

Responsibilities:
  - Start / stop / restart bot worker sub-processes
  - Periodic health-check (hung detection, crash-loop protection)
  - Recover bots whose desired_state='running' after a restart
  - Heartbeat to MySQL for external monitoring
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import Config
from db.connection import Database
from db import queries as Q
from models import WorkerInfo

log = logging.getLogger("manager")

WORKER_LOGS_DIR = Path(__file__).resolve().parent / "logs"
WORKER_LOGS_DIR.mkdir(exist_ok=True)


class BotManager:

    def __init__(self) -> None:
        self.db = Database()
        self.workers: dict[int, WorkerInfo] = {}
        self._running = True
        self._lock = threading.Lock()
        self._bg_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Background loop (health-check + heartbeat)
    # ------------------------------------------------------------------

    def start_background(self) -> None:
        """Launch the health-check loop in a daemon thread."""
        self._running = True
        self._bg_thread = threading.Thread(
            target=self._loop, daemon=True, name="manager-loop"
        )
        self._bg_thread.start()
        log.info("Background health-check loop started")

    def stop_background(self) -> None:
        self._running = False
        if self._bg_thread and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=5)

    def _loop(self) -> None:
        while self._running:
            try:
                self._health_check()
                self._update_heartbeat()
            except Exception:
                log.exception("Error in manager loop")
            time.sleep(Config.HEALTH_CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Recovery on startup
    # ------------------------------------------------------------------

    def recover(self) -> list[int]:
        """Start workers for all bots with desired_state='running'."""
        rows = self.db.execute(Q.SELECT_RUNNING_BOTS)
        recovered: list[int] = []
        for row in rows:
            uid = row["user_id"]
            try:
                self._start_worker(uid)
                self._log_event(uid, "info", "Бот восстановлен после перезапуска менеджера")
                recovered.append(uid)
            except Exception:
                log.exception("Failed to recover bot for user %s", uid)
        log.info("Recovered %d bots: %s", len(recovered), recovered)
        return recovered

    # ------------------------------------------------------------------
    # Public API (called from Flask endpoints)
    # ------------------------------------------------------------------

    def start_bot(self, user_id: int) -> dict:
        with self._lock:
            self._validate_user(user_id)
            if user_id in self.workers and self.workers[user_id].alive:
                return {"status": "already_running", "pid": self.workers[user_id].pid}
            cmd_id = self._audit("start", user_id)
            try:
                self._start_worker(user_id)
                self.db.execute(Q.FINISH_COMMAND, (cmd_id,))
                return {"status": "started", "pid": self.workers[user_id].pid}
            except Exception as exc:
                self.db.execute(Q.FAIL_COMMAND, (str(exc), cmd_id))
                raise

    def stop_bot(self, user_id: int) -> dict:
        with self._lock:
            cmd_id = self._audit("stop", user_id)
            try:
                result = self._stop_worker(user_id)
                self.db.execute(Q.FINISH_COMMAND, (cmd_id,))
                return result
            except Exception as exc:
                self.db.execute(Q.FAIL_COMMAND, (str(exc), cmd_id))
                raise

    def restart_bot(self, user_id: int) -> dict:
        with self._lock:
            cmd_id = self._audit("restart", user_id)
            try:
                self._stop_worker(user_id)
                time.sleep(2)
                self._start_worker(user_id)
                self.db.execute(Q.FINISH_COMMAND, (cmd_id,))
                return {"status": "restarted", "pid": self.workers[user_id].pid}
            except Exception as exc:
                self.db.execute(Q.FAIL_COMMAND, (str(exc), cmd_id))
                raise

    def close_positions(self, user_id: int) -> dict:
        with self._lock:
            cmd_id = self._audit("close_positions", user_id)
            try:
                if user_id not in self.workers or not self.workers[user_id].alive:
                    raise RuntimeError("Бот не запущен")
                os.kill(self.workers[user_id].pid, signal.SIGUSR1)
                self.db.execute(Q.FINISH_COMMAND, (cmd_id,))
                return {"status": "signal_sent", "signal": "SIGUSR1"}
            except Exception as exc:
                self.db.execute(Q.FAIL_COMMAND, (str(exc), cmd_id))
                raise

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def get_worker_status(self, user_id: int) -> dict | None:
        w = self.workers.get(user_id)
        if not w:
            return None
        info = w.to_dict()
        rows = self.db.execute(Q.SELECT_STATE, (user_id,))
        if rows:
            db_row = rows[0]
            # Не отдавать db_state от предыдущего воркера (replication lag / окно после рестарта)
            if db_row.get("worker_pid") == w.pid:
                info["db_state"] = db_row
            else:
                log.debug(
                    "Skipping stale db_state: worker_pid=%s != current pid=%s",
                    db_row.get("worker_pid"), w.pid,
                )
                info["db_state"] = None
        return info

    def list_workers(self) -> list[dict]:
        result = []
        for uid, w in self.workers.items():
            d = w.to_dict()
            rows = self.db.execute(Q.SELECT_STATE, (uid,))
            if rows:
                s = rows[0]
                d["actual_state"] = s.get("actual_state")
                d["current_spread_pct"] = (
                    float(s["current_spread_pct"]) if s.get("current_spread_pct") else None
                )
                d["pnl_total_pct"] = (
                    float(s["pnl_total_pct"]) if s.get("pnl_total_pct") else None
                )
                d["position_open"] = bool(s.get("position_open"))
            result.append(d)
        return result

    def get_overview(self) -> dict:
        return {
            "manager_pid": os.getpid(),
            "workers_total": len(self.workers),
            "workers_alive": sum(1 for w in self.workers.values() if w.alive),
            "uptime_seconds": None,
        }

    # ------------------------------------------------------------------
    # Graceful shutdown (SIGTERM from systemd / deployer)
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        log.info("Graceful shutdown initiated")
        self._running = False
        with self._lock:
            for uid, w in list(self.workers.items()):
                if w.alive:
                    log.info("Sending SIGTERM to worker user_id=%s pid=%s", uid, w.pid)
                    os.kill(w.pid, signal.SIGTERM)

            deadline = time.time() + 30
            for uid, w in list(self.workers.items()):
                remaining = max(0, deadline - time.time())
                try:
                    w.process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    log.warning("Worker user_id=%s did not stop in time, sending SIGKILL", uid)
                    os.kill(w.pid, signal.SIGKILL)
                    w.process.wait(timeout=5)
                if w.log_file:
                    try:
                        w.log_file.close()
                    except Exception:
                        pass

            self.workers.clear()
        log.info("All workers stopped")

    # ------------------------------------------------------------------
    # Internal: start / stop worker
    # ------------------------------------------------------------------

    def _start_worker(self, user_id: int) -> None:
        keys = self.db.execute(Q.SELECT_USER_KEYS, (user_id,))
        if not keys or not keys[0].get("okx_api_key"):
            raise ValueError(f"API-ключи OKX не настроены для user_id={user_id}")

        cfg = self.db.execute(Q.SELECT_CONFIG, (user_id,))
        if not cfg:
            raise ValueError(f"Конфиг бота не найден для user_id={user_id}")

        log_path = WORKER_LOGS_DIR / f"worker_{user_id}.log"
        log_file = open(log_path, "a", buffering=1)

        process = subprocess.Popen(
            [Config.PYTHON_BIN, Config.WORKER_SCRIPT, "--user-id", str(user_id)],
            stdout=log_file,
            stderr=log_file,
        )

        self.workers[user_id] = WorkerInfo(
            process=process,
            user_id=user_id,
            log_file=log_file,
        )

        self.db.execute(Q.SET_DESIRED_STATE, ("running", user_id))
        self.db.execute(Q.UPSERT_STATE_RUNNING, (user_id, process.pid, process.pid))
        self._log_event(user_id, "info", f"Worker запущен (pid={process.pid})")
        log.info("Started worker for user_id=%s pid=%s", user_id, process.pid)

    def _stop_worker(self, user_id: int) -> dict:
        w = self.workers.get(user_id)
        if not w:
            return {"status": "not_running"}

        if not w.alive:
            del self.workers[user_id]
            self.db.execute(Q.SET_DESIRED_STATE, ("stopped", user_id))
            self.db.execute(Q.SET_STATE_STOPPED, (user_id,))
            return {"status": "already_exited", "exit_code": w.process.returncode}

        os.kill(w.pid, signal.SIGTERM)
        try:
            w.process.wait(timeout=Config.WORKER_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.kill(w.pid, signal.SIGKILL)
            w.process.wait(timeout=5)
            self._log_event(user_id, "warning", "Worker убит по timeout (SIGKILL)")

        if w.log_file:
            try:
                w.log_file.close()
            except Exception:
                pass
        del self.workers[user_id]
        self.db.execute(Q.SET_DESIRED_STATE, ("stopped", user_id))
        self.db.execute(Q.SET_STATE_STOPPED, (user_id,))
        self._log_event(user_id, "info", "Worker остановлен")
        log.info("Stopped worker for user_id=%s", user_id)
        return {"status": "stopped"}

    # ------------------------------------------------------------------
    # Health-check
    # ------------------------------------------------------------------

    def _health_check(self) -> None:
        now = datetime.utcnow()
        with self._lock:
            for uid, w in list(self.workers.items()):
                if not w.alive:
                    exit_code = w.process.returncode
                    tail = ""
                    try:
                        log_path = WORKER_LOGS_DIR / f"worker_{uid}.log"
                        if log_path.exists():
                            tail = log_path.read_text(errors="replace")[-2000:]
                    except Exception:
                        pass
                    if w.log_file:
                        try:
                            w.log_file.close()
                        except Exception:
                            pass
                    log.error("Worker user_id=%s died (exit_code=%s)\n%s", uid, exit_code, tail)
                    self._log_event(uid, "error", f"Worker упал (exit_code={exit_code}): {tail[:500]}")
                    self._try_restart(uid, w)
                    continue

                rows = self.db.execute(Q.SELECT_STATE, (uid,))
                if rows:
                    last_update = rows[0].get("updated_at")
                    if last_update and (now - last_update).total_seconds() > Config.WORKER_HANG_TIMEOUT:
                        self._log_event(uid, "error", "Worker завис — нет обновлений")
                        os.kill(w.pid, signal.SIGKILL)
                        w.process.wait(timeout=5)
                        self._try_restart(uid, w)

    def _try_restart(self, user_id: int, w: WorkerInfo) -> None:
        now = datetime.utcnow()
        window = timedelta(seconds=Config.RESTART_WINDOW_SECONDS)

        if now - w.last_restart_window_start > window:
            w.restart_count = 0
            w.last_restart_window_start = now

        if w.restart_count >= Config.MAX_RESTARTS_PER_WINDOW:
            self.db.execute(Q.SET_STATE_ERROR, (user_id,))
            self.db.execute(Q.SET_DESIRED_STATE, ("stopped", user_id))
            self._log_event(
                user_id, "error",
                f"Бот остановлен: слишком много перезапусков "
                f"({Config.MAX_RESTARTS_PER_WINDOW} за {Config.RESTART_WINDOW_SECONDS}с)",
            )
            self.db.execute(
                Q.INSERT_NOTIFICATION,
                (user_id, "Бот остановлен из-за повторяющихся сбоев. Проверьте настройки."),
            )
            self.workers.pop(user_id, None)
            return

        w.restart_count += 1
        self._log_event(
            user_id, "warning",
            f"Перезапуск worker (попытка {w.restart_count}/{Config.MAX_RESTARTS_PER_WINDOW})",
        )
        self.workers.pop(user_id, None)
        try:
            self._start_worker(user_id)
        except Exception:
            log.exception("Failed to restart worker for user %s", user_id)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _update_heartbeat(self) -> None:
        alive = sum(1 for w in self.workers.values() if w.alive)
        pid = os.getpid()
        self.db.execute(Q.UPSERT_HEARTBEAT, (pid, alive, pid, alive))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_user(self, user_id: int) -> None:
        rows = self.db.execute(Q.SELECT_USER, (user_id,))
        if not rows:
            raise ValueError(f"Пользователь user_id={user_id} не найден")
        if rows[0].get("is_blocked"):
            raise PermissionError(f"Пользователь user_id={user_id} заблокирован")

    def _audit(self, command: str, user_id: int) -> int:
        return self.db.insert_id(Q.INSERT_COMMAND, (user_id, command))

    def _log_event(self, user_id: int, level: str, message: str, details: dict | None = None) -> None:
        self.db.execute(
            Q.INSERT_EVENT,
            (user_id, level, message, json.dumps(details) if details else None),
        )
