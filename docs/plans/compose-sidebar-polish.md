# Compose Sidebar Polish: Undo Delete, Keyboard Navigation, Status Indicators

## Objective

Three enhancements to the Compose sidebar that improve safety, accessibility, and information density. These build on top of the existing sidebar which already has: composition picker, drag-reorder, right-click context menu (Rename/Duplicate/Pin/Delete), bulk actions (checkbox + shift-select + Pin/Delete/Clear), search filter, cross-project pinned visibility, and localStorage persistence.

## Architecture Context

- **Frontend**: Vanilla JS, no framework. All compose logic lives in `static/js/app.js`. CSS in `static/style.css`. HTML shell in `templates/index.html`.
- **Backend**: Flask at `app/routes/compose_api.py`. Models at `app/compose/models.py`.
- **Sidebar rendering**: `_renderComposeSidebar()` in `app.js` builds HTML string and sets `sidebar.innerHTML`. Called after every state change (switch, create, delete, pin, reorder, filter).
- **State**: `_composeProjectsList` (array of composition objects), `_activeComposeProjectId`, `_composeSelected` (Set), `_composeSearchFilter`, `_composeLastClickedId`.
- **Board response**: `/api/compose/board` returns `{ project, sections, status: { total_sections, complete, in_progress, not_started }, conflicts, sibling_projects }`.
- **Existing keyboard shortcuts**: `attachComposeShortcuts()` handles N (new section), P (plan), R (refresh), E (export), Escape (clear selection, then close drill-down), ? (help).
- **Existing context menu**: `_composeCtxMenu()` creates a positioned div, `_composeCtxMenuClose()` cleans up DOM + listener.
- **Bulk delete**: `_composeBulkDelete()` sequentially calls `DELETE /api/compose/projects/<id>` for each selected item, then calls `initCompose()`.
- **Single delete**: `_composeDelete()` calls `DELETE /api/compose/projects/<id>`, then calls `initCompose()`.

---

## Enhancement 1: Undo for Bulk Delete with Grace Period

### What to build

When the user deletes compositions (single or bulk), instead of immediately calling the DELETE endpoint:

1. Hide the deleted items from the sidebar immediately (optimistic UI)
2. Show a toast with an "Undo" button and a 5-second countdown
3. If the user clicks "Undo" before the timer expires, restore the items to the sidebar
4. If the timer expires without undo, execute the actual DELETE calls
5. If the user navigates away from compose view during the grace period, execute the DELETEs immediately (don't lose them)

### Step 1: Add pending-delete state and modify `_composeDelete()` / `_composeBulkDelete()`

**What to change:**
- Add `_composePendingDeletes` array to hold `{ ids: [...], timer: timeoutId }` objects
- Modify `_composeDelete()` to use the grace period instead of immediate DELETE
- Modify `_composeBulkDelete()` to use the grace period instead of immediate DELETE
- Modify `_renderComposeSidebar()` to filter out items whose IDs are in `_composePendingDeletes`
- Clear `_composePendingDeletes` in `resetComposeState()`

**Acceptance criteria:**
- Clicking delete hides the item immediately but doesn't call the backend yet
- The composition is still on disk during the grace period
- `_composePendingDeletes` tracks what's pending so multiple deletes can be in flight

### Step 2: Add undo toast with countdown

**What to change:**
- Create `_showUndoToast(message, undoCallback, timeoutMs)` function
- The toast should show the message + an "Undo" button + a countdown indicator
- Clicking "Undo" calls `undoCallback`, clears the timer, and removes the toast
- When the timer expires, execute the actual deletes and remove the toast
- Style the undo toast distinctly from regular toasts (keep it visible longer, different position or style)
- Check if `showToast` already supports custom actions/buttons — if so, extend it. If not, create a separate undo toast mechanism.

**Acceptance criteria:**
- Toast appears immediately after delete with "Undo" button
- Clicking Undo restores items to the sidebar
- Timer expiring triggers actual backend DELETE calls
- Multiple pending deletes can coexist (each with their own toast/timer)

### Step 3: Handle edge cases — navigation away, active composition deleted

**What to change:**
- In `resetComposeState()` or when switching away from compose view, flush all pending deletes immediately (execute the DELETE calls)
- If the active composition is in the pending-delete set and the user doesn't undo, clear `_activeComposeProjectId` and localStorage after the timer fires
- If the user clicks Undo and the restored composition was the active one, re-select it

**Acceptance criteria:**
- Switching projects or views during grace period executes pending deletes
- Active composition deletion is handled correctly in both undo and no-undo paths
- No orphaned timers after view switch

---

## Enhancement 2: Keyboard-Driven Sidebar Navigation

### What to build

Full keyboard navigation for the composition sidebar list:

- **Arrow Up/Down**: Move focus between compositions in the sidebar
- **Enter**: Switch to the focused composition (same as clicking it)
- **Space**: Toggle the checkbox on the focused composition (same as clicking the checkbox)
- **Shift+Arrow**: Extend selection from current to next/previous (like shift-click range)
- Focus should be visually indicated with a distinct outline/highlight
- Focus state should be separate from selection state (you can focus an unselected item)

### Step 4: Add focus state and arrow key navigation

**What to change:**
- Add `_composeFocusedId` state variable (the ID of the keyboard-focused composition, null = none)
- In `attachComposeShortcuts()`, add handlers for ArrowUp, ArrowDown, Enter, Space
- ArrowUp/Down should move `_composeFocusedId` through `_composeProjectsList` (respecting search filter — skip filtered-out items)
- Render a focus ring on the focused item in `_renderComposeSidebar()` (CSS class `compose-sidebar-focused`)
- Clear `_composeFocusedId` in `resetComposeState()`
- Set `_composeFocusedId` when clicking a composition (so mouse and keyboard focus stay in sync)

**Acceptance criteria:**
- Arrow keys move a visible focus indicator through the sidebar list
- Focus wraps at top/bottom or stops (designer's choice — stopping is simpler)
- Focus skips items hidden by the search filter
- Focus indicator is visually distinct from selection highlight and active highlight
- Clicking a composition sets keyboard focus to it

### Step 5: Enter, Space, and Shift+Arrow handlers

**What to change:**
- **Enter** on a focused item: call `switchComposition(_composeFocusedId)` — same as clicking
- **Space** on a focused item: call `_composeToggleSelect(event, _composeFocusedId)` — toggles checkbox
- **Shift+ArrowUp/Down**: Move focus AND add the traversed items to `_composeSelected` (range extension)
- Ensure these handlers only fire when the compose view is active and no input/textarea has focus (to avoid conflicts with search input or other text fields)

**Acceptance criteria:**
- Enter switches to the focused composition
- Space toggles selection on the focused composition
- Shift+Arrow extends selection while moving focus
- None of these fire when typing in the search input or other text fields
- The sidebar visually updates immediately (no lag)

---

## Enhancement 3: Composition Status Indicators in Sidebar

### What to build

Show a small progress indicator next to each composition name in the sidebar. The data is already available — the board endpoint returns `status: { total_sections, complete, in_progress, not_started }` for the active composition, and `sibling_projects` is an array of project objects. The challenge is that sibling_projects doesn't currently include section status — that data needs to be added.

### Step 6: Backend — include status summary in sibling_projects

**What to change:**
- In the `/api/compose/board` endpoint (compose_api.py), when building `sibling_projects`, compute and include a `status` object for each project (total_sections, complete, in_progress, not_started)
- This requires calling `get_sections()` for each sibling project. To keep it efficient, only compute counts (not full section data).
- Also include a `has_conflicts` boolean (any pending conflicts in the project's context)

**Acceptance criteria:**
- Each object in `sibling_projects` array now has a `status` field with section counts
- Each object has a `has_conflicts` boolean
- The active project's status matches the top-level `status` field (consistency)
- Performance: getting section counts for ~10 projects should be fast (disk reads only, no computation)

### Step 7: Frontend — render status indicators in sidebar

**What to change:**
- In `_renderComposeSidebar()`, for each composition in the list, render a small status indicator after the name
- Format: `"3/7"` (complete/total) as a faint text span, OR a thin colored bar, OR a colored dot (green/yellow/red)
- Recommended: use the fraction format `"3/7"` with a colored dot:
  - Green dot: all sections complete
  - Yellow/amber dot: sections in progress
  - Red dot: has unresolved conflicts
  - Gray dot: no sections yet
- The indicator should be small and unobtrusive — it's supplementary info, not the primary content

**Acceptance criteria:**
- Each composition in the sidebar shows its section progress
- Colors correctly reflect the composition's state
- Compositions with 0 sections show a gray dot (no misleading "complete" state)
- The indicators update when `initCompose()` re-fetches data
- The styling doesn't break the existing sidebar layout (checkbox + icon + name + pin dot + status)

---

## Execution Process

Execute each step (1 through 7) sequentially. After completing each step:

1. **Self-verify**: Check for syntax errors, missing imports, broken references.

2. **Run the review team**:

   > Run the review team: Test Engineer, Quality Engineer, Product Manager, Senior Software Engineer. Review Step N of the Compose Sidebar Polish spec. Check: Does the implementation match the spec for this step? Are there unintended consequences to other parts of the app? Does it follow the existing code patterns and architectural practices in this codebase? Fix all issues found. Report what you found and what you fixed.

3. **If the review team found and fixed issues**: Run the review team a second time to verify the fixes didn't introduce new problems. Then proceed to the next step.

4. **If no issues were found**: Proceed directly to the next step.

5. **After all 7 steps are complete**: Run a final end-to-end review of ALL changes together. Verify the three enhancements work together without conflicts. Check edge cases across feature boundaries (e.g., undo delete + keyboard focus, keyboard navigation + search filter, status indicators + drag reorder).

6. **If the final review found issues**: Fix them, run the review team a second time. Report final status.

7. **After the final review is clean**: Suggest the top 3 enhancements consistent with the spec's intent or pointing out unknown unknowns. Plain English suggestions only — do not implement them.

## Rules

- Do NOT restart the server or daemon. The user will do that manually.
- Do NOT modify any files outside the scope of this spec.
- Follow existing code patterns — vanilla JS, no frameworks, same CSS variable usage, same naming conventions.
- Keep changes minimal — don't refactor surrounding code.
- Check existing `showToast` implementation before building a new toast system — extend it if possible.
- The undo toast must be distinct from normal toasts — it needs to persist for 5 seconds with a clickable button, not auto-dismiss like normal toasts.
- Keyboard handlers must not conflict with the search input or any other text fields. Use `document.activeElement.tagName` checks.
- Status indicators must not add significant latency to `initCompose()`. Getting section counts should be fast.
