"""
ClaudeCodeGUI Flask application factory.
"""

from flask import Flask


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder='../templates',
        static_folder='../static',
    )

    # Register blueprints
    from .routes.main import bp as main_bp
    from .routes.sessions_api import bp as sessions_bp
    from .routes.project_api import bp as project_bp
    from .routes.git_api import bp as git_bp
    from .routes.live_api import bp as live_bp
    from .routes.analysis_api import bp as analysis_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(sessions_bp)
    app.register_blueprint(project_bp)
    app.register_blueprint(git_bp)
    app.register_blueprint(live_bp)
    app.register_blueprint(analysis_bp)

    # Start background git fetch at startup
    from .git_ops import start_bg_fetch
    start_bg_fetch()

    return app
