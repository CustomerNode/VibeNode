# Compose UI Redesign: Match Workflow Pattern

## Objective

Redesign the Compose view so it works like the Workflow (kanban) view — with a composition picker/switcher in the sidebar, the ability to see all compositions for the active project, and the ability to create and switch between compositions without leaving the view. Currently Compose auto-selects the most recent composition with no way to see or switch to others.

## Architecture Context

- **Compose data lives on disk** at `compose-projects/` — each composition is a folder with `project.json`, `compose-context.json`, and `sections/`
- **`ComposeProject`** model in `app/compose/models.py` has: `id`, `name`, `created_at`, `root_session_id`, `shared_prompts_enabled`, `parent_project`
- **`parent_project`** stores the encoded VibeNode project name (e.g., `"C--Users-donca-Documents-VibeNode"`) to scope compositions to projects
- **`localStorage.getItem('activeProject')`** on the frontend holds the current VibeNode project's encoded name
- **The Workflow view** uses a sidebar with a board list — each board is clickable, active one is highlighted. The Compose sidebar should follow the same pattern.

## Current State (What's Broken)

1. `initCompose()` in `static/js/app.js` (~line 1721) calls `GET /api/compose/board?project=X` which auto-selects the **most recent** composition matching the active project. No list, no picker, no way to switch.
2. `GET /api/compose/projects` in `app/routes/compose_api.py` (line 171) returns ALL compositions with **no parent_project filtering**.
3. `_renderComposeSidebar()` in `static/js/app.js` (~line 1951) renders sidebar content but has **no composition list**.
4. There is no `switchComposition()` function — the only way to see a different composition is to have it be the most recent one.

## Spec: What To Build

### Step 1: Backend — Add parent_project filter to GET /api/compose/projects

**File:** `app/routes/compose_api.py`, lines 171-175

Add an optional `?project=` query parameter to `list_all_projects()`. When provided, filter the results to only return compositions where `parent_project` matches the parameter. When not provided, return all (backwards compatible).

**Acceptance criteria:**
- `GET /api/compose/projects?project=C--Users-donca-Documents-VibeNode` returns only compositions belonging to that project
- `GET /api/compose/projects` (no param) returns all compositions (unchanged behavior)
- Compositions with `parent_project = None` should be included when no filter is applied, excluded when a filter is applied

---

### Step 2: Frontend State — Add composition selection state

**File:** `static/js/app.js`

Add two new state variables near the existing compose state (~line 1711-1715):
- `_activeComposeProjectId` — the ID of the currently selected composition (null = auto-select most recent)
- `_composeProjectsList` — array of all composition objects for the active project

Clear both in `resetComposeState()` (~line 2569).
Clear `_activeComposeProjectId` when the parent project changes in `setProject()` — look at the compose reset block around line 186-191.

**Acceptance criteria:**
- State variables exist and are initialized
- They are cleared on parent project switch and compose state reset
- No functional change yet (these are just variables)

---

### Step 3: Rewrite initCompose() — Two-step fetch with composition selection

**File:** `static/js/app.js`, `initCompose()` (~line 1721)

Rewrite to:
1. Fetch the filtered project list: `GET /api/compose/projects?project=${activeProject}`
2. Store the list in `_composeProjectsList`
3. Determine which composition to load:
   - If `_activeComposeProjectId` is set and exists in the list, use it
   - Otherwise default to the most recent one (`list[list.length - 1]`)
   - If the list is empty, call `_renderComposeEmpty()` and return
4. Set `_activeComposeProjectId` to the chosen project's ID
5. Fetch that specific board: `GET /api/compose/board?project_id=${_activeComposeProjectId}`
6. Continue with existing render logic (header, sections, sidebar)

**Acceptance criteria:**
- Compose view loads the correct composition
- If multiple compositions exist for the project, the most recent is shown by default
- If the list is empty, the "Welcome to Compose" empty state shows
- Switching parent projects and returning to compose loads the right composition

---

### Step 4: Rewrite _renderComposeSidebar() — Add composition list

**File:** `static/js/app.js`, `_renderComposeSidebar()` (~line 1951)

Add a "Compositions" section to the sidebar that lists all compositions in `_composeProjectsList`. Each entry should:
- Show the composition name
- Highlight the active one (matching `_activeComposeProjectId`)
- Be clickable — calls `switchComposition(projectId)` on click
- Show a "+ New Composition" button at the bottom that calls `composeCreateProject()`

Follow the same visual pattern as the Workflow sidebar's board list. Use similar CSS classes or create new ones that match the style.

**Acceptance criteria:**
- All compositions for the active project appear in the sidebar
- The active composition is visually highlighted
- Clicking a different composition switches to it
- "+ New Composition" button works and creates a new composition

---

### Step 5: Add switchComposition() function

**File:** `static/js/app.js`

Add a new function `switchComposition(projectId)` that:
1. Sets `_activeComposeProjectId = projectId`
2. Calls `initCompose()` to reload the board with the new selection
3. Updates the URL hash if applicable

**Acceptance criteria:**
- Clicking a composition in the sidebar switches the board view to that composition
- The sidebar updates to highlight the new active composition
- The board header shows the correct composition name
- Sections from the previous composition are replaced by sections from the new one

---

### Step 6: Update _submitComposeProject() — Auto-switch to new composition

**File:** `static/js/app.js`, `_submitComposeProject()` (~line 1815)

After successfully creating a new composition, set `_activeComposeProjectId` to the new project's ID before calling `initCompose()`. This ensures the view switches to the newly created composition.

**Acceptance criteria:**
- Creating a new composition immediately switches to it
- The new composition appears in the sidebar list
- The board shows the new (empty) composition

---

### Step 7: Bump cache versions

**File:** `templates/index.html`

Bump the version query parameter on any changed static files (app.js at minimum) so browsers fetch the updated code.

**Acceptance criteria:**
- After server restart and browser refresh, the new UI appears without needing hard refresh or cache clear

---

## Execution Process

Execute each step sequentially. After completing each step:

1. **Self-verify**: Make sure the code you wrote is correct — no syntax errors, no missing imports, no broken references.

2. **Run the review team**: Invoke the review team prompt:

   > Run the review team: Test Engineer, Quality Engineer, Product Manager, Senior Software Engineer. Review Step N of the Compose UI Redesign. Check: Does the implementation match the spec for this step? Are there unintended consequences to other parts of the app? Does it follow the existing code patterns and architectural practices in this codebase? Fix all issues found. Report what you found and what you fixed.

3. **If the review team found and fixed issues**: Run the review team a second time to verify the fixes didn't introduce new problems. Then proceed to the next step.

4. **If no issues were found**: Proceed directly to the next step.

5. **After all steps are complete**: Do a final end-to-end review of all changes together. Verify the complete flow works: switching projects in sidebar, creating new compositions, the board loading correctly, cache busting working.

## Rules

- Do NOT restart the server or daemon. The user will do that manually.
- Do NOT modify any files outside the scope of this spec.
- Follow existing code patterns — vanilla JS, no frameworks, same CSS variable usage, same naming conventions.
- Keep changes minimal — don't refactor surrounding code.
- The Workflow sidebar implementation in `static/js/workforce.js` and the kanban sidebar in `static/js/workspace.js` are your reference for how sidebar lists should look and behave. Match their patterns.
