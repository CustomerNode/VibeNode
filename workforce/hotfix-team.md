---
id: hotfix-team
name: Hotfix Team
department: compose
source: vibenode
version: 1.0.0
depends_on: []
type: prompt-template
---

# Hotfix Team

Reusable prompt template. Invoke by typing: **hotfix team**

## The Prompt

Run the Hotfix Team: Rapid Triage, Surgical Fix Engineer, Quick Verification. All have full knowledge of the VibeNode codebase and architecture.

Restore production stability fast with the smallest safe change. Prefer containment over elegance.

Use the current conversation as context. Ground all conclusions in the actual codebase, not assumptions.

Run as a coordinated team. Triage classifies and scopes, Surgical Fix Engineer implements the minimum safe fix, Quick Verification confirms it works and nothing else broke.

## Severity Levels

**P0 — Total blocker.** App does not start, sessions crash, severe data integrity risk, or core system unusable.

**P1 — Major workflow broken.** Core capability fails for real users.

**P2 — Degraded but usable.** Wrong behavior, partial failure, or serious UX defect without total blockage.

## Standing Hotfix Criteria

Every Hotfix Team run follows these rules:

### Minimal scope
- Fix the specific production issue only
- No cleanup, refactor, or opportunistic improvements

### Root cause or safe containment
- Prefer root cause fix when possible
- If full resolution is too risky or too large, apply safe containment and document the deeper issue explicitly

### Mandatory safety checks
- Add a regression test for the bug
- Run existing relevant tests
- Preserve CLAUDE.md compliance
- Preserve PERF-CRITICAL behavior
- Check changed files for security and public repo safety

### Compatibility
- Do not break APIs, config formats, IPC contracts, or WebSocket consumers
- Maintain backward compatibility unless explicit emergency migration is required

### Follow-up tracking
- Every containment fix must include required follow-up work
- Deferred cleanup, broader testing, deeper redesign, and documentation work must be listed explicitly
- Follow-up ticket or task creation is mandatory if the fix is containment rather than full resolution

## Escalate when

- The fix requires changing a PERF-CRITICAL optimization
- The issue comes from a design flaw that cannot be safely patched
- The minimum safe fix would change behavior beyond restoring expected operation
- Data corruption may have occurred and recovery steps may be required

## Output

Return one combined team report:
- Severity: P0, P1, or P2
- Symptom
- Root cause
- Fix applied
- Regression test
- Verification
- Blast radius
- Follow-up needed
