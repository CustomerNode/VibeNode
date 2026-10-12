"""A test app starts no background work.

``create_app(testing=True)`` exists so that pytest can build the Flask app
without the daemon connection, the git fetch, the compose watcher and the
other services a real web server starts.  The SpeechNode pre-warm sat outside
that guard: every test process that built an app also began loading the voice
model, in native code, on a daemon thread.

A process that exits while that load is running is killed by the C++ runtime:
``terminate called without an active exception``, SIGABRT, exit status 134.
Seen on 2026-10-12 at the end of a short run in which every test had passed,
and reproduced by exiting a test process 0.20 to 0.28 seconds after the model
library was imported.  To a caller that reads the exit status (the pre-publish
test run does) that is a failed suite.
"""

from __future__ import annotations

import pytest


def test_a_test_app_does_not_start_loading_the_voice_model(monkeypatch):
    from app import create_app      # also puts a sibling SpeechNode checkout on sys.path

    engine = pytest.importorskip("speechnode.engine", reason="SpeechNode is not installed")
    started = []
    monkeypatch.setattr(engine, "_deps_available", lambda: True)
    monkeypatch.setattr(engine, "start_install", lambda *args, **kwargs: started.append(args) or {})

    app = create_app(testing=True)

    assert started == [], "create_app(testing=True) started the voice model load"
    assert any(rule.rule.startswith("/api/speechnode") for rule in app.url_map.iter_rules()), \
        "the voice routes themselves are still mounted in a test app"
