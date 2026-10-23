# Contributing to VibeNode

Thanks for your interest in contributing! This guide covers everything you need to get started.

## Getting Started

### Prerequisites

- Python 3.10 or later
- Git
- [Claude Code](https://claude.ai/download) installed and signed in (the only supported backend today)
- Windows, macOS, or Linux. The test suite runs on all three; a handful of tests are platform-specific and skip elsewhere.

### Setup

```bash
git clone https://github.com/CustomerNode/VibeNode.git
cd VibeNode
pip install -r requirements-test.txt   # includes requirements.txt
playwright install chromium            # browser-driven tests (skipped without it)
```

`requirements.txt` is what the app needs; `requirements-test.txt` adds the test
tooling. Node 18+ is needed for the two `node tests/*.js` suites.

### Running the App

```bash
python session_manager.py
```

Opens automatically at http://localhost:5050. Two processes start: the web
server (port 5050) and the Session Engine (port 5051), which owns the Claude
CLI processes. Restarting the web server is cheap; restarting the Session
Engine ends every running session, so the app only does that when you ask
(System → Developer Tools → Restart Server → Session Engine). A change under
`daemon/` is not live until the Session Engine restarts.

### Running Tests

```bash
# The suite (fast tests; e2e is excluded by pytest.ini)
pytest

# One file
pytest tests/test_session_manager.py -v

# The JavaScript suites
node tests/test_thread_scroll.js
node tests/test_thread_scroll_classic.js

# End-to-end (needs a running server, Selenium and Chrome; also runs nightly in CI)
pytest tests/e2e -m e2e -v --timeout=300 --reruns 2
```

On Linux the suite re-executes itself inside a PID namespace (see
`tests/conftest.py`) so a bug in the process-kill path can only reach the test
run. Where `unshare` is unavailable it warns and continues behind two guards.
Never run tests with a `MagicMock` backend whose `extract_process_pid` returns
a mock; see the "kill path" section of `CLAUDE.md`.

## Project Structure

`CLAUDE.md` (project root) is the architecture reference and the list of
hard-won rules every change must respect. Read it before touching code. Key
points:

- **Web server** lives in `app/` — Flask + Flask-SocketIO; routes in `app/routes/` are thin, logic is in helper modules
- **Session Engine** lives in `daemon/` — owns Claude CLI processes; talks to the web server over local IPC (`app/daemon_client.py`, `daemon/daemon_server.py`)
- **HTML** lives in `templates/index.html` — one page; no inline JS or CSS
- **CSS** lives in `static/style.css` (plus `static/css/mobile.css`) — CSS custom properties for theming
- **JavaScript** lives in `static/js/` — vanilla JS, no framework, no bundler
- **Tests** live in `tests/` — pytest; browser tests drive the real `index.html` in headless Chromium
- **API docs** live in `docs/api/` — `openapi.yaml` and `socketio-events.md`, both enforced by coverage tests

## Making Changes

### Before You Start

1. Read `CLAUDE.md` for the project rules, performance-critical patterns, and the regressions that must not come back
2. Check existing issues to avoid duplicate work
3. For large changes, open an issue first to discuss the approach

### Development Workflow

1. Create a branch: `git checkout -b your-feature-name`
2. Make your changes
3. Run tests: `pytest tests/ -v`
4. Test in both dark and light themes
5. Commit with a clear message: `git commit -m "Add session export as PDF"`
6. Push and open a pull request

### Code Style

**Python:**
- Type hints on function signatures
- `Path` objects for file paths
- Specific exception handling (no bare `except:`)
- Routes under 20 lines — delegate to helpers

**JavaScript:**
- `const`/`let`, never `var`
- `async/await` for fetch calls
- Always escape user content with `escHtml()`
- Functions are global (required for `onclick=""` attributes)

**CSS:**
- All colors MUST use CSS custom properties: `var(--bg-card)`, `var(--text-primary)`
- Never add hardcoded hex colors — add a new variable to both dark and light themes
- Class names: lowercase with hyphens

### Adding a New Feature

1. **Backend route:** Add to the appropriate blueprint in `app/routes/`
2. **Business logic:** Add to the appropriate helper module in `app/`
3. **Frontend JS:** Add to the appropriate `static/js/` file (or create a new one if it's a distinct feature)
4. **CSS:** Add styles to `static/style.css` using CSS variables
5. **Tests:** Add unit tests for the backend logic and integration tests for the route
6. **Update `CLAUDE.md`** if you're adding a new architectural component or a rule future changes must respect
7. **Update API docs** if you added or changed a route or a SocketIO event (see below)

### Maintaining API Documentation

VibeNode has a comprehensive API reference at `docs/api/`. When you add or modify routes:

1. **Add the endpoint to `docs/api/openapi.yaml`** — include path, method, summary, parameters, request body, response schema, and tags. Use the existing entries as a template.
2. **If you added SocketIO events**, update `docs/api/socketio-events.md` with the event name, direction, payload schema, and example. Every `@socketio.on(...)` handler and every event name the browser listens for with `socket.on(...)` needs a `### \`event_name\`` section.
3. **Run the coverage tests** to verify nothing is missing: `pytest tests/test_api_docs_coverage.py tests/test_socketio_docs_coverage.py -v`
4. **View the docs locally** at http://localhost:5050/api/docs (Redoc renders the spec automatically).

`test_api_docs_coverage.py` compares all registered Flask routes against the OpenAPI spec; `test_socketio_docs_coverage.py` compares the server's handlers and the browser's listeners against the events reference. Either fails when something is undocumented.

### Adding a New Theme Color

1. Pick a descriptive name following the convention: `--bg-{component}`, `--text-{purpose}`, `--border-{context}`
2. Add the variable to BOTH theme blocks in `static/style.css`:
   - `:root, [data-theme="dark"]` — dark value
   - `[data-theme="light"]` — light value
3. Use `var(--your-new-variable)` in your CSS
4. Test in both themes

### Interaction Feedback Checklist

Every user-facing action needs feedback:

- [ ] Success toast (e.g., "Session renamed")
- [ ] Error toast for failures (red, plain language)
- [ ] Loading state during async operations (spinner or skeleton)
- [ ] Hover states on interactive elements
- [ ] Disabled state when action isn't available

## Testing

### Writing Tests

- One test file per module: `test_sessions.py` for `app/sessions.py`
- Name tests clearly: `test_{function}_{scenario}`
- Test happy path + at least one error path
- Use the fixtures in `tests/conftest.py` for mock session files

### What to Test

- **New routes:** Integration test with Flask test client
- **New helper functions:** Unit test with mock data
- **Edge cases:** Empty files, missing fields, corrupt JSON, large files
- **Error handling:** Verify proper error responses

### Coverage Targets

| Module | Target |
|--------|--------|
| `app/config.py` | 90% |
| `app/sessions.py` | 90% |
| `app/routes/` | 80% |
| `app/git_ops.py` | 70% |
| `app/process_detection.py` | 50% |

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR
- Include a description of what changed and why
- Reference any related issues
- Ensure all tests pass
- Test in both dark and light themes
- Screenshots appreciated for UI changes

## Reporting Issues

- Use GitHub Issues
- Include: what you expected, what happened, steps to reproduce
- Include your Python version and OS
- Screenshots help

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
