# VibeNode Project Rules

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

## Slash commands are intercepted client-side
Claude CLI slash commands (e.g. `/compact`, `/rewind`, `/clear`) are NOT sent to the SDK. They get silently eaten with no response, leaving the session stuck idle. Instead, `_interceptSlashCommand()` in `live-panel.js` catches them at every submit path and either triggers the GUI equivalent (e.g. `/rewind` clicks the Rewind toolbar button, `/compact` fires `liveCompact()`) or shows a toast explaining the command isn't supported in the GUI. The command map lives in `_slashCommandMap`. Messages with `/` that aren't bare commands (e.g. "fix /etc/config") pass through normally.
