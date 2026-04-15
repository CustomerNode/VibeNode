---
id: build-team
name: Build Team
department: compose
source: vibenode
version: 1.0.0
depends_on: [review-team]
type: prompt-template
---

# Build Team

Reusable prompt template. Invoke by typing: **build team**

**Unique value**: Catches implementation errors, step-by-step correctness, and spec-to-code drift during construction. No other team builds with integrated review gates.

## The Prompt

Fully implement the spec.

Before starting, read `architecture-intent.md` (the strategic decisions ledger). This file documents **why** key structures exist — not just what they are. If your proposed implementation would contradict a documented architectural intent (e.g., flattening a structure that is documented as load-bearing, replacing a pattern that exists for strategic reasons), flag it as a **Strategic Conflict** and escalate to the user. Do not auto-resolve strategic conflicts. Do not silently deviate from documented intent because it makes implementation easier. If `architecture-intent.md` does not exist or is empty, proceed normally.

Break the work into logical sequential steps. Complete one step at a time.

After each step, run the Review Team and evaluate:
- whether the step fully achieves its intended part of the spec,
- whether it introduces unintended consequences,
- functional correctness and completeness, including edge cases and failure paths,
- whether it follows sound architecture, design, maintainability, and coding practices,
- whether it remains consistent with the full spec and prior completed steps,
- whether there is sufficient test coverage or validation to prove the step works as intended.
- whether any changes touch code near a `PERF-CRITICAL` marker, and if so, whether the performance optimization is preserved (see CLAUDE.md performance section).
- whether the code is thoroughly documented so that both humans and AI can understand it at a glance:
  - functions and methods have clear docstrings explaining purpose, parameters, return values, and any non-obvious behavior,
  - complex logic, workarounds, and non-trivial decisions have inline comments explaining WHY, not just what,
  - module-level comments describe the file's role in the system and its key responsibilities,
  - any new constants, config values, or magic numbers are explained where they are defined.
- whether errors are handled gracefully:
  - new code does not silently swallow exceptions or fail without feedback,
  - user-facing errors are clear and actionable,
  - internal errors are logged with enough context to diagnose the issue.
- whether the step introduces any security or public-repo risks:
  - no secrets, API keys, tokens, passwords, or credentials in any tracked file,
  - no hardcoded personal paths, usernames, emails, or PII,
  - no user data, runtime artifacts, or local state committed to tracked directories,
  - `.gitignore` coverage exists for any new file types that should not be published.

Additionally, for any optimization that skips, caches, or defers work that the old code always performed, the Review Team must answer these questions before approving:
- What guarantee did the old code provide that the new code no longer provides?
- What condition gates the skip, and can that condition be wrong, stale, or racy?
- If the condition IS wrong, what is the user-visible consequence? Is it silent data loss, stale UI, or a visible error?
- Does the skip decision depend on client-side state to make a server-side correctness decision? If yes, it is almost certainly a bug — client state is always eventually stale.
- What is the worst-case timing window? Walk through the exact sequence: user does X, then immediately does Y — does the optimization still hold?

Fix all issues found before continuing.

If the first review finds issues, run the Review Team a second time after fixes are applied. Fix any remaining issues from that second review, then proceed to the next step.

Continue until all steps are complete.

Then run the Review Team on the full implementation and evaluate:
- whether the entire spec has been achieved,
- whether there are any system-level unintended consequences,
- functional correctness and completeness across the full system,
- whether the overall solution follows strong architecture, design, maintainability, and coding practices,
- whether the final implementation is coherent, complete, and production-ready,
- whether there is sufficient test coverage or validation to prove the full solution works as intended.
- whether any changes touch code near a `PERF-CRITICAL` marker, and if so, whether the performance optimization is preserved (see CLAUDE.md performance section).
- whether the full implementation is well-documented end-to-end:
  - every new or modified function/method has a clear docstring,
  - complex logic and non-obvious decisions have inline comments explaining the reasoning,
  - module-level comments orient a reader (human or AI) to the file's purpose and key responsibilities,
  - data flows, integration points, and cross-module dependencies are documented where they occur,
  - any constants, config values, or thresholds are explained at their definition site.

Additionally, evaluate backward compatibility:
- If the change modifies APIs, config formats, data structures, IPC contracts, or WebSocket events, verify that all existing callers and consumers still work correctly.
- If a breaking change is unavoidable, document it clearly and ensure any migration path is implemented, not just noted.

For the full-solution review, also run the "stale state adversarial pass": for every place where the implementation skips, caches, defers, or short-circuits work, trace the fast-switching scenario (user acts on object A, immediately switches to object B, immediately switches back to A). If any code path returns stale data, loses entries, or silently drops work in that scenario, it is a blocking bug.

Fix all issues found.

If the full-solution review finds issues, run the Review Team a second time after fixes are applied. Fix any remaining issues from that second review.

After the final review is complete, run a CLAUDE.md compliance pass:
- Verify the implementation does not violate any rule in CLAUDE.md — server restart rules, file organization, slash command handling, performance-critical patterns, public repo safety, and any other project-specific constraints.
- This is a mandatory gate. Any violation is a blocking issue that must be fixed before proceeding.

Then run a cleanup pass:
- Remove any dead code, unused imports, debug prints, TODO placeholders, or temporary scaffolding introduced during the build.
- Do not ship construction materials — the final code should contain only what is needed for production.

Then update and enhance the regression tests:
- Add new test cases that cover every significant behavior introduced or changed by this implementation.
- Update any existing tests that are now outdated, incomplete, or broken due to the changes.
- Ensure edge cases, failure paths, and boundary conditions discovered during the build and review cycles are captured as test cases.
- Verify that all tests pass before proceeding.
- If the project has an existing regression test file or suite, add to it rather than creating a separate file. Follow the existing test patterns and conventions.

At the end, provide a final report with:
- the steps completed,
- the issues found and corrected,
- any remaining assumptions, tradeoffs, or risks,
- the top 3 suggested enhancements.

Enhancement suggestions must be plain English only. Do not implement them. Keep them aligned with the spec's intent or use them to highlight important unknown unknowns.

Do not skip review cycles, do not collapse major steps into one, and do not stop early.

Run this workflow through completion without asking for further prompts unless blocked by missing required information, missing access, or a material ambiguity that makes correct implementation impossible.
