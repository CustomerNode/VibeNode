"""Code shared by the web server (``src/web/``) and the Session Engine (``src/engine/``).

The two layers run in separate processes and restart independently, so the
engine must never import the web package (``tests/test_layering.py``).  What
both need lives here, and this package imports neither of them.
"""
