# Agent Pipeline

## Flow

```
User Request
    |
    v
[1] Product Manager  -->  SPEC
    |
    v
[2] Architect  -->  BUILD PLAN
    |
    v
[3] Coder  -->  CODE CHANGES + SUMMARY
    |
    v
[4] Code Reviewer  -->  PASS / REJECT
    |                      |
    |  (if REJECT) --------+---> back to [3] Coder with issues
    v
[5] Tester  -->  PASS / FAIL
    |                |
    |  (if FAIL) ----+---> back to [3] Coder with test failures
    v
DONE  -->  show result to user
```

## Retry Rules

- Reviewer REJECT: Coder gets the issues list, fixes, resubmits. Max 3 attempts.
- Tester FAIL: Coder gets the failure details, fixes, resubmits to Reviewer then Tester. Max 3 attempts.
- After 3 failures at any step: pipeline stops. Show the user the last spec, the last test results, and what's stuck. Let them decide.

## What Each Agent Receives

| Agent            | Receives                                              |
|------------------|-------------------------------------------------------|
| Product Manager  | User's request                                        |
| Architect        | Spec                                                  |
| Coder            | Spec + Build Plan (+ issues list on retry)            |
| Code Reviewer    | Spec + Build Plan + Coder's summary                   |
| Tester           | Spec + Coder's summary                                |

## Data That Persists Across the Pipeline

- The SPEC never changes once written. If the spec is wrong, the pipeline stops and the user revises.
- The BUILD PLAN never changes once written. If the plan is flawed, the pipeline stops and the user revises.
- The Coder's work accumulates. On retry, the Coder gets its previous summary plus the Reviewer's or Tester's feedback.

## Init Step (Before Agent 1)

Scan the working directory. Capture:
- File tree (top 3 levels)
- Key config files (package.json, requirements.txt, CLAUDE.md, etc.)
- Git status (branch, clean/dirty)

This context is available to all agents but is primarily used by the Architect and Code Reviewer.
