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
| `thinking_level` | string | No | Thinking level override |
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
| `compose_changing` | `{project_id, section_id, changing}` | Changing flag toggled |
| `compose_directive_logged` | `{project_id, directive, conflicts}` | Directive added |
| `compose_directive_conflict_resolved` | `{project_id, conflict_id, action}` | Conflict resolved |
