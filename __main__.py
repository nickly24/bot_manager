"""Allow running as `python -m bot_manager` to start the server."""

from bot_manager.server import create_app, Config

if __name__ == "__main__":
    app = create_app()
    app.run(
        host=Config.MANAGER_HOST,
        port=Config.MANAGER_PORT,
        debug=False,
        use_reloader=False,
    )
