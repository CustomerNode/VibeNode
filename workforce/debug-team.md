---
id: debug-team
name: Debug Team
department: compose
source: vibenode
version: 1.0.0
depends_on: []
type: prompt-template
---

# Debug Team

Reusable prompt template. Invoke by typing: **debug team**

**Unique value**: Catches unknown-cause failures through root cause analysis and causal investigation. No other team diagnoses problems backward from symptom to cause.

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

## Output

Return one combined team report:
- Symptom
- Reproduction steps
- Root cause
- Fix applied
- Blast radius
- Regression test
- Related issues found
- What was not validated or could not be fully verified
- Confidence: HIGH, MEDIUM, or LOW
