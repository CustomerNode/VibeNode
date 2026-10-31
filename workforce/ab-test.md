---
id: ab-test
name: A/B Test
department: compose
source: vibenode
version: 1.2.0
depends_on: []
type: prompt-template
playbook: true
playbook_line: Prompts and models compared side by side
playbook_what: Runs each variant in its own new session, on the model you pick, and lays the answers side by side.
playbook_say: a b test who is the president | a b test on opus, sonnet and haiku: summarize this
playbook_how: Opens one new session per variant: A, B, C and on. | Sends each its prompt and model: the same for all, or one each. | Waits until every session has answered. | Lays the answers side by side with time, model and tools. | Says where they agree, where they differ, and which did better. | The sessions stay in your sidebar to open later.
---

# A/B Test

Reusable prompt template. Invoke by typing: **a b test** (also "ab test", "a/b test", "a b c test", or "a b: …")

**Unique value**: Runs the same request, or deliberately different prompts, in separate fresh VibeNode sessions (A, B, C and as many as asked) and reports every answer side by side with its time, model and tools, so a choice between prompts or approaches rests on several real runs instead of one. No other agent compares independent sessions.

## Invocation Contract

The caller MUST include in the kickoff prompt:
- The variants, any number from two up: either one prompt run N times (the user says how many, or the letters, "a b c" = 3), or a different prompt per variant.
- The session names, if the user gave any (default: "A/B test A", "A/B test B", "A/B test C", and so on).
- The model for each variant, if he named any (one for all, or one each).
- What the user wants compared, if he said (accuracy, length, tools used, speed); otherwise compare all four.

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting.

## The Prompt

Run this directly in the session the user typed it in. It needs no subagent: the work is one script and a comparison.

1. Run the A/B runner from the VibeNode repository folder in the foreground, with a timeout of at least 10 minutes:
   - one prompt, N runs (named A/B test A, B, C…): `python scripts/ab_test.py --prompt "<prompt>" --count <N> --json <temp file>`
   - one prompt under names he gave: `python scripts/ab_test.py --prompt "<prompt>" --names "<name 1>" "<name 2>" "<name 3>" --json <temp file>`
   - a different prompt per variant (named A, B, C…): `python scripts/ab_test.py --each "<prompt A>" "<prompt B>" "<prompt C>" --json <temp file>`
   - a different prompt per variant under his names: `python scripts/ab_test.py "<name 1>=<prompt 1>" "<name 2>=<prompt 2>" --json <temp file>`
   - models: add `--models <model> …` to any of these, one model for all variants or one per variant in order ("opus", "sonnet", "haiku", "sonnet 5", "Opus 4.7" or an exact id). One prompt plus `--models opus sonnet haiku` runs one variant per model. The report warns when a session ran on a different model than asked.
   The prompts are the user's words, passed through unchanged; split them into variants only where he did.
   The script starts each variant as a real session in the active project (it shows in the sidebar under its name), follows each session to the ID it switches to, and prints each final reply about 8 seconds after it lands.
2. Read the JSON file for the full replies. The printed summary shows only each reply's short answer.
3. Compare the variants on what the user asked for. Say where they agree, where they differ, and which one did better and why, from the replies themselves, never from guesses.

Do not edit any code. The only thing this agent creates is the sessions the user asked for. Do not delete them afterwards: he asked for them, and they stay in his sidebar.

## Standing Criteria

- Every variant is a fresh session started by the script. Never answer a variant yourself.
- Each result is reported from that session's own reply, with its time, model and tools.
- A variant that did not finish before the timeout is reported as unfinished, never dropped.
- Sessions are named exactly as the user said.

## Escalate when

- The web server at port 5050 does not answer: report the script's raw error.
- A session ends on an error or a usage limit: report it verbatim and do not retry on your own.
- A variant ran on a different model than asked (the report prints a WARNING): report it as such and never present that variant as a result for the model asked.

## Output Format

Return one report in this numbered structure:

1. **Variants run**: each session's name, question and session ID.
2. **Results**: a table with one row per variant: time, model, tools used, short answer.
3. **Comparison**: where they agree, where they differ, and which did better on what the user wanted compared.
4. **What was not validated or could not be fully verified**: unfinished variants, or claims in a reply nobody checked.
5. **Obstacles encountered**: script errors verbatim, timeouts, anything the next run should know.
