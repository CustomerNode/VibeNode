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
18. **Chrome-first browser launch** — `run.py` `_find_chrome()` + `open_browser()`. The Web Speech API (voice input) is Chromium-only. `open_browser()` MUST find and launch Chrome via `ShellExecuteW`, NOT use `os.startfile(url)` as the primary method (that opens the default browser which may be Firefox). `os.startfile` is ONLY the fallback when Chrome is not installed. Do NOT "simplify" by removing `_find_chrome()` or replacing `ShellExecuteW` with `os.startfile` — this exact regression already broke voice input in production.

## Compose project-scoping — DO NOT REMOVE (fixed 2026-04-13)

Three bugs combined to make the Compose feature unusable across multiple VibeNode projects. All three fixes are load-bearing — reverting any one of them re-breaks Compose.

1. **`?project=` filter in `_addToCompose()`** — `static/js/sessions.js`. The right-click → Add to Compose fetch MUST pass `?project=<activeProject>` so the API only returns compositions belonging to the current project. Without it every composition across all projects is returned and the picker shows unrelated items. Do NOT remove the query param from the fetch call.

2. **Stale-project fallback in `initCompose()`** — `static/js/compose.js`. When the saved `_activeComposeProjectId` (from localStorage) points to a deleted or missing composition, `initCompose` MUST clear the stale ID and retry with just the `?project=` parent filter. Without this fallback the compose view renders completely empty even when valid compositions exist. Do NOT remove the `if (_activeComposeProjectId)` retry block inside the `if (!data || !data.project)` guard.

3. **Snapshot-based test cleanup** — `tests/test_compose_api.py`. The `cleanup_projects` fixture MUST snapshot `COMPOSE_PROJECTS_DIR` before each test and remove anything new after. The old `startswith("test-")` check missed cloned projects (directory names like `copy-of-test-clone-src-*` and UUIDs), which leaked 52 orphan projects into production data. Do NOT revert to name-prefix cleanup.

## Slash commands are intercepted client-side
Claude CLI slash commands (e.g. `/compact`, `/rewind`, `/clear`) are NOT sent to the SDK. They get silently eaten with no response, leaving the session stuck idle. Instead, `_interceptSlashCommand()` in `live-panel.js` catches them at every submit path and either triggers the GUI equivalent (e.g. `/rewind` clicks the Rewind toolbar button, `/compact` fires `liveCompact()`) or shows a toast explaining the command isn't supported in the GUI. The command map lives in `_slashCommandMap`. Messages with `/` that aren't bare commands (e.g. "fix /etc/config") pass through normally.
