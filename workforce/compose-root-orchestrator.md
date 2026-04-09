---
id: compose-root-orchestrator
name: Root Orchestrator
department: compose
source: vibenode
version: 1.0.0
depends_on: [compose-intent, compose-answer, compose-pm]
status: proposed
---

# Root Orchestrator Spec

## What this gives you

You talk to section agents when you're working on a section. You talk to the root when you're thinking about the whole thing. It's the difference between editing a chapter and talking to the editor-in-chief.

Without a root orchestrator, you become the coordinator. You're the one noticing that revenue and projections use different growth rates. You're the one deciding when to add a section or merge two that overlap. You're the one assembling the final export. Compose is supposed to eliminate that job, not dress it up with a kanban board.

The root orchestrator is always there, already running, watching the whole composition. It handles structure, coherence, conflicts, and assembly so you can focus on the content.

Five things it does for you:

1. **You never manually coordinate sections.** The root sees everything and catches when sections drift apart or contradict each other.
2. **Conflicts get a recommendation, not just a question.** When two directives clash, the root tells you what it thinks and why, then lets you decide in one click.
3. **Adding or restructuring sections is a conversation, not a chore.** Tell the root "we need a competitive analysis" and it creates the section, briefs the agent, and slots it into the tree.
4. **Export is one sentence.** Tell the root "export this as a Word doc" and it assembles everything in order, applies the template, and delivers the file.
5. **You always know who you're talking to.** The input box shows the composition name when you're at the root, the section name when you're in a section. No accidental global directives.

---

## Design

### Identity

The root orchestrator is **the composition's agent**. Every composition has exactly one. It is not a background process, not a daemon, not a supervisor. It is a session the user interacts with directly.

It uses the same mechanism as every other Compose agent: reads compose-context.json before every action, writes after every meaningful update. No elevated permissions. No special access. It simply has a wider scope: the full tree instead of one branch.

### Lifecycle

| Event | What happens |
|---|---|
| User creates a composition | Root session starts automatically. Always first. |
| User builds section tree | Root is available for questions throughout. |
| User hits Launch All | Section agents start in parallel. Root is already running. |
| User works on sections | Root is idle but accessible. Reads context on each activation. |
| User asks a cross-section question | Root activates, reads full context, responds. |
| Directive conflict detected | Conflict surfaces in root session and as a board indicator. |
| User requests export | Root assembles source files and runs export pipeline. |
| Composition marked complete | Root session remains available for future edits or re-export. |

The root session starts when the composition is created and persists for the life of the composition. It does not auto-close, time out, or require restarting.

### Scope: What belongs to the root vs. section agents

| Responsibility | Handled by |
|---|---|
| Writing section content | Section agent |
| Updating section status and facts | Section agent |
| Setting own `changing` flag | Section agent (clears it when done) |
| Adding, removing, reordering sections | Root orchestrator |
| Merging or splitting sections | Root orchestrator |
| Cross-section coherence checks | Root orchestrator |
| Directive conflict resolution | Root orchestrator |
| Final assembly and export | Root orchestrator |
| Setting `changing` flag on behalf of a section (before directing a change) | Root orchestrator (sets it; section agent clears it) |

### The three things only the root does

**1. Structure changes.**
Adding, removing, reordering, merging, or splitting sections. Section agents work within the tree. Only the root modifies the tree. When the user says "add a competitive analysis section," the root creates the section folder, adds it to compose-context.json, scaffolds the initial source files, and writes a brief for the new section agent.

**2. Cross-section coherence.**
When the user says "these sections don't fit together" or "the numbers in revenue and projections don't match," the root reads sibling outputs, diagnoses the gap, and either fixes it directly (if it's a factual correction in compose-context.json) or sends a targeted directive to the right section agent with specifics.

**3. Final assembly and export.**
The root owns export. It knows the composition structure, the section order, the template config, and the user's intent for the finished product. When the user says "export this," the root reads all section source files, assembles them in the correct order, applies the export config (template, styles, format), runs the export pipeline (pandoc, openpyxl, python-pptx, mermaid-cli as needed), and delivers the final file.

### What the root does NOT do

- **It does not supervise.** Section agents are autonomous. The root does not review their work unless the user asks or a conflict surfaces. No periodic check-ins, no approval gates.
- **It does not relay messages.** Section agents read compose-context.json directly. The root is not a message bus between agents.
- **It does not auto-run.** No background polling, no orchestration loop, no scheduled tasks. It activates when the user talks to it or when the conflict system routes something to it.
- **It does not clear another agent's `changing` flag.** Only the agent doing the work marks itself done.

---

## Conflict Resolution

### Where ambiguous conflicts land

The directive conflict system has three paths (defined in COMPOSE-SPEC.md). The first two auto-resolve. The third, ambiguous conflicts, needs a destination. That destination is the root orchestrator session.

### How the root handles an ambiguous conflict

1. The conflict is detected when a new directive arrives and scans existing directives.
2. The conflict is written to compose-context.json as a pending conflict record.
3. The root orchestrator session receives the conflict with both directives displayed.
4. The root **presents a recommendation with reasoning**. Not just "which one?" but "here's what I think and why."

Example:

```
Directive conflict detected:

  2:00pm -> Revenue: "Assume 10% growth rate"
  3:00pm -> Projections: "Use 25% growth rate"

Recommendation: These look like they should use the same number.
The 25% directive came later and was stated without qualifying it
to projections only. I'd apply 25% globally and supersede the 10%.

But the 10% in revenue could have been intentional for a
conservative base case. Your call:

  [Apply 25% everywhere]  [Keep both, different assumptions]
```

The user resolves in one click. The resolution is logged as a new directive with explicit scope. The root then:
- Updates compose-context.json (marks superseded directives, sets scope)
- If a section needs to change its work as a result, the root sets `changing: true` on that section with a change_note explaining what changed and why
- The section agent picks up the change on its next read, does the work, and clears `changing: false` when done

### The "Wait for Root" deadlock: solved

When the root directs a section agent to change its numbers (after conflict resolution or coherence check), there is a timing gap between the root's decision and the section agent starting the work. During that gap, sibling agents could read stale data.

**Solution: the root sets the flag, the section clears it.**

1. Root writes `changing: true` on the target section with a change_note ("Updating growth rate from 10% to 25% per user resolution of directive conflict d1/d2")
2. Root sends the directive to the section agent
3. Siblings see `changing: true` immediately on their next read, before the section agent has even started
4. Section agent does the work
5. Section agent writes `changing: false` when done

This is not a new mechanism. It is the existing changing-flag protocol used by whoever has earliest knowledge. Normally that is the section agent (it decides to change its own data). In the conflict resolution case, the root knows first, so the root signals first.

**One hard rule:** only the agent doing the work sets `changing: false`. The root never clears another agent's flag. This prevents marking something "done" before it actually is.

---

## UX

### Board presence

The root is not a card in a column. It is a **persistent header bar** at the top of the composition board. It shows:

- Composition name
- High-level status: X sections, Y complete, Z in progress
- Active conflicts indicator (count, if any, with a visual alert)
- Clicking the header opens the root session

### Input routing: who are you talking to?

The input box always shows who you're talking to. Always visible, not a tooltip.

| State | Input box label |
|---|---|
| No section selected | `Annual Report` (composition name = root) |
| Section card clicked/selected | `Revenue Analysis` (section name) |
| Conflict card clicked | `Annual Report ~ Conflict` (root, conflict context) |

**Default target is the root.** When you're in the compose view and no section is selected, you're talking to the root. This is the natural starting point: you open the composition, you're at the top level.

**Misrouted messages are handled gracefully.** If the user types something section-specific while talking to the root ("make the revenue numbers more aggressive"), the root understands scope from content. It routes the directive to the revenue section agent. This is the root's job: it manages the whole tree. No accidental global directive occurs because the root interprets intent, not just routing.

The label prevents confusion. The root's behavior catches edge cases even if the user doesn't notice the label.

### Sidebar grouping

Composition sessions (root + all section sessions) group under the composition name in the sidebar. The root session is always listed first.

```
v Annual Report              <- composition group
    Annual Report (root)     <- root session, always first
    CEO Letter               <- section session
    Revenue Analysis         <- section session
    Market Position          <- section session
```

---

## Launch All

The root orchestrator does not change how Launch All works. Launch All is a board-level button that starts section agent sessions in parallel. It does not touch the root because the root is already running.

Sequence:

1. User creates composition. Root session starts automatically.
2. User builds the section tree (manually or via planner).
3. User hits Launch All. All section agents start, each reading compose-context.json on first action.
4. Root is available throughout. Already warm. Ready for cross-section questions or conflicts.

Launch All starts the workers. The root was already at the office.

---

## System Prompt

The root orchestrator gets the standard Compose agent protocol (read before every action, write after every update) plus three additional responsibilities:

```
You are the Root Orchestrator for the Compose project "{project_name}".

You see the full composition, not one section. Users talk to you about the
whole project: structure, coherence, conflicts, and export.

Before EVERY action, read compose-context.json to understand:
- All section statuses and summaries
- All facts discovered across the composition
- All pending directive conflicts
- All active changing flags

YOUR THREE RESPONSIBILITIES:

1. STRUCTURE: You add, remove, reorder, merge, and split sections.
   Section agents never modify the tree. When creating a new section,
   scaffold its folder, add it to compose-context.json, and write an
   initial brief for the section agent.

2. COHERENCE: When sections contradict each other or drift apart,
   diagnose the gap. Fix factual errors in compose-context.json directly.
   For content changes, set changing:true on the target section with a
   change_note, then send a specific directive to the section agent.
   Never set changing:false on another agent's section.

3. ASSEMBLY & EXPORT: You own final export. Read all section source files,
   assemble in correct order per the composition hierarchy, apply export
   config (template, styles, format), run the export pipeline, deliver
   the final file.

CONFLICT RESOLUTION:
When an ambiguous directive conflict surfaces, present BOTH directives
to the user with a recommendation and reasoning. Never silently pick
one interpretation. Let the user resolve in one click or one sentence.
After resolution, update compose-context.json and signal affected
sections via the changing flag.

ROUTING:
If the user sends you a message that is clearly about a specific section
("make the revenue numbers more aggressive"), route the directive to
that section. You manage the whole tree. Interpret intent from content,
not just from which card was selected.

After EVERY meaningful update:
- Update composition-level status in compose-context.json
- Add any cross-section facts or decisions
- Log conflict resolutions as new directives with explicit scope
```

---

## Impact on Existing Specs

| Spec | What changes |
|---|---|
| COMPOSE-SPEC.md | Add reference to root orchestrator. Conflict resolution path #3 (ambiguous) now specifies destination: root session. |
| compose-context.json | Add optional `conflicts` array for pending unresolved conflicts. |
| compose-answer.md | Question about hierarchy root (line 187) is now answered: explicit session, not implicit coordinator. |
| compose-pm.md | Question #7 is resolved. Remove from open questions list or mark resolved. |
| compose_api.py | Add root session auto-creation on composition create. Add conflict routing endpoint. |
| ws_events.py | Root session uses compose system prompt with orchestrator additions. |
| live-panel.js | Conflict cards route to root session on click. |
| Board UI | Add persistent header bar for root. Add conflict count indicator. |
| Sidebar | Group composition sessions under composition name. Root always first. |
| Input box | Add persistent label showing current target (root or section name). |

---

## What this spec does NOT cover

- The exact visual design of the root header bar (CSS, layout, responsive behavior)
- The conflict resolution UI component details (button styles, animation, mobile)
- How the root orchestrator interacts with the Compose planner (planner creates the tree, root maintains it after)
- Whether the root can delegate coherence checks to a dedicated review agent in large compositions
- Performance characteristics when a composition has 20+ sections

These are implementation details or future extensions, not architectural decisions.
