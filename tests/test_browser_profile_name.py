"""The test browser's command line carries no name other projects sweep.

Other test runners on the same machine clean up "orphaned" browsers by
matching Playwright's shared temporary profile name on the command line, and
a sweep during a run killed every VibeNode test browser at once.  See
``launch_chromium`` in ``tests/_browser.py``.
"""

import os
import pathlib

import pytest

from tests import _browser
from tests._browser import shared_browser, shared_browser_scope  # noqa: F401

SHARED_NAME = "playwright_chromiumdev_profile"


def _descendant_cmdlines():
    """Command lines of every process started under this one."""
    parent, cmd = {}, {}
    for d in pathlib.Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                stat = (d / "stat").read_text()
                parent[int(d.name)] = int(stat.rsplit(")", 1)[1].split()[1])
                cmd[int(d.name)] = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except (OSError, ValueError, IndexError):
                pass
    mine, todo = set(), [os.getpid()]
    while todo:
        p = todo.pop()
        for c, pp in parent.items():
            if pp == p and c not in mine:
                mine.add(c)
                todo.append(c)
    return [cmd[c] for c in mine if c in cmd]


@pytest.mark.skipif(os.name != "posix" or not pathlib.Path("/proc").is_dir(),
                    reason="reads /proc")
def test_the_shared_browser_has_no_shared_profile_name_on_its_command_line():
    browser = shared_browser()
    page = browser.new_page()
    try:
        page.set_content("<b>ok</b>")
        assert page.inner_text("b") == "ok"
        mine = _descendant_cmdlines()
        assert any(_browser._WRAP_NAME in c and "--user-data-dir=" in c for c in mine), \
            "the test browser was not started through the wrapper"
        assert not [c for c in mine if SHARED_NAME in c]
    finally:
        page.close()
