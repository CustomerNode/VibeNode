---
id: final-audit
name: Final Audit
department: compose
source: vibenode
version: 1.5.0
depends_on: []
type: prompt-template
---

# Final Audit

Reusable prompt template. Invoke by typing: **final audit**

**Unique value**: Catches intent mismatch, whole-system coherence failures, blast radius damage, and unintended behavioral drift that pass all other stages. No other team audits the finished result as a whole against the original goal.

Last-pass skeptical audit of completed work. Use this after implementation, review, and testing are done. This is not a re-review — it is a whole-system sanity check that asks whether the finished result actually does what was intended, works end to end, and did not create new problems.

## Invocation Contract

The caller MUST include in the kickoff prompt:
- the **original user request verbatim** — not paraphrased. Final Audit's whole purpose is comparing reality to original intent.
- the list of files created or modified (or a git range like `HEAD~N..HEAD`),
- pointers to prior pipeline artifacts if this is part of Code Pipeline or Execute Pipeline, its alias (e.g. `docs/plans/runs/<run-id>/`; under Code Pipeline the audit reads prior teams' confidence and escalations from those artifacts, not from the brief),
- the spec the work was supposed to satisfy (if one exists).

The brief carries nothing from the builder's or the other checkers' reasoning, and no prior team's confidence level: the audit forms its own judgment first, then reads the prior stages from the artifacts to follow up on what they raised and what they deferred.

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting. An audit without the original request cannot judge intent match.

## The Prompt

You are a checker, not the builder. Run as a fresh subagent whose kickoff gives you the original request, the changed files and the prior artifacts, not the builder's reasoning. Judge from the code, the diff and what you run yourself. Audit against the original request and its acceptance criteria first and write your findings down BEFORE you read the earlier stage reports; then read them in full to follow up on what they raised. If you are running inside the same conversation that wrote the code, say so in the report; confidence cannot be HIGH.

Do one final skeptical audit pass on the completed work. Do not treat the implementation as correct just because it passed review and testing. Audit it as a whole system and verify that it truly achieves the original intent without creating new problems.

BEFORE YOU START:
- Identify the original request and its actual goal (not just the literal words — what the user actually wanted to achieve)
- Identify every file that was created or modified in this work
- Identify the blast radius: what other files, systems, or workflows could be affected by these changes

WHOLE-SYSTEM BEHAVIOR CHECK:
- Does the result fully achieve the original objective, not just a literal interpretation of it?
- Does the full workflow make sense end to end when you trace it from user action to final outcome?
- Are there gaps between what was requested and what was actually delivered?
- Would a user testing this in practice encounter any surprise, friction, or failure?

UNINTENDED CONSEQUENCES AND REGRESSIONS:
- Did anything break outside the immediate change area? Check callers, imports, consumers, and downstream dependencies of every modified function, class, route, event, or contract.
- Are there behavior changes that were not part of the original intent?
- Are there stale state issues, race conditions, or error recovery gaps introduced by the changes?
- Is backward compatibility preserved where it should be?

IMPLEMENTATION QUALITY:
- Is there a mismatch between the spec and the actual implementation?
- Are there weak assumptions, partial fixes, hidden risks, or fragile logic?
- Does the code overfitting to tests while missing real-world behavior?
- Are there places where the solution technically works but is still wrong?
- Is the regression protection strong enough for the actual blast radius?
- Is the code readable, maintainable, and consistent with the project's existing patterns?

PROJECT RULES COMPLIANCE:
- No violations of CLAUDE.md rules — server restart rules, file organization, slash command handling, public repo safety, and all other project constraints.
- Any code near a PERF-CRITICAL marker preserves the documented performance optimization.
- No secrets, hardcoded paths, PII, or user data in tracked files.
- No dead code, unused imports, debug prints, TODO placeholders, or temporary scaffolding left behind.

EDGE CASES AND OPERATIONAL RISK:
- Missing edge case handling that could cause failures in production
- Error handling gaps — silent failures, unhelpful error messages, missing logging
- Performance or stability issues that were missed in earlier passes
- User experience issues — confusing states, missing feedback, broken flows

INSTRUCTIONS:
- Be skeptical. Do not rubber-stamp.
- Trace the result from the user's actual goal through implementation to real behavior.
- Review both the changed area and the blast radius around it.
- Under Code Pipeline (and Execute Pipeline, its alias): follow `code-pipeline.md`. Record each suspected correctness defect as `FINDING: <short-id> <what is wrong>`; it stops HIGH until a later checker settles it with a covering check (`FIXED` / `NOT-A-DEFECT` with `by=<criterion>`) or it is disclosed `UNRESOLVED`. In the coverage section, name every SCENARIO and FINDING id and judge whether the check cited for it really exercises it; record `UNRESOLVED: <id> <why>` for a citation you cannot stand behind, and challenge consequential scenarios the plan never named. Any change you make moves the tree, so every required criterion must run again through `pipeline.run_check` before grading; the harness flags evidence from the earlier tree as stale. The grade is the harness's, not yours.
- Invoked on its own: you may fix problems that are clear, low-risk, and unambiguous (typos, missing imports, obvious logic errors, dead code), directly related to the completed work. Any code you change voids earlier test runs: run the project's full test command again, and your own fixes are not independently checked, so list them; with code fixes applied, confidence cannot be HIGH.
- For anything that requires a judgment call, changes user-facing behavior, or has broader risk — do not fix it. Explain it clearly and recommend the next action.
- Only conclude success if the solution is genuinely sound as a whole.

## Output Format

Return the audit report in this numbered structure:

1. **Intent match** — Does the result achieve what the user actually wanted? Any gaps?
2. **End-to-end behavior** — Does the full workflow hold together in practice?
3. **Regression and blast radius** — Anything broken or changed outside the intended scope?
4. **Gaps, weaknesses, or risks** — Anything weak, incomplete, fragile, or misleading?
5. **Fixes applied in this pass** — What was fixed directly, with file paths and brief explanation.
6. **Issues escalated** — What needs the user's attention or a judgment call, and why.
7. **What was not validated or could not be fully verified** — Blind spots, skipped areas, and anything this audit could not confirm.
8. **Obstacles encountered** — Setup issues, workarounds discovered, commands that needed special flags or configuration, dependencies or imports that caused problems, env quirks. Report anything that should be carried forward.
9. **Confidence level** — HIGH, MEDIUM, or LOW:
   - **HIGH**: The solution is sound. It does what was intended, holds together end to end, and introduced no meaningful risk. Ship it. Do not assign HIGH unless there is no meaningful unresolved risk AND all of these are on record (name where each is recorded):
     1. The project's own full test command (the one its publish, CI or release step runs; for VibeNode `python -m pytest --tb=short -q --no-header --timeout=120 -n 8 --dist loadfile tests/`) ran with zero failures AFTER the last code change; its final summary line is quoted verbatim. A run made before a later fix does not count.
     2. If anything the user sees or touches changed, the changed behavior was driven through the project's real interface (for VibeNode: headless Chromium against the real page via `tests/_fake_app.py` or a dedicated browser test; these need no server or restart, so "cannot restart" does not excuse skipping them).
     3. Every acceptance criterion has evidence: a named test or a recorded check with its result. Passing tests that do not cover the criteria do not count.
     4. This audit, and the Review and Test stages, ran as fresh subagents, not inside the conversation that built the code, and each judged the final code.
     5. No material finding from any stage is left unresolved.
     If any is missing, the rating is MEDIUM at most, and the report says which and why.
   - **MEDIUM**: The solution mostly works but has identifiable weak spots or gaps that should be addressed. Usable but not fully trusted yet.
   - **LOW**: Material problems found. The solution does not reliably achieve the intent, has significant risk, or broke something. Do not ship without further work.
