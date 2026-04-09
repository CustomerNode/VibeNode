# Root Orchestrator Implementation Notes

## Step 1: Data Model & Project Scaffolding
**Files created:** `app/compose/__init__.py`, `app/compose/models.py`
**Status:** COMPLETE

### Review Results
- **Product Manager:** PASS. Models match spec. Project/Section/Conflict/Directive/Fact all present. Scaffolding creates correct folder structure.
- **Test Engineer:** PASS. Inline tests confirm create/serialize/deserialize round-trip works. Scaffolding creates all expected files.
- **QA Engineer:** PASS. Name collision handling present (appends id suffix). shutil.rmtree uses ignore_errors. sanitize_folder_name strips unsafe chars.
- **Senior Coder:** PASS. Follows kanban dataclass pattern (to_dict/from_dict). JSON file storage matches codebase convention.

### Non-blocking issues logged:
- NB-1: `project_dir()` scans all project folders linearly on every call. Fine for <100 projects. Could add caching later.
- NB-2: No file locking on project.json writes yet (context_manager.py will handle context locking in Step 4).

## Step 2: Compose API — Project CRUD
**Files modified:** `app/routes/compose_api.py` (replaced stub)
**Files created:** `app/compose/context_manager.py`, `app/compose/prompt_builder.py` (stub), `app/compose/conflict_detector.py` (stub)
**Status:** COMPLETE

### Review Results
- **Product Manager:** PASS. All CRUD endpoints present. Board endpoint returns project+sections+status+conflicts. Follows kanban_api pattern.
- **Test Engineer:** PASS. App starts with 15 compose routes. Blueprint registered correctly.
- **QA Engineer:** PASS. Error handling wraps all endpoints in try/except with logging. 404s returned for missing projects/sections. Input validation on required fields.
- **Senior Coder:** PASS. Pattern matches kanban_api.py (Blueprint, _emit helper, JSON responses). Deferred imports avoid circular dependency. context_manager uses per-project locks and atomic writes.

### Non-blocking issues logged:
- NB-3: GET /api/compose/board falls back to "most recently created project" when no project_id param. May want explicit active-project tracking later.
- NB-4: prompt_builder and conflict_detector are stubs. Steps 5 and 6 will implement.

## Step 3: Section CRUD & Hierarchy
**Files modified:** Already in `app/routes/compose_api.py` (included in Step 2)
**Status:** COMPLETE (combined with Step 2)

### Review Results
- **Product Manager:** PASS. Section create/update/delete/reorder endpoints all present. parent_id supports hierarchy. Section scaffolding creates folder structure.
- **Test Engineer:** PASS. Section creation auto-increments order. Reorder accepts ID list.
- **QA Engineer:** PASS. Section deletion removes from both context and filesystem. Orphaned section handling implicit (parent_id remains even if parent deleted).
- **Senior Coder:** PASS. Context-centric design (sections live in compose-context.json, not separate DB table). Matches JSON-file-storage pattern.

## Step 4: Compose Context Management
**Files created:** `app/compose/context_manager.py` (full implementation, created in Step 2)
**Files modified:** `app/compose_watcher.py` (replaced stub with real implementation)
**Status:** COMPLETE

### Review Results
- **Product Manager:** PASS. All context operations work: read/write, facts, section status, directives with auto-gen, changing flag with ownership validation, conflicts.
- **Test Engineer:** PASS. End-to-end test confirms all context operations. Changing flag ownership enforcement works correctly.
- **QA Engineer:** PASS. Atomic writes (write to temp, os.replace). Per-project threading locks prevent concurrent corruption. Lock hierarchy is clean (no deadlock risk).
- **Senior Coder:** PASS. compose_watcher uses polling (no external dependency). Daemon thread auto-exits. app_context() used correctly for SocketIO emission.

### Non-blocking issues logged:
- NB-5: Watcher polls at 1s interval. Could use watchdog for lower latency, but polling is simpler and matches existing patterns.
- NB-6: Watcher emits compose_changing for every section with changing:true on each poll cycle, not just on transitions. Acceptable for now.

## Step 5: Root Orchestrator System Prompt & Session
**Files modified:** `app/compose/prompt_builder.py` (replaced stub with full implementation)
**Status:** COMPLETE

### Review Results
- **Product Manager:** PASS. Root prompt includes full spec (3 responsibilities, conflict resolution, routing). Section prompt scoped correctly. ws_events.py integration works end-to-end.
- **Test Engineer:** PASS. Root and section prompts generate correctly. Task ID parsing handles all formats. Session linking updates both project and section records.
- **QA Engineer:** PASS. Invalid task IDs return clear error messages. Missing project/section returns proper error dict. link_session handles missing entities gracefully.
- **Senior Coder:** PASS. compose_task_id format is clean and parseable. Prompt templates inject current context (sections, facts, directives, conflicts).

### Non-blocking issues logged:
- ~~NB-7: Root session does not auto-create when project is created.~~ **RESOLVED** — create_project() now auto-spawns root session via daemon_client. Failure is non-blocking.

## Step 6: Directive Conflict Detection & Resolution
**Files modified:** `app/compose/conflict_detector.py` (replaced stub with full implementation)
**Status:** COMPLETE

### Review Results
- **Product Manager:** PASS. Three-path resolution works: global auto-supersedes, contextual auto-scopes, ambiguous creates conflict. User resolution via API with supersede/scope/keep_both.
- **Test Engineer:** PASS. All three paths tested. Conflict resolution marks conflict as resolved, logs resolution directive, sets changing flag on affected sections.
- **QA Engineer:** PASS. Keyword detection uses word boundaries (no partial matches). Resolution is atomic (within context lock). Superseded directives stay in history.
- **Senior Coder:** PASS. Heuristic conflict detection (word overlap) is simple but effective for MVP. generate_recommendation produces clear user-facing text.

### Non-blocking issues logged:
- NB-8: Conflict detection heuristic (30% word overlap) may produce false positives. Could be refined with LLM-based classification later.
- NB-9: Resolution directive has predictable id format (resolution-{conflict_id}). Fine for now.

## Step 7: Changing Flag Protocol
**Files modified:** Already in `app/compose/context_manager.py` (implemented in Step 4)
**Status:** COMPLETE

### Review Results
- **Product Manager:** PASS. Root sets changing on any section, only section can clear its own flag. API endpoint works. Socket event emitted.
- **Test Engineer:** PASS. All ownership rules verified: root set, root blocked from clear, section self-clear, section self-set.
- **QA Engineer:** PASS. ValueError raised with clear message when ownership rule violated. No race condition risk (context lock held during validation).
- **Senior Coder:** PASS. Clean separation of set_changing and clear_changing. changing_set_by field enables ownership tracking.

## Step 8: UI — Root Header Bar, Input Routing, Sidebar
**Files modified:** `templates/index.html`, `static/js/app.js`, `static/style.css`
**Status:** COMPLETE

### Review Results
- **Product Manager:** PASS. Root header bar shows composition name, section count, conflict indicator. Input target shows who you're talking to (root vs section). Sidebar grouping structure defined.
- **Test Engineer:** PASS. Index page renders with compose-root-header. Board endpoint returns data. initCompose fetches and renders.
- **QA Engineer:** PASS. Header hidden when no project exists. Conflict indicator only shows when pending conflicts >0. resetComposeState cleans up on view switch.
- **Senior Coder:** PASS. Follows existing patterns (socket event handlers, viewMode checks). No circular dependencies. CSS uses teal accent consistently.

### Non-blocking issues logged:
- ~~NB-10: Sidebar session grouping (getComposeSessionGroups) is defined but not yet called from renderList.~~ **RESOLVED** — renderList now uses compose grouping when viewMode === 'compose'.
- ~~NB-11: initCompose could pre-populate the skeleton board with real section cards.~~ **RESOLVED** — _renderComposeSectionCards() renders status-column cards with click selection.

## Step 9: Tests
**Files created:** `tests/test_compose_models.py`, `tests/test_compose_context.py`, `tests/test_compose_conflicts.py`, `tests/test_compose_api.py`
**Status:** COMPLETE

### Results
- 71 tests, all passing
- Coverage: models (18), context management (11), conflict detection (16), API endpoints (26)
- Tests verify: serialization round-trips, atomic writes, fact merging, directive gen incrementing, conflict detection (all 3 paths), resolution flow, changing flag ownership rules, full CRUD endpoints, board endpoint

## Step 10: Integration & Cleanup
**Status:** COMPLETE

### Integration Test
Full end-to-end flow verified:
1. Create project -> root session starts -> prompt built
2. Create 3 sections -> scaffold folders -> add to context
3. Launch section sessions -> section prompts built with project context
4. Directives flow -> gen numbers increment
5. Conflict detected (ambiguous) -> user resolves (supersede)
6. Changing flags set by root -> section clears own flag
7. Facts and status updates propagate to root prompt
8. Root prompt reflects full current state

### Final Verification
- App starts clean with 15 compose routes
- All 71 tests pass
- No orphaned imports or dead code
- Compose watcher thread starts automatically

---

## All Non-Blocking Issues (Deferred)

| # | Issue | Priority |
|---|-------|----------|
| NB-1 | project_dir() scans folders linearly. Add caching for >100 projects. | Low |
| NB-2 | No file locking on project.json writes (only context gets locked). | Low |
| NB-3 | Board endpoint falls back to most recent project. Add explicit active-project tracking. | Medium |
| NB-7 | ~~Root session does not auto-create on project creation.~~ **RESOLVED** — auto-creates via daemon_client in create_project(). | ~~High~~ Done |
| NB-5 | Watcher polls at 1s. Could use watchdog for lower latency. | Low |
| NB-6 | Watcher emits compose_changing on every poll cycle for flagged sections. | Low |
| NB-8 | Conflict detection heuristic (word overlap) may produce false positives. Refine with LLM. | Medium |
| NB-9 | Resolution directive has predictable id format. | Low |
| NB-10 | ~~Sidebar session grouping defined but not wired into renderList.~~ **RESOLVED** — renderList calls getComposeSessionGroups in compose mode. | ~~Medium~~ Done |
| NB-11 | ~~initCompose doesn't populate section cards in the board yet.~~ **RESOLVED** — _renderComposeSectionCards() renders cards in status columns. | ~~Medium~~ Done |

## Files Created/Modified

### Created
- `app/compose/__init__.py` - Package init, re-exports models
- `app/compose/models.py` - Data models, scaffolding, CRUD helpers
- `app/compose/context_manager.py` - Context read/write, facts, directives, changing flags
- `app/compose/prompt_builder.py` - Root orchestrator and section agent system prompts
- `app/compose/conflict_detector.py` - Three-path conflict detection and resolution
- `tests/test_compose_models.py` - 18 model tests
- `tests/test_compose_context.py` - 11 context management tests
- `tests/test_compose_conflicts.py` - 16 conflict detection tests
- `tests/test_compose_api.py` - 26 API endpoint tests

### Modified
- `app/routes/compose_api.py` - Replaced stub with 15 real endpoints; NB-7: added root session auto-creation in create_project()
- `app/compose_watcher.py` - Replaced stub with real file watcher
- `templates/index.html` - Added root header bar, input target HTML, NB-11: compose-sections-board container
- `static/js/app.js` - Added initCompose, compose state management, socket handlers; NB-11: _renderComposeSectionCards()
- `static/js/sessions.js` - NB-10: extracted _renderSessionRow(), added compose grouping to renderList()
- `static/style.css` - Added root header, input target, sidebar grouping styles; NB-11: compose-sections-board, compose-card-selected
- `tests/test_compose_deferred.py` - 5 NB-7 tests for root session auto-creation
