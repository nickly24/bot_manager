"""
Entry point — start the Bot Manager server.

Usage:
    cd bot_manager
    python app.py
"""

import sys
from pathlib import Path

# Add parent dir to path so `bot_manager` package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot_manager.server import create_app, Config

app = create_app()

if __name__ == "__main__":
    app.run(
        host=Config.MANAGER_HOST,
        port=Config.MANAGER_PORT,
        debug=False,
        use_reloader=False,
    )
