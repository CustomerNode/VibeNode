"""Every menu shares the session right-click menu's look.

The real index.html with every real script in headless Chromium; only the
network is faked. Each menu is opened (or, for the Workflow, composition and
folder menus, drawn with the markup their code builds) and its box and rows
are measured: one padding, one corner, one row height and type size, and every
icon centred on its label.
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions, init_script

ITEM = ".ws-ctx-item, .sidebar-sort-opt, .sidebar-menu-sub, button, .kanban-context-item, .compose-ctx-item, .compose-sort-item"
MEASURE = """(m, ITEM) => {
  const cs = getComputedStyle(m);
  const rows = [...m.querySelectorAll(':scope > ' + ITEM.split(', ').join(', :scope > '))]
    .filter(r => r.offsetParent !== null);
  return {pad: cs.paddingTop, radius: cs.borderTopLeftRadius,
    rows: rows.map(r => { const s = getComputedStyle(r); return [s.paddingTop, s.paddingLeft, s.fontSize, s.borderTopLeftRadius]; }),
    off: Math.max(0, ...rows.map(r => { const i = r.querySelector(':scope > svg'); if (!i) return 0;
      const a = i.getBoundingClientRect(), b = r.getBoundingClientRect();
      return Math.abs((a.top + a.height / 2) - (b.top + b.height / 2)); }))};
}"""


@pytest.fixture(scope="module")
def browser():
    with shared_browser_scope() as b:
        yield b


def _check(m, name):
    assert m["pad"] == "6px" and m["radius"] == "12px", name
    assert m["rows"], name
    for r in m["rows"]:
        assert r == ["8px", "10px", "13.5px", "8px"], (name, r)
    assert m["off"] <= 1, name


def test_every_menu_shares_one_look(browser):
    rows = fake_sessions(3)
    app = FakeApp(browser, rows, init=init_script() + ";localStorage.setItem('vnSmartTermination', 'on');")
    pg = app.page
    pg.locator(".sidebar-menu-wrap .sidebar-menu-btn").first.click()
    _check(pg.locator(".sidebar-menu-wrap .sidebar-menu-dropdown").evaluate(MEASURE, ITEM), "sidebar more")
    pg.mouse.click(900, 600)
    pg.locator("#sidebar-filter-btn").click()
    _check(pg.locator(".sidebar-filter-wrap .sidebar-menu-dropdown").evaluate(MEASURE, ITEM), "filter")
    pg.mouse.click(900, 600)
    pg.evaluate("document.getElementById('hdr-sys-dropdown').classList.add('open')")
    _check(pg.locator("#hdr-sys-dropdown").evaluate(MEASURE, ITEM), "system")
    pg.evaluate("document.getElementById('hdr-sys-dropdown').classList.remove('open')")
    pg.evaluate("sid => sessionContextMenu({preventDefault(){}, stopPropagation(){}, clientX: 300, clientY: 120}, sid)", rows[0]["id"])
    _check(pg.locator(".session-ctx-menu").evaluate(MEASURE, ITEM), "session")
    pg.mouse.click(900, 600)
    pg.evaluate("ids => { ids.forEach(i => multiSelectedIds.add(i));"
                " sessionContextMenu({preventDefault(){}, stopPropagation(){}, clientX: 300, clientY: 120}, ids[0]); }",
                [rows[0]["id"], rows[1]["id"]])
    _check(pg.locator(".sidebar-bulk-ctx-menu").evaluate(MEASURE, ITEM), "several selected")
    pg.evaluate("""() => {
      const ic = '<svg width="14" height="14" viewBox="0 0 24 24"><path d="M3 3h18"/></svg>';
      const mk = (cls, item) => { const d = document.createElement('div'); d.className = cls; d.dataset.demo = cls;
        d.style.cssText = 'position:fixed;left:400px;top:300px;display:block;z-index:99999';
        d.innerHTML = '<div class="' + item + '">' + ic + ' Rename</div><div class="' + item + '">Edit</div>';
        document.body.appendChild(d); };
      mk('kanban-context-menu', 'kanban-context-item'); mk('compose-ctx-menu', 'compose-ctx-item');
      mk('compose-sort-menu', 'compose-sort-item'); mk('ws-ctx-menu', 'ws-ctx-item');
    }""")
    for cls in ("kanban-context-menu", "compose-ctx-menu", "compose-sort-menu", "ws-ctx-menu"):
        _check(pg.locator(f'[data-demo="{cls}"]').evaluate(MEASURE, ITEM), cls)


def test_the_sidebar_menu_is_four_rows_whose_lists_open_beside_it(browser):
    rows = fake_sessions(3)
    app = FakeApp(browser, rows)
    pg = app.page
    pg.locator("#sidebar-menu-btn").click()
    dd = pg.locator("#sidebar-menu-dropdown")
    assert dd.locator(":scope > .sidebar-menu-sub").all_inner_texts()[0].startswith("Sort")
    assert dd.locator(":scope > .sidebar-menu-sub").count() == 4
    assert dd.bounding_box()["height"] < 220
    assert pg.locator("#sidebar-menu-sort-val").inner_text().strip()     # the current sort, beside Sort
    dd.locator(".sidebar-menu-sub", has_text="Sort").hover()
    fly = dd.locator(".sidebar-menu-sub.open .vn-ctx-flyout")
    box = fly.bounding_box()
    # Not clipped by the sidebar: the list's middle is the list itself.
    hit = pg.evaluate("([x, y]) => !!document.elementFromPoint(x, y).closest('.vn-ctx-flyout')",
                      [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
    assert hit
    assert "Oldest first" in fly.inner_text()
    dd.locator(".sidebar-menu-sub", has_text="Display").hover()
    assert "Oldest first" not in dd.locator(".sidebar-menu-sub.open .vn-ctx-flyout").inner_text()
    assert not app.errors


def test_on_a_phone_menus_are_sheets_with_lists_in_place(browser):
    rows = fake_sessions(3)
    app = FakeApp(browser, rows, viewport={"width": 390, "height": 844})
    pg = app.page
    pg.evaluate("toggleSidebarMenu()")
    sheet = pg.locator("#mobile-sheet")
    assert sheet.locator(".sheet-item").count() == 4
    sheet.locator(".sheet-item", has_text="Sort").click()
    assert "Oldest first" in sheet.inner_text()
    assert sheet.locator(".sheet-item .vn-ctx-tick").count() == 1          # the current sort is ticked
    sheet.locator(".sheet-item", has_text="Oldest first").click()
    pg.wait_for_function("!document.getElementById('mobile-sheet').classList.contains('show')")
    pg.wait_for_function("document.querySelector('.sidebar-sort-opt[data-sort=date][data-asc=true]').classList.contains('active')")
    # A menu other code drops on the page becomes the sheet too.
    pg.evaluate("""() => { window._ran = ''; const m = document.createElement('div'); m.className = 'kanban-context-menu';
      m.innerHTML = '<div class="kanban-context-item" id="t-edit">Edit</div>'
        + '<div class="kanban-context-separator"></div><div class="kanban-context-item kanban-context-danger">Delete</div>';
      m.querySelector('#t-edit').addEventListener('click', () => { window._ran = 'edit'; });
      document.body.appendChild(m); }""")
    pg.wait_for_function("document.getElementById('mobile-sheet').classList.contains('show')")
    assert sheet.locator(".sheet-item").all_inner_texts() == ["Edit", "Delete"]
    sheet.locator(".sheet-item", has_text="Edit").click()
    pg.wait_for_function("window._ran === 'edit'")
    assert not app.errors
