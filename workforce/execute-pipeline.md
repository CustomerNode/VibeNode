---
id: execute-pipeline
name: Execute Pipeline
department: compose
source: vibenode
version: 1.1.0
depends_on: [plan-team, build-team, review-team, test-team, final-audit]
type: prompt-template
---

# Execute Pipeline

Reusable prompt template. Invoke by typing: **execute pipeline**

Full end-to-end orchestrator that chains Plan Team, Build Team, Review Team, Test Team, and Final Audit into a single uninterrupted execution. Use this when you want a request taken from idea to shipped result without stopping for intermediate prompts.

## The Prompt

I want you to execute this request from start to finish using the team workflow below. This task is not complete after planning. Continue through all required teams until the work is fully implemented, reviewed, tested, documented where needed, and verified.

OPERATING TEMPO:
VibeNode operates at compressed timescales. A typical feature goes from idea to shipped in 15-60 minutes. A bug fix ships in 5-15 minutes. A full Execute Pipeline run completes in one sitting. Tradeoff decisions must account for this velocity. A contained fix that ships now and gets refined next pass is usually better than a perfect fix that delays three stages. But speed is not an excuse for skipping regression protection, ignoring blast radius, or shipping known-broken behavior. The question is not "is this perfect" — it is "is this safe to ship now and improvable later."

STANDARDS — NON-NEGOTIABLE:
- Do no harm. Existing functionality, workflows, and external behavior must not regress unless explicitly intended.
- Any meaningful fix or change must include regression protection through tests or equivalent validation.
- Code must remain readable and maintainable. Update inline comments, docstrings, or related documentation where needed.
- Check blast radius before changing shared logic, contracts, state handling, or utilities.
- Preserve performance, especially in performance-sensitive areas and near PERF-CRITICAL markers.
- Validate real workflows, not just isolated functions.
- Surface important assumptions instead of making them silently. If ambiguity remains, resolve it intelligently where possible and document the decision.
- Keep changes scoped to the actual intent. Do not drift into unrelated work unless required for a correct result.
- If something material is deferred, call it out explicitly. If a risk is material, call it out clearly.
- Favor complete execution over partial progress. Do not stop at a draft, outline, or partial implementation unless blocked by something real.
- Preserve backward compatibility unless the request explicitly requires otherwise.
- Every team in the workflow must remove a distinct class of failure. If two stages repeatedly find the same issues without adding unique value, merge, narrow, or remove the weaker one.

TEAM UNIQUE VALUE — each team exists because it catches failures the others cannot:
- Plan Team: catches spec gaps, ambiguity, and architectural risk before any code is written.
- Build Team: catches implementation errors, step-by-step correctness, and spec-to-code drift during construction.
- Review Team: catches code quality, security, documentation, and project rule violations in completed code.
- Test Team: catches integration failures, workflow breakage, concurrency issues, and performance regressions through broad validation.
- Debug Team: catches unknown-cause failures through root cause analysis and causal investigation.
- Refactor Team: catches structural decay, duplication, and maintainability drag without changing behavior.
- Hotfix Team: catches production-blocking failures and applies minimum safe containment under time pressure.
- Final Audit: catches intent mismatch, whole-system coherence failures, blast radius damage, and unintended behavioral drift that pass all other stages.

TEAM WORKFLOW:

1. Plan Team
Convert the request into a hardened, executable implementation spec. Write the spec to docs/plans/.
Challenge weak assumptions.
Resolve ambiguity where possible.
Identify risks, dependencies, edge cases, failure modes, performance concerns, and blast radius.
Define exactly what should be built, how success will be judged, and what must not break.
When the spec is complete, hand off immediately to Build Team.
If Build Team, Review Team, or Test Team later discovers a flaw that invalidates part of the spec, loop back here to revise before continuing.

2. Build Team
Implement the approved spec end to end.
Work step by step.
Keep changes clean, scoped, and aligned to intent.
Do not drift into unrelated cleanup or refactoring unless required.
Preserve external behavior outside the requested scope.
Add or update tests as implementation progresses.
Document the code where needed so future developers can understand what changed and why.
When implementation is complete, hand off to Review Team.
If Review Team finds issues, fix them and re-submit for review. Do not proceed to Test Team until Review passes.

3. Review Team
Review the result for code quality, security, compliance, documentation, maintainability, and adherence to project rules.
Do not rubber-stamp.
If something is weak, fix it or send it back to Build Team for correction.
Make sure the implementation matches the spec and does not create hidden risk.
Confirm the code is understandable, the documentation is accurate, and the regression suite is appropriately strengthened for the change.
When review passes, hand off to Test Team.
If issues are fundamental enough to require spec changes, loop back to Plan Team.

4. Test Team
Run broad validation beyond implementation tests.
Validate integrated behavior, real workflows, edge cases, stale state, concurrency risks, error handling, browser and platform behavior, and performance-sensitive paths where relevant.
Do not stop at unit tests.
Fix low-risk obvious issues directly. Escalate anything deeper back to Build Team with specifics.
Strengthen regression coverage where gaps are found.
When validation passes, proceed to Final Audit.
If testing reveals a design-level problem, loop back to Plan Team or Build Team as appropriate.

5. Final Audit
This is not a re-review. Review and Test already did their jobs. This is a whole-system skeptical audit that asks whether the finished result actually does what was intended, works end to end, and did not create new problems.

Before auditing, scope the work:
- Identify the original request and its actual goal — not the literal words, but what the user wanted to achieve.
- Identify every file that was created or modified.
- Identify the blast radius: what other files, systems, or workflows could be affected.

Then audit against these areas:

Whole-system behavior:
- Does the result fully achieve the original objective, not just a literal interpretation?
- Does the full workflow make sense end to end from user action to final outcome?
- Are there gaps between what was requested and what was delivered?
- Would a user testing this in practice encounter any surprise, friction, or failure?

Unintended consequences and regressions:
- Did anything break outside the immediate change area? Check callers, imports, consumers, and downstream dependencies of every modified function, class, route, event, or contract.
- Are there behavior changes that were not part of the original intent?
- Are there stale state issues, race conditions, or error recovery gaps introduced by the changes?
- Is backward compatibility preserved where it should be?

Implementation quality:
- Is there a mismatch between the spec and the actual implementation?
- Are there weak assumptions, partial fixes, hidden risks, or fragile logic?
- Does the code overfit to tests while missing real-world behavior?
- Is the regression protection strong enough for the actual blast radius?

Project rules compliance:
- No violations of CLAUDE.md rules.
- Code near PERF-CRITICAL markers preserves the documented optimization.
- No secrets, hardcoded paths, PII, or user data in tracked files.
- No dead code, unused imports, debug prints, or temporary scaffolding.

Edge cases and operational risk:
- Missing edge case handling that could cause production failures.
- Error handling gaps — silent failures, unhelpful messages, missing logging.
- Performance or stability issues missed in earlier passes.
- User experience issues — confusing states, missing feedback, broken flows.

Fix problems that are clear, low-risk, and unambiguous. Do not expand scope — only fix issues directly related to the completed work. For anything that requires a judgment call, changes user-facing behavior, or has broader risk — do not fix it. Explain it clearly and recommend the next action.

Produce the final report with confidence level.

ROUTING RULES:
- New feature or major change → Plan Team → Build Team → Review Team → Test Team → Final Audit.
- Bug with unknown cause → Debug Team → Build Team → Review Team → Test Team → Final Audit.
- Production issue where speed matters → Hotfix Team → Review Team → targeted test validation → Final Audit.
- Internal cleanup with no intended behavior change → Refactor Team → Review Team → Test Team → Final Audit.

OUTPUT FORMAT:
Return results in this structure:

1. Hardened spec (file path in docs/plans/)
2. Implementation summary
3. Review findings and fixes
4. Test coverage and validation results
5. Final audit findings: intent match, blast radius, fixes applied, issues escalated
6. Risks, tradeoffs, and follow-up items
7. Final confidence level: HIGH, MEDIUM, or LOW
   - HIGH: Solution is sound, holds together end to end, no meaningful risk. Ship it. Do not assign HIGH unless there is no meaningful unresolved risk.
   - MEDIUM: Mostly works but has identifiable weak spots that should be addressed. Usable but not fully trusted.
   - LOW: Material problems found. Does not reliably achieve intent, has significant risk, or broke something. Do not ship.

Execute this request through the full team workflow and take it to completion.
