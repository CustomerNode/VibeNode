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
- If a user asks you to restart the daemon, **warn them** that doing so will terminate all active sessions and agents across the entire application. Direct them to do it manually if they still want to: **System → Developer Tools → Restart Server → Session Engine**.

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

## Sleep must stick — user-stop intent beats ghost recovery (fixed 2026-07-20)

`_liveSubmitDirect()` in `static/js/live-panel.js` arms "ghost recovery" timers on every
submit: if no `session_state` arrives within 3s, it emits `close_session` and then, 1.5s
later, `start_session` with `resume: true` — reviving a session the daemon silently
dropped and re-sending the last prompt.

Their cancel handlers (`_ghostCancel` / `_ghostCancel2`) deliberately ignore
`state === 'stopped'`, because a genuinely dead session also reports stopped and that is
exactly the case recovery exists to repair. The side effect was the **"Sleep won't stick"
bug**: the one event proving the user slept the session was the one event that could not
cancel recovery. Sleeping within ~3s of sending got silently overridden — the session came
back awake and re-ran the prompt. The daemon was innocent (its own
`SessionManager._user_stopped()` guard held correctly); the resurrection was entirely
client-side, which is why the session looked dead in `~/.claude/gui_active_sessions.json`
yet kept reappearing in the UI.

The fix mirrors the daemon's guard on the client. The discriminator is not the *state* but
the *cause* — did the user ask for this stop?

- `markUserStopped(sid)` / `clearUserStopped(sid)` / `isUserStopped(sid)` live in
  `live-panel.js` and are exported on `window` for cross-script use.
- **Every explicit user stop path must call `markUserStopped()` BEFORE emitting
  `close_session`** so an already-pending ghost timer sees the intent when it fires:
  `closeSession()` (live-panel.js), `deleteSession()` (toolbar.js), `sleepAllSessions()`
  (app.js), `_bulkStop()` and `deleteOne()` (sessions.js). If you add a new sleep/stop/delete
  entry point, add the call there too.
- Both ghost timers check `isUserStopped()` **twice** — once when the 3s timer fires and
  again inside the 1.5s close→start gap, because the user can hit Sleep mid-recovery.
- The `send_message` error fallback (`/not found|is stopped/`) checks it too — "is stopped"
  is precisely what the daemon returns for a freshly-slept session.
- An explicit send (`_liveSubmitDirect`, `_autoSendPendingInput`) clears the flag: the user
  asking for work supersedes an earlier sleep.
- A 60s TTL (`_USER_STOP_INTENT_TTL_MS`) is a backstop so a stale flag can never permanently
  disable ghost recovery. Recovery windows are at most ~4.5s.

Do NOT "simplify" the ghost timers by cancelling on `state === 'stopped'` — that restores
Sleep but breaks recovery for genuinely dead sessions. Do NOT remove the second
`isUserStopped()` check inside the inner `setTimeout`; without it, sleeping during the
close→start gap is still ignored.

## Test runs of Claude must never save sessions into a user project (added 2026-10-05)

Any probe, smoke test, or verification that launches Claude (`claude -p`, the Python SDK, `ClaudeSDKClient`, a subprocess, anything) MUST NOT write a transcript into a real project. VibeNode lists every `.jsonl` under `~/.claude/projects/<project>/` as a session, so a "reply ok" probe run from the repo shows up in the user's sidebar as a session they never made. This happened on 2026-10-05: two "reply ok" sessions appeared in Program VibeNode from a model-verification script.

- **CLI:** always pass `--no-session-persistence`.
- **Python SDK:** pass `extra_args={"no-session-persistence": None}` in the options.
- **If persistence can't be disabled:** set the working directory to `~/.claude/_system` (VibeNode's hidden utility project), never the repo or any user project folder.
- If a test session leaks anyway, trash it via `DELETE /api/delete/<id>?project=<encoded>` (recoverable) and say so in your report.

## The kill path fails closed, and agents run the suite sandboxed (2026-10-06)

Running the test suite logged the desktop out and killed every agent session three times on 2026-10-06. A test's `MagicMock` backend returned a mock from `extract_process_pid`; `SessionManager._kill_process_tree` (`daemon/session_manager.py`) had its `pid <= 1` guard inside a `try/except Exception: pass` that fell through on the `TypeError`, `os.getpgid(mock)` coerced the mock to 1, and the function called `os.killpg(1, ...)`, which is `kill(-1)`: every process the user owns.

- **`_kill_process_tree` refuses anything it cannot prove is one CLI:** a pid that is not a plain `int` > 1, the daemon's own pid, or a resolved process group that is not a plain `int` > 1. Do NOT loosen these checks or put them back inside a swallow-everything `try`. Tests: `tests/test_kill_process_tree_guard.py`.
- **`tests/conftest.py` wraps `os.kill` / `os.killpg` at import time** for the whole pytest process and raises on pid/pgid 0, 1, negative, or non-int. It is the backstop for the next bug of this shape; do NOT turn it into a fixture (teardown ordering and background threads would escape it).
- **A fake backend has no process:** a test that replaces `_sdk` with a `MagicMock` sets `extract_process_pid` to return `0`.
- **Every run of the suite puts itself in a PID namespace (Linux).** `pytest_configure` in `tests/conftest.py` re-execs the same command line under `unshare -U --map-current-user -p -f --mount-proc --kill-child`, with `bash` as pid 1 (an init that reaps orphans; with pytest as pid 1 three process-group tests fail) and `setsid -w` giving pytest its own group. Inside the namespace a broadcast signal can only reach the test run itself, so this whole class of bug cannot touch the desktop, VibeNode or agent sessions. Verified with a harmless `SIGCONT` broadcast: a canary outside never received it. It lives in conftest, not in a wrapper script, because the suite is started from many places (the UI's pre-publish run, agent sessions, a terminal) and all three logouts came from a plain `pytest`. So a plain `pytest` IS the safe way to run it; do NOT remove the re-exec or move it behind an opt-in. Where `unshare` is unavailable the run warns and continues behind the two guards. `VIBENODE_TEST_NO_SANDBOX=1` opts out. Guard test: `test_the_suite_is_running_inside_its_own_pid_namespace`.

## Floating notices share ONE stack above the composer (added 2026-10-05)

Every bottom-of-screen notice (`#toast`, `#git-sync-mini`, `.vn-undo-toast`, `.compose-undo-toast`) is placed by `_layoutFloats()` in `static/js/utils.js`: one right-aligned stack sitting `_FLOAT_CLEARANCE` above the chat composer (`#live-input-bar`), or 20px off the corner when no composer is on screen. They used to sit in three different corners at fixed offsets, and the bottom-right ones covered the Send button; the clickable git-sync indicator blocked it outright during every sync. Rules: anything that shows or hides one of these calls `_updateFloatOffset()` / `_layoutFloats()`; a NEW floating notice is added to `_floatEls()` instead of getting its own fixed `bottom`/corner; do not give any of them a hard-coded `bottom` in CSS (a desktop `bottom: 22px` override silently undid the toast fix once).

**Nothing may ever cover the composer's paste / mic / Send buttons** (hardened 2026-10-05, hours after the stack shipped — the first version still covered them three different ways). Each fix below is load-bearing; tests: `tests/test_float_stack_layout.py`, which drives the real CSS and JS in headless Chromium and asserts zero intersection at phone-portrait, phone-landscape and desktop.

1. **The on-screen test is `bar.getClientRects().length`, NOT `bar.offsetParent`** — `_updateFloatOffset()`. `offsetParent` is `null` for ANY `position: fixed` element in Blink and WebKit, and the phone layout pins the composer with `position: fixed` (`mobile.css .live-input-bar`). So on every phone the bar measured as off-screen, the lift fell back to 20px, and the whole stack landed on all three buttons. Do NOT go back to `offsetParent`, nor to an `offsetHeight` guard (also 0 under a hidden ancestor).

2. **No float may transition its own `bottom`** — `_layoutFloats()` writes `bottom`, so an animated one travels across the buttons on its way to its slot. `.git-sync-mini` used `transition: all 0.25s`, and `all` includes `bottom`: the indicator slid up across Send for 250ms on every sync. Name the cosmetic properties instead. Guard test: `test_no_float_transitions_its_own_bottom`.

3. **`_FLOAT_CLEARANCE` (14px) must exceed every float's entry travel.** Each notice slides up into place from below its final `bottom`, so the travel distance is how far it dips toward the composer mid-animation. The travels are normalised to 10px in CSS (`miniSlideIn`, `.toast`, `.vn-undo-toast`, `.compose-undo-toast`). Raise a transform, raise the clearance. Guard test: `test_entry_animations_cannot_dip_into_the_composer`.

4. **The composer's "+" menu anchors by `bottom`, not `top`** — `showMenu()` in `static/js/image-attach.js`. A `top: Math.max(8, …)` viewport clamp pushed the menu back DOWN over the very buttons it opens from whenever it was taller than the space above them (short viewport, or landscape with the keyboard up). Pin `bottom` 8px above the button row and cap `max-height`; the menu scrolls instead (`overflow-y: auto` on `.vn-more-menu`). `_positionStatusPanel()` in `invoke-workforce.js` already used this pattern.

5. **The composer's top edge is a tooltip clip boundary** — `place()` in `static/js/tooltip.js`. Tooltips prefer dropping below their anchor and only flipped when the VIEWPORT bottom would clip them, so an anchor sitting just above the composer put its tooltip on the button row. `floor = min(vh, barTop)` makes the existing flip-above logic handle it.

6. **The float stack re-measures when the on-screen keyboard moves the bar** — `updateKeyboardOffset()` in `static/js/mobile.js` calls `_updateFloatOffset()`. The keyboard changes the bar's `bottom`, not its size, so the `ResizeObserver` behind the stack never fires and the lift would go stale.

## Publish-gate fix sessions belong to the VibeNode project (fixed 2026-10-05)

`_startFixSession()` in `static/js/git-sync.js` (the "Fix errors" button after a failed pre-publish test run, and "Fix with AI" after a security scan) creates its session in THIS repo's project, resolved from `GET /api/repo-project` (`app/routes/project_api.py`), switching the browser to that project first when it is registered. It used to use the browser's active project, so pressing Update while viewing CustomerNode put a "Fix test failures" session in CustomerNode. The session works on VibeNode's tests and scan, so it lives in VibeNode's project; do not go back to `_currentProjectDir()` for its `cwd`.

## A resume never runs without a folder (fixed 2026-10-05)

`start_session(resume=True)` with an empty `cwd` takes the folder from `SessionManager._transcript_cwd()` (`daemon/session_manager.py`). Without it the CLI started in the daemon's own directory, the VibeNode repo: a CustomerNode session whose registry entry had an empty `cwd` was woken by a model switch (`_resume_with_model`), ran with VibeNode's CLAUDE.md, and showed up under VibeNode as "New Session". `_transcript_cwd()` does NOT use the latest `cwd` in the transcript, because that follows every `cd` and records any wrong folder a bad wake caused. It returns the first recorded `cwd` whose `_encode_cwd()` equals the name of the project directory the `.jsonl` lives in, which is the session's real home. Tests: `test_resume_without_cwd_uses_transcript_cwd` in `tests/test_session_manager.py`.

## Session-id remap fires on init, not only on RESULT (fixed 2026-10-05)

A session launched under a client temp id adopts the CLI's real id via `SessionManager._remap_session_id()` (`daemon/session_manager.py`), called from BOTH the `init` handler and the RESULT handler. The `init` call is load-bearing: the CLI announces its real id in the init message seconds into the turn, so a first turn that CRASHES before any RESULT (seen 2026-10-05, Windows CLI exit code 3221225786) no longer strands the temp id as a fileless "working" phantom while the real transcript lives under the CLI id. That stranding is what produced duplicate sidebar rows (two "Source Code Fix", two "Template Fix" in CustomerNode). The helper is idempotent (early-returns when the alias already exists), so the RESULT call re-firing it is a harmless backstop for the no-init path. The init remap runs AFTER the init branch's model broadcast, so that broadcast still emits under the launch id the client currently knows. Do NOT delete the init-handler call as "redundant with RESULT" — RESULT is too late when the first turn dies. Tests: the init-remap block in `tests/test_session_manager.py::TestSetSessionModel`.

## Stranded background workers: the watchdog's job 4 (added 2026-10-06)

Claude Code delivers a **nested** agent's completion notice (an agent launched *by a background worker*, typically its adversarial reviewer) to the top-level session, never to the worker that launched it. Transcripts on disk show this in every CLI version from 2.1.235 through 2.1.291. It became a deadlock once the CLI let a worker end its turn to wait for its own agent ("It may resume on its own when that work completes"). The worker waits forever. The top-level session ends its turn "waiting on the worker's final report". Nothing is left running, so nothing wakes anyone. On 2026-10-06 two workers sat 343 and 317 minutes after their reviewers had finished, while the session showed plain Idle.

The fix cannot live in the CLI binary, so `HealthMonitor` job 4 (`daemon/health_monitor.py`) detects the state from the CLI's own files (`daemon/orphaned_workers.py`). The inputs are the root transcript's `queue-operation`/`enqueue` notification lines, `subagents/agent-<id>.meta.json` (`parentAgentId` marks a nested agent), and each agent's transcript (a stopped agent ends on `assistant` + `stop_reason: end_turn`). Once a session has been IDLE for 90s, the monitor sends it ONE message naming each stranded worker and the result to forward with SendMessage.

- **Jobs 1-3 only watch WORKING sessions.** This failure is an IDLE session, which is why the stall watchdog never fired. Do not gate job 4 on WORKING.
- **Each stranded result is nudged at most once** (`_orphan_nudged`, keyed by session, nested agent and delivery time). Sessions the user stopped (`_interrupted`) or that have queued messages are skipped. Results delivered before the daemon took the session on (`created_ts`) are ignored, so a daemon restart never nudges stale history.
- **The visible signal is the `_announce` system entry.** `send_message` only pushes the user bubble to clients after an interrupt, and this nudge has none.
- **The scan is cheap by construction.** It does a byte-level `find` over the root transcript, reads it incrementally, and skips the evaluation entirely while the transcript size is unchanged. Do not replace it with a full JSON parse per tick.
- Kill switch: `VIBENODE_ORPHAN_RECOVERY=0`. Tests: `tests/test_orphaned_workers.py` (detection against real file layouts) and the job-4 block in `tests/test_health_monitor.py`.

## Silent-wait backstop: the watchdog's job 5 (added 2026-10-07)

Every stall fix before this (jobs 1 and 4, phantom wake-ups, queue starvation) recognised one specific cause, so each new cause got through until someone diagnosed it, and the user lost the hours in between. Job 5 (`daemon/silent_wait.py`, wired in `daemon/health_monitor.py`) watches the **symptom**, whatever the cause. The session is IDLE. Its final message says it is waiting on background work it launched in this task. Nothing is alive.

- **Liveness is the discriminator, not wording or silence.** On the full local history (2,082 turn endings), most "waiting" was waiting on the user, and every slow-but-legit wait (up to 72 min without an agent writing) had a background command still running. "Alive" means three things: a write anywhere in the footprint (root transcript, `subagents/*`, the CLI's `tasks/*.output`), a background shell under the CLI process (`live_commands`, via psutil), or a CLI task event (`_bg_work_at`).
- **Thresholds.** It acts after 10 min of total silence when nothing runs, after 45 min when commands are alive but nothing writes (hung command, forgotten dev server), and after 30 min when psutil is missing.
- **Act once per task, then tell the user.** The first trigger nudges the session. If the same task idles "waiting" on dead work again, it escalates: an error entry, plus a `session_stalled` push that `static/js/socket.js` turns into `VNNotify.attention` (chime, notification, title flash) in every open tab. It never nudges twice for the same task. A new user message re-arms it.
- **The daemon hooks feeding it are timestamps only, with no I/O.** `send_message` stamps `_task_started_at` on genuine sends only (not self-heal or auto-retry resends). `_process_message` stamps `_bg_work_at` on background launches (`_tool_launches_background`) and on CLI task events (`_TASK_EVENT_SUBTYPES`). Do not move these into `_send_query`.
- Do not gate the wording check on generic "wait": it must stay "waiting on background work" and exclude waiting on the user (see the regex tests). Do not drop the liveness check to "simplify": silence alone false-alarms on quiet test runs.
- Kill switch: `VIBENODE_SILENT_WAIT_RECOVERY=0`; quiet window: `VIBENODE_SILENT_WAIT_MINUTES`. Tests: `tests/test_silent_wait.py`.

## Usage limits continue by themselves at reset (fixed 2026-10-07)

A long autonomous session hit "You've hit your session limit · resets 5pm (America/New_York)" at 4:43pm and was still dead after 5pm, showing "Session ended with error". Four gaps stacked, all in `daemon/session_manager.py`, and each fix is load-bearing:

1. **Recognition.** `_parse_usage_limit` did not know the CLI's new wording. The nouns now include named windows ("session limit", "weekly limit", "5-hour limit", and so on), and "hit" counts as exhaustion. A negative guard covers warnings ("approaching", "you'll hit"). The reset comes from an epoch when present, otherwise from the clock form ("resets 5pm (America/New_York)", "resets Oct 9, 5pm") via `_parse_reset_clock`, which falls back to local time when the zone can't be loaded. A clock time that passed less than 2h ago means "just reset", not "tomorrow".
2. **Continue at reset.** `_schedule_limit_resume` runs right after `_apply_usage_limit`. It sets `_limit_resume_at`: the reset + 90s, or +2 min if the reset just passed, or a probe every 30 min if no reset time was given (bounded by `_API_RETRY_MAX`). `_arm_api_retry` waits for that instant instead of the exponential backoff, so the countdown, Cancel and Retry-now all work. The model-switch CTA stays. The limit banner (`_buildUsageLimitBanner` in `static/js/live-panel.js`) shows "continues automatically in …" with Cancel. Do not go back to "a limit never auto-continues".
3. **Trimmed history.** Idle sessions keep only the last 200 in-memory entries, so a long turn trims the user's message away. `_has_user_message` used to answer "nothing to resume" and every retry was refused. It is now trim-aware (`_entries_trimmed`), and `_fire_api_retry` / `_resume_turn_prompt` continue with the CONTINUE prompt in that case. Do not go back to scanning `info.entries` alone.
4. **Notification-triggered turns.** Turns started by a background-task notification are consumed by the post-turn drain/listener and never pass through the `_send_query` / `_drive_session` `finally` blocks that arm retries. `_extended_post_turn_listener` must call `_arm_flagged_retry` both at its start and after each RESULT. Without it, any error ending such a turn (limit or 529) is flagged and never armed.
5. **Disk backstop: the guarantee (HealthMonitor job 6, `daemon/limit_watch.py`).** The user's rule is that a session stopped by a usage limit must ALWAYS continue after the reset. Points 1-4 are in-memory, so a Session Engine restart loses the timer, and they depend on the wording and the turn path. Job 6 does not. Every 2 minutes it reads the transcripts of live-IDLE and dormant (restart-memory) sessions. If one still ends on the CLI's structural stop entry (`isApiErrorMessage` + `error: "rate_limit"`, or `"server_error"`) after the stated reset + 5 min, it sends the continue prompt. If there's no reset time, it probes at 30 min, 1h, 2h, then every 4h. A stop written after its own stated reset (stale or rounded time) backs off the same way. It skips WORKING sessions, `_interrupted` (Stop), STOPPED, and anything not in the restart memory (an explicit Sleep/Delete calls `forget_dormant`), so "Sleep must stick" holds. It also skips sessions with a live Layer-1 countdown, until that countdown is 10 min overdue. Do not narrow it back to in-memory state.
- Kill switches: `VIBENODE_LIMIT_AUTO_RESUME=0` (Layer 1), `VIBENODE_LIMIT_BACKSTOP=0` (job 6). Tests: `TestUsageLimitAutoResume` in `tests/test_api_error_retry.py`, `tests/test_limit_watch.py`.

## Slash commands are intercepted client-side
Claude CLI slash commands (e.g. `/compact`, `/rewind`, `/clear`) are NOT sent to the SDK. They get silently eaten with no response, leaving the session stuck idle. Instead, `_interceptSlashCommand()` in `live-panel.js` catches them at every submit path and either triggers the GUI equivalent (e.g. `/rewind` clicks the Rewind toolbar button, `/compact` fires `liveCompact()`) or shows a toast explaining the command isn't supported in the GUI. The command map lives in `_slashCommandMap`. Messages with `/` that aren't bare commands (e.g. "fix /etc/config") pass through normally.

## Mobile socket recovery — the DO / DO NOT list (2026-07-14 → 2026-07-15)

Attempting to auto-heal WebSocket zombies on mobile went through several iterations, some of which caused their own severe regressions. This section captures the survivors and the tombstones so a future maintainer doesn't re-introduce the bugs.

### The original problem
After being on for a while — phone locks, tab backgrounds, Tailscale hands off between wifi/cellular, iOS Safari bfcache-restores the tab — the WebSocket transport can die silently. `socket.connected` still reports `true`, Socket.IO's ping/pong takes ~30s to notice, and any `socket.emit()` on that zombie is dropped. The user saw stale UI or an "infinite skeleton" that only cleared after closing the tab and clearing history.

### What SURVIVED and is load-bearing

1. **bfcache reload** (`static/js/socket.js` — `pageshow` handler). When `event.persisted === true`, the entire JS runtime was frozen and thawed with the underlying transport dead. `socket.disconnect() + socket.connect()` cannot cleanly rebuild from that corrupted state — a full `window.location.reload()` is the only definitive recovery. Guarded by `#restart-overlay` presence so an in-app restart's own reload flow is never fought. This is the one "aggressive" mechanism that stayed because it targets a genuinely different failure mode (frozen JS runtime), not merely a dead transport.

2. **`socket.on('connect')` unconditionally re-emits `get_session_log`** (`static/js/socket.js`). Socket.IO does NOT replay events missed during a disconnect, so any `session_entry` / `session_state` push that fired during the outage is lost forever. If a live session is open, always re-fetch its log on every reconnect. Fires only when the socket actually reconnected (not speculatively), so it's cheap and correct.

3. **Skeleton-stuck watchdog on `get_session_log`** (`static/js/live-panel.js` `window._skeletonStuckTimer` and `window._loadMoreStuckTimer`). Three stages, each only fires while the user still sees a stuck skeleton for the same live session — no periodic loop, no background timer. Stage 1 (8s): re-emit `get_session_log`. Stage 2 (16s): probe `/api/ping` over HTTP; if HTTP is fine but the WebSocket produced nothing, cycle the socket ONCE (rate-limited by `window._skeletonZombieCycleAt` to at most 1/60s). Stage 3 (28s): if the cycle also failed to recover, do a full `window.location.reload()` — rate-limited by `sessionStorage['vn_skel_reload_at']` to at most 1/120s, and guarded off if `#restart-overlay` is present so it never fights an in-app restart. This stage-3 reload is what cures the iOS-Safari case where `pageshow.event.persisted` is falsely false and Socket.IO's engine.io state comes back wedged — matches the user's "turn off and back on" workaround, automatically. Cleared by the `session_log` handler in `socket.js` on any matching response. The rate limits + user-visible-symptom trigger together prevent the 2026-07-15 storm from recurring.

4. **`_wakeSocketResync()` on visibilitychange/pageshow/focus/online** (`static/js/socket.js`). Only takes two actions: (a) if the socket claims disconnected, call `socket.connect()`; (b) if connected, emit `request_state_snapshot` to refresh state. **Does NOT cycle a connected-looking socket under any circumstance.** Debounced 750ms.

   **Action (a) must skip a socket that is still connecting (fixed 2026-10-06).** `socket.connect()` on a socket whose transport is open but whose namespace handshake is still pending sends a SECOND `CONNECT` packet. python-socketio answers the duplicate with `CONNECT_ERROR` ("Unable to connect"); the Socket.IO client then destroys the socket's listeners while `socket.connected` is true from the first handshake. Result: a socket that looks connected, still sends, and never receives. `pageshow` fires on every page load, usually before the handshake finishes (`handle_connect` does a daemon IPC first), so this hit almost every load: the first chat thread sat on its skeleton for ~16-20s until the skeleton watchdog's stage 2 cycled the socket (measured: thread rendered at ~20s before, ~0.4s after). The guard is `if (socket.active && socket.io._readyState !== 'closed') return;` before the `socket.connect()` call. Do NOT remove it, and do not add any other `socket.connect()` call that can run during the initial handshake. Diagnostic signature: a `connect_error` with an empty message within a millisecond of `connect`. Tests: `tests/test_wake_socket_resync.py`.

### What was TRIED and REVERTED (2026-07-15) — DO NOT re-add these

The following mechanisms were added between 2026-07-14 evening and 2026-07-15 morning and caused a worse regression than the original bug (flashing "Engine Stopped" overlay, in-flight session streams dropped mid-response). Do NOT reintroduce without a fundamentally different design:

- **`_BG_CYCLE_THRESHOLD_MS` (3s) backgrounded-tab always-cycle path.** Bug: `_lastForegroundAt` (misnamed; actually set on visibilitychange→hidden) was never reset after being used. Once the tab had been backgrounded once, `bgDuration = now - _lastForegroundAt` stayed huge FOREVER, so every subsequent focus/tap re-cycled the socket. Cycling a live socket drops in-flight `session_entry` pushes (user reported this as "session stream got fucked"). It also floods the server with reconnect handshakes, each triggering `get_all_states()` IPC to the daemon — enough congestion to trip the daemon heartbeat (`app/daemon_client.py` HEARTBEAT_TIMEOUT = 8s) and produce a spurious "Engine Stopped" overlay. If you ever bring back a "cycle after real backgrounding" heuristic, `_lastHiddenAt` MUST be reset to `now` inside `_wakeSocketResync` after computing `bgDuration`, and there MUST be a hard per-cycle rate limit (e.g. one cycle per 30s max) so no single user interaction can chain into a cycle storm.

- **20s foreground zombie watchdog** (`setInterval` that cycled the socket if `staleness > 45s`). Bug: idle sessions legitimately go 45s+ between server pushes. The watchdog fired on healthy-but-idle sockets and killed streams mid-response. If you ever need a "socket died silently while foregrounded" detector, prefer an actively-emitted heartbeat with a specific response event, not raw event silence.

- **`_wakeSocketResync()` call from `startLivePanel()`.** Combined with the `_lastForegroundAt` bug above, this cycled the socket on every tap into a session after any prior backgrounding. On its own it's not fatal, but adding it back means the wake-resync function had better stay strictly passive (only reconnect if disconnected, never cycle).

- **Active client heartbeat (`client_ping` / `client_pong`).** Added and reverted 2026-07-15 in the same session. Design was "silence alone is never enough — only a missed pong cycles the socket, rate-limited to 1/min." Failed in practice because Flask's threading async_mode serializes handlers behind the concurrent-daemon-IPC bottleneck; under a real load burst (multiple session_state updates + get_all_states from the awareness cache miss), pongs took >8s → heartbeat cycled → fresh handle_connect fired another get_all_states → made the congestion worse → chain of "Engine Stopped" flashes. If you ever try again: pong must be served by a dedicated non-blocking path (separate thread/queue) that CANNOT be starved by IPC latency, AND the cycle rate limit must be at least 5x the observed p99 IPC time under peak load.

**The rule that emerged:** cycling a socket that reports `connected` based on any heuristic is a foot-gun. Cycling drops in-flight streams (Socket.IO has no replay), triggers a fresh handle_connect that cascades into daemon IPC, and can chain across many taps if the heuristic isn't perfectly reset. Only cycle when you have proof the transport is broken — a failed response to an actively-emitted probe, or a `disconnect` event from Socket.IO itself.

### Diagnosing future regressions
Watch the browser console for `[WS]` and `[skeleton-watchdog]` lines. If a user reports "stale UI after being away," the presence of `wake: socket disconnected — forcing reconnect` near the incident means the recovery machinery ran. Absence means a lifecycle-event binding was removed. If a user reports "session stream cut off mid-response," search for any `disconnect()` + `connect()` pair in the socket.js code path — that's almost always the cause.

## Per-session model must STICK across devices and reloads (hardened 2026-09-23)

The model selector "usually worked" — and failed whenever a session was touched from a second device (desktop → phone) or after a page reload. Four independent gaps combined; each fix below is load-bearing. Tests: `tests/test_session_manager.py::TestSetSessionModel` (hardening block) and `tests/test_session_model.py`.

1. **Bare wake pins the registry model on the CLI** — `daemon/session_manager.py` `start_session`. `claude --resume` does NOT remember a session's model; without `--model` it comes back on the CLI default. The registry model used to be seeded onto `info.model` (the badge) only, so the badge claimed one model while the session ran on another. The wake now passes `_cli_model_id(reg_model)` to `_drive_session` as well. A caller-supplied model always wins. Do NOT revert to badge-only seeding.

2. **`_cli_model_id()` cleans every recorded model before it becomes a launch arg, and KEEPS `[1m]`** (changed 2026-10-05). The CLI reports `claude-opus-5[1m]` when 1M context is active. `[1m]` IS a valid `--model` / `set_model` value for Opus and Sonnet (probed against every model: launch and live switch both come up in 1M), and it is the user's context-window choice, so stripping it silently woke 1M sessions on 200K. It strips any OTHER bracketed marker, and `[1m]` on Haiku, which the API rejects with a 400 ("long context beta is not yet available for this subscription"). Every path that turns a recorded model into `--model` (wake seed, `send_message` auto-resume, self-heal reconnect) goes through it. Client mirror: `SessionModel._cleanId`. The picker (`_modelSelectorGroupsHtml` in app.js) shows ONE chip per model with no "1M" tag; a separate "5.5" / "5.5 1M" pair was tried and removed because both chips gave 1M, and a "1M" tag on the chips and on the running badge (`_runningModelLabel` in invoke-workforce.js) was removed 2026-10-06 because it sat on nearly every model and was noise. Only the context readout states the window size. A bare id leaves the window to the CLI, which can enable 1M by itself (the daemon logs show Opus 4.6 to 5.5 sessions launched on bare ids running to ~1M tokens), so `_set_confirmed_model` no longer guesses by carrying a previous `[1m]` forward; the CLI's `init` report is the authority. For the same reason the context readouts (`window._ctxWindowFor` in invoke-workforce.js) measure every Fable/Opus/Sonnet session against 1M and Haiku against 200K, not by the marker alone. Do NOT go back to stripping `[1m]` unconditionally.

**Context reading (fixed 2026-10-05).** The SDK now delivers `message_start` with the payload in `event` (a dict) and `data` empty. The daemon's usage extraction in the StreamEvent handler only matched the old string shape, so no `session_usage` was ever pushed and no context number reached the UI. The daemon now normalizes both shapes, and `socket.js`'s `stream_event` handler also reads usage from the raw `message_start`, so the readout works even on a daemon that has not been restarted. Keep both. Live readings only exist once a session replies, so an idle session opened after a page load had none: `_fetchContextUsage` (invoke-workforce.js) fills an EMPTY slot from `GET /api/session-context/<id>` (live_api.py), which reads the last main-thread reply's usage from the transcript tail, or a later `compact_boundary`'s `postTokens`. Tests: `tests/test_session_context.py`.

3. **Every model change broadcasts `session_model_changed` via `_broadcast_model_changed()`** — the live switch, the resume-with-`--model` switch, AND the CLI `init` handler when `_set_confirmed_model` reports a change. The socket reply `session_model_result` is reply-only (one socket); other devices learn ONLY from the broadcast. The payload carries `resumed`/`turn_resumed` so a client can treat the broadcast as its confirmation. Never replace this with `_emit_state` — on an IDLE session that fires `_try_dispatch_queue` and surprise-starts a queued message.

4. **Wake paths resolve the pin via `SessionModel.resumeModel(id)`** — `static/js/live-panel.js` `_liveSubmitDirect`, `static/js/sessions.js` wake. Order: marker-stripped **confirmed** (daemon truth, kept fresh from every device by broadcasts/snapshots) → this tab's `desiredModel` (local, never persisted, written only on the tab that chose) → `''` (send nothing; daemon pins its registry model). Sending `getDesired` alone re-sent a STALE local choice and silently undid a switch made on another device. Do NOT "simplify" back to `getDesired`.

5. **Switch flows accept the broadcast as confirmation** — `_applyLiveSessionModel` (invoke-workforce.js) and `liveSwitchModelAndResume` (live-panel.js) listen for BOTH `session_model_result` and a matching `session_model_changed`. On mobile the transport can reconnect between request and reply, losing the reply; the broadcast survives. Without the fallback a successful switch showed "timed out — NOT changed" while the badge quietly updated. Timeout is 30s (daemon's 15s control-request timeout + IPC); the timeout toast must stay honest ("no confirmation") rather than claim "NOT changed".

6. **`_model_switch_in_progress` self-expires** — `_model_switch_pending()` honors the flag only within `_MODEL_SWITCH_INIT_WINDOW_S` (20s) of a live switch and a failed switch clears it. The flag exists so the CLI's side-effect `init` after `set_model` isn't misread as an auto-resume; a stale flag swallowed the NEXT real auto-resume signal, leaving the session shown IDLE while working. Do NOT read the raw attribute in the listeners.

7. **`session_model_changed` client handler honors the limit fields** — `static/js/socket.js`. It only clears `_sessionLimitState`/`_sessionError` when the payload reports no limit, because the event now also fires on a bare `init` refinement (e.g. `[1m]` added) and a still-limited session must keep its CTA.
