---
id: review-team
name: Review Team
department: compose
source: vibenode
version: 1.0.0
depends_on: [compose-test-engineer, compose-quality-engineer, compose-product-manager, compose-senior-engineer, compose-expert-user]
type: prompt-template
---

# Review Team

Reusable prompt template. Invoke by typing: **review team**

**Unique value**: Catches code quality, security, documentation, and project rule violations in completed code. No other team does deep code-level quality and compliance review.

## The Prompt

Run the review team: Test Engineer, Quality Engineer, Product Manager, Senior Software Engineer, Expert User. All have full knowledge of the product spec and VibeNode architecture.

Review what we've been working on. You should understand what this means from our conversation. If additional context is needed, check git diff and implementation-notes.md. Run the review as a team, not four independent reports. Each agent reviews from their lane, but findings feed into a shared picture. If one agent's finding affects another's area, they coordinate.

## Standing Review Criteria

Regardless of who calls this review or what context is provided, the team always evaluates the following:

### Correctness & Architecture
- Functional correctness and completeness, including edge cases and failure paths.
- Sound architecture, design, maintainability, and coding practices.
- Backward compatibility — if APIs, config formats, data structures, IPC contracts, or WebSocket events were modified, do all existing callers and consumers still work? If a breaking change is unavoidable, is the migration path implemented and documented?

### Documentation
- Every new or modified function/method has a clear docstring explaining purpose, parameters, return values, and any non-obvious behavior.
- Complex logic, workarounds, and non-trivial decisions have inline comments explaining WHY, not just what.
- Module-level comments describe the file's role in the system and its key responsibilities.
- Data flows, integration points, and cross-module dependencies are documented where they occur.
- Constants, config values, and thresholds are explained at their definition site.

### Error Handling
- Code does not silently swallow exceptions or fail without feedback.
- User-facing errors are clear and actionable.
- Internal errors are logged with enough context to diagnose the issue.

### Security & Public Repo Safety
- No secrets, API keys, tokens, passwords, or credentials in any tracked file.
- No hardcoded personal paths, usernames, emails, or PII.
- No user data, runtime artifacts, or local state committed to tracked directories.
- `.gitignore` coverage exists for any new file types that should not be published.

### Strategic Alignment
- If `architecture-intent.md` exists, verify the implementation does not contradict any documented architectural intent.
- A "Strategic Conflict" is distinct from a technical bug — the code may work correctly but violate the strategic reason a structure exists (e.g., flattening a matrix that is documented as load-bearing for a scoring engine).
- Strategic Conflicts are escalated to the user, not auto-resolved. Present the conflict clearly: what the code does vs. what the intent document says and why.
- If `architecture-intent.md` does not exist or is empty, skip this check.

### CLAUDE.md Compliance
- The code does not violate any rule in CLAUDE.md — server restart rules, file organization, slash command handling, performance-critical patterns, public repo safety, and any other project-specific constraints.
- Any code near a `PERF-CRITICAL` marker preserves the documented performance optimization.

### Cleanup
- No dead code, unused imports, debug prints, TODO placeholders, or temporary scaffolding left behind.
- The code contains only what is needed for production.

## Fix Policy

The team fixes what it finds. Don't report problems back to me unless:
- The fix would change the spec or user-facing behavior
- The fix has a meaningful tradeoff that needs a judgment call
- The issue is ambiguous enough that two reasonable people would disagree on the right answer

Everything else, just fix it. Update the code, update the tests, update implementation-notes.md. When done, give me one combined team report: what you found, what you fixed, what (if anything) needs my input. Keep it short.
