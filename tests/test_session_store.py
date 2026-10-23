"""SessionStore (static/js/session-store.js) owns the session list.

The contract the rest of the page relies on, checked against the real page:
  * has() is O(1) and in step with the list after every mutation
    (PERF-CRITICAL #15), including legacy writes through the globals;
  * set() inserts a new id at the front and keeps the caller's object;
  * rekey() MERGES into an existing row under the new id (the
    session_id_remapped rule in CLAUDE.md "Session names reach every open
    sidebar"), carrying a title the surviving row lacks;
  * the legacy globals (allSessions, allSessionIds, sessionKinds, runningIds,
    window._sessionSubstatus, ...) read and write the store.
"""

from __future__ import annotations

import pytest

from tests._browser import shared_browser_scope
from tests._fake_app import FakeApp, fake_sessions

XDIST_SPLITTABLE = True


@pytest.fixture(scope="module")
def app():
    with shared_browser_scope() as b:
        a = FakeApp(b, fake_sessions(5))
        yield a
        assert not a.errors, a.errors
        a.close()


@pytest.mark.slow
def test_store_contract(app):
    r = app.eval("""() => {
      const S = SessionStore, out = {};
      const sync = () => allSessions.length === allSessionIds.size
        && allSessions.every(s => S.has(s.id) && S.get(s.id) === s);
      out.loaded = S.all().length === 5 && allSessions === S.all() && sync();
      const obj = {display_title: 'New Session', custom_title: ''};
      const row = S.set('new-1', obj);
      out.setKeepsObject = row === obj && S.all()[0] === obj && S.has('new-1');
      S.set('new-1', {size: '1 KB'});
      out.setPatches = S.get('new-1').size === '1 KB' && S.all().length === 6;
      // Merge: stub under the launch id titled, transcript row under the CLI id nameless.
      S.get('new-1').custom_title = 'Named'; S.get('new-1').display_title = 'Named';
      S.set('cli-1', {display_title: 'first message', custom_title: null});
      const kept = S.rekey('new-1', 'cli-1');
      out.merge = kept === S.get('cli-1') && !S.has('new-1') && kept.custom_title === 'Named'
        && S.all().filter(s => s.id === 'cli-1').length === 1 && sync();
      // Rename in place.
      S.set('launch-2', {display_title: 'x'});
      const r2 = S.rekey('launch-2', 'cli-2');
      out.rename = r2.id === 'cli-2' && S.has('cli-2') && !S.has('launch-2') && sync();
      // Legacy writes go through the store.
      allSessions = allSessions.filter(s => s.id !== 'cli-2');
      out.legacyAssign = !S.has('cli-2') && sync();
      allSessions.unshift({id: 'raw-1'}); allSessionIds.add('raw-1');      // git-sync.js style
      out.legacyUnshift = S.has('raw-1') && S.get('raw-1').id === 'raw-1';
      out.dedup = S.replaceAll([{id: 'a', n: 1}, {id: 'a', n: 2}, {id: 'b'}]).length === 2
        && S.get('a').n === 1 && sync();
      S.remove('a');
      out.remove = !S.has('a') && S.all().length === 1 && sync();
      // Per-session tables.
      sessionKinds = {z: 'working'};
      out.kindsSetter = S.map('kinds') === sessionKinds && sessionKinds.z === 'working';
      S.mapSet('running', 'z', true);
      out.running = runningIds.has('z');
      window._sessionSubstatus = {};
      S.mapSet('substatus', 'z', 'compacting');
      out.substatus = window._sessionSubstatus.z === 'compacting';
      out.titles = _knownSessionTitles === S.titles;
      return out;
    }""")
    assert all(r.values()), r


@pytest.mark.slow
def test_subscribe_batches_per_microtask(app):
    events = app.eval("""async () => {
      const seen = [];
      const off = SessionStore.subscribe(e => seen.push(e.type + ':' + (e.id || '')));
      SessionStore.set('sub-1', {}); SessionStore.remove('sub-1');
      const syncCount = seen.length;
      await Promise.resolve(); await Promise.resolve();
      off();
      SessionStore.set('sub-2', {});
      await Promise.resolve();
      return {syncCount, seen};
    }""")
    assert events["syncCount"] == 0
    assert events["seen"] == ["add:sub-1", "remove:sub-1"]
