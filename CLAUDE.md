# ClaudeGUI Project Rules

## NEVER start or restart server processes
Do NOT use subprocess, os.system, taskkill, or any other method to start, stop, or restart the daemon or web UI servers. This causes flashing terminal windows on Windows. The user will handle all server restarts manually.

Only read and edit files. No process management.

## Slash commands are intercepted client-side
Claude CLI slash commands (e.g. `/compact`, `/rewind`, `/clear`) are NOT sent to the SDK. They get silently eaten with no response, leaving the session stuck idle. Instead, `_interceptSlashCommand()` in `live-panel.js` catches them at every submit path and either triggers the GUI equivalent (e.g. `/rewind` clicks the Rewind toolbar button, `/compact` fires `liveCompact()`) or shows a toast explaining the command isn't supported in the GUI. The command map lives in `_slashCommandMap`. Messages with `/` that aren't bare commands (e.g. "fix /etc/config") pass through normally.
