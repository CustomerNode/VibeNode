---
id: plan-team
name: Plan Team
department: compose
source: vibenode
version: 1.0.0
depends_on: []
type: prompt-template
---

# Plan Team

Reusable prompt template. Invoke by typing: **plan team**

## The Prompt

Run the Plan Team: Spec Analyst, Product Strategist, Architect, Implementation Auditor, Integration Reviewer, Expert User. All have full knowledge of the VibeNode codebase and architecture.

Review the plan/spec we've been working on. Use the current conversation as context, but ground all conclusions in the spec, implementation-notes.md, and the relevant codebase. If needed, check those sources to resolve uncertainty before making changes.

Run as a coordinated team in sequence:
- Spec Analyst identifies contradictions, ambiguities, missing definitions, and gaps in the spec.
- Product Strategist identifies missing functionality, scope gaps, and places where the plan fails the intended user or business outcome.
- Architect defines the technical approach and flags proposals that are structurally unsound, over-complex, or inconsistent with the system design.
- Implementation Auditor checks the plan against the actual codebase, architecture, constraints, and implementation reality.
- Integration Reviewer identifies blast radius, dependency risks, migration concerns, and effects on existing features or flows.
- Expert User evaluates the plan from the perspective of a daily VibeNode power user — flags anything that would break existing workflows, feel inconsistent with the rest of the app, confuse users, or be undiscoverable.

Each agent's findings must feed into the next. This is one shared analysis, not six separate reports.

The team should fix what it finds directly in the spec. Preserve the original intent of the spec unless one of the escalation conditions below is triggered.

Fix contradictions, ambiguities, vague areas, missing sections, sequencing problems, codebase mismatches, and integration gaps. Add necessary detail where needed. Do not invent new scope unless it is required to make the spec coherent, complete, and implementable.

Do not keep proposals that conflict with the real codebase, architecture, or known integration constraints unless they are explicitly surfaced for my input.

Do not report issues back to me unless:
- the fix would significantly change scope,
- there is a real tradeoff with meaningful costs either way,
- two reasonable interpretations exist and the right one is not obvious,
- the fix would require changing existing shipped behavior.

Everything else, just fix directly in the spec.

When complete, give me one short combined team report covering:
- what you found,
- what you fixed,
- what, if anything, needs my input.
