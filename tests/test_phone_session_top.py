"""On a phone the area above the sessions reads like a phone app.

The real index.html with every real script in headless Chromium at phone
width; only the network is faked. The project name, New Session and Search
start on one left edge, every control is at least 44px tall (a finger's
target), New Session is a full-width button and Search a filled field.
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def test_the_phone_top_is_tidy_rows_with_a_filled_search(browser):
    app = FakeApp(browser, fake_sessions(4), viewport={"width": 390, "height": 844})
    m = app.page.evaluate("""() => {
      const r = s => document.querySelector(s).getBoundingClientRect();
      const cs = s => getComputedStyle(document.querySelector(s));
      return {project: r('#btn-project'), add: r('#btn-add-agent'), search: r('.sidebar-search-btn'),
              filter: r('#sidebar-filter-btn'), menu: r('#sidebar-menu-btn'),
              searchBg: cs('.sidebar-search-btn').backgroundColor, addBg: cs('#btn-add-agent').backgroundColor,
              header: document.querySelector('.sidebar-header').getBoundingClientRect()};
    }""")
    lefts = {round(m[k]["left"]) for k in ("project", "add", "search")}
    assert len(lefts) == 1, lefts
    for k in ("project", "add", "search", "filter", "menu"):
        assert m[k]["height"] >= 44, k
    assert m["filter"]["width"] >= 44 and m["menu"]["width"] >= 44
    assert m["add"]["width"] >= m["header"]["width"] - 40           # full width
    assert m["searchBg"] not in ("rgba(0, 0, 0, 0)", "transparent")  # a filled field
    assert m["addBg"] not in ("rgba(0, 0, 0, 0)", "transparent")
    assert not app.errors


def test_the_computer_top_is_unchanged(browser):
    app = FakeApp(browser, fake_sessions(2))
    h = app.page.evaluate("document.querySelector('#btn-add-agent').getBoundingClientRect().height")
    assert h < 44
