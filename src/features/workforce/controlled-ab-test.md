---
id: controlled-ab-test
name: Controlled A/B Test
department: compose
source: vibenode
version: 1.2.2
depends_on: []
type: prompt-template
playbook: true
playbook_risk: high
playbook_capabilities: [read, write, run_commands, run_experiments, modify_code]
playbook_line: Run isolated experiments and independently verify results
playbook_what: Runs each approach in isolated copies, repeated, and judges every trial by checks fixed in advance that contestants never see.
playbook_say: controlled ab test code pipeline versus normal coding | controlled a b test opus against sonnet on this assignment
playbook_how: Agrees the question, the one difference, the baseline and the checks first. | Validates the experiment and seals a copy of the hidden tests. | Runs every trial in its own hidden, isolated copy of the baseline. | Watches every contestant and stops one that reaches outside its folder. | Proves which implementation ran, then runs the hidden checks itself. | Reports correctness, defects, time, cost and consistency, or says inconclusive.
playbook_use: Use controlled A/B test
---

# Controlled A/B Test

Reusable prompt template. Invoke by typing: **controlled ab test** (also "controlled a/b test", "controlled experiment").

**Unique value**: Establishes which approach actually accomplishes more under controlled conditions: each trial runs in its own isolated copy of a fixed baseline, the implementation that ran is proven from outside the contestant, and success is decided only by independent checks fixed before any contestant starts, never by what a contestant says it did. AI Compare puts answers side by side; this verifies outcomes.

## Invocation Contract

The caller MUST include in the kickoff prompt:
- The question the experiment tests and the approaches compared (for example Code Pipeline against normal coding, or two models on one assignment).
- The baseline: a repository and commit (or a folder, or none for a task with no starting files).
- The assignment every contestant gets, word for word.
- How success is judged: hidden acceptance tests (a folder outside the baseline) and any regression command, or for a non-coding task a rubric for a blinded judge.
- The number of trials per approach (default 3; 1 is a pilot only).
- Anything the contestants must never see (prior solutions, earlier reviews), if the user named any.

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting.

## The Prompt

Run this in the session the user typed it in, from the VibeNode repository folder.

1. Print the spec format: `python scripts/controlled_ab_test.py spec-help`. Write the experiment spec as a JSON file in `docs/plans/experiments/<id>.json` (gitignored). Fill it from the user's request: `question`, `baseline`, `assignment`, `variants` (each with its `prompt_template` containing `{assignment}`, its `model` when models differ, and its `provenance` adapter), `differs` (exactly the fields that differ on purpose: every other field must be equal, and the check refuses an uncontrolled difference), `evaluation`, `trials`, `timeout`, and `forbidden_material`. Write the hidden acceptance tests yourself only if the user asked you to, in a folder outside the baseline, before any contestant starts; never inside the baseline. Give a known-good solution in `evaluation.reference_dir` whenever one exists. Your test and reference files are never changed or removed. Every session runs as the same user, so a folder is not a lock; protection is three things the controller does itself: each trial's copy gets an unguessable name under a parent folder the account cannot list, a copy of the tests is sealed with a key kept only there, and a break-out watch stops any contestant whose tool calls reach outside its own folder (a sibling, the parent, the record, the tests, the reference, VibeNode's own routes, a permission command). Keep `hidden_tests` at `protected`; `not_secret` runs without that protection and the report says so.
2. Provenance: every variant names how the controller proves what ran, with an adapter from `src/features/experiments/adapters/` and an `expect` identity (an exact version in `expect_identity` when it matters). A label is not proof: the adapter must name a record from that trial, or for `none` the paths the controller re-checks. For Code Pipeline against normal coding use `src/features/experiments/adapters/code_pipeline.py` with `expect` set to `code-pipeline` and `none`. When only the model differs, set `provenance_not_applicable` with the reason: the controller checks the model from the transcript.
3. Validate: `python scripts/controlled_ab_test.py validate docs/plans/experiments/<id>.json`. Fix the spec until it prints `valid`; fix a problem by making the experiment sound, never by dropping a check. Before any contestant starts, the controller also requires the acceptance checks to fail on the starting code (and pass on the reference solution when given); an experiment whose checks do not qualify never starts. Show the user the question, the one difference, the baseline, the checks and the trial count, and get the user's go-ahead before the next step: a run starts real sessions that write code in isolated copies.
4. Run it as a durable job, because a real experiment outlasts a turn: `python vibenode.py job run --name "<id>" --cwd <VibeNode folder> --done "^Report: " --not-resumable --command "python scripts/controlled_ab_test.py run docs/plans/experiments/<id>.json"`. `--not-resumable` matters: a blind relaunch after a reboot would start a second experiment, so a person decides. For a small synthetic pilot under ten minutes, run `python scripts/controlled_ab_test.py run <spec>` in the foreground instead. The controller freezes the spec and seals the hidden tests in `data/experiments/<run id>/`, gives each trial a fresh copy of the baseline under `~/.vibenode-experiments/<run id>/` with no history, remote or project memory, and starts trial k of every variant together in its own VibeNode session working in that copy.
5. When it finishes, read `report.md` and `experiment.json` in the experiment folder. Each trial folder holds its prompt, transcript copy, changes, provenance, acceptance and regression logs.
6. Report from that evidence. Say the experiment status first: completed (every trial evaluated; a contestant failing the checks is a result), infrastructure failure, invalid (including any break-out, listed in the report's Containment section), or incomplete (a timeout or a cancel is not an evaluation). A trial is verified only when its session completed, it changed something to evaluate, its copy was isolated and on the baseline, its implementation was proven, nothing contaminated it, and the controller's own checks passed. A session that timed out, was cancelled, broke out, or outlived a controller failure is stopped through VibeNode before anything sealed is revealed; one whose stop could not be confirmed is never evaluated and makes the run an infrastructure failure. The report's Containment section names the isolation level: access control on this account, watched; never operating-system isolation. A contestant's "done" is a claim, recorded beside the verdict, never a substitute for it. Repeat the report's answer to "what the evidence establishes" as written: an invalid experiment has no winner, one trial is a pilot, and speed or a self-reported grade never decides a winner.

Only touch the spec, the hidden tests the user asked you to write, and what the controller creates. Do not modify the contestants' copies, the sealed tests, the frozen spec or a trial's records during or after a run. Never push, merge, publish or deploy anything a contestant produced. Do not start a real experiment the user has not approved in step 3.

## Standing Criteria

- The evaluation is fixed and sealed before any contestant starts, and the sealed tests are re-hashed after every trial.
- Every trial runs in its own isolated copy of the baseline, whose files are verified equal to the baseline commit before it starts.
- The implementation that ran is proven by an adapter the contestant does not run, bound to that trial; a record from before the trial fails.
- A contestant's transcript that mentions the sealed records, the canary, another trial's copy or forbidden material makes that trial contaminated and the experiment invalid; the watch stops such a session while it runs, and the finished trials, reference and permissions are re-checked at the end.
- Time is wall clock per session, cost is the CLI's reported figure, and a missing cost is never estimated.
- Every experiment has its own record folder, created once; nothing from an earlier experiment is reused.

## Escalate when

- The spec does not validate and the fix would change what is being tested: ask the user.
- The web server does not confirm a session runs in its trial's copy: stop and report it (an older web server needs a restart to load the route).
- A trial is contaminated, a provenance check fails, or the sealed tests changed: report the experiment as invalid with the evidence; do not rerun on your own.
- A usage limit or error stops sessions: report it verbatim.

## Output Format

Return one report in this numbered structure:

1. **Experiment**: the question, the one difference, the baseline, the checks, the trials, and the record folder.
2. **Results by approach**: trials, completed, verified, claimed done, failed checks, time, cost, model calls, retries, human interventions.
3. **Answers**: more correct; fewer consequential defects; less time and cost; more consistent; more human intervention; what the evidence establishes.
4. **Recommendation**: supported by the verified results, or "inconclusive" or "invalid" with why.
5. **What was not validated or could not be fully verified**: costs not reported, judgments that are subjective, contamination that could not be ruled out.
6. **Obstacles encountered**: controller errors verbatim, sessions that failed or timed out, anything the next experiment should know.
