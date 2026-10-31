---
id: review-team
name: Review Team
department: compose
source: vibenode
version: 1.6.0
depends_on: [compose-test-engineer, compose-quality-engineer, compose-product-manager, compose-senior-engineer, compose-expert-user]
type: prompt-template
---

# Review Team

Reusable prompt template. Invoke by typing: **review team**

**Unique value**: Catches code quality, security, documentation, and project rule violations in completed code. No other team does deep code-level quality and compliance review.

## Invocation Contract

The caller MUST include in the kickoff prompt:
- the specific scope to review: file list, git range (e.g. `git diff HEAD~3..HEAD`), or PR description,
- the spec, intent, or user request the code was supposed to satisfy,
- any areas explicitly out of scope (e.g. "do not review the migration in app/legacy/"),
- whether to auto-fix issues per the Fix Policy or only report them (under Code Pipeline, follow `code-pipeline.md`),
- any PERF-CRITICAL paths or strategic-intent constraints in the touched code.

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting. A review without scope drifts and produces low-signal noise.

## The Prompt

Run the review team: Test Engineer, Quality Engineer, Product Manager, Senior Software Engineer, Expert User. All have full knowledge of the product spec and VibeNode architecture.

Review the scope named in the kickoff. You are a checker, not the builder: run as a fresh subagent whose kickoff gives you the request, the scope (files or git range) and the run directory, not the builder's reasoning. Judge from the code and the diff (`git diff` on the scope), not from the builder's account of what it meant. Write your own findings down BEFORE you read any builder or earlier stage report; then read those reports to follow up on what they raise. If you are running inside the same conversation that wrote the code, say so in the report under "What was not validated". Run the review as a team, not four independent reports. Each agent reviews from their lane, but findings feed into a shared picture. If one agent's finding affects another's area, they coordinate.

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

**Under Code Pipeline (and Execute Pipeline, its alias):** follow `code-pipeline.md`. Review's fixes land before Test runs, and Test and Audit, both fresh, check the result on the final tree; the harness grades only evidence produced on that tree. Record every suspected correctness defect on its own line as `FINDING: <short-id> <what is wrong>`, however minor it looks: you do not decide whether it is tracked, the evidence decides how it is settled. Only what is not about correctness is a NOTE, and it names its kind after the id: `NOTE: <short-id> cosmetic: <text>` (or `enhancement:` / `preference:`); a NOTE without that word is tracked as a finding. Give every issue its own id; never label an observation "non-blocking" in prose. A later checker settles each finding with a check that covers it (`FIXED` / `NOT-A-DEFECT` with `by=<criterion>`) or discloses it as `UNRESOLVED`; an argument that it cannot happen is not a settlement. See `code-pipeline.md`.

**Invoked on its own, with the caller asking for auto-fix:** the team fixes what it finds. Don't report problems back to me unless:
- The fix would change the spec or user-facing behavior
- The fix has a meaningful tradeoff that needs a judgment call
- The issue is ambiguous enough that two reasonable people would disagree on the right answer

Everything else, just fix it. Update the code and the tests, and list each fix in the report.

## Output Format

Return one combined team report in this numbered structure:

1. **Scope reviewed** — Files or git range. Confirm out-of-scope items were skipped.
2. **Findings by category** — Correctness & Architecture / Documentation / Error Handling / Security / Strategic Alignment / CLAUDE.md Compliance / Cleanup. Group findings by kind: correctness concerns (each a `FINDING:` line under Code Pipeline) and non-correctness notes (each a `NOTE:` line). Severity says how bad a concern is; it never decides whether it is tracked.
3. **Fixes applied** — What was fixed directly, with file paths.
4. **Strategic conflicts** — Anything that contradicts `architecture-intent.md` and was escalated rather than auto-resolved.
5. **What needs the user's input** — Items meeting escalation criteria: spec/behavior changes, real tradeoffs, ambiguous interpretations.
6. **What was not validated or could not be fully verified** — Areas skipped, depth limits, env constraints.
7. **Obstacles encountered** — Setup issues, workarounds, commands needing special flags, dependency or env quirks discovered during review.

Keep prose short. The artifact is the structure.
