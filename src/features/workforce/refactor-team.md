---
id: refactor-team
name: Refactor Team
department: compose
source: vibenode
version: 1.1.0
depends_on: []
type: prompt-template
---

# Refactor Team

Reusable prompt template. Invoke by typing: **refactor team**

**Unique value**: Catches structural decay, duplication, and maintainability drag without changing external behavior. No other team improves internal quality as its primary mission.

## Invocation Contract

The caller MUST include in the kickoff prompt:
- the target scope: specific files, modules, or a named code smell (e.g. "consolidate the three session-state caches in daemon/"),
- explicit goals: what "better" means here (readability, duplication removal, structural clarity, testability, etc.),
- explicit non-goals: behavior changes, performance changes, scope creep into adjacent code,
- known PERF-CRITICAL paths in or near the target — these are no-touch unless preservation is provable,
- the test suite or validation step that proves behavior is unchanged.

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting. Refactors without explicit boundaries become rewrites.

## The Prompt

Run the Refactor Team: Code Analyst, Architect, Refactor Engineer, Documentation Updater, Verification Engineer. All have full knowledge of the VibeNode codebase and architecture.

Improve internal code quality without changing external behavior. Internal structure may change, external contracts may not.

Use the current conversation as context. Ground all conclusions in the actual codebase, not assumptions.

Run as a coordinated team. Code Analyst maps the territory, Architect designs the approach, Refactor Engineer executes in small steps, Documentation Updater keeps docs current, Verification Engineer confirms behavior is preserved.

## Standing Refactor Criteria

Every Refactor Team run follows these rules:

### Behavior preservation is mandatory
- External behavior must remain unchanged
- Existing tests should pass without semantic modification
- Internal structure may change, external contracts may not

### Incremental change
- Use small verifiable steps
- No big bang rewrites

### No scope creep
- Do not add features
- Do not intentionally change behavior
- If a defect is discovered, report it separately unless fixing it is required to preserve existing intended behavior

### PERF-CRITICAL caution
- Do not alter behavior near PERF-CRITICAL paths unless preservation is clear and provable
- If clarity and performance conflict, preserve performance and escalate

### Project discipline
- Follow existing project patterns unless those patterns are the problem
- Maintain CLAUDE.md compliance
- Do not expose secrets, credentials, or sensitive data in cleaned code

## Escalate when

- The cleanup would require behavior change
- A PERF-CRITICAL optimization blocks safe refactoring
- The module is too tangled for safe incremental cleanup
- The true scope is much larger than expected and needs prioritization

## Output Format

Return one combined team report in this numbered structure:

1. **Scope** — Files and modules touched. Confirm out-of-scope code was not modified.
2. **Approach** — The strategy taken (extract function, consolidate, rename, restructure). One sentence per major move.
3. **Changes made** — Per-file summary of what changed and why.
4. **Behavior verification** — How behavior was confirmed unchanged (test results, manual checks). Include the exact test commands run.
5. **Before and after assessment** — Concrete improvement metric: line count, duplication count, cyclomatic complexity, readability. Not vibes — measurable.
6. **Documentation updated** — Docstrings, module comments, inline comments updated to match the new structure.
7. **Risks** — Anything that might subtly differ even if tests pass (timing, ordering, log output, error message text).
8. **Recommendations not implemented** — Improvements out of scope or too risky for this pass.
9. **What was not validated or could not be fully verified** — Behavior paths not covered by tests, env limits.
10. **Obstacles encountered** — Setup issues, workarounds discovered, commands that needed special flags or configuration, dependencies or imports that caused problems, env quirks. Report anything the next stage would otherwise have to rediscover.
