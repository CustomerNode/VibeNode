# Compose — Design Specification

## What Is Compose?

Compose is a new view in VibeNode (alongside Sessions, Workflow, Workforce) for **knowledge and content creation** — documents, spreadsheets, presentations, writing, research, concepts. It uses the same kanban-style board UI as Workflow but the hierarchy is geared toward content production, not software development.

## Why It Exists

The problem: when doing complex knowledge work with AI, you either put everything in one giant session (context bloats, sequential, AI loses focus) or break it into many small sessions (no shared awareness, user becomes the manual coordinator). Compose solves this with a hierarchy of AI-coordinated agents that decompose, delegate, and synthesize — working in parallel with shared context.

## The Parallel With Workflow

| Aspect | Workflow | Compose |
|--------|----------|---------|
| Spine | Codebase (source files) | Document source files (.md, .csv, .yaml, etc.) |
| Target | Software development | Knowledge/content creation |
| Board | Kanban columns | Kanban columns (same GUI pattern) |
| Task example | "Build auth module" | "Write market analysis section" |
| Agent works on | .py, .js, .ts | .md, .csv, .yaml, .html, .mmd |
| Output | Running software | Exported Word/Excel/PPT/PDF |

## The Shared Brain — compose-context.json

Every Compose project has a `compose-context.json` file that serves as a live shared brain for all agents. This is NOT a mailbox or event system — it is a file every agent reads before every action and writes to after every meaningful update.

```json
{
  "project": "Annual Report",
  "shared_prompts_enabled": true,
  "facts": {
    "tam_total": "$4.2B",
    "primary_competitor": "Acme Corp",
    "fiscal_year": "2026",
    "revenue_q1": "$12.3M"
  },
  "sections": {
    "revenue-analysis": {
      "status": "writing",
      "summary": "Analyzing Q1-Q4 trends, seeing 18% YoY growth",
      "last_updated": "2026-04-07T14:32:00Z",
      "changing": false,
      "change_note": null
    },
    "competitor-scan": {
      "status": "complete",
      "summary": "Identified 4 primary competitors, Acme leads at 34% share",
      "last_updated": "2026-04-07T14:28:00Z",
      "changing": false,
      "change_note": null
    }
  },
  "user_directives": [
    {
      "id": "d1",
      "gen": 1,
      "time": "2026-04-07T14:00:00Z",
      "said_to": "revenue-analysis",
      "directive": "Assume 10% growth rate",
      "shared": true,
      "scope": "revenue-analysis",
      "superseded_by": null
    }
  ]
}
```

### How agents use the shared brain

Every Compose agent, on every action:
1. **Reads** compose-context.json — knows what everyone else knows and is doing
2. **Does its work** on source files
3. **Writes back** its latest state, findings, key outputs to compose-context.json

This is baked into the agent system prompt, not optional, not user-configured.

### Mid-stream change awareness

When an agent is making a significant change to data other sections may depend on, it sets `"changing": true` with a `change_note` explaining what's changing. Other agents read the room:

- "changing" + I haven't used that data yet → keep working, grab final number later
- "changing" + I already built on that number → pause that part, continue on unrelated work
- "changing" + it's minor/irrelevant to me → ignore, keep going

The agent has full context to make that judgment from the change_note.

## User Directives — Temporal vs Contextual

When a user gives prompts to different sections, the system must handle the case where later prompts might contradict earlier ones. The key ambiguity: is a new directive a **correction** (supersedes the old one) or **contextual** (both are valid for different sections)?

### How directives are processed

Each user prompt gets logged to `user_directives` with a generation number (not just timestamp). When a new directive arrives, the agent scans existing directives for conflicts:

- **Clearly global update** (user says "actually" / "across the board") → mark old directive superseded, set scope to global
- **Clearly contextual** (user says "for the projections section") → scope both appropriately, no conflict
- **Ambiguous** → surface the conflict to the user immediately. Never silently guess.

Example conflict surfacing:
```
Directive conflict detected:

  2:00pm -> Revenue: "Assume 10% growth rate"
  3:00pm -> Projections: "Use 25% growth rate"

  Should the 25% rate apply to all sections (superseding 10%),
  or is each section using a different assumption intentionally?
```

User resolves in one click/sentence. Resolution logged as a new directive.

### Shared Prompts Toggle — PROJECT LEVEL

One toggle per Compose project (not per-prompt, not per-session):

- **ON (default):** All user prompts across all sections flow into the shared directives ledger. Conflict detection active. Full shared brain experience.
- **OFF:** Agents still share the context file (facts, status, outputs) but individual prompts to each agent stay private.

Even when OFF, if the agent detects a private prompt contains something that sounds like a global fact/assumption, it nudges: "This looks like it might affect other sections. Want to share it?" Not blocking, just a rare nudge.

This toggle lives in Compose project settings. Set once, change anytime.

## Artifact Types (Source -> Export)

The AI works on editable source representations, then exports to final formats:

| Deliverable | AI edits this | Export tool |
|---|---|---|
| Word document | Markdown (.md) | pandoc -> .docx |
| Spreadsheet | CSV or JSON schema | openpyxl / python-xlsx |
| PowerPoint | Markdown + YAML front matter | pandoc / python-pptx |
| PDF report | Markdown + LaTeX snippets | pandoc -> PDF |
| Diagram | Mermaid / PlantUML text | mermaid-cli -> SVG/PNG |
| Website/email | HTML + CSS | browser / mjml |
| Flowcharts | Mermaid (.mmd) | mermaid-cli |
| Org charts | Mermaid/PlantUML | mermaid-cli |
| Timelines | Mermaid gantt / timeline | mermaid-cli |

Full content types: reports, proposals, briefs, contracts, letters, blog posts, whitepapers, SOPs, manuals, scripts, copy, financial models, budgets, forecasts, survey results, comparison matrices, scorecards, pitch decks, board decks, training materials, webinar slides, sales decks, wireframes, infographics, project plans, checklists, questionnaires, forms, inventories, catalogs, emails, newsletters, social posts, press releases, talking points.

## Project Folder Structure

```
/compose-projects/{project-name}/
  compose-context.json        <- shared brain (facts, status, directives)
  brief.md                    <- root agent's project brief
  sections/
    ceo-letter/
      draft.md
    revenue/
      analysis.md
      data.csv
    market/
      competitors.md
      tam.md
    projections/
      model.md
      financials.csv
  export/
    config.yaml               <- template, styles, format settings
```

## Agent System Prompt Core

Every Compose agent gets this baked into its system prompt:

```
You are working on section "{section_name}" of the Compose project "{project_name}".

Before EVERY action, read compose-context.json to understand:
- What other agents have discovered (facts)
- What other sections are doing (status, summaries)
- Whether anything you depend on is mid-change (changing flag)

After EVERY meaningful update to your work:
- Update your section's status and summary in compose-context.json
- Add any facts/numbers/decisions other sections might need
- If you're making a significant change to something you already published,
  set changing:true with a change_note BEFORE you start,
  then set changing:false when done

You have full access to read any sibling's output files.
Use your judgment on whether a sibling's mid-stream change affects you.
```

When shared_prompts_enabled is true, the agent also:
- Logs every user directive to user_directives in compose-context.json
- Scans existing directives for potential conflicts on the same topic
- Surfaces ambiguous conflicts to the user for resolution
- Never silently picks one interpretation over another

## How It Maps to the Existing Codebase

### Reuse from Workflow (kanban)
- Board UI: columns, task cards, drag-and-drop (kanban.js)
- Task CRUD: create, edit, delete, move, reorder (kanban_api.py)
- Hierarchy: parent/child task nesting, drill-down views
- AI planner: slideout panel, session-based planning (kanban-planner.js -> new compose-planner.js)
- Session linking: tasks spawn/link to Claude sessions

### New for Compose
- `compose-context.json` management (read/write/merge shared brain)
- Agent system prompt injection (compose-specific system prompt with shared brain instructions)
- Shared prompts toggle (project-level setting)
- Directive conflict detection and resolution UI
- Export pipeline (source files -> final deliverables via pandoc/openpyxl/python-pptx)
- Project folder management (create/organize section folders)
- Mid-stream change awareness (changing flag handling in UI — subtle indicator on cards)
- Compose-specific view mode in app.js (`viewMode: 'compose'`)

### Navigation
Add "Compose" as a new view mode alongside Home, Sessions, Workflow, Workforce:
```javascript
compose: { label: 'Compose', desc: 'Knowledge & content creation with coordinated AI agents' }
```

### Backend
- New route file: `app/routes/compose_api.py`
- Compose project CRUD
- compose-context.json read/write endpoints
- Export endpoints (trigger pandoc/openpyxl conversions)
- Reuse kanban board/task data model or create parallel compose_board/compose_task tables
