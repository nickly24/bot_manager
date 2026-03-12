"""
Entry point — start the Bot Manager server.

Usage (from bot_manager folder):
    python app.py
"""
from server import create_app, Config

app = create_app()

if __name__ == "__main__":
    app.run(
        host=Config.MANAGER_HOST,
        port=Config.MANAGER_PORT,
        debug=False,
        use_reloader=False,
    )
