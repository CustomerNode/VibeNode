"""Regression tests: a transient backend blip must never strand the skeleton.

``loadProjects()`` in static/js/app.js carries a long comment about "the
'infinite skeleton until I exit and come back' bug" and retries its first
fetch for ~30s so boot always completes. That hardening covered exactly one
fetch. Two ``/api/set-project`` calls sit downstream of it -- one in
``loadProjects``, one in ``setProject`` -- and both were plain ``await fetch``
with nothing around them. Both sit *between* ``showSkeletonLoader()`` and
``loadSessions()``.

So a single rejected fetch there threw, ``loadSessions()`` never ran, and the
skeleton shimmered forever: no error surfaced, no watchdog, no recovery short
of a manual reload. Reproduced on 2026-08-31 by restarting the web server
under an open tab -- the backend was healthy on every endpoint within seconds
while the UI stayed stuck on placeholders indefinitely.

The fix routes both call sites through ``_syncActiveProject()``, which retries
and, crucially, *never throws*: ``loadSessions()`` passes ``?project=``
explicitly and does not depend on the sync having landed, so resolving the
skeleton to a real list always beats leaving the user on placeholders.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "static" / "js"
APP_JS = JS / "app.js"
_NODE = shutil.which("node")


def _app_source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _extract_sync_active_project() -> str:
    """Slice _syncActiveProject out of app.js.

    app.js cannot be eval'd whole -- it has top-level side effects and touches
    DOM globals -- so the tested unit is lifted by source, the same shape as
    tests/test_session_model.py's harness.
    """
    src = _app_source()
    start = src.index("async function _syncActiveProject(")
    end = src.index("async function loadProjects()", start)
    return src[start:end]


# ---------------------------------------------------------------------------
# Source guards -- the structural invariant
# ---------------------------------------------------------------------------

def test_no_unguarded_set_project_fetch_remains():
    """THE regression.

    A bare ``await fetch('/api/set-project'...)`` outside the retry helper is
    the exact shape that stranded the skeleton.
    """
    src = _app_source()
    # Strip the helper itself -- it legitimately contains the fetch.
    without_helper = src.replace(_extract_sync_active_project(), "")
    assert "fetch('/api/set-project'" not in without_helper, (
        "an unguarded /api/set-project fetch is back; route it through "
        "_syncActiveProject() so a transient failure cannot strand the skeleton"
    )


def test_both_call_sites_use_the_resilient_helper():
    src = _app_source()
    assert src.count("await _syncActiveProject(") == 2, (
        "expected both call sites (loadProjects and setProject) to use the helper"
    )


def test_skeleton_is_shown_before_the_project_sync():
    """Ordering is what makes an unguarded throw fatal rather than harmless.

    The skeleton goes up first, so anything that throws between it and
    loadSessions() leaves it up forever.
    """
    src = _app_source()
    skel = src.index("if (reload) showSkeletonLoader();")
    sync = src.index("await _syncActiveProject(encoded);")
    load = src.index("loadSessions();", sync)
    assert skel < sync < load


# ---------------------------------------------------------------------------
# Behavior -- run the real function under node
# ---------------------------------------------------------------------------

_HARNESS = r"""
const assert = require('assert');
global.console.warn = () => {};          // silence the give-up warning
__FN__

(async () => {
  __BODY__
  console.log('PASS');
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(_NODE is None, reason="node not available — source guards still apply")
class TestSyncActiveProjectNeverThrows:

    def _run(self, tmp_path, body: str):
        script = (_HARNESS
                  .replace("__FN__", _extract_sync_active_project())
                  .replace("__BODY__", body))
        f = tmp_path / "sync_project_test.js"
        f.write_text(script, encoding="utf-8")
        proc = subprocess.run([_NODE, str(f)], capture_output=True, text=True)
        assert proc.returncode == 0 and "PASS" in proc.stdout, (
            f"node assertion failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

    def test_resolves_false_instead_of_throwing_when_backend_is_down(self, tmp_path):
        """The core guarantee. Throwing here is what killed the boot chain."""
        self._run(tmp_path, """
            global.fetch = async () => { throw new Error('ECONNREFUSED'); };
            const out = await _syncActiveProject('proj', 2);
            assert.strictEqual(out, false, 'must resolve false, not reject');
        """)

    def test_returns_true_once_the_backend_answers(self, tmp_path):
        """A server still coming up must be waited out, not given up on."""
        self._run(tmp_path, """
            let n = 0;
            global.fetch = async () => {
              n++;
              if (n < 3) throw new Error('mid-restart');
              return { ok: true };
            };
            const out = await _syncActiveProject('proj', 8);
            assert.strictEqual(out, true);
            assert.strictEqual(n, 3);
        """)

    def test_a_non_ok_response_is_retried_not_treated_as_success(self, tmp_path):
        """A 503 from a half-booted server is not an answer."""
        self._run(tmp_path, """
            let n = 0;
            global.fetch = async () => { n++; return { ok: false, status: 503 }; };
            const out = await _syncActiveProject('proj', 3);
            assert.strictEqual(out, false);
            assert.strictEqual(n, 3, 'every attempt should have been used');
        """)

    def test_it_posts_the_project_the_caller_asked_for(self, tmp_path):
        self._run(tmp_path, """
            let seen = null;
            global.fetch = async (url, opts) => {
              seen = { url, body: JSON.parse(opts.body) };
              return { ok: true };
            };
            await _syncActiveProject('C--Users-x-code-Thing', 2);
            assert.strictEqual(seen.url, '/api/set-project');
            assert.strictEqual(seen.body.project, 'C--Users-x-code-Thing');
        """)

    def test_it_gives_up_in_bounded_time(self, tmp_path):
        """Boot cannot hang here -- an unbounded retry is just a slower stall."""
        self._run(tmp_path, """
            let n = 0;
            global.fetch = async () => { n++; throw new Error('down'); };
            await _syncActiveProject('proj', 4);
            assert.strictEqual(n, 4, 'attempts must be bounded by the argument');
        """)
