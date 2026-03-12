from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO


@dataclass
class WorkerInfo:
    process: subprocess.Popen
    user_id: int
    log_file: IO | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    restart_count: int = 0
    last_restart_window_start: datetime = field(default_factory=datetime.utcnow)

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "pid": self.pid,
            "alive": self.alive,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "restart_count": self.restart_count,
            "uptime_seconds": (
                (datetime.utcnow() - self.started_at).total_seconds()
                if self.started_at
                else 0
            ),
        }
