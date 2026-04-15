---
id: test-team
name: Test Team
department: compose
source: vibenode
version: 1.0.0
depends_on: []
type: prompt-template
---

# Test Team

Reusable prompt template. Invoke by typing: **test team**

**Unique value**: Catches integration failures, workflow breakage, concurrency issues, and performance regressions through broad validation. No other team tests the system as a user would experience it.

## The Prompt

Run the Test Team: Integration Test Engineer, E2E Workflow Tester, Stress and Edge Case Tester, Performance Validator, Browser and Platform Tester. All have full knowledge of the VibeNode codebase and architecture.

Validate completed work through broader testing than the Build Team normally performs. Build covers implementation-level testing and regression updates. Test Team validates integrated behavior, workflow integrity, edge cases, performance-sensitive paths, and browser/platform behavior.

Use the current conversation as context. Ground all conclusions in the actual codebase, not assumptions.

Run as a coordinated team. Each role tests from their lane, but findings feed into a shared picture.

## Standing Test Criteria

Every Test Team run checks:

### Integration integrity
- API success and error behavior is correct
- WebSocket events fire with correct payloads
- IPC between daemon and web server works on changed paths
- Shared state remains consistent across daemon, server, UI, sidebar, and open panels

### User workflow preservation
- Core workflows still work end to end
- No existing workflow is broken, slowed, or made more confusing

### Stale state and concurrency
- Fast switch across objects preserves correct state
- Refresh mid-operation recovers safely
- Concurrent operations do not cause stale UI, lost updates, or race failures

### Performance baseline
- Page load, session switch, message submit, and kanban interactions remain acceptable
- No new memory leaks, runaway timers, or accumulating listeners

### Error resilience
- Daemon failure, session crash, and WebSocket disconnect produce clear recoverable behavior
- No silent failure, blank screen, or frozen UI

## Fix Policy

Test Team may fix only small, obvious issues when:
- The root cause is clear
- The fix is low risk
- The blast radius is small
- The fix does not change spec or user-facing behavior

Otherwise escalate to Debug Team or Build Team.

## Escalate when

- The fix would change user-facing behavior or the spec
- The issue reveals a design flaw or architectural weakness
- The root cause is unclear
- The fix touches PERF-CRITICAL paths in a risky way
- The issue requires broader implementation work rather than validation

## Output

Return one combined team report:
- Tests executed
- Issues found
- Issues fixed
- Issues escalated
- Regression tests added
- What was not validated or could not be fully verified
- Confidence: HIGH, MEDIUM, or LOW with explanation
