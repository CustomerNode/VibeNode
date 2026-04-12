"""
VibeNode Flask application factory.
"""

from flask import Flask, request
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
    app.config["TEMPLATES_AUTO_RELOAD"] = True

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
    from .routes.kanban_api import bp as kanban_bp
    from .routes.kanban_report_api import bp as kanban_reports_bp
    from .routes.compose_api import bp as compose_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(sessions_bp)
    app.register_blueprint(project_bp)
    app.register_blueprint(git_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(analysis_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(kanban_bp)
    app.register_blueprint(kanban_reports_bp)
    app.register_blueprint(compose_bp)

    # Prune stale utility session JSONL files (>24h) — once at startup,
    # not on every /api/sessions request.
    from .config import _cleanup_system_sessions
    _cleanup_system_sessions()

    # Start background git fetch at startup
    from .git_ops import start_bg_fetch
    start_bg_fetch()

    # Start compose-context.json file watcher for real-time board pushes
    from .compose_watcher import start_compose_watcher
    start_compose_watcher(socketio, app)

    # Auto cache-busting: {{ versioned_static('js/app.js') }} → /static/js/app.js?v=<mtime>
    import os as _os

    @app.context_processor
    def _static_cache_buster():
        def versioned_static(filename):
            filepath = _os.path.join(app.static_folder, filename)
            try:
                mtime = int(_os.path.getmtime(filepath))
            except OSError:
                mtime = 0
            return f"/static/{filename}?v={mtime}"
        return dict(versioned_static=versioned_static)

    # Prevent aggressive browser caching of static JS/CSS
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

    @app.after_request
    def _no_cache_static(response):
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response

    return app
