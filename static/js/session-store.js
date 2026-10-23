/* session-store.js — the ONE owner of the client's session list and every
 * per-session map (added 2026-10-23).
 *
 * Before this file the session list was a bare `let allSessions` in app.js,
 * reassigned or spliced at 19 sites in six files, and shadowed by a dozen side
 * tables (allSessionIds, sessionKinds, runningIds, waitingData, guiOpenSessions,
 * _knownSessionTitles, window._sessionSubstatus, ...).  Every site had to keep
 * every table in step by hand; PERF-CRITICAL #15 (the id Set) was the one that
 * kept breaking.
 *
 * Now:
 *   SessionStore.all()              the list (the live array, newest-inserted first)
 *   SessionStore.get(id)            the row, O(1)
 *   SessionStore.has(id)            O(1), Set-backed. This IS PERF-CRITICAL #15:
 *                                   never replace it with a scan.
 *   SessionStore.set(id, patch)     update a row in place, or insert `patch`
 *                                   itself at the FRONT when the id is new (the
 *                                   object is kept, not copied, so callers may
 *                                   hold a reference to it)
 *   SessionStore.replaceAll(list)   bulk replace; dedups by id, first copy wins
 *   SessionStore.remove(id)         drop a row
 *   SessionStore.rekey(old, new)    SDK id remap: MERGES into an existing row
 *                                   under `new` (carrying a title it lacks) or
 *                                   renames the row in place
 *   SessionStore.subscribe(fn)      fn({type, id}) after each mutation, batched
 *                                   per microtask; returns an unsubscribe
 *   SessionStore.map(name)          a per-session table (see MAPS below)
 *   SessionStore.mapSet(name, id, v) / mapDelete(name, id)
 *   SessionStore.titles             the title memory (_knownSessionTitles)
 *
 * Legacy globals keep working through window accessors, so any site not yet
 * moved (and git-sync.js, which this change does not own) still reads and
 * writes the store:
 *   allSessions          get -> the store's array; set -> replaceAll()
 *   allSessionIds        get -> the store's Set (add/delete still work)
 *   sessionKinds, runningIds, waitingData, guiOpenSessions,
 *   window._sessionSubstatus, _sessionRetryState, _sessionError, _sessionUsage,
 *   _workingSinceMap, _sessionStateTs, _subAgents, _idRemaps
 *                        get -> the store's table; set -> replace the table
 * `if (!window._x) window._x = {}` and `sessionKinds = newKinds` keep their
 * meaning.  Because these are accessors on `window`, NO script may declare a
 * top-level `let`/`const`/`var` of the same name: it would throw at load.
 *
 * Loaded right after utils.js and before app.js (templates/index.html).
 */
(function () {
  'use strict';
  if (window.SessionStore) return;

  let _list = [];
  let _ids = new Set();
  let _byId = new Map();

  function _reindex() {
    _ids = new Set();
    _byId = new Map();
    for (const s of _list) {
      if (!s || !s.id) continue;
      _ids.add(s.id);
      if (!_byId.has(s.id)) _byId.set(s.id, s);
    }
  }

  // ── Change notification (batched per microtask) ─────────────────────────
  const _subs = new Set();
  let _pending = [];
  let _flushQueued = false;
  function _notify(type, id) {
    if (!_subs.size) return;
    _pending.push({ type, id: id || null });
    if (_flushQueued) return;
    _flushQueued = true;
    Promise.resolve().then(() => {
      _flushQueued = false;
      const batch = _pending;
      _pending = [];
      for (const fn of Array.from(_subs)) {
        for (const ev of batch) {
          try { fn(ev); } catch (e) { console.error('[SessionStore] subscriber failed', e); }
        }
      }
    });
  }

  // ── The list ─────────────────────────────────────────────────────────────
  function all() { return _list; }

  function has(id) { return !!id && _ids.has(id); }

  function get(id) {
    if (!id) return null;
    const hit = _byId.get(id);
    if (hit && hit.id === id) return hit;
    // The Map can lag a legacy in-place mutation (`row.id = x`, or a raw
    // `allSessions.unshift` from code that predates the store).  Repair on miss.
    for (const s of _list) {
      if (s && s.id === id) { _byId.set(id, s); _ids.add(id); return s; }
    }
    if (hit) _byId.delete(id);
    return null;
  }

  function set(id, patch) {
    if (!id) return null;
    const cur = get(id);
    if (cur) {
      if (patch && patch !== cur) Object.assign(cur, patch, { id });
      _notify('update', id);
      return cur;
    }
    const row = patch || {};
    row.id = id;
    _list.unshift(row);
    _ids.add(id);
    _byId.set(id, row);
    _notify('add', id);
    return row;
  }

  function replaceAll(list) {
    const seen = new Set();
    const out = [];
    for (const s of (list || [])) {
      if (!s || !s.id || seen.has(s.id)) continue;   // first copy wins
      seen.add(s.id);
      out.push(s);
    }
    _list = out;
    _reindex();
    _notify('replace', null);
    return _list;
  }

  function remove(id) {
    if (!id) return false;
    const before = _list.length;
    _list = _list.filter(s => s.id !== id);
    _ids.delete(id);
    _byId.delete(id);
    if (_list.length !== before) { _notify('remove', id); return true; }
    return false;
  }

  /**
   * The SDK assigned a different id than the one the client launched with.
   * If the list already has a row under `newId` (a reload during the first
   * turn lists the session twice: the daemon's stub under the launch id and
   * the CLI transcript under the CLI's id), MERGE: keep the transcript row,
   * carry the stub's title over if the transcript row has none, drop the
   * stub.  Otherwise rename the row in place.  Returns the surviving row.
   */
  function rekey(oldId, newId) {
    const s = get(oldId);
    if (!s || !newId || oldId === newId) return s ? s : get(newId);
    const same = get(newId);
    if (same && same !== s) {
      if (s.custom_title && !same.custom_title) {
        same.custom_title = s.custom_title;
        same.display_title = s.custom_title;
      }
      _list = _list.filter(x => x !== s);
      _ids.delete(oldId);
      _byId.delete(oldId);
      _notify('remove', oldId);
      _notify('update', newId);
      return same;
    }
    s.id = newId;
    _ids.delete(oldId);
    _byId.delete(oldId);
    _ids.add(newId);
    _byId.set(newId, s);
    _notify('rekey', newId);
    return s;
  }

  /** Re-derive the id index from the array, if a legacy mutation drifted it. */
  function reindexIfDrifted() {
    if (_ids.size === _list.length && _byId.size === _list.length) return false;
    _reindex();
    return true;
  }

  function subscribe(fn) {
    if (typeof fn !== 'function') return () => {};
    _subs.add(fn);
    return () => _subs.delete(fn);
  }

  // ── Per-session tables ───────────────────────────────────────────────────
  // name -> [kind, legacy global name]
  //   kind 'obj' = plain object keyed by id, 'set' = Set of ids
  let _guiOpenInit = [];
  try { _guiOpenInit = JSON.parse(localStorage.getItem('guiOpenSessions') || '[]'); } catch (e) { /* corrupt */ }
  const _maps = {
    kinds:       { v: {},                      global: 'sessionKinds' },     // id -> 'question'|'working'|'idle'
    running:     { v: new Set(),               global: 'runningIds' },
    waiting:     { v: {},                      global: 'waitingData' },      // id -> {question, options, kind}
    guiOpen:     { v: new Set(_guiOpenInit),   global: 'guiOpenSessions' },
    substatus:   { v: {},                      global: '_sessionSubstatus' },
    retry:       { v: {},                      global: '_sessionRetryState' },
    error:       { v: {},                      global: '_sessionError' },
    usage:       { v: {},                      global: '_sessionUsage' },
    workingSince:{ v: {},                      global: '_workingSinceMap' },
    stateTs:     { v: {},                      global: '_sessionStateTs' },
    subAgents:   { v: {},                      global: '_subAgents' },
    idRemaps:    { v: {},                      global: '_idRemaps' },
  };

  function map(name) { return _maps[name] ? _maps[name].v : undefined; }

  function mapSet(name, id, value) {
    const m = _maps[name];
    if (!m || !id) return;
    if (m.v instanceof Set) { if (value) m.v.add(id); else m.v.delete(id); }
    else m.v[id] = value;
  }

  function mapDelete(name, id) {
    const m = _maps[name];
    if (!m || !id) return;
    if (m.v instanceof Set) m.v.delete(id);
    else delete m.v[id];
  }

  // Title memory: see _knownSessionTitles in app.js for the rules.
  const titles = new Map();

  // ── Legacy globals ───────────────────────────────────────────────────────
  Object.defineProperty(window, 'allSessions', {
    get: all,
    set: (v) => { replaceAll(Array.isArray(v) ? v : []); },
    configurable: false, enumerable: true,
  });
  Object.defineProperty(window, 'allSessionIds', {
    get: () => _ids,
    // A legacy `allSessionIds = new Set(...)`: the array is the truth, so
    // re-derive from it rather than trusting a Set that may disagree.
    set: () => { _reindex(); },
    configurable: false, enumerable: true,
  });
  for (const name of Object.keys(_maps)) {
    const m = _maps[name];
    Object.defineProperty(window, m.global, {
      get: () => m.v,
      set: (v) => {
        if (m.v instanceof Set) m.v = (v instanceof Set) ? v : new Set(v || []);
        else m.v = (v && typeof v === 'object') ? v : {};
      },
      configurable: false, enumerable: true,
    });
  }

  window.SessionStore = {
    all, get, has, set, replaceAll, remove, rekey, subscribe,
    reindexIfDrifted, map, mapSet, mapDelete, titles,
  };
})();
