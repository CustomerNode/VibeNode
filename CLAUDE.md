# VibeNode Project Rules

## PUBLIC REPOSITORY — CRITICAL

VibeNode is an **open-source public repository on GitHub**. Everything you commit will be published to the web. Some developers working on this project are non-technical and use a one-click "Update" button in the UI that commits and pushes — they will not manually review diffs before publishing.

**You must treat every file change as if it will be immediately visible to the entire internet.**

- **NEVER** put secrets, API keys, tokens, passwords, or credentials in any tracked file. Use environment variables or gitignored config files (`kanban_config.json`, `.env`).
- **NEVER** hardcode personal paths, usernames, emails, or any personally identifiable information. Derive paths dynamically (e.g. `Path(__file__).resolve().parents[1]`, `os.getcwd()`).
- **NEVER** commit user data, runtime artifacts, logs, database files, test screenshots, or local state. These belong in gitignored directories.
- **NEVER** commit planning docs, specs, implementation notes, or working documents to tracked directories. Use `docs/plans/` which is gitignored.
- **If it's even borderline** — if you're not 100% sure something is safe to publish — **ASK the user before committing it.** Do not guess. Do not assume. Ask.

Review the `.gitignore` before creating new files in unfamiliar directories. If a new category of file doesn't have a gitignore rule, add one.

## Diagnosing slow sessions — NEVER blame large context

When a user asks why a session is slow and they are trying to optimize their time, **do NOT cite large context as an explanation and stop there.** Large context is a factor in model latency, not a verdict. The user already knows their session is slow — they need actionable help, not a description of why it will always be slow.

**Required behavior:**
- Diagnose the actual, specific bottleneck for that session at that moment (e.g. browser automation wall-clock time, a blocking tool call, a permission prompt waiting for approval, an oversized file snapshot, a task stuck in a loop).
- If `/compact` would help, say so — but only after identifying the real bottleneck, and frame it as one option among others.
- If the session is slow because of something fixable in VibeNode itself (snapshot size, turn latency, IPC overhead), treat that as a bug to investigate and fix, not as something the user must work around.

**Forbidden response pattern:** "Your context is ~Xk tokens, which means turns will take ~Y seconds. Run /compact to shrink it." That is the context-blame anti-pattern. It is not wrong, but it is useless — it converts a diagnostic question into a shrug with a workaround attached.

## Never end a turn on pending background work

The failure mode: launch an Agent or Bash with background execution, treat "dispatched" as "done," end the turn. The user comes back and asks "did you do it?" and the session has to re-read output it should have read the first time. This wastes turns, breaks trust, and produces sessions that look idle when they should be working — exactly the "session doing nothing when I asked it to do something" symptom users report.

**Hard rules:**

- **Foreground by default.** Any tool call that produces the answer the user is waiting for runs in foreground. On the Agent tool, pass `run_in_background: false`. On Bash, do not pass `run_in_background: true`. Foreground is the default even when it feels slow — the user is already waiting, and a visible wait beats an invisible one.
- **Background is opt-in, not default.** Use it only when (a) the task is >2 minutes and belongs in `run-detached`, (b) the user explicitly asked for parallel/fan-out work, or (c) you have other productive tool calls to make in the same turn AND you will still wait for the background result before your final message.
- **Never end a turn while primary work is pending.** If a background task IS the deliverable, wait for the completion notification before responding. "It's running, I'll check when it finishes" is not a valid final turn. "The agent went to background instead of foreground, reading its output now" is the failure this rule exists to prevent.
- **Turn-end self-check.** Before sending the final message of a turn, ask: is there a background task, subagent, or detached job that is the answer the user asked for? If yes, keep working. If no, respond.

For long-running jobs that genuinely need to survive session teardown (>2 min), use the `run-detached` skill — that is a different tool for a different purpose and is not covered by "foreground by default."

## Server restarts — CRITICAL RULES

### You must have explicit permission FIRST
Do NOT restart any server (web or daemon) unless the user has **explicitly told you to restart** in the current session. Making code changes does not imply permission to restart. If you think a restart is needed, ASK the user — do not just do it.

### Web server only (port 5050)
When the user gives you explicit permission to restart, you may ONLY restart the web server:
```bash
curl -s -X POST http://localhost:5050/api/restart -H "Content-Type: application/json" -d '{"scope":"web"}'
```
This restarts only the Flask web server. The session daemon (port 5051) stays alive and all running Claude sessions/agents are preserved.

### NEVER restart the daemon (port 5051) — ABSOLUTE PROHIBITION
The daemon manages ALL active Claude sessions and agents. Restarting it destroys every running agent across the entire application. **No AI agent is allowed to restart the daemon under any circumstances.** This is not a guideline — it is a hard rule with zero exceptions.

- NEVER use `scope: "daemon"` or `scope: "both"` in the restart endpoint.
- NEVER kill, stop, or restart the daemon process by any means.
- If a user asks you to restart the daemon, **warn them** that doing so will terminate all active sessions and agents across the entire application. Direct them to do it manually if they still want to: **System → Restart Server → Session Daemon**.

The `/api/restart` endpoint accepts a `scope` parameter: `"web"` (default), `"daemon"`, or `"both"`. AI agents must only ever use `"web"`, and only when the user has explicitly asked for a restart.

### No direct process management
Do NOT use subprocess, os.system, taskkill, or any other method to start, stop, or manage server processes directly. No terminal window spawning. The only allowed restart mechanism is the `/api/restart` endpoint with `scope: "web"`.

## File organization — keep the root clean
All planning documents, implementation notes, design specs, task breakdowns, and working docs belong in `docs/plans/` — NEVER in the project root. The root directory is for code, config, and the README only. If you need to create a spec, plan, or notes file, put it in `docs/plans/`. This folder is gitignored and is not shipped to users.

## Workforce agents — follow the authoring standard
When you create or edit any `.md` file in `workforce/`, you MUST first read `workforce/AGENT_BEST_PRACTICES.md` and follow the rules it defines (Invocation Contract section, numbered Output Format with "Obstacles Encountered", unique-value statement, version bump, etc.). The agents in `workforce/` are loaded into every Claude session's catalog via `/api/workforce/assets`, so structural inconsistencies in them propagate to every spawned subagent. Treat the authoring standard as load-bearing, not optional.

## Performance-critical patterns — DO NOT MODIFY without profiling

VibeNode underwent a measurement-driven performance overhaul. The patterns below were profiled and validated with real instrumentation. Reverting any of them causes measurable regression. Look for `PERF-CRITICAL` markers in the code.

**Before modifying any code near a `PERF-CRITICAL` marker, you MUST understand the performance reason documented there. If you think the code can be "simplified" or "cleaned up," that is almost certainly a regression. ASK the user before changing it.**

1. **`is_post_turn` guard on `_detect_changed_files`** — `daemon/session_manager.py`. Pre-turn runs cause a 199-file scan per message (+2-138ms). Do NOT call `_detect_changed_files` unconditionally.
2. **`asyncio.gather()` in `_send_query`** — `daemon/session_manager.py`. `_write_file_snapshot` and `_record_pre_turn_mtimes` run in parallel. Sequential awaits add 60-70ms. Do NOT replace with sequential calls.
3. **`_turn_had_direct_edit = False` placement** — `daemon/session_manager.py`. Must reset BEFORE the gather, not between/after. Moving it creates a race condition.
4. **Mtime carry-forward in `_record_pre_turn_mtimes`** — `daemon/session_manager.py`. Carries forward `_post_turn_mtimes` from the previous turn. Removing forces a full `git ls-files` + stat every turn.
5. **First-turn mtime overlapped with `client.connect()`** — `daemon/session_manager.py` `_drive_session`. `run_in_executor` starts before `await client.connect()`. Moving it after adds 70-90ms.
6. **`get_entry_count` method** — `daemon/session_manager.py`. Returns `len(info.entries)` without serialization. Do NOT replace with `get_entries` (25-32ms vs 0-1ms).
7. **`tracked_files` snowball prevention** — `daemon/session_manager.py` `_write_file_snapshot`. `fs_changed` from `_detect_changed_files` are snapshot extras only, NOT added to `tracked_files`. Adding causes 20-55s turns with 1400+ entries.
8. **Debounced `_save_queues()`** — `daemon/session_manager.py`. 1-second timer batches disk writes. Do NOT call `_save_queues_now()` directly from queue operations.
9. **`_GIT_LS_FILES_CACHE_TTL`** — `daemon/session_manager.py`. Currently 180s. Do NOT reduce below 120s.
10. **`get_all_states()` cache** — `app/session_awareness.py`. 2s TTL. Do NOT remove or bypass.
11. **`get_kanban_config()` cache** — `app/config.py`. 10s TTL with invalidation on save. Do NOT remove.
12. **Module-level `_setup_executor`** — `app/routes/ws_events.py`. Do NOT create per-request.
13. **`_cleanup_system_sessions()` at startup only** — `app/__init__.py`. Do NOT call from `all_sessions()` or any per-request path.
14. **IPC profiling logger namespace** — `run.py`. `"app.daemon_client"` must be in the logger namespace list. Removing silences IPC profiling.
15. **`allSessionIds` Set** — `static/js/app.js`. Must stay in sync with `allSessions` at all mutation sites. Do NOT replace `.has()` with `.find()`.
16. **Watchdog dedup** — `static/js/live-panel.js`. `window._watchdogSid`/`window._watchdogTimer` enable cross-script dedup. Do NOT remove the `window.` assignments.
17. **`performance.mark()`/`performance.measure()` instrumentation** — `static/js/socket.js`. Submit timing and session switch timing. Do NOT remove.
18. **Chrome-first browser launch** — `run.py` `_find_chrome()` / `_find_chrome_linux()` / `_find_chrome_macos()` + `open_browser()`. The Web Speech API (voice input) is Chromium-only. ALL THREE PLATFORMS must find and launch Chrome/Chromium before falling back to the system default browser opener — the default may be Firefox, which silently breaks voice with no error messages. This regression already happened once on Windows and shipped to users. Do NOT replace any platform's Chrome-first path with only the system fallback (`os.startfile`, `xdg-open`, or `open`) as the sole method. Platform pattern:
   - Windows: `_find_chrome()` → `ShellExecuteW(chrome, --app=URL + --user-data-dir=DIR)` → `os.startfile` fallback
   - Linux:   `_find_chrome_linux()` → `Popen([chrome, --app=URL, --user-data-dir=DIR])` → `xdg-open` fallback
   - macOS:   `_find_chrome_macos()` → `Popen([chrome, --app=URL, --user-data-dir=DIR])` → `open` fallback

    **Isolated Chrome instance (added 2026-06-13).** All three platforms pass `--app=<URL>` and `--user-data-dir=data/chrome-profile` so VibeNode runs in its own Chrome window with its own profile. Without isolation, VibeNode borrowed the user's everyday Chrome and the launcher-spawned window wedged Chrome's focus state — new windows opened from outside VibeNode would silently no-op until Chrome was fully closed and reopened. The dedicated profile also bypasses Chrome's session restore (no "Continue where you left off" interference on cold start), which was the original reason `--new-window` existed. Do NOT remove the `--app=` or `--user-data-dir=` flags. The profile directory is gitignored (`data/chrome-profile/`).

    **`browser_launch_mode` toggle + tradeoff-free tab mode (added 2026-07-13).** `open_browser()` reads `kanban_config.json["browser_launch_mode"]`. **Default is `"tab"`** — a tab in the user's everyday Chrome profile, how VibeNode behaved for most of its history. `"app"` opts back into the isolated app window; item 18's flags are intact and reachable via `"app"`, only the default changed.

    Tab mode does NOT reintroduce the two 6/13 bugs, because they are mutually exclusive by Chrome's running state and `open_browser()` now branches on it via `_chrome_running()`:
    - **Chrome already running** → bare URL → new tab. A focus wedge requires a launcher-spawned *window*, not a tab; and session restore already ran on Chrome's own startup, so there is nothing to swallow the URL.
    - **Chrome not running (cold start)** → `--new-window <url>` → forces the URL to display (the original pre-6/13 fix for "Continue where you left off" swallowing a bare URL), and there is no running Chrome for a new window to wedge.

    So `--new-window` is used ONLY on a cold start, which is exactly when it cannot wedge. Do NOT collapse the `_chrome_running()` branch to an unconditional bare URL (reintroduces the cold-start swallow) or an unconditional `--new-window` (reintroduces the focus wedge). `_chrome_running()` biases to `True` on detection failure so the common already-open case never risks a wedge.

## Compose project-scoping — DO NOT REMOVE (fixed 2026-04-13)

Three bugs combined to make the Compose feature unusable across multiple VibeNode projects. All three fixes are load-bearing — reverting any one of them re-breaks Compose.

1. **`?project=` filter in `_addToCompose()`** — `static/js/sessions.js`. The right-click → Add to Compose fetch MUST pass `?project=<activeProject>` so the API only returns compositions belonging to the current project. Without it every composition across all projects is returned and the picker shows unrelated items. Do NOT remove the query param from the fetch call.

2. **Stale-project fallback in `initCompose()`** — `static/js/compose.js`. When the saved `_activeComposeProjectId` (from localStorage) points to a deleted or missing composition, `initCompose` MUST clear the stale ID and retry with just the `?project=` parent filter. Without this fallback the compose view renders completely empty even when valid compositions exist. Do NOT remove the `if (_activeComposeProjectId)` retry block inside the `if (!data || !data.project)` guard.

3. **Snapshot-based test cleanup** — `tests/test_compose_api.py`. The `cleanup_projects` fixture MUST snapshot `COMPOSE_PROJECTS_DIR` before each test and remove anything new after. The old `startswith("test-")` check missed cloned projects (directory names like `copy-of-test-clone-src-*` and UUIDs), which leaked 52 orphan projects into production data. Do NOT revert to name-prefix cleanup.

4. **Compose DOM skeleton preserved on project switch** — `static/js/app.js`. When the user switches projects while in compose view, the cleanup code MUST NOT set `compose-board.innerHTML = ''`. The `#compose-board` container holds static child elements defined in `index.html` (`compose-root-header`, `compose-input-target`, `compose-sections-board`) that `initCompose()` writes into by ID. Nuking the parent's innerHTML destroys those elements and `initCompose()` silently writes to `null`, producing a blank panel even though the API returns valid data. Only clear `compose-sections-board.innerHTML` (the dynamic card area). Do NOT replace with a blanket `innerHTML = ''` on the parent — this exact regression already blanked the compose panel in production (fixed 2026-04-14).

## Detached web-server launch — DO NOT REVERT (fixed 2026-06-13)

The web server is spawned detached on every platform. Reverting any of the pieces below re-introduces the "minimized launcher window got closed → web server dies → user sees a dead page even though sessions are intact" failure mode that hit a user on 6/12.

1. **`launch.bat` uses `pythonw.exe`** — `start "" pythonw session_manager.py` then `exit /b`. pythonw has no console window, so there is nothing for the user (or Windows on sign-out) to close. Fallback to legacy `python session_manager.py` (minimized re-launch) only when pythonw is genuinely missing from PATH. Do NOT switch the primary path back to `python` or remove the `start ""` — both reintroduce the closeable window.

2. **`launch.sh` background-spawns with `nohup` and `disown`** — `nohup "$PY" session_manager.py >> logs/_server.log 2>&1 &` followed by `disown`. The terminal can close without taking the server down via SIGHUP. Output goes to `logs/_server.log` so diagnostic prints from `ensure_daemon()` and `run.py` are preserved. Do NOT remove `nohup`, `&`, or `disown` — the trio is what makes the spawn a true daemon under bash.

3. **Spawn-mode log line in `session_manager.py`** — writes `spawn exe=pythonw mode=detached(...) sid=… pgid=…` to `logs/_server.log` immediately on startup. This is the only on-disk signal that tells future maintainers whether a given run was attached or detached. If a future launcher regression silently foregrounds the server, this line is the smoking gun.

4. **`server-reachable` health check in `static/js/healthchecks.js`** — registers a blocker overlay (same machinery as the wifi check) that probes `/api/ping` every 5s and shows "VibeNode Server Unreachable" after 3 consecutive failures. Catches any post-load server death (crash, manual kill, port conflict) that the detached spawn doesn't prevent. The `/api/ping` route lives in `app/routes/main.py` and must stay side-effect-free so it never lies about reachability. Do NOT remove the check or repurpose `/api/auth-status` — auth-status is a heavier call that can stall on a slow Claude CLI shell-out and would produce false positives.

## Slash commands are intercepted client-side
Claude CLI slash commands (e.g. `/compact`, `/rewind`, `/clear`) are NOT sent to the SDK. They get silently eaten with no response, leaving the session stuck idle. Instead, `_interceptSlashCommand()` in `live-panel.js` catches them at every submit path and either triggers the GUI equivalent (e.g. `/rewind` clicks the Rewind toolbar button, `/compact` fires `liveCompact()`) or shows a toast explaining the command isn't supported in the GUI. The command map lives in `_slashCommandMap`. Messages with `/` that aren't bare commands (e.g. "fix /etc/config") pass through normally.

## Mobile socket recovery — the DO / DO NOT list (2026-07-14 → 2026-07-15)

Attempting to auto-heal WebSocket zombies on mobile went through several iterations, some of which caused their own severe regressions. This section captures the survivors and the tombstones so a future maintainer doesn't re-introduce the bugs.

### The original problem
After being on for a while — phone locks, tab backgrounds, Tailscale hands off between wifi/cellular, iOS Safari bfcache-restores the tab — the WebSocket transport can die silently. `socket.connected` still reports `true`, Socket.IO's ping/pong takes ~30s to notice, and any `socket.emit()` on that zombie is dropped. The user saw stale UI or an "infinite skeleton" that only cleared after closing the tab and clearing history.

### What SURVIVED and is load-bearing

1. **bfcache reload** (`static/js/socket.js` — `pageshow` handler). When `event.persisted === true`, the entire JS runtime was frozen and thawed with the underlying transport dead. `socket.disconnect() + socket.connect()` cannot cleanly rebuild from that corrupted state — a full `window.location.reload()` is the only definitive recovery. Guarded by `#restart-overlay` presence so an in-app restart's own reload flow is never fought. This is the one "aggressive" mechanism that stayed because it targets a genuinely different failure mode (frozen JS runtime), not merely a dead transport.

2. **`socket.on('connect')` unconditionally re-emits `get_session_log`** (`static/js/socket.js`). Socket.IO does NOT replay events missed during a disconnect, so any `session_entry` / `session_state` push that fired during the outage is lost forever. If a live session is open, always re-fetch its log on every reconnect. Fires only when the socket actually reconnected (not speculatively), so it's cheap and correct.

3. **Skeleton-stuck watchdog on `get_session_log`** (`static/js/live-panel.js` `window._skeletonStuckTimer` and `window._loadMoreStuckTimer`). Two-stage: after 8s of no response, re-emit once; after another 8s (16s total), probe `/api/ping` over HTTP — if HTTP responds fast but WebSocket produced nothing, socket is a proven zombie and gets cycled ONCE. Hard rate-limited by `window._skeletonZombieCycleAt` to at most one cycle per 60s across the entire page. This is the ONLY code path in the codebase permitted to cycle a socket based on WebSocket-vs-HTTP disagreement; everything else stays strictly passive. Cleared by the `session_log` handler in `socket.js` on any matching response. The rate limit + user-visible-symptom trigger (skeleton on screen, not a background timer) is what prevents the 2026-07-15 storm from recurring.

4. **`_wakeSocketResync()` on visibilitychange/pageshow/focus/online** (`static/js/socket.js`). Only takes two actions: (a) if the socket claims disconnected, call `socket.connect()`; (b) if connected, emit `request_state_snapshot` to refresh state. **Does NOT cycle a connected-looking socket under any circumstance.** Debounced 750ms.

### What was TRIED and REVERTED (2026-07-15) — DO NOT re-add these

The following mechanisms were added between 2026-07-14 evening and 2026-07-15 morning and caused a worse regression than the original bug (flashing "Engine Stopped" overlay, in-flight session streams dropped mid-response). Do NOT reintroduce without a fundamentally different design:

- **`_BG_CYCLE_THRESHOLD_MS` (3s) backgrounded-tab always-cycle path.** Bug: `_lastForegroundAt` (misnamed; actually set on visibilitychange→hidden) was never reset after being used. Once the tab had been backgrounded once, `bgDuration = now - _lastForegroundAt` stayed huge FOREVER, so every subsequent focus/tap re-cycled the socket. Cycling a live socket drops in-flight `session_entry` pushes (user reported this as "session stream got fucked"). It also floods the server with reconnect handshakes, each triggering `get_all_states()` IPC to the daemon — enough congestion to trip the daemon heartbeat (`app/daemon_client.py` HEARTBEAT_TIMEOUT = 8s) and produce a spurious "Engine Stopped" overlay. If you ever bring back a "cycle after real backgrounding" heuristic, `_lastHiddenAt` MUST be reset to `now` inside `_wakeSocketResync` after computing `bgDuration`, and there MUST be a hard per-cycle rate limit (e.g. one cycle per 30s max) so no single user interaction can chain into a cycle storm.

- **20s foreground zombie watchdog** (`setInterval` that cycled the socket if `staleness > 45s`). Bug: idle sessions legitimately go 45s+ between server pushes. The watchdog fired on healthy-but-idle sockets and killed streams mid-response. If you ever need a "socket died silently while foregrounded" detector, prefer an actively-emitted heartbeat with a specific response event, not raw event silence.

- **`_wakeSocketResync()` call from `startLivePanel()`.** Combined with the `_lastForegroundAt` bug above, this cycled the socket on every tap into a session after any prior backgrounding. On its own it's not fatal, but adding it back means the wake-resync function had better stay strictly passive (only reconnect if disconnected, never cycle).

- **Active client heartbeat (`client_ping` / `client_pong`).** Added and reverted 2026-07-15 in the same session. Design was "silence alone is never enough — only a missed pong cycles the socket, rate-limited to 1/min." Failed in practice because Flask's threading async_mode serializes handlers behind the concurrent-daemon-IPC bottleneck; under a real load burst (multiple session_state updates + get_all_states from the awareness cache miss), pongs took >8s → heartbeat cycled → fresh handle_connect fired another get_all_states → made the congestion worse → chain of "Engine Stopped" flashes. If you ever try again: pong must be served by a dedicated non-blocking path (separate thread/queue) that CANNOT be starved by IPC latency, AND the cycle rate limit must be at least 5x the observed p99 IPC time under peak load.

**The rule that emerged:** cycling a socket that reports `connected` based on any heuristic is a foot-gun. Cycling drops in-flight streams (Socket.IO has no replay), triggers a fresh handle_connect that cascades into daemon IPC, and can chain across many taps if the heuristic isn't perfectly reset. Only cycle when you have proof the transport is broken — a failed response to an actively-emitted probe, or a `disconnect` event from Socket.IO itself.

### Diagnosing future regressions
Watch the browser console for `[WS]` and `[skeleton-watchdog]` lines. If a user reports "stale UI after being away," the presence of `wake: socket disconnected — forcing reconnect` near the incident means the recovery machinery ran. Absence means a lifecycle-event binding was removed. If a user reports "session stream cut off mid-response," search for any `disconnect()` + `connect()` pair in the socket.js code path — that's almost always the cause.
