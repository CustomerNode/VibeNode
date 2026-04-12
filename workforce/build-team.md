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

## The Prompt

Fully implement the spec.

Break the work into logical sequential steps. Complete one step at a time.

After each step, run the Review Team and evaluate:
- whether the step fully achieves its intended part of the spec,
- whether it introduces unintended consequences,
- functional correctness and completeness, including edge cases and failure paths,
- whether it follows sound architecture, design, maintainability, and coding practices,
- whether it remains consistent with the full spec and prior completed steps,
- whether there is sufficient test coverage or validation to prove the step works as intended.

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

For the full-solution review, also run the "stale state adversarial pass": for every place where the implementation skips, caches, defers, or short-circuits work, trace the fast-switching scenario (user acts on object A, immediately switches to object B, immediately switches back to A). If any code path returns stale data, loses entries, or silently drops work in that scenario, it is a blocking bug.

Fix all issues found.

If the full-solution review finds issues, run the Review Team a second time after fixes are applied. Fix any remaining issues from that second review.

At the end, provide a final report with:
- the steps completed,
- the issues found and corrected,
- any remaining assumptions, tradeoffs, or risks,
- the top 3 suggested enhancements.

Enhancement suggestions must be plain English only. Do not implement them. Keep them aligned with the spec's intent or use them to highlight important unknown unknowns.

Do not skip review cycles, do not collapse major steps into one, and do not stop early.

Run this workflow through completion without asking for further prompts unless blocked by missing required information, missing access, or a material ambiguity that makes correct implementation impossible.
