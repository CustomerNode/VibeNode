"""One way for a test fixture to get ``engine.session_manager`` bound to a fake SDK.

Fixtures did ``import engine.session_manager; importlib.reload(it)`` inside
``patch.dict('sys.modules', ...)``.  The patch forgets the module when it ends,
so the next test's import executed the whole file and the reload at once
executed it again, 4 ms twice per test across hundreds of tests.  It runs once
now, exactly as fresh: an import that finds nothing executes the file, and a
module already loaded is reloaded as before.

``ctypes`` is imported here so that it stays outside the patch; it binds to
nothing and was executed again with every SessionManager.
"""

import ctypes  # noqa: F401
import importlib
import sys

NAME = "engine.session_manager"
# The engine's wait after a turn stream that yielded nothing, which a fake SDK
# does at the end of nearly every turn.  Only a guard against pegging the CPU on
# a wedged transport; 0.5 s in production (SessionManager._EMPTY_CYCLE_WAIT_S).
EMPTY_CYCLE_WAIT_S = 0.01
# The stagger before a self-heal reconnects, 0.5 s in production: only spreads
# many sessions reconnecting at once, which a test with one session does not do.
RECONNECT_STAGGER_S = 0
# How long a killed CLI tree gets between SIGTERM and SIGKILL, 0.3 s in production.
KILL_GRACE_S = 0.02


def fresh_session_manager():
    already_loaded = NAME in sys.modules
    module = importlib.import_module(NAME)
    if already_loaded:
        module = importlib.reload(module)
    module.SessionManager._EMPTY_CYCLE_WAIT_S = EMPTY_CYCLE_WAIT_S
    module.SessionManager._RECONNECT_STAGGER_S = RECONNECT_STAGGER_S
    module.SessionManager._KILL_GRACE_S = KILL_GRACE_S
    return module
