"""Empty working directory for fake SessionManager turns.

Tests that drive turns through a real SessionManager used to pass
``cwd="/tmp"``.  On Windows that resolves to ``C:\\tmp``, and if that folder
exists on the dev machine (one had ~17k entries), ``_record_pre_turn_mtimes``
and ``_detect_changed_files`` walk it on every turn.  That cost seconds per
test, made results depend on whatever happened to be on disk, and under the
8-worker Publish run it caused timeouts.

``EMPTY_CWD`` is one empty directory per test process.  Scanning it is
sub-millisecond, so the turn logic is exercised exactly as before without
touching the machine's filesystem.  Use it for any test whose session
actually runs a turn.  Tests that only store or compare a cwd string
(registry round-trips) do not need it.
"""

import atexit
import shutil
import tempfile

EMPTY_CWD = tempfile.mkdtemp(prefix="vn_test_cwd_")
atexit.register(lambda: shutil.rmtree(EMPTY_CWD, ignore_errors=True))
