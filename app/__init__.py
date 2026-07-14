"""
VibeNode Flask application factory.
"""

import logging as _logging
import os as _os
import sys as _sys

from flask import Flask, request
from flask_socketio import SocketIO

from .config import _VIBENODE_DIR

socketio = SocketIO()

# --- SpeechNode (extracted package: github.com/CustomerNode/SpeechNode) ---
# Prefer a real install; fall back to the sibling source checkout next to the repo.
# Reuse VibeNode's existing model/venv cache so consuming the package never triggers
# a re-download. (Mounted via register_speechnode() in create_app.)
try:
    import speechnode  # noqa: F401
except ImportError:
    _sn_src = _VIBENODE_DIR.parent / "SpeechNode"
    if _sn_src.is_dir():
        _sys.path.insert(0, str(_sn_src))
_os.environ.setdefault("SPEECHNODE_MODEL_DIR", str(_VIBENODE_DIR / ".cache" / "speechnode-models"))
_os.environ.setdefault("SPEECHNODE_VENV_DIR", str(_VIBENODE_DIR / ".cache" / "speechnode-venv"))


def create_app(testing=False) -> Flask:
    """Create and configure the Flask application.

    Args:
        testing: If True, skip all background services (daemon, git fetch,
                 compose watcher, session cleanup). Used by pytest so tests
                 don't spawn hundreds of threads and daemon connections.
    """
    app = Flask(
        __name__,
        template_folder=str(_VIBENODE_DIR / "templates"),
        static_folder=str(_VIBENODE_DIR / "static"),
    )
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    if testing:
        app.config["TESTING"] = True

    # Initialize SocketIO with threading mode (Flask's default)
    socketio.init_app(app, async_mode='threading', cors_allowed_origins='*')

    if not testing:
        # Connect to the session daemon (runs in a separate process)
        from .daemon_client import DaemonClient
        app.session_manager = DaemonClient()
        app.session_manager.start(socketio, app=app)

        # Register WebSocket event handlers
        from .routes.ws_events import register_ws_events
        register_ws_events(socketio, app)
    else:
        # In test mode, provide a no-op session manager stub so routes
        # that reference app.session_manager don't crash on attribute access.
        from unittest.mock import MagicMock
        app.session_manager = MagicMock()

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
    from .routes.test_api import bp as test_bp
    from .routes.admin_api import bp as admin_bp
    from .routes.watchdog_api import bp as watchdog_bp
    from .routes.mobile_api import bp as mobile_bp

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
    app.register_blueprint(test_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(watchdog_bp)
    app.register_blueprint(mobile_bp)

    # SpeechNode (extracted package) — mount its routes + serve its web assets.
    # Optional: if the package isn't importable, VibeNode still runs (voice off).
    try:
        from speechnode.flask import register_speechnode
        import speechnode as _speechnode
        register_speechnode(app)                       # /api/speechnode/*
        from flask import send_from_directory as _sfd
        _sn_web = _speechnode.web_dir()

        @app.route("/speechnode/<path:filename>")
        def _speechnode_assets(filename):              # serves speechnode.js / speechnode.css
            return _sfd(_sn_web, filename)

        # Pre-warm: if the model was already installed before this restart, start
        # reloading it into memory NOW (in the background) rather than waiting for
        # the first client poll. The web-server restart animation takes ~5-10s, so
        # the model is often warm again before the user can even try to use voice.
        # start_install() is idempotent and thread-safe — safe to call at startup.
        try:
            from speechnode import engine as _sn_engine
            if _sn_engine._deps_available():
                _sn_engine.start_install()
                _logging.getLogger("app").info("SpeechNode: pre-warming model after restart.")
        except Exception:
            pass  # best-effort; client-side polling handles it if this fails
    except Exception as _e:                            # noqa: BLE001
        _logging.getLogger("app").warning("SpeechNode unavailable: %s", _e)

    if not testing:
        # PERF-CRITICAL: Startup-only cleanup — do NOT call from all_sessions() or per-request paths. See CLAUDE.md #13.
        # Prune stale utility session JSONL files (>24h) — once at startup,
        # not on every /api/sessions request.
        from .config import _cleanup_system_sessions, _cleanup_aititle_orphans
        _cleanup_system_sessions()

        # Prune ai-title-only JSONL orphans created when the CLI auto-titles
        # a session but the conversation never lands on disk under that ID
        # (CLI death mid-write after a delete, or SDK session-ID remap).
        # Without this sweep these files accumulate and surface in the
        # sidebar as bare-UUID "empty chats".  Startup-only — same hot-path
        # rule as _cleanup_system_sessions.
        try:
            n = _cleanup_aititle_orphans()
            if n:
                _logging.getLogger("app").info(
                    "Pruned %d ai-title-only orphan JSONL file(s) at startup", n
                )
        except Exception:
            _logging.getLogger("app").exception("ai-title orphan cleanup failed")

        # Start background git fetch at startup
        from .git_ops import start_bg_fetch
        start_bg_fetch()

        # Start compose-context.json file watcher for real-time board pushes
        from .compose.watcher import start_compose_watcher
        start_compose_watcher(socketio, app)

        # Mobile Command: if the user left phone access ON, re-establish the private
        # tailnet bridge (tailscale serve) now so it persists across restarts —
        # mirrors the Persistent Storage "set once, always works" behavior.
        # Best-effort and silent; no-ops when disabled or Tailscale is unavailable.
        try:
            from . import mobile_command
            mobile_command.rearm()
        except Exception:
            _logging.getLogger("app").exception("Mobile Command rearm failed (non-fatal)")

    # Auto cache-busting: {{ versioned_static('js/app.js') }} → /static/js/app.js?v=<mtime>
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
        elif response.mimetype == 'text/html':
            # The SPA shell must revalidate every load. Without this the browser
            # (esp. iOS Safari / home-screen PWA) serves a cached HTML that points
            # at stale cache-busted JS/CSS — so fixes never reach the device even
            # though versioned_static changed the ?v= for the assets.
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
        return response

    return app
