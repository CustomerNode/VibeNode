---
id: debug-team
name: Debug Team
department: compose
source: vibenode
version: 1.1.0
depends_on: []
type: prompt-template
---

# Debug Team

Reusable prompt template. Invoke by typing: **debug team**

**Unique value**: Catches unknown-cause failures through root cause analysis and causal investigation. No other team diagnoses problems backward from symptom to cause.

## Invocation Contract

The caller MUST include in the kickoff prompt:
- the symptom — what is observed, in user-facing language ("compose panel goes blank when I switch projects"),
- the exact reproduction steps if known (or a clear note that reproduction is unknown),
- any logs, error output, stack traces, or screenshots already captured,
- recent changes that might be related (git range, recent PRs, recent deploys),
- the suspected blast radius (which files, modules, or workflows are likely involved),
- raw test output if a test failure triggered the investigation (full trace, not a summary).

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting. Debug runs without reproduction steps waste time on speculation.

## The Prompt

Run the Debug Team: Triage Lead, Root Cause Analyst, Context Investigator, Fix Engineer, Regression Guard. All have full knowledge of the VibeNode codebase and architecture.

Diagnose and fix the issue under discussion. Debug Team owns unknown-cause issues and deeper causal investigation, even if the eventual code change is small.

Use the current conversation as context. Ground all conclusions in the actual codebase, logs, and git history — not assumptions.

Run as a coordinated team in sequence. Triage sets direction, Root Cause Analyst and Context Investigator work the diagnosis, Fix Engineer implements, Regression Guard verifies and protects.

## Standing Debug Criteria

Every Debug Team run follows this process:

### Reproduce first
- Do not fix what cannot be reproduced
- Document exact reproduction steps

### Find the real cause
- Fix why the bug happens, not just how it appears
- If the cause is not certain, narrow to concrete hypotheses and test them

### Check blast radius
- Identify all affected callers, consumers, and related state flows before changing shared code

### Prevent recurrence
- Add a regression test that would have caught the bug
- Fix other confirmed instances of the same bug pattern

### Do no additional harm
- No performance regression
- No CLAUDE.md violations
- No unrelated behavior changes

## Escalate when

- The fix requires changing a PERF-CRITICAL optimization
- The fix changes user-facing behavior or spec
- The issue is really a design flaw that needs architectural change
- Root cause remains ambiguous after real investigation
- Data integrity may be compromised

## Output Format

Return one combined team report in this numbered structure:

1. **Symptom** — What was observed, in user-facing language.
2. **Reproduction steps** — Exact steps that reliably reproduce the bug. If reproduction was not achieved, say so explicitly.
3. **Root cause** — The actual underlying mechanism. Not the trigger, the cause.
4. **Fix applied** — File-level changes and why each one is part of the fix.
5. **Blast radius** — All callers, consumers, and related state flows checked. Note any that were modified vs. only verified.
6. **Regression test** — The test that locks in the fix and would have caught the bug originally.
7. **Related issues found** — Other instances of the same pattern, fixed or flagged.
8. **What was not validated or could not be fully verified** — Hypotheses ruled out vs. hypotheses untested. Env limits.
9. **Obstacles encountered** — Setup issues, workarounds discovered, commands that needed special flags or configuration, dependencies or imports that caused problems, env quirks. Report anything the next stage would otherwise have to rediscover.
10. **Confidence** — HIGH, MEDIUM, or LOW with one-sentence rationale.
