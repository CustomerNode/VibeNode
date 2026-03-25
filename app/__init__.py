"""
VibeNode Flask application factory.
"""

from flask import Flask
from flask_socketio import SocketIO

from .config import _VIBENODE_DIR

socketio = SocketIO()


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=str(_VIBENODE_DIR / "templates"),
        static_folder=str(_VIBENODE_DIR / "static"),
    )

    # Initialize SocketIO with threading mode (Flask's default)
    socketio.init_app(app, async_mode='threading', cors_allowed_origins='*')

    # Connect to the session daemon (runs in a separate process)
    from .daemon_client import DaemonClient
    app.session_manager = DaemonClient()
    app.session_manager.start(socketio, app=app)

    # Register WebSocket event handlers
    from .routes.ws_events import register_ws_events
    register_ws_events(socketio, app)

    # Register blueprints
    from .routes.main import bp as main_bp
    from .routes.sessions_api import bp as sessions_bp
    from .routes.project_api import bp as project_bp
    from .routes.git_api import bp as git_bp
    from .routes.live_api import bp as live_bp
    from .routes.analysis_api import bp as analysis_bp
    from .routes.auth_api import bp as auth_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(sessions_bp)
    app.register_blueprint(project_bp)
    app.register_blueprint(git_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(analysis_bp)
    app.register_blueprint(auth_bp)

    # Start background git fetch at startup
    from .git_ops import start_bg_fetch
    start_bg_fetch()

    return app
