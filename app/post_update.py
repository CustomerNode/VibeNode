"""Data migrations that run with the code an Update just pulled (2026-10-29).

The one-click Update pulls new code into the running web server's folder.
The browser picks up the new screen code at once (static files, live-assets
reload), but the web server keeps running the Python it started with until
its next restart.  Any data migration that ships with an update and lives
only in server code was therefore skipped for hours, while the new screen
code already relied on it.  That is how every session the owner had marked
Done turned Needs you on 2026-10-29.

``app/git_ops.py`` runs this file in a FRESH interpreter (so it is the pulled
code) right after a pull that brought something in.  The web server also runs
the same migrations itself on its first session listing after a restart, so
either path is enough.  Every migration here must be idempotent and run once
(a marker file), and must never need the web server or the Session Engine.

Usage: ``python -m app.post_update`` from the repo folder; prints JSON.
"""

from __future__ import annotations

import json
import sys


def run() -> dict:
    out = {}
    try:
        from . import session_holds
        out["session_holds"] = session_holds.migrate_all()
    except Exception as e:      # report, never fail the update
        out["session_holds_error"] = str(e)
    return out


if __name__ == "__main__":
    print(json.dumps(run()))
    sys.exit(0)
