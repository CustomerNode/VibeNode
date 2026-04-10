# VibeNode — Agent Standards & Guidelines

This document defines the standards for any agent (human or AI) working on VibeNode. Follow these rules for every change.

---

## Architecture

```
VibeNode/
  session_manager.py          # Entrypoint — creates app, starts server
  app/
    __init__.py               # App factory, blueprint registration
    config.py                 # Paths, caches, shared state
    sessions.py               # Session loading, parsing, summarization
    code_extraction.py        # Code block extraction from sessions
    git_ops.py                # Background git fetch, sync operations
    process_detection.py      # Windows process scanning, SendKeys input
    routes/
      main.py                 # GET / — serves the SPA
      sessions_api.py         # Session CRUD endpoints
      project_api.py          # Project switching
      git_api.py              # Git status and sync
      live_api.py             # Live terminal, respond to questions
      analysis_api.py         # Summary, extract code, compare, export
  templates/
    index.html                # HTML shell — no inline JS or CSS
  static/
    style.css                 # All styles, CSS custom properties for theming
    js/
      utils.js                # escHtml(), showToast()
      markdown.js             # mdParse() renderer
      app.js                  # Global state, project/session loading
      sessions.js             # Session list rendering, sorting, tooltips
      toolbar.js              # Toolbar actions, rename, delete, duplicate
      modals.js               # Modal/overlay management
      git-sync.js             # Git status polling, sync UI
      live-panel.js           # Live terminal panel, input bar state machine
      find.js                 # Find-in-session
      extract.js              # Code extraction drawer
      compare.js              # Session comparison
      workforce.js            # Workforce grid view
      theme.js                # Theme cycling (dark/light/auto)
      polling.js              # Background polling orchestration
  tests/
    conftest.py               # Shared fixtures (app, client, mock sessions)
    test_config.py            # Config and path helpers
    test_sessions.py          # Session loading, summary, caching
    test_code_extraction.py   # Code block extraction
    test_git_ops.py           # Git operations
    test_routes/
      test_sessions_api.py    # Session CRUD endpoints
      test_project_api.py     # Project switching
      test_git_api.py         # Git status/sync endpoints
      test_live_api.py        # Live terminal endpoints
      test_analysis_api.py    # Summary, extract, compare endpoints
    test_integration.py       # End-to-end workflow tests
```

### Principles

- **Separation of concerns.** Python backend handles data and logic. HTML is structure. CSS is presentation. JS is behavior. Never mix them.
- **Routes are thin.** Route handlers validate input, call helper functions, return JSON. Business logic lives in `app/sessions.py`, `app/git_ops.py`, etc.
- **No inline styles or scripts** in templates, except the theme flash prevention script (must be inline to avoid FOUC).
- **All state in one place.** Python state lives in `app/config.py`. JS state lives in `app.js` globals.

---

## UI/UX Standards

### Theming

VibeNode supports three theme modes: **dark**, **light**, and **auto** (adaptive).

#### CSS Custom Properties

All colors MUST use CSS custom properties defined in `static/style.css`. Never use hardcoded hex colors in new CSS.

```css
/* Correct */
background: var(--bg-card);
color: var(--text-secondary);
border: 1px solid var(--border);

/* Wrong */
background: #1e1e1e;
color: #ccc;
```

#### Variable naming convention

| Prefix | Purpose | Example |
|--------|---------|---------|
| `--bg-*` | Background colors | `--bg-body`, `--bg-card`, `--bg-hover` |
| `--text-*` | Text colors | `--text-primary`, `--text-muted`, `--text-heading` |
| `--border-*` | Border colors | `--border`, `--border-light`, `--border-focus` |
| `--accent*` | Accent/brand colors | `--accent`, `--accent-text`, `--accent-hover` |

#### Adding new themed elements

When adding a new UI element:

1. Check if an existing variable fits (e.g., `--bg-card` for a new card background)
2. If not, add a new variable to **both** the dark and light theme blocks in `style.css`
3. Name it descriptively: `--bg-{component}-{state}` (e.g., `--bg-question-pulse`)
4. Test in both themes before committing

#### Auto theme

Auto mode switches between light (7am-7pm) and dark based on the user's system clock. The check runs every 60 seconds. The theme preference is stored in `localStorage` under the key `theme`.

#### Theme flash prevention

The inline script in `<head>` applies the saved theme before any CSS loads. This prevents a flash of the wrong theme. Do not remove or move this script to an external file.

### Premium Modal System

**Never use browser `alert()`, `confirm()`, or `prompt()`.** All user-facing dialogs must use the premium modal utilities in `utils.js`.

#### Available Functions

| Function | Returns | Use for |
|----------|---------|---------|
| `showAlert(title, body, opts)` | `Promise<void>` | Informational messages, errors, clipboard notifications |
| `showConfirm(title, body, opts)` | `Promise<boolean>` | Destructive actions (delete, close), irreversible operations |
| `showPrompt(title, body, opts)` | `Promise<string\|null>` | Renaming, text input |

#### Options

All functions accept an `opts` object:

| Option | Type | Description |
|--------|------|-------------|
| `icon` | string | Emoji displayed above the title |
| `confirmText` | string | Primary button text (default: "Confirm" / "OK") |
| `cancelText` | string | Secondary button text (default: "Cancel") |
| `danger` | boolean | Red primary button for destructive actions |
| `placeholder` | string | Input placeholder (showPrompt only) |
| `value` | string | Pre-filled input value (showPrompt only) |

#### Examples

```javascript
// Delete confirmation
const ok = await showConfirm('Delete Session',
  '<p>Delete <strong>' + escHtml(name) + '</strong>?</p><p>This cannot be undone.</p>',
  { danger: true, confirmText: 'Delete', icon: '\uD83D\uDDD1\uFE0F' });
if (!ok) return;

// Error notification
showAlert('Send Failed', '<p>' + escHtml(err) + '</p>', { icon: '\u26A0\uFE0F' });

// Rename prompt
const newName = await showPrompt('Rename', '<p>Enter a new name.</p>', {
  value: currentName, confirmText: 'Save' });
```

#### Design Standards for Modals

- **Card:** `border-radius: 14px`, `padding: 28px 32px`, themed shadow
- **Backdrop:** `backdrop-filter: blur(4px)` for depth
- **Animation:** Scale up on open (`0.95 → 1.0`), fade out on close
- **Buttons:** Right-aligned, secondary left, primary/danger right
- **Icon:** 32px emoji above the title for context
- **Body:** Supports HTML (`<p>`, `<strong>`) for formatting
- **z-index:** 5000 (above all other overlays)

### Interaction Feedback

Every user action must have visible feedback. The user should never wonder "did that work?"

#### Toasts

Use `showToast(message)` for success and `showToast(message, true)` for errors.

| Action | Toast |
|--------|-------|
| Theme switch | "Dark theme" / "Light theme" / "Auto theme (adapts to time of day)" |
| Session renamed | "Renamed to {name}" |
| Session deleted | "Session deleted" |
| Session duplicated | "Session duplicated" |
| Git sync complete | Shows result messages from server |
| Auto-name complete | Shows the generated name |
| Export/copy | "Copied to clipboard" / "Download started" |
| Errors | Red toast with plain-language explanation |

Rules:
- Toasts auto-dismiss after 3 seconds
- Error toasts are visually distinct (red border/text via `.toast.error`)
- Never show raw error messages or stack traces — translate to plain language
- Toast text should be short (under 60 characters)

#### Hover States

Every interactive element must have a hover state:

- **Buttons:** Border brightens, text lightens (`var(--border-hover)`, `var(--text-heading)`)
- **Session rows:** Background shifts to `var(--bg-hover)`
- **Cards (workforce view):** Border brightens, slight background shift
- **Links:** Underline on hover
- **Resize handles:** Accent color on hover

All hover transitions use `transition: 0.15s` for consistency.

#### Active/Selected States

- **Selected session:** Left border accent (`var(--accent)`), background `var(--bg-active)`
- **Active sort button:** Accent background and text
- **Open dropdown:** Accent border on trigger
- **Focused input:** Border changes to `var(--border-focus)`

#### Loading States

- **Session list loading:** Animated skeleton rows (shimmer animation) matching the table layout
- **Individual actions:** Spinner icon + descriptive text ("Naming...", "Building summary...")
- **Button during action:** Disable + show spinner. Re-enable on completion.
- Never leave the user staring at a blank screen — always show a loading indicator

#### Microinteractions

- **Skeleton shimmer:** `animation: shimmer 1.5s infinite linear` with staggered delays per row
- **Pulse animations:** Waiting sessions pulse orange, working sessions pulse purple
- **Slide transitions:** Extract drawer slides in from right (`transition: right 0.25s ease`)
- **Fade transitions:** Overlays fade in via `opacity` transition
- **Toast fade:** 0.3s opacity transition

#### Tooltips

- Session rows show a tooltip on hover with: title, preview text, date, size, message count, and status
- Tooltips appear after a short delay (CSS transition: `opacity 0.12s`)
- Tooltips follow the cursor position
- Tooltips disappear immediately on mouse leave
- Button tooltips use the native `title` attribute for simplicity

#### Disabled States

- Disabled buttons: `opacity: 0.4; cursor: default`
- Toolbar buttons disabled when no session is selected
- The entire toolbar is hidden (not just disabled) when no session is selected

### Mobile Friendliness

- Use `flex-wrap: wrap` on toolbars so buttons wrap on narrow screens
- Sidebar has `min-width: 180px` and is resizable
- Modals use `max-width: 90vw` to stay within viewport
- Touch targets should be at least 32px
- Font sizes should never go below 10px

### Accessibility

- All buttons must have a `title` attribute describing their action
- Interactive elements must be keyboard-navigable where possible
- Color should not be the only indicator of state — use icons/text alongside
- Sufficient contrast ratios in both themes (test with browser dev tools)

---

## Coding Standards

### Python

- **Python 3.10+** required (uses `str | None` union syntax)
- Use type hints for function signatures
- Use `Path` objects (not string paths) for file operations
- Use `from __future__ import annotations` if supporting older syntax
- Error handling: catch specific exceptions, never bare `except:`
- Subprocess calls: always use `capture_output=True` and `timeout`
- File I/O: always specify `encoding="utf-8"`
- No print statements in library code — only in the entrypoint banner
- Flask routes return `jsonify()` for API endpoints
- Route handlers should be under 20 lines — delegate to helper functions

### JavaScript

- Vanilla JS only — no frameworks, no bundler, no npm
- All functions are global (required by `onclick=""` attributes in HTML)
- Use `const` and `let`, never `var` (except in the inline theme script which targets maximum compatibility)
- Use `async/await` for fetch calls, not `.then()` chains
- Use template literals for HTML construction
- Always escape user content with `escHtml()` before inserting into HTML
- Event listeners: prefer `onclick` attributes for buttons, `addEventListener` for complex interactions
- DOM queries: use `getElementById` for known IDs, `querySelectorAll` for pattern matching

### CSS

- All colors via CSS custom properties (see Theming above)
- Use `var(--name)` syntax, never hardcoded hex in new code
- Class naming: lowercase with hyphens (`.session-item`, `.btn-group-label`)
- ID naming: lowercase with hyphens (`#main-toolbar`, `#btn-theme`)
- Prefer `flex` and `grid` for layout
- Transitions: use `0.15s` as default duration
- Animations: define `@keyframes` with descriptive names
- Scrollbar styling: consistent across the app (4-6px width, themed track/thumb)
- Media queries: add as needed for responsive behavior

### Git

- Commit messages in plain language: "Add session export feature", "Fix tooltip positioning on light theme"
- Commit after each logical change, not after every file edit
- Never commit `.pyc`, `__pycache__/`, or output files

---

## Testing Standards

### Setup

```bash
pip install pytest pytest-cov
```

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=app --cov-report=term-missing

# Run a specific test file
pytest tests/test_sessions.py -v

# Run a specific test
pytest tests/test_sessions.py::test_load_session_summary_caches -v
```

### When to Run Tests

- **Before committing:** Always run the full suite
- **After any Python change:** Run at minimum the relevant test file
- **After refactoring:** Run full suite + integration tests
- **Before merging/pushing:** Full suite must pass with 0 failures

### Test Structure

#### Unit Tests

Unit tests cover individual functions in isolation. Mock external dependencies (filesystem, subprocess, network).

```python
# tests/test_sessions.py
def test_load_session_summary_returns_correct_fields(tmp_path):
    """Summary dict must contain all required keys."""
    session_file = tmp_path / "abc123.jsonl"
    session_file.write_text('{"type":"user","message":{"content":"hello"},"timestamp":"2026-01-01T00:00:00Z"}\n')
    result = load_session_summary(session_file)
    assert result["id"] == "abc123"
    assert result["message_count"] >= 1
    assert result["preview"] == "hello"
    assert "date" in result
    assert "size" in result

def test_load_session_summary_caches(tmp_path):
    """Second call with same mtime returns cached result."""
    session_file = tmp_path / "abc123.jsonl"
    session_file.write_text('{"type":"user","message":{"content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n')
    r1 = load_session_summary(session_file)
    r2 = load_session_summary(session_file)
    assert r1 is r2  # same object from cache
```

#### Integration Tests

Integration tests verify end-to-end flows through the Flask app using the test client.

```python
# tests/test_integration.py
def test_full_session_workflow(client, mock_sessions_dir):
    """Create, rename, list, delete a session."""
    # List sessions
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    sessions = resp.get_json()
    assert len(sessions) > 0

    # Rename
    sid = sessions[0]["id"]
    resp = client.post(f"/api/rename/{sid}", json={"name": "Test Session"})
    assert resp.get_json()["ok"]

    # Verify rename
    resp = client.get(f"/api/session/{sid}")
    assert resp.get_json()["display_title"] == "Test Session"

    # Delete
    resp = client.post(f"/api/delete/{sid}")
    assert resp.get_json()["ok"]
```

#### Fixtures

Shared fixtures live in `tests/conftest.py`:

```python
@pytest.fixture
def app(tmp_path):
    """Create a test app with a temporary sessions directory."""
    # Patch _CLAUDE_PROJECTS and _sessions_dir to use tmp_path
    ...
    return create_app()

@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()

@pytest.fixture
def mock_sessions_dir(tmp_path):
    """Create a temp directory with sample .jsonl session files."""
    ...
```

### Extending Tests

When adding a new feature:

1. **Write the test first** (or alongside the code)
2. **One test file per module:** `test_sessions.py` tests `app/sessions.py`, etc.
3. **Test the happy path** and at least one error path
4. **Test edge cases:** empty files, missing fields, corrupt JSON, huge files
5. **For new routes:** Add both a unit test (mock the data layer) and an integration test (use test client)
6. **For new UI interactions:** Document the expected behavior in this file under the relevant section. JS is not unit-tested (no test runner configured), so UI behavior is verified manually and documented.

### Test naming convention

```python
def test_{function_name}_{scenario}():
    """Human-readable description of what this verifies."""
```

Examples:
- `test_load_session_summary_empty_file()`
- `test_load_session_summary_corrupt_json()`
- `test_api_rename_nonexistent_session()`
- `test_git_sync_pull_with_conflicts()`

### Coverage targets

- **app/config.py:** 90%+ (core utilities)
- **app/sessions.py:** 90%+ (critical data layer)
- **app/routes/:** 80%+ (all endpoints covered)
- **app/git_ops.py:** 70%+ (subprocess calls are hard to test)
- **app/process_detection.py:** 50%+ (heavily platform-dependent)

---

## Performance Standards

- **Session list load:** Under 3 seconds for 200 sessions (first load), under 200ms cached
- **API response times:** Under 500ms for list endpoints, under 100ms for cached endpoints
- **Page load:** Skeleton visible within 100ms, interactive within 3 seconds
- **Git status:** Never blocks other requests (background thread with cache)
- **File I/O:** Use head+tail reading for large session files, never read entire file for summaries
- **Caching:** Session summaries cached by (path, mtime, size) key. Invalidated automatically on file change.
- **Parallelism:** Use ThreadPoolExecutor (16 workers) for loading multiple session files

---

## Error Handling

### Python

- API errors return `{"ok": false, "error": "description"}` with appropriate HTTP status
- Never expose internal paths, stack traces, or technical details in API responses
- Log errors server-side (when logging is added), return user-friendly messages
- Subprocess failures: catch `TimeoutExpired` and `CalledProcessError` specifically
- File not found: return 404 with `{"error": "Not found"}`

### JavaScript

- Wrap all `fetch()` calls in try/catch
- On network error: show error toast, don't crash
- On unexpected API response: show generic error toast
- Never show raw JSON or error objects to the user
- Console.error for debugging, toast for user-facing errors

---

## Checklist for Every Change

- [ ] Does it work in dark theme?
- [ ] Does it work in light theme?
- [ ] Does it show loading state during async operations?
- [ ] Does it show success/failure feedback (toast)?
- [ ] Do interactive elements have hover states?
- [ ] Are new colors using CSS custom properties (not hardcoded hex)?
- [ ] Are tests passing? (`pytest tests/ -v`)
- [ ] Are new features covered by tests?
- [ ] Is the code in the right file per the architecture?
