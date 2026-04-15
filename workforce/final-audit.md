---
id: final-audit
name: Final Audit
department: compose
source: vibenode
version: 1.0.0
depends_on: []
type: prompt-template
---

# Final Audit

Reusable prompt template. Invoke by typing: **final audit**

**Unique value**: Catches intent mismatch, whole-system coherence failures, blast radius damage, and unintended behavioral drift that pass all other stages. No other team audits the finished result as a whole against the original goal.

Last-pass skeptical audit of completed work. Use this after implementation, review, and testing are done. This is not a re-review — it is a whole-system sanity check that asks whether the finished result actually does what was intended, works end to end, and did not create new problems.

## The Prompt

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
- Fix problems that are clear, low-risk, and unambiguous (typos, missing imports, obvious logic errors, dead code). Do not expand scope — only fix issues directly related to the completed work.
- For anything that requires a judgment call, changes user-facing behavior, or has broader risk — do not fix it. Explain it clearly and recommend the next action.
- Only conclude success if the solution is genuinely sound as a whole.

OUTPUT FORMAT:

1. **Intent match** — Does the result achieve what the user actually wanted? Any gaps?
2. **End-to-end behavior** — Does the full workflow hold together in practice?
3. **Regression and blast radius** — Anything broken or changed outside the intended scope?
4. **Gaps, weaknesses, or risks** — Anything weak, incomplete, fragile, or misleading?
5. **Fixes applied in this pass** — What was fixed directly, with file paths and brief explanation.
6. **Issues escalated** — What needs the user's attention or a judgment call, and why.
7. **Confidence level** — HIGH, MEDIUM, or LOW:
   - **HIGH**: The solution is sound. It does what was intended, holds together end to end, and introduced no meaningful risk. Ship it. Do not assign HIGH unless there is no meaningful unresolved risk.
   - **MEDIUM**: The solution mostly works but has identifiable weak spots or gaps that should be addressed. Usable but not fully trusted yet.
   - **LOW**: Material problems found. The solution does not reliably achieve the intent, has significant risk, or broke something. Do not ship without further work.
