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

**Unique value**: Catches spec gaps, ambiguity, and architectural risk before any code is written. No other team operates before implementation begins.

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

## Standing Plan Criteria

In addition to each agent's lane-specific analysis, the team always evaluates the plan against the following:

### Time Estimates
- Time estimates must reflect VibeNode's actual execution velocity, not traditional development timelines. A phase that would take a human team 6-8 hours typically completes in 30-60 minutes. Estimate in minutes, not hours.

### CLAUDE.md Constraint Awareness
- Verify the plan does not propose anything that violates CLAUDE.md rules — server restart restrictions, file organization, slash command handling, performance-critical patterns, or any other project constraint.
- If the plan touches code near a `PERF-CRITICAL` marker, it must acknowledge the optimization and describe how it will be preserved.
- Catching rule violations at plan time is mandatory. Do not defer them to build or review.

### Security & Public Repo Safety
- Flag any feature that could introduce secrets, API keys, tokens, credentials, PII, or hardcoded personal paths into tracked files.
- If the feature requires sensitive data, the plan must specify how it will be handled (environment variables, gitignored config, etc.) — do not leave this for the builder to figure out.

### Documentation Expectations
- The plan must specify what documentation is expected for the implementation: docstrings, module-level comments, inline explanations for complex logic, and any user-facing documentation.
- If the spec is silent on documentation, add a documentation section so the Build Team knows what's expected.

### Testability
- The plan must consider how the proposed work will be tested — unit tests, regression tests, and any manual verification needed.
- If a proposed design is inherently difficult to test, that is a planning problem. Redesign for testability before passing to Build.

### Error Handling Strategy
- The plan must define how errors and edge cases are handled, especially for user-facing features.
- Do not leave error handling unspecified for the builder to guess. Specify expected behavior for failure paths, invalid input, and degraded conditions.

### Backward Compatibility
- If the plan touches APIs, config formats, data structures, IPC contracts, or WebSocket events, it must explicitly address what happens to existing consumers.
- If a breaking change is required, the plan must include the migration path — not just note that one is needed.

### Lifecycle States
- For any user-facing feature or change, the plan must explicitly address what the user sees and what the system does at each of these moments. Silent assumptions here are the #1 source of startup bugs, race conditions, and "it works on my machine" failures.
- **First-ever launch**: no config, no cache, no favorites, no auth. What does the user see? What does the system fetch? Is there a welcome state?
- **Second launch (warm start)**: config exists, cache may be fresh or stale. Does the UI show cached data immediately? Does it revalidate in the background?
- **Cold start with stale cache**: cache exists but is older than TTL. Show stale with a refresh indicator? Block on fresh data?
- **Launch during daemon/backend startup race**: the backend is still initializing when the UI asks for data. What does the UI show during the wait? Does it retry? Does it respond to push events?
- **Offline launch**: no network. Does the app degrade gracefully? Show cached data? Show clear error state?
- **Launch after corrupted cache/config**: JSON parse fails. Does the app recover? Fall back to defaults? Surface the problem?
- **Cache miss mid-session**: user adds a new location and the cache hasn't fetched yet. What do they see in the sidebar/main panel during the fetch?
- If any of these states is unspecified, the plan is incomplete. Each must have a defined UX and data flow, even if the answer is "show loading spinner" or "fall back to defaults."

## Fix Policy

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
- what, if anything, needs my input,
- what you did not validate or could not fully verify.
