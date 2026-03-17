import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class Config:
    # --- MySQL ---
    DB_HOST = os.getenv("DB_HOST", "147.45.138.77")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "cryptobot")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "cryptobot")
    DB_NAME = os.getenv("DB_NAME", "pairtrading")
    DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))

    # --- Manager Server ---
    # В контейнере нужен 0.0.0.0, иначе приложение недоступно снаружи
    MANAGER_HOST = os.getenv("MANAGER_HOST", "0.0.0.0")
    MANAGER_PORT = int(os.getenv("PORT") or os.getenv("MANAGER_PORT", "6800"))
    MANAGER_SECRET = os.getenv("MANAGER_SECRET", "change-me-in-production")

    # --- Encryption ---
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

    # --- Worker ---
    WORKER_SCRIPT = str(
        Path(__file__).resolve().parent / "bot_worker.py"
    )
    PYTHON_BIN = os.getenv("PYTHON_BIN", "python3")

    # --- Health-check ---
    HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "10"))
    WORKER_HANG_TIMEOUT = int(os.getenv("WORKER_HANG_TIMEOUT", "60"))
    WORKER_STOP_TIMEOUT = int(os.getenv("WORKER_STOP_TIMEOUT", "15"))
    MAX_RESTARTS_PER_WINDOW = int(os.getenv("MAX_RESTARTS_PER_WINDOW", "3"))
    RESTART_WINDOW_SECONDS = int(os.getenv("RESTART_WINDOW_SECONDS", "300"))

    # --- OKX: "1" = demo/testnet, "0" = production ---
    OKX_DEMO = os.getenv("OKX_DEMO", "0")
