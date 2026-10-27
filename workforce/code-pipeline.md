---
id: code-pipeline
name: Code Pipeline
department: compose
source: vibenode
version: 1.0.0
depends_on: [plan-team, build-team, review-team, test-team, final-audit, debug-team, hotfix-team, refactor-team]
type: prompt-template
---

# Code Pipeline

Reusable prompt template. Invoke by typing: **code pipeline**

**Unique value**: Takes a code request, in any repository, from idea to a delivered result whose confidence grade is computed by a program from what the Session Engine recorded, so no agent can award a grade it did not earn. No other agent produces a grade with evidence behind it.

Execute Pipeline is the older, prose-enforced version and is left as it is. This one is a standalone engine: the programs under `pipeline/` (`start`, `depth`, `run_check`, `harness`, `ledger`) do the bookkeeping, and the teams do the thinking.

**Why this agent diverges** from AGENT_BEST_PRACTICES "When you create a new agent" step 4: it is not added to Execute Pipeline's TEAM UNIQUE VALUE list or routing rules, because it is a second orchestrator beside Execute Pipeline, not a team Execute Pipeline routes to, and Execute Pipeline is deliberately left untouched (Rule 11).

## Invocation Contract

The caller MUST include in the kickoff prompt:
- the original request verbatim (it is copied into `00-request.md` untouched and every checker reads it),
- the repository the work happens in (defaults to the current working directory),
- any constraints, deadlines or out-of-scope items the user has already stated,
- pointers to prior context that matter (a spec path, a conversation decision),
- whether this is new work, an unknown-cause bug, a production fire, or cleanup, if known.

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting.

## The Prompt

Take this request from start to finish. The task is not complete after planning, building, or writing a report. It is complete when `python -m pipeline.harness <run-dir>` has printed the grade, that grade and its findings are the first lines of your final report, and the ledger has a row for the run.

The rules of this pipeline are enforced by programs, not by this text. Where the text and the harness disagree, the harness is right, and the fix is to change the harness, not to argue with it.

**Step 0: read the ledger.** Open `docs/plans/runs/LEDGER.md` in the repository if it exists. Its `obstacles` column is what earlier runs learned the hard way. Do not rediscover them.

**Step 1: start the run.** Write the request verbatim to a temp file and run:

    python -m pipeline.start --repo <repo> --slug <short-slug> --route <a|b|c|d> --request-file <file>

Routes: (a) new feature or major change; (b) bug with unknown cause; (c) production issue where speed matters; (d) internal cleanup with no behavior change. If the request is ambiguous between routes, ask; the wrong route costs more than the question. The command prints `PIPELINE-RUN nonce=…`; that line is how the harness later finds this session's transcript. Never run it outside the session doing the work.

The `pipeline` package lives in the VibeNode checkout, so every `python -m pipeline.<cmd>` runs from there (or with it on `PYTHONPATH`); `--repo` points at the repository being worked on. When the session's working directory is not that repository, pass `--project-cwd <session cwd>` as well: the harness looks for this session's transcript under the project of the working directory, not of the repository.

**Step 2: plan, at the depth the diff demands.** For route (a), run the Plan Team and write the hardened spec to `docs/plans/` and the Plan Team report to `01-plan.md`. For (b), (c), (d) the entry team writes `05-debug.md`, `06-hotfix.md` or `07-refactor.md` instead. Whatever the route, the planning file ends with the verification plan, a fenced block the programs read:

    ```verification
    - id: suite
      cmd: <the repository's own full test command>
      expect: \d+ passed
    - id: ui
      cmd: <a headless-browser or UI-driving test for the changed screen>
      expect: \d+ passed
    - id: native
      executable: false
      reason: <why it cannot run on this machine>
    ```

One criterion per thing that must be true for the request to be met. Choose what proves it for THIS project: its own test command (read its CLAUDE.md or equivalent), a browser test when a web screen changed, build-and-run for a desktop app, schema and rollback checks for a database change, evaluation data for an AI feature. A new project with no test framework gets one as part of the plan. Anything that cannot run here is listed as `executable: false` with the reason; the harness caps the grade at MEDIUM for it and the report names it. Do not invent a criterion to make the list longer; do not leave out the one that matters to make it pass.

Depth is not your call. After the plan (and again after the build), run `python -m pipeline.depth <run-dir>`. It prints `full` or `light` from the diff: protected files (`daemon/`, `common/`, auth, billing, schemas, IPC, anything near a PERF-CRITICAL marker, the repository's `.pipeline-protected`) or more than 5 files / 200 lines mean full. Light depth skips the Plan Team and the Review Team. It never skips the independent test on the final tree, the audit, or the fourth question. At light depth on route (a) there is no `01-plan.md`, so the verification block goes at the end of `00-request.md`, below the verbatim request, which stays untouched.

Write the `expect` regex against what the command really prints here. Read the repository's pytest configuration before adding `-q`: VibeNode's `pytest.ini` already passes `-q`, a second one makes `-qq`, and the `N passed` summary line disappears (the ledger's first obstacle). Run the command once by hand and match a line you saw.

**Step 3: build.** The Build Team implements the plan end to end, writes `02-build.md`, and adds or updates tests as it goes. Every defect a later stage finds becomes a regression test that failed before the fix and passes after. That is how the pipeline converges; the ledger only remembers.

**Step 4: the checkers.** Review Team (full depth), Test Team and Final Audit each run as a FRESH subagent launched with the Agent tool, one at a time, in this order: Review finishes and its fixes land, then Test runs on that tree, then Audit reads both. Never two checkers on a moving tree (the first run of this pipeline did exactly that: Review changed six files while Test was running the suite, and every piece of Test's evidence went stale). Foreground, or background only if you wait for it before starting anything else. The brief for a checker contains only: the original request verbatim, the run directory path, the spec path if any, and the changed files or git range. It does not contain the builder's report, the builder's reasoning, or any hint of the expected verdict. The harness reads each checker's kickoff prompt and flags a pasted builder report; it flags a checker stage the root session wrote as self-graded. Each checker writes its own stage file (`03-review.md`, `04-test.md`, `08-audit.md`) with the Write tool from inside its own session.

The Test Team runs every executable criterion through the program, never by hand:

    python -m pipeline.run_check <run-dir> <criterion-id>

That records the command, exit status, output, the plan it read and the tree it ran against in `<run-dir>/evidence/<id>.json`, and prints a `PIPELINE-EVIDENCE` line the engine keeps. A passing summary typed into `04-test.md` is worth nothing; the harness only counts evidence a transcript shows being produced. Failures go into `04-test.md` verbatim (full traceback, full assertion) per Test Team's raw-output rule.

If a check fails, the Test Team hands the specifics back to the Build Team. After ANY change to the code, every required criterion runs again: the harness grades the tree as it is now, and evidence from an earlier tree is stale. Two fix-and-recheck loops per checker stage; on the third disagreement, stop and bring it to the user with both positions stated. Route (c) has 15 minutes before it escalates.

**Step 5: the audit.** Final Audit is a fresh subagent that reads `00-request.md`, every stage file and the evidence, and asks whether the result does what the user wanted, end to end, without new problems (intent, blast radius, regressions, project rules, operational risk, as in `final-audit.md`). It fixes only what is clear, low-risk and unambiguous. `08-audit.md` ends with a heading containing the words "what could still be wrong" and a real answer to: *if every check passed, what could still be wrong that no check would catch?* A material answer does one of three things: triggers another criterion and another run, stays as a named reason the grade is MEDIUM, or goes to the user for a decision. It is never recorded and waved through. A non-material answer is a candidate check for later.

Two rules for every checker. A checker may challenge the criteria against the original request, in its report; it does not edit the plan file, and the harness flags a plan edited by a checker, because a checker that rewrites `expect` has graded its own criteria. And a checker reads evidence with `cat`, `Get-Content` or `jq`; an inline interpreter (`python -c`) naming the evidence folder reads as a write and grades as a forgery.

**Step 6: the grade.** Run `python -m pipeline.harness <run-dir>`. It prints `PIPELINE-VERDICT HIGH|MEDIUM|LOW|BLOCKED` and its findings. You report that grade. You do not round it up, and you do not argue it down. If a finding is wrong about the facts, fix the fact (run the check again, launch the checker properly) and grade again.

**Step 7: deliver and record.** State separately what was built, verified, committed, merged, packaged and deployed. Commit and push only if the user's rules allow it; anything irreversible (production data, auth, payments, secrets, destructive git, anything leaving the machine) waits for the user. Then:

    python -m pipeline.ledger <run-dir> --delivered "<files or commit>" --obstacles "<one line the next run needs>"

## Standing Criteria

- No stage file is evidence. The transcript that shows a command running, or a subagent writing, is.
- The grade applies to the tree at grading time. A change after a check invalidates that check.
- Less work is allowed at light depth. Less evidence never is.
- The repository's own rules (its CLAUDE.md, its publish-gate command, its protected paths) are read, not assumed from VibeNode's.
- No secrets, personal paths or user data in anything committed; run directories are not committed to a public repository.
- Every obstacle met goes in the ledger row, one line, so it is met once.

## Escalate when

- the route is ambiguous;
- a checker and the builder disagree three times;
- the fourth question's answer is material and cannot be turned into a check;
- the next step is irreversible;
- the harness says BLOCKED for a reason you cannot fix (no transcript, no repository).

## Output Format

Return the final pipeline report in this numbered structure. The first three items are the headline and must fit on a phone screen.

1. **Grade** — the `PIPELINE-VERDICT` line verbatim, with its findings, and the path to `verdict.json`.
2. **Delivered** — built / verified / committed / merged / packaged / deployed, each stated as a fact or "not done".
3. **For you** — what the user must still do (restart, decision, nothing).
4. **Route and depth** — the route, the one-sentence rationale, and the depth the diff required with its reasons.
5. **Run directory** — path to `docs/plans/runs/<…>/` with the stage artifacts and the evidence folder.
6. **Implementation summary** — brief; detail in `02-build.md`.
7. **Checker findings and fixes** — brief; detail in `03-review.md`, `04-test.md`, `08-audit.md`. Raw failure traces live in the artifacts.
8. **The fourth question** — the audit's answer, and what was done about it.
9. **Risks, tradeoffs and follow-ups** — anything material that was deferred or remains uncertain.
10. **What was not validated or could not be fully verified** — the unexecuted criteria and every other blind spot.
11. **Obstacles encountered** — setup issues, workarounds, flags, environment quirks, aggregated across stages, and the one line that went into the ledger.
