---
id: code-pipeline
name: Code Pipeline
department: compose
source: vibenode
version: 1.5.0
depends_on: [plan-team, build-team, review-team, test-team, final-audit, debug-team, hotfix-team, refactor-team]
type: prompt-template
---

# Code Pipeline

Reusable prompt template. Invoke by typing: **code pipeline**

**Unique value**: Takes a code request, in any repository, from idea to a delivered result whose confidence grade is computed by a program from what the Session Engine recorded, so no agent can award a grade it did not earn. No other agent produces a grade with evidence behind it.

This is the one programming workflow. Typing **execute pipeline** runs it too: Execute Pipeline, the older version whose rules lived only in prose, became an alias for Code Pipeline once Code Pipeline passed its qualification run (a planted defect caught at LOW, the fix graded HIGH, a late edit voiding HIGH). The programs under `pipeline/` (`start`, `depth`, `run_check`, `attest`, `escalate`, `harness`, `deliver`, `ledger`) do the bookkeeping, and the teams do the thinking.

**The teams, and the failure each one removes** (AGENT_BEST_PRACTICES Rule 10; a new team must add a line here):
- Plan Team: spec gaps, ambiguity, and architectural risk before any code is written.
- Build Team: implementation errors, step-by-step correctness, and spec-to-code drift during construction.
- Review Team: code quality, security, documentation, and project rule violations in completed code.
- Test Team: integration failures, workflow breakage, concurrency issues, and performance regressions, with evidence the program records.
- Debug Team: unknown-cause failures, through root cause analysis.
- Refactor Team: structural decay, duplication, and maintainability drag without changing behavior.
- Hotfix Team: production-blocking failures, contained with the minimum safe change under time pressure.
- Final Audit: intent mismatch, whole-system incoherence, blast radius damage, and gaps between the checks and the request.

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

Routes: (a) new feature or major change; (b) bug with unknown cause; (c) production issue where speed matters; (d) internal cleanup with no behavior change. If the request is ambiguous between routes, ask; the wrong route costs more than the question. The command prints `PIPELINE-RUN nonce=… route=… request=… pinned=… protected=… dir=…`. That line is the run's fixed point: the harness finds this session's transcript by the nonce, and takes the route, the hash of the verbatim request, and the hashes of the repository's `.pipeline-verify` and `.pipeline-protected` from the line the engine recorded, never from `run.json` or any file an agent can edit. Changing any of them afterwards is tampering, graded LOW. Never run it outside the session doing the work, and never append anything to `00-request.md`.

The `pipeline` package lives in the VibeNode checkout, so every `python -m pipeline.<cmd>` runs from there (or with it on `PYTHONPATH`); `--repo` points at the repository being worked on. When the session's working directory is not that repository, pass `--project-cwd <session cwd>` as well: the harness looks for this session's transcript under the project of the working directory, not of the repository.

**Step 2: plan, at the depth the diff demands.** For route (a), run the Plan Team and write the hardened spec to `docs/plans/` and the Plan Team report to `01-plan.md`. For (b), (c), (d) the entry team writes `05-debug.md`, `06-hotfix.md` or `07-refactor.md` instead. Whatever the route, the planning file ends with the verification plan, a fenced block the programs read:

    ```verification
    - id: sub-works
      cmd: {python} -m pytest tests/test_sub.py
      expect: \d+ passed
      detects_change: true
      baseline_files: tests/test_sub.py
      fails_with: has no attribute 'sub'
    - id: ui
      cmd: <a headless-browser or UI-driving test for the changed screen>
      expect: \d+ passed
    - id: native
      executable: false
      reason: <why it cannot run on this machine>
    ```

`{python}` is the interpreter running the check; never write one machine's virtualenv path into a plan.

**Name the consequential failure scenarios before building.** Passing tests do not show the user's intent was met unless they exercise the ways this change could go wrong. The plan file lists each one it can name on its own line, `SCENARIO: <short-id> <what must hold, under which condition>`, and gives it a check whose entry says `covers: <short-id>`. Think through the categories that apply to THIS change and only those: persistence and recovery, identity and authorization, concurrency and ordering, consistency across a boundary (client and server, two processes, a cache and its source), data integrity, user-visible state, error handling and retries. A scenario is a verification obligation: it is VERIFIED when a covering check passes on the final tree (or a checker records `VERIFIED: <id> by=<criterion>`), and otherwise it is FIXED, NOT-A-DEFECT or UNRESOLVED as above. A scenario written into the plan cannot be removed later; the harness reads every version the session wrote. An open or unresolved scenario caps the grade at MEDIUM. The register proves that every NAMED scenario was settled, not that every consequential one was named: the audit challenges the omissions.

**The project's full suite is pinned, not planned.** A repository declares its mandatory checks in `.pipeline-verify` at its root (the same list syntax, no fence; VibeNode's holds the publish-gate suite). `pipeline.start` copies it into the run as `pinned-verify.txt` and records its hash in the start line, and those checks are added to every plan as required criteria. A plan cannot redefine them, and a run that edits `.pipeline-verify` is graded on the pinned copy, forced to full depth, and held below HIGH until a person reviews the change. A repository with no declaration yet (a new project) marks its suite in the plan with `suite: true`; without one the grade is MEDIUM at most. Add the declaration so later runs cannot change it.

**The tests must detect the change.** On routes (a), (b) and (c), at least one required criterion is a regression demonstration: `detects_change: true`, with `baseline_files` (the test files, comma-separated globs) and `fails_with` (a regex the failure must print, naming the missing behavior). The Test Team runs it twice: normally, where it must pass, and with `python -m pipeline.run_check <run-dir> <id> --baseline`, which checks the code as it was when the run started out into a temporary git worktree (your working directory is never touched), copies the test files in, and must FAIL there for the reason `fails_with` names. A test that passes without the change does not test the change; a failure for some other reason (a missing helper file, an import error) does not count. Where no behavior can be demonstrated (configuration, documentation), the plan says so with a non-executable item marked `waiver: true` and a reason; the audit must name it in its coverage section and still judge the request covered. Route (d), a cleanup with no behavior change, needs none.

One criterion per thing that must be true for the request to be met. Choose what proves it for THIS project: its own test command (read its CLAUDE.md or equivalent), a browser test when a web screen changed, build-and-run for a desktop app, schema and rollback checks for a database change, evaluation data for an AI feature. A new project with no test framework gets one as part of the plan. Anything that cannot run here is listed as `executable: false` with the reason; the harness caps the grade at MEDIUM for it and the report names it. Do not invent a criterion to make the list longer; do not leave out the one that matters to make it pass.

Depth is not your call. After the plan (and again after the build), run `python -m pipeline.depth <run-dir>`. It prints `full` or `light` from the diff: protected files (`daemon/`, `common/`, auth, billing, schemas, IPC, anything near a PERF-CRITICAL marker, the repository's `.pipeline-protected` as pinned at the start, so deleting or editing that file during the run removes no protection and is itself a full-depth change), a risk signal in an added or removed line (a shell call, a swallowed exception, a skipped test, a removed assertion or access check, disabled TLS, raw HTML), more than 5 files / 200 lines (pure renames not counted), no commit to bound the change against, or a checker's escalation all mean full. Light depth skips the Plan Team and the Review Team. It never skips the independent test on the final tree, the audit, or the fourth question. At light depth on route (a) there is no `01-plan.md`, so the verification block goes in `00-plan.md`; `00-request.md` is never touched after the start.

Any checker may demand full depth with `python -m pipeline.escalate <run-dir> "<why>"` when the change is larger than its diff looks. A checker can demand more review, never less: the escalation is recorded in the checker's transcript and cannot be undone, and the run then needs every full-depth stage.

The block's keys are fixed (`id`, `cmd`, `expect`, `required`, `executable`, `reason`, `cwd`, `timeout`, `suite`, `detects_change`, `fails_with`, `baseline_files`, `waiver`, `covers`); an unknown or repeated key is an error, so a typo cannot silently drop an assertion. A check pinned in `.pipeline-verify` must be executable and can never be a waiver: `pipeline.start` refuses such a declaration. A baseline demonstration counts only if it exited non-zero, did not time out, and printed `fails_with`. The repository a run checks is the one its run directory lives in, never what `run.json` says, and each piece of evidence names the `pipeline` implementation that produced it, so run the programs from the VibeNode checkout. Run directories carry a short random suffix, so two runs can start together.

Write the `expect` and `fails_with` regexes against what the command really prints here. Read the repository's pytest configuration before adding `-q`: VibeNode's `pytest.ini` already passes `-q`, a second one makes `-qq`, and the `N passed` summary line disappears (the ledger's first obstacle). Run the command once by hand and match a line you saw.

**Step 3: build.** The Build Team implements the plan end to end, writes `02-build.md`, and adds or updates tests as it goes. Every defect a later stage finds becomes a regression test that failed before the fix and passes after. That is how the pipeline converges; the ledger only remembers.

**Step 4: the checkers.** Review Team (full depth), Test Team and Final Audit each run as a FRESH subagent launched with the Agent tool, one at a time, in this order: Review finishes and its fixes land, then Test runs on that tree, then Audit reads both. Never two checkers on a moving tree (the first run of this pipeline did exactly that: Review changed six files while Test was running the suite, and every piece of Test's evidence went stale). Foreground, or background only if you wait for it before starting anything else. The brief for a checker contains only: the original request verbatim, the run directory path, the spec path if any, and the changed files or git range. It does not contain the builder's report, the builder's reasoning, or any hint of the expected verdict. The harness reads each checker's kickoff prompt and flags a pasted builder report; it flags a checker stage the root session wrote as self-graded. Each checker writes its own stage file (`03-review.md`, `04-test.md`, `08-audit.md`) with the Write tool from inside its own session. Each stage is a DIFFERENT subagent: one that writes two checker stages has checked its own judgment, and the harness flags it. The audit is written after the last code change; an audit older than the code is stale.

**Every checker signs its report.** After writing its stage file, and again after any change to it, the checker runs, from inside its own session, as a single command with nothing chained after it:

    python -m pipeline.attest <run-dir> <stage-file>      # e.g. 08-audit.md

That records the sha of the report and the fingerprint of the tree the checker examined, and prints a `PIPELINE-ATTEST` line the engine keeps. The harness requires it for every checker stage: the sha must be the file's sha at grading time (a report changed after the checker signed it is not the checker's report), and the fingerprint must be the tree at grading time (a reviewer whose tree is not the final tree has not reviewed the final code). So after ANY code change, the Review Team looks again at full depth and attests again, not only the Test Team. Put the attest instruction in every checker's brief.

**Findings are settled by evidence, never by a word** (`pipeline/findings.py`). Every suspected correctness defect a checker finds goes on its own line as `FINDING: <short-id> <what is wrong>` (`BLOCKING:` means the same). The checker does not decide whether it matters enough to track: if it could make the result wrong, it is a FINDING. Only what is genuinely not about correctness (cosmetic, an optional enhancement, a preference) is a `NOTE: <short-id> <text>`, which needs nothing more; a NOTE worded as a correctness concern (a race, lost or stale data, a crash, wrong output) is held to the same rules as a FINDING, and a line that labels something "non-blocking" outside these two forms is itself a finding of the harness. A finding stops HIGH until a LATER checker stage, written by a different subagent, records one disposition:

- `FIXED: <id> by=<criterion>`: the criterion is a plan check whose entry declares `covers: <id>`, it FAILED on the defective tree (run it through `run_check` before the fix, or let its baseline demonstration show it) and passes on the final tree;
- `NOT-A-DEFECT: <id> by=<criterion>`: a covering check that exercises the suspected failure passes on the final tree. An explanation, a design argument or an invariant stated in prose is not a disposition: an invariant counts only when a check would fail if it were violated;
- `UNRESOLVED: <id> <what is unknown and what it could cost>`: disclosed in the verdict and in `delivered.json`, and the grade cannot be HIGH.

The builder, not a checker, adds the covering check to the plan (a checker that edits the plan has graded its own criteria) and, for a real defect, runs it through `run_check` once before fixing. `RESOLVED:` alone settles nothing. Evidence from an earlier tree, a criterion not in the plan, a check whose plan entry does not cover the id, or the project's pinned suite does not settle a finding either. The stage that raised a finding cannot settle it, the builder's report does not count, and rewriting the report does not make the finding go away: the attest records keep every finding id a signed version carried. The audit is the last stage, so a finding the audit raises is settled by a FRESH auditor (a different subagent) that rewrites `08-audit.md` after the fix with the disposition (without repeating the FINDING line); the auditor that raised it cannot. The harness checks the mechanics (the id, the disposition, the citation, the evidence and its tree); whether the cited check really exercises the concern is the audit's judgment, so the audit names every finding and scenario id in its coverage section. Put these lines in the briefs' instructions so every checker uses them.

The Test Team runs every executable criterion through the program, never by hand, in two steps:

    python -m pipeline.run_check authorize <run-dir> <criterion-id> [--baseline]
    python -m pipeline.run_check execute   <run-dir> <criterion-id> [--baseline] --decision <id>

`authorize` first refuses, with exit 2 and nothing run, a check that cannot pass as written (preflight: a `{python} -m` module this interpreter lacks, a test file named in the command that does not exist, a missing `cwd`, a pytest `-q` stacked on a configured `-q` while `expect` waits for the "N passed" line that stacking hides, baseline files that match nothing); fix the plan and authorize again. When the evidence on disk already passed for exactly this operation (same command, plan, tree, interpreter and pipeline), it prints `PIPELINE-REUSE` and stops: running it again proves nothing new. Pass `--rerun` only when the harness says that evidence does not count (for example, it was not produced by an independent checker). Otherwise it binds the exact operation (the expanded command, the folder, the repository, the plan, the source fingerprint, the environment names it passes) to a decision and prints `PIPELINE-DECISION id=<id> … verdict=allow|pending`. Under the user's **auto** permission policy the decision is allowed at once: run `execute` next, with that id. Under any other policy it is **pending** and the command prints a card: **end your turn with that card, alone, exactly as printed.** The user's tap sends the card's reply ("Run the tests (id)" or "Don't run them (id)") as his own message; on your next turn run `execute` with the id. `execute` rebuilds the operation from the state now and refuses when anything changed since the approval (then authorize again), refuses an unanswered or refused card, uses each decision once, strips secret-shaped variables from the command's environment, and runs the criterion (`--baseline`: the regression demonstration against the starting snapshot, in an isolated worktree). It records the command, exit status, output, the plan, the tree and the decision in `<run-dir>/evidence/<id>.json` and prints the `PIPELINE-EVIDENCE` line first and last. A criterion whose timeout is over nine minutes is executed as a background shell command.

A passing summary typed into `04-test.md` is worth nothing; the harness counts only evidence a transcript shows being produced, under a decision the same session's `authorize` issued, allowed by the policy or by the user's own answer. Evidence with no decision grades LOW (`unauthorized`). Failures go into `04-test.md` verbatim (full traceback, full assertion) per Test Team's raw-output rule.

If a check fails, the Test Team hands the specifics back to the Build Team. After ANY change to the code, every required criterion runs again: the harness grades the tree as it is now, and evidence from an earlier tree is stale. Two fix-and-recheck loops per checker stage; on the third disagreement, stop and bring it to the user with both positions stated. Route (c) has 15 minutes before it escalates.

**Step 5: the audit.** Final Audit is a fresh subagent that reads `00-request.md`, every stage file and the evidence, and asks whether the result does what the user wanted, end to end, without new problems (intent, blast radius, regressions, project rules, operational risk, as in `final-audit.md`). It fixes only what is clear, low-risk and unambiguous. `08-audit.md` ends with a heading containing the words "what could still be wrong" and a real answer to: *if every check passed, what could still be wrong that no check would catch?* A material answer does one of three things: triggers another criterion and another run, stays as a named reason the grade is MEDIUM, or goes to the user for a decision. It is never recorded and waved through. A non-material answer is a candidate check for later.

`08-audit.md` must also have a heading containing the words "checks prove the request", answered criterion by criterion: for each required criterion (named by its id), does it prove part of what the user asked for, and is any part of the request left with no check? It also names every SCENARIO and FINDING id and says whether the check cited for it really exercises it; a citation the audit cannot stand behind is recorded as `UNRESOLVED: <id> <why>`, and a consequential scenario the plan never named is added and checked or reported. The section ends with a line `Verdict: covered` or `Verdict: not covered`. Passing checks can all be trivial on an unfinished feature, so the harness caps the grade at MEDIUM when the section is missing, skips a required criterion, has no verdict line, or says `not covered`. A `not covered` answer is fixed the same way as a material fourth-question answer: add the missing criterion and run again, or report the gap.

Two rules for every checker. A checker may challenge the criteria against the original request, in its report; it does not edit the plan file, and the harness flags a plan edited by a checker, because a checker that rewrites `expect` has graded its own criteria. And a checker reads evidence with `cat`, `Get-Content` or `jq`; an inline interpreter (`python -c`) naming the evidence folder reads as a write and grades as a forgery.

**Step 6: the grade.** Run `python -m pipeline.harness <run-dir>`. It prints `PIPELINE-VERDICT HIGH|MEDIUM|LOW|BLOCKED` and its findings, and exits 0 whatever the grade, so a MEDIUM or LOW run still gives the user a full report of what is missing. The verdict keeps two judgments apart: `criteria:` (did the prescribed checks run, with valid evidence, on the final tree) and `register:` (did the evidence settle every planned scenario and every finding, and which are unresolved). After the verdict it prints `PIPELINE-METRICS` and writes `metrics.json`: wall clock, time per stage, model calls and tokens per stage (cost when a price table is given in `PIPELINE_PRICES`), every repeated check with its cause (revalidation after a code change, an avoidable rerun on an unchanged tree, a retry), and every run_check call that produced no evidence. Metrics never change the grade; quote the summary line in the report. You report that grade. You do not round it up, and you do not argue it down. If a finding is wrong about the facts, fix the fact (run the check again, launch the checker properly) and grade again.

**The completion gate is `python -m pipeline.deliver <run-dir>`.** It recomputes the grade on the tree as it is now (it never reads `verdict.json`), writes `delivered.json`, prints `PIPELINE-DELIVERED grade=… accepted=…`, and exits 0 only for HIGH. Run it immediately before calling the work verified or complete, and before any delivery action (commit, merge, push, package, deploy). Exit 1 means the work is not verified: quote the line in the first line of the report, do not deliver it as done, and name what is missing.

**Below HIGH, only the user can accept, on a card.** There is no flag you can type for it. Run `python -m pipeline.deliver <run-dir> --request-acceptance <commit|merge|push|package|deploy>`: it issues a decision bound to this run, this grade, this code and these findings, and prints a card that says in plain words what happened, what it means and what the user decides, with two buttons, **Fix it first** (recommended) and **Commit it anyway**. End your turn with that card, alone, exactly as printed. A "Fix it first" tap is your next instruction: close the gap and ask again. A "Commit it anyway" tap sends `Commit it anyway (<id>)`; on your next turn run `python -m pipeline.deliver <run-dir> --decision <id>`, which passes only if the user's answer is in the transcript, the card was shown unchanged, and nothing (code, grade, findings) changed since. A card you edit answers nothing. Not answering leaves the work undelivered; nothing happens on its own.

**Step 7: deliver and record.** State separately what was built, verified, committed, merged, packaged and deployed, quoting the `PIPELINE-DELIVERED` line. Commit and push only if the user's rules allow it and the gate passed; anything irreversible (production data, auth, payments, secrets, destructive git, anything leaving the machine) waits for the user. A commit of the verified code does not change the grade: the fingerprint is the tree's content, not its commit id. Then:

    python -m pipeline.ledger <run-dir> --delivered "<files or commit>" --obstacles "<one line the next run needs>"

The ledger recomputes the grade as well, so its row says what the evidence says at that moment.

**What `run_check` runs, and what approving it means.** Each criterion's command runs through the platform shell on this machine; VibeNode's own permission check sees `pipeline.run_check`, not the inner command, so the pipeline asks for the inner command itself: it reads the user's permission policy and, when that policy is not automatic, puts the exact command on a card for the user. Approving a command says nothing about the code it runs: a permitted `pytest` still executes the project's tests, and the command still has the filesystem and the network; secret-shaped variables are kept out of its environment, which is not a sandbox. Do not put anything in a `cmd` you would not run by hand here: no network calls to production, no credentials, no destructive steps. A baseline worktree isolates the source files, not the machine. A commit made in the run's repository before a passed delivery is reported (`committed-before-delivery`) and flagged in the ledger, not prevented: Code Pipeline does not sit in VibeNode's permission path.

## Standing Criteria

- No stage file is evidence. The transcript that shows a command running, or a subagent writing and attesting, is.
- The route, the request and the pinned declarations are what the start line recorded. `run.json`, `00-request.md` and the pinned copies are read, and a difference is tampering.
- The grade applies to the tree at grading time, by content. A change after a check invalidates that check and every checker's attestation; a commit of unchanged code invalidates nothing. The ledger and the delivery gate recompute the grade; neither trusts `verdict.json`.
- The project's pinned checks always run; the tests always show they detect the change (or a waiver the audit accepts); every checker is a different subagent.
- Less work is allowed at light depth. Less evidence never is.
- A finding or a planned scenario is settled by a check that covers it, or disclosed as unresolved; a checker's word settles nothing.
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
8. **Coverage and the fourth question** — the audit's coverage verdict and its answer to the fourth question, and what was done about each.
9. **Risks, tradeoffs and follow-ups** — anything material that was deferred or remains uncertain.
10. **What was not validated or could not be fully verified** — the unexecuted criteria and every other blind spot.
11. **Obstacles encountered** — setup issues, workarounds, flags, environment quirks, aggregated across stages, and the one line that went into the ledger.
