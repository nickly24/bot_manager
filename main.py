"""
Точка входа для облачных платформ (Timeweb, Render и т.д.).
- gunicorn main:app
- python main.py
"""
from bot_manager.app import app
from bot_manager.config import Config

__all__ = ["app"]

if __name__ == "__main__":
    app.run(host=Config.MANAGER_HOST, port=Config.MANAGER_PORT, debug=False, use_reloader=False)
