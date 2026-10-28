# VibeNode SocketIO Events Reference

VibeNode uses Flask-SocketIO for real-time bidirectional communication between the browser and the web server (port 5050). The web server proxies session operations to the session daemon (port 5051) via TCP IPC and re-emits push events as SocketIO events.

**Connection URL:** `ws://localhost:5050`

**Query parameters on connect:**
- `project` — encoded project identifier (filters session state to the active project)

---

## Client -> Server Events

These events are sent by the browser to the server.

### `connect`

**Triggered:** Automatically on WebSocket connection  
**Payload:** None  
**Server response:** Emits `state_snapshot` with all session states, queues, and ID aliases  

---

### `request_state_snapshot`

**Triggered:** On workspace switch or manual refresh  
**Payload:**
```json
{
  "project": "<encoded-project-id>"
}
```
**Server response:** Emits `state_snapshot`

---

### `start_session`

**Triggered:** When user starts or resumes a Claude session  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "prompt": "Initial message text",
  "cwd": "/path/to/project",
  "name": "Session Name",
  "resume": false,
  "model": "sonnet",
  "system_prompt": "Custom system prompt",
  "thinking_level": "medium",
  "max_turns": 10,
  "allowed_tools": ["Read", "Edit", "Bash"],
  "permission_mode": "default",
  "session_type": "",
  "compose_task_id": "section:<project-id>:<section-id>",
  "voice": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | Yes | UUID for the session |
| `prompt` | string | No | Initial message to send |
| `cwd` | string | No | Working directory for the session |
| `name` | string | No | Display name |
| `resume` | boolean | No | Resume existing session |
| `model` | string | No | Model alias (sonnet, opus, haiku) or full ID |
| `system_prompt` | string | No | Custom system prompt override |
| `thinking_level` | string | No | CLI `--effort` level: `low`, `medium`, `high`, `xhigh`, `max`. Omitted: new sessions use the model default, resumed sessions keep the effort the daemon remembers. `default`: explicit reset to the model default on a resume. Any other value is ignored. |
| `max_turns` | integer | No | Max conversation turns |
| `allowed_tools` | string[] | No | Restrict to these tools |
| `permission_mode` | string | No | One of: default, plan, acceptEdits, bypassPermissions |
| `session_type` | string | No | "planner" or "title" for utility sessions |
| `compose_task_id` | string | No | Links session to a compose section |
| `voice` | boolean | No | If true, prompt was transcribed from voice |

**Server response:** Emits `session_started` on success, `error` on failure

---

### `send_message`

**Triggered:** When user sends a follow-up message to an idle session  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "text": "Message content",
  "voice": false
}
```
**Server response:** Emits `message_ack` on success, `send_failed` on failure. If the session is busy, the daemon auto-queues and returns `message_ack` with `queued: true`.

---

### `permission_response`

**Triggered:** When user approves or denies a tool permission request  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "action": "y"
}
```

| Action | Meaning |
|--------|---------|
| `y` | Allow this one time |
| `n` | Deny |
| `a` | Always allow this tool |
| `aa` | Almost always allow |

---

### `interrupt_session`

**Triggered:** When user clicks the interrupt/stop button  
**Payload:**
```json
{
  "session_id": "<uuid>"
}
```

---

### `close_session`

**Triggered:** When user explicitly closes a session  
**Payload:**
```json
{
  "session_id": "<uuid>"
}
```

---

### `get_session_log`

**Triggered:** When the live panel loads or refreshes  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "since": 0,
  "project": "<encoded-project>",
  "limit": 100,
  "before": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| `since` | integer | Return entries after this index |
| `limit` | integer | Max entries (null = all, for backwards compat) |
| `before` | integer | Load entries before this index (for "load older") |

**Server response:** Emits `session_log`

---

### `get_permission_policy`

**Triggered:** On page load to sync permission settings  
**Payload:** None  
**Server response:** Emits `permission_policy_loaded`

---

### `set_permission_policy`

**Triggered:** When user changes permission settings  
**Payload:**
```json
{
  "policy": "manual",
  "customRules": {
    "Edit": "always",
    "Bash": "deny"
  }
}
```

| Policy | Meaning |
|--------|---------|
| `manual` | Ask for every tool use |
| `auto` | Auto-allow all |
| `almost_always` | Auto-allow unless risky |
| `custom` | Per-tool rules |

---

### `get_ui_prefs`

**Triggered:** On page load  
**Payload:** None  
**Server response:** Emits `ui_prefs_loaded`

---

### `set_ui_prefs`

**Triggered:** When user changes UI preferences  
**Payload:** Arbitrary JSON object with preference key-value pairs

---

### `queue_message`

**Triggered:** When user queues a message for a busy session  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "text": "Queued message text"
}
```

---

### `remove_queue_item`

**Triggered:** When user removes a queued message  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "index": 0
}
```

---

### `edit_queue_item`

**Triggered:** When user edits a queued message  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "index": 0,
  "text": "Updated message text"
}
```

---

### `clear_queue`

**Triggered:** When user clears all queued messages  
**Payload:**
```json
{
  "session_id": "<uuid>"
}
```

---

### `get_queue`

**Triggered:** When UI needs to refresh queue state  
**Payload:**
```json
{
  "session_id": "<uuid>"
}
```
**Server response:** Emits `queue_updated`

---

### `set_session_model`

**Triggered:** When the user picks a model for a session (the model selector). A live session gets a `set_model` control request; a session that is not live is resumed with `--model`.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "model": "claude-opus-5-5"
}
```
**Server response:** Emits `session_model_result` to the requesting socket only. Every other client learns of the change from the `session_model_changed` broadcast, which a client may also treat as its confirmation (the reply can be lost across a mobile reconnect).

---

### `set_session_effort`

**Triggered:** When the user changes a session's thinking level. Applied in place, mid-turn included, through the CLI's `apply_flag_settings` control request and confirmed with a read-back; a relaunch is only the fallback for an older CLI or Session Engine.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "effort": "high"
}
```
`effort` is a CLI level (`low`, `medium`, `high`, `max`, …) or `default` / `""` for the model's own default.  
**Server response:** Always emits `session_effort_result` to the requesting socket. Other clients learn from `session_effort_changed`.

---

### `cancel_auto_retry`

**Triggered:** The Cancel button on an API-error or usage-limit auto-retry countdown. On a usage-limit switch countdown, cancel means "do not switch; wait for the reset".  
**Payload:**
```json
{
  "session_id": "<uuid>"
}
```
**Server response:** None on success; `error` with `session_id` on failure.

---

### `retry_now`

**Triggered:** The Retry-now button: fires a pending auto-retry immediately, or a manual retry of the last failed turn.  
**Payload:**
```json
{
  "session_id": "<uuid>"
}
```
**Server response:** None on success; `error` with `session_id` on failure (for example when the session is not idle).

---

## Server -> Client Events

These events are pushed from the server to connected browsers.

### `state_snapshot`

**Triggered:** On connect, on `request_state_snapshot`, and on daemon reconnect  
**Payload:**
```json
{
  "sessions": [
    {
      "session_id": "<uuid>",
      "state": "idle",
      "cost_usd": 0.05,
      "name": "Session Name",
      "cwd": "/path/to/project",
      "session_type": "",
      "queue": ["queued msg 1"]
    }
  ],
  "queues": {
    "<session-id>": ["msg1", "msg2"]
  },
  "aliases": {
    "<old-id>": "<new-id>"
  }
}
```

---

### `session_state`

**Triggered:** Whenever a session's state changes (idle, working, waiting, stopped)  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "state": "working",
  "cost_usd": 0.12,
  "error": null,
  "name": "Session Name"
}
```

---

### `session_entry`

**Triggered:** When a session produces new output (text, tool use, tool result)  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "entry": {
    "kind": "asst",
    "text": "Here is the response..."
  }
}
```

Entry kinds: `user`, `asst`, `tool_use`, `tool_result`, `system`

---

### `session_permission`

**Triggered:** When a session requests permission to use a tool  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "tool_name": "Edit",
  "tool_input": {
    "file_path": "/path/to/file.py",
    "old_string": "...",
    "new_string": "..."
  }
}
```

---

### `session_started`

**Triggered:** After a session is successfully started  
**Payload:**
```json
{
  "session_id": "<uuid>"
}
```

---

### `session_log`

**Triggered:** In response to `get_session_log`  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "entries": [...],
  "total": 150,
  "offset": 100,
  "has_more": true,
  "prepend": false
}
```

---

### `message_ack`

**Triggered:** After a message is accepted by the daemon  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "queued": false
}
```

---

### `send_failed`

**Triggered:** When a message send fails  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "error": "Daemon did not respond",
  "text": "Original message text"
}
```

---

### `daemon_reconnect`

**Triggered:** When the web server loses/regains connection to the daemon  
**Payload:**
```json
{
  "status": "connecting",
  "attempt": 3,
  "message": "Reconnecting to daemon (attempt 3)..."
}
```

Status values: `disconnected`, `connecting`, `restarting`, `connected`

---

### `permission_policy_loaded`

**Triggered:** In response to `get_permission_policy`  
**Payload:**
```json
{
  "policy": "manual",
  "custom_rules": {}
}
```

---

### `ui_prefs_loaded`

**Triggered:** In response to `get_ui_prefs`  
**Payload:** Arbitrary JSON object with saved preferences

---

### `queue_updated`

**Triggered:** In response to `get_queue` or after queue modifications  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "queue": ["msg1", "msg2"]
}
```

---

### `usage_limits`

**Triggered:** Broadcast to all clients when a Claude turn reports a changed account usage window (session, weekly, or Fable limit). Same shape as `GET /api/usage-limits`.  
**Payload:**
```json
{
  "updated_at": 1790866000.0,
  "windows": {
    "five_hour": {"label": "Session", "percent": 16, "resets_at": 1790871000, "seen_at": 1790866000.0, "expired": false},
    "seven_day": {"label": "Week", "percent": 46, "resets_at": 1790964000, "seen_at": 1790866000.0, "expired": false},
    "seven_day_overage_included": {"label": "Fable", "percent": 14, "resets_at": 1790964000, "seen_at": 1790866000.0, "expired": false}
  }
}
```

---

### `external_notice`

**Triggered:** Broadcast once to all clients for every `POST /api/notify/approval` (a local script pinging the user). Clients ping once: a chime, and a one-off notification if the tab is in the background. Nothing starts flashing on this event. `id` is set when the ping named an approval page that the server is now watching, and is `""` otherwise.  
**Payload:**
```json
{
  "title": "Release 4.2",
  "message": "Click Approve or Cancel in your browser to continue.",
  "source": "pipeline",
  "id": "5f1c9a0be2d4"
}
```

---

### `approvals_open`

**Triggered:** Broadcast to all clients when the list of open approval pages changes, and every few seconds while it is not empty. An approval is open while the server can still fetch its unchanged page (`app/approval_watch.py`). `GET /api/notify/approvals` returns the same list, with each page's address added.

A client treats each snapshot as a lease: an approval is open for `ttl_ms` after the last snapshot that listed it, and is over as soon as a snapshot arrives without it. Background tabs flash their title only for open approvals, so a client that stops receiving snapshots stops flashing.  
**Payload:**
```json
{
  "open": [
    {
      "id": "5f1c9a0be2d4",
      "title": "Release 4.2",
      "message": "Click Approve or Cancel in your browser to continue.",
      "source": "pipeline",
      "since": 1791800000.0
    }
  ],
  "ttl_ms": 10000
}
```

---

### `error`

**Triggered:** On any WebSocket handler error  
**Payload:**
```json
{
  "message": "Error description",
  "session_id": "<uuid>"
}
```

---

### `stream_event`

**Triggered:** For every raw streaming event the CLI produces during a turn (`message_start`, `content_block_delta`, …). Clients render partial text from it and read context usage from `message_start`.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "event": { "type": "content_block_delta", "delta": { "type": "text_delta", "text": "Hel" } }
}
```

---

### `session_usage`

**Triggered:** After each API call in a turn, with the session's accumulated usage (input, output, cache read and cache creation tokens). Drives the context readout. A session opened after a page load has no live reading until it replies; `GET /api/session-context/<id>` fills the gap from the transcript.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "usage": { "input_tokens": 1200, "output_tokens": 340, "cache_read_input_tokens": 88000, "cache_creation_input_tokens": 0 }
}
```

---

### `system_message`

**Triggered:** For CLI system messages that have no dedicated handling (compaction has: it becomes a `session_entry`). Diagnostic.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "subtype": "status",
  "data": {}
}
```

---

### `session_model_result`

**Triggered:** Reply to `set_session_model`, to the requesting socket only.  
**Payload:**
```json
{
  "ok": true,
  "session_id": "<uuid>",
  "model": "claude-opus-5-5",
  "resumed": false,
  "turn_resumed": false
}
```
`resumed` is true when the daemon had to resume a not-live session with `--model` instead of a live switch; `turn_resumed` when that resume also carried the interrupted turn's prompt. On failure `ok` is false and `error` says why.

---

### `session_model_changed`

**Triggered:** Broadcast to all clients whenever a session's model changes: a live switch, a resume with `--model`, the CLI's `init` report refining the model (for example adding `[1m]`), or a usage-limit auto-switch. Clients clear limit state only when the payload reports no limit.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "model": "claude-opus-5-5[1m]",
  "limit_reset_at": null,
  "limited_model": null,
  "error": "",
  "resumed": false,
  "turn_resumed": false
}
```

---

### `session_effort_result`

**Triggered:** Reply to `set_session_effort`, to the requesting socket only. `ok` is true only after the CLI confirmed the level; otherwise exactly one of `not_live`, `not_ready`, `live_unavailable` tells the client which fallback applies (relaunch when free, wait for the CLI to finish starting, or relaunch because the Session Engine or CLI is too old).  
**Payload:**
```json
{
  "ok": true,
  "session_id": "<uuid>",
  "effort": "high",
  "applied": "high",
  "not_live": false,
  "not_ready": false,
  "live_unavailable": false
}
```

---

### `session_effort_changed`

**Triggered:** Broadcast to all clients after the CLI confirms a thinking-level change.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "effort": "high",
  "applied": "high"
}
```

---

### `session_id_remapped`

**Triggered:** When a session launched under a client temp id adopts the CLI's real id (fired from the CLI's `init` message and again, idempotently, on RESULT). Clients MERGE when a row under `new_id` already exists rather than renaming the old row onto it.  
**Payload:**
```json
{
  "old_id": "<temp-uuid>",
  "new_id": "<cli-uuid>"
}
```

---

### `session_renamed`

**Triggered:** Broadcast when a session gets a title (auto-naming or a manual rename). Clients remember every title they learn and fill it into nameless rows created later; a title the server returns always wins.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "title": "Fix the kanban column widths",
  "project": "<encoded-project-id>"
}
```

---

### `session_hold`

**Triggered:** Broadcast by `POST /api/session-hold/<session_id>` when a session is put on Standby, marked Done, or has that cleared. The hold lasts until the session's next message; every open sidebar redraws the row. `since` is when the hold was set (null when cleared).  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "state": "standby",
  "project": "<encoded-project-id>",
  "since": 1793203200.5
}
```

---

### `naming_progress`

**Triggered:** By the bulk naming job (`POST /api/sessions/name-untitled`): once at start (`done: 0`), every 5 sessions, and once at the end with `finished: true`. Each title also fires `session_renamed`.  
**Payload:**
```json
{
  "project": "<encoded-project-id>",
  "done": 15,
  "total": 965,
  "current_title": "Draft the claims section",
  "named": 14,
  "skipped": 1,
  "failed": 0,
  "finished": false
}
```

---

### `sessions_refresh`

**Triggered:** Broadcast when the session list must be reloaded from the server, for example after an admin scrub removed phantom sessions. Clients clear their remembered titles before reloading so scrubbed names are not painted back.  
**Payload:**
```json
{
  "reason": "scrub-phantoms"
}
```

---

### `session_stalled`

**Triggered:** By the Session Engine's silent-wait watchdog (job 5) the second time a task idles "waiting on background work" that is dead. Clients raise attention (chime, notification, title flash) until the tab is looked at.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "name": "Session Name",
  "text": "Still waiting on background work that is no longer running…"
}
```

---

### `queue_dispatched`

**Triggered:** When a queued message is being sent to a session that just went idle.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "text": "the message being sent",
  "remaining": 2
}
```

---

### `hook_message`

**Triggered:** Broadcast for every `POST /api/hook/message` (a local script posting a line into a session). `delivered` is `"now"`, `"queued"` or `"no"`.  
**Payload:**
```json
{
  "session_id": "<uuid>",
  "source": "pipeline",
  "kind": "info",
  "text": "Build 412 is green",
  "message": "[pipeline] Build 412 is green",
  "delivered": "now"
}
```

---

### `inbox_updated`

**Triggered:** When a child subsession reports to its parent (`report-to-parent`), from the web server or the Session Engine.  
**Payload:**
```json
{
  "parent_session_id": "<uuid>",
  "undelivered_count": 1,
  "from_child_session_id": "<uuid>"
}
```

---

### `job_needs_person`

**Triggered:** By the Durable Jobs supervisor when a job reaches `needs_person` or `failed`, or a relaunch fails. Clients draw a card in the shared float stack with Relaunch and Leave-it buttons. Pushed once per state change.  
**Payload:**
```json
{
  "job_id": "job-20261015-1201",
  "name": "Transcribe interviews",
  "state": "needs_person",
  "reason": "dead three times with no progress",
  "progress": 42,
  "session_id": "<uuid>",
  "project": "<encoded-project-id>"
}
```

---

### `job_registry_error`

**Triggered:** Once per file version when `data/jobs.json` cannot be parsed. The registry is never rewritten by the supervisor.  
**Payload:**
```json
{
  "path": "C:/…/data/jobs.json",
  "error": "Expecting value: line 1 column 1 (char 0)"
}
```

---

### `approval_needed` (deprecated)

The pre-2026-10-12 name of `external_notice`. The server no longer emits it; `socket.js` still listens so a web server that has not been restarted since the JS was pulled keeps working. New code must not emit it.

---

### Kanban Events

These events are emitted by kanban API routes when tasks change.

| Event | Payload | Triggered When |
|-------|---------|----------------|
| `kanban_task_created` | Task object | Task created |
| `kanban_task_updated` | Task object | Task fields updated |
| `kanban_task_moved` | `{task_id, old_status, new_status, position}` | Task moved between columns |
| `kanban_board_refresh` | `{reason}` | Board-wide change (bulk action, plan applied, columns updated) |

---

### Compose Events

| Event | Payload | Triggered When |
|-------|---------|----------------|
| `compose_board_refresh` | `{project_id}` | Project-wide change |
| `compose_task_created` | `{project_id, section}` | Section created |
| `compose_task_updated` | `{project_id, section}` | Section updated |
| `compose_task_moved` | `{project_id, section_id, old_status, new_status}` | Section status changed |
| `compose_changing` | `{project_id, section_id, changing, change_note?}` | Changing flag toggled (API or the compose watcher) |
| `compose_context_updated` | `{project_id, context}` | The compose watcher saw a section's context file change |
| `compose_directive_logged` | `{project_id, directive, conflicts}` | Directive added |
| `compose_directive_conflict_resolved` | `{project_id, conflict_id, action}` | Conflict resolved |
