---
id: refactor-team
name: Refactor Team
department: compose
source: vibenode
version: 1.0.0
depends_on: []
type: prompt-template
---

# Refactor Team

Reusable prompt template. Invoke by typing: **refactor team**

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

## Output

Return one combined team report:
- Scope
- Approach
- Changes made
- Behavior verification
- Before and after assessment
- Documentation updated
- Risks
- Recommendations not implemented
