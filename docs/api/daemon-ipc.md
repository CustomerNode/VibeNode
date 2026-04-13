# Daemon IPC Protocol (Internal Reference)

> **This is an internal reference for contributors.** External consumers should use
> the REST API (port 5050) and SocketIO events exclusively. Do not connect to the
> daemon directly.

## Overview

The session daemon runs on `localhost:5051` and manages all active Claude Code
sessions. The web server (port 5050) communicates with the daemon over a raw TCP
connection using a JSON-line protocol (one JSON object per line, newline-delimited).

The `DaemonClient` class in `app/daemon_client.py` handles all IPC from the web
server side.

## Protocol Format

### Request (Web -> Daemon)

```json
{"req_id": "<8-char-hex>", "method": "<method-name>", "params": {}}
```

- `req_id` — unique identifier for correlating responses
- `method` — the IPC method name (see table below)
- `params` — method-specific parameters (may be empty `{}`)

### Response (Daemon -> Web)

```json
{"req_id": "<8-char-hex>", "result": {}}
```

Or on error:

```json
{"req_id": "<8-char-hex>", "error": "Error description"}
```

### Push Event (Daemon -> Web, unsolicited)

```json
{"event": "<event-name>", "data": {}}
```

Push events have no `req_id`. They are re-emitted as SocketIO events to all
connected browsers.

## IPC Methods

| Method | Parameters | Returns | Description |
|--------|-----------|---------|-------------|
| `start_session` | `session_id, prompt, cwd, name, resume, model, system_prompt, max_turns, allowed_tools, permission_mode, session_type` | `{ok: true}` | Start or resume a Claude session |
| `send_message` | `session_id, text, voice` | `{ok: true}` or `{queued: true}` | Send message to an idle session |
| `resolve_permission` | `session_id, allow, always, almost_always` | `{ok: true}` | Resolve a pending permission request |
| `interrupt_session` | `session_id` | `{ok: true}` | Send interrupt signal to a running session |
| `close_session` | `session_id` | `{ok: true}` | Close a session (async) |
| `close_session_sync` | `session_id` | `{ok: true}` | Close a session (blocks until done) |
| `remove_session` | `session_id` | `{ok: true}` | Remove session from daemon registry |
| `save_registry_now` | (none) | `{ok: true}` | Force immediate registry persistence |
| `get_all_states` | (none) | `[{session_id, state, cost_usd, name, cwd, ...}]` | Get all session states |
| `get_entries` | `session_id, since` | `[{kind, text, ...}]` | Get log entries for a session |
| `get_entry_count` | `session_id` | `integer` | Get entry count for a session |
| `has_session` | `session_id` | `boolean` | Check if daemon manages this session |
| `get_session_state` | `session_id` | `string` or `null` | Get state of a specific session |
| `get_permission_policy` | (none) | `{policy, custom_rules}` | Get current permission policy |
| `set_permission_policy` | `policy, custom_rules` | `{ok: true}` | Update permission policy |
| `get_ui_prefs` | (none) | `{...}` | Get persisted UI preferences |
| `set_ui_prefs` | `{...}` | `{ok: true}` | Save UI preferences |
| `hook_pre_tool` | `tool_name, tool_input, session_id` | `{action: "allow"}` or `{action: "deny"}` | Handle CLI PreToolUse hook |
| `resolve_hook_permission` | `session_id, allow, always, almost_always` | `{ok: true}` | Resolve a hook-based permission |
| `queue_message` | `session_id, text` | `{ok: true}` | Add message to session queue |
| `get_queue` | `session_id` | `[string]` | Get queued messages |
| `remove_queue_item` | `session_id, index` | `{ok: true}` | Remove queue item by index |
| `edit_queue_item` | `session_id, index, text` | `{ok: true}` | Edit queue item |
| `clear_queue` | `session_id` | `{ok: true}` | Clear all queued messages |
| `get_aliases` | (none) | `{old_id: new_id, ...}` | Get session ID remap aliases |
| `ping` | (none) | `{ok: true}` | Health check |

## Push Events

The daemon pushes these events to the web server, which re-emits them as SocketIO:

| Daemon Event | SocketIO Event | Data | Trigger |
|-------------|----------------|------|---------|
| `session_state` | `session_state` | `{session_id, state, cost_usd, error, name}` | Session state transition |
| `session_entry` | `session_entry` | `{session_id, entry: {kind, text, ...}}` | New output from session |
| `session_permission` | `session_permission` | `{session_id, tool_name, tool_input}` | Tool permission needed |
| `id_remap` | (handled internally) | `{old_id, new_id}` | SDK reassigned session ID |

## Connection Lifecycle

1. **Connect:** `DaemonClient._connect()` opens a TCP socket to `127.0.0.1:5051`
   with TCP_NODELAY. Retries up to 50 times with 100-200ms delays.

2. **Reader thread:** A background thread (`daemon-reader`) continuously reads
   lines from the socket. Responses with `req_id` are matched to pending requests.
   Push events (with `event` key) are queued for SocketIO emission.

3. **Emitter thread:** A separate thread (`socketio-emitter`) dequeues push events
   and calls `socketio.emit()`. This decouples IPC from WebSocket write latency.

4. **Reconnection:** On disconnect, `DaemonClient._reconnect_loop()` retries
   every 2 seconds. After 5 failed attempts (~10s), it attempts to restart the
   daemon process automatically.

5. **Resync on reconnect:** After reconnecting, the client fetches accumulated
   ID aliases and permission policy from the daemon to restore state.

## Timeout Behavior

- Default request timeout: **30 seconds**
- If the daemon doesn't respond within the timeout, the request returns
  `{ok: false, error: "Daemon did not respond to <method> (timeout)"}`
- The `hook_pre_tool` endpoint retries up to 3 times with 1-second delays
  between attempts

## Thread Safety

- `_write_lock` serializes all socket writes
- `_pending_lock` protects the pending-request map
- The reader thread is the only thread that reads from the socket
- SocketIO emissions are serialized through `_emit_queue`
