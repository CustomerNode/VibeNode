---
id: ai-compare
name: AI Compare
department: compose
source: vibenode
version: 2.2.1
depends_on: []
type: prompt-template
playbook: true
playbook_risk: medium
playbook_capabilities: [read, write, run_commands, read_only_sessions]
playbook_line: Compare AI models, prompts, and responses
playbook_what: Runs each variant in its own fresh read-only session and lays the answers side by side with measured time and cost.
playbook_say: ai compare who is the president | ai compare on opus, sonnet and haiku: summarize this
playbook_how: Opens one fresh read-only session per variant. | Sends each prompt exactly as written, on the model you pick. | Waits for every session: done, failed, timed out or cancelled. | Lays the answers side by side with time, tokens and cost. | Says where they agree, differ, and which answered better, as judgment. | Keeps every reply in its own folder; sessions stay in your sidebar.
playbook_use: Use AI compare
---

# AI Compare

Reusable prompt template. Invoke by typing: **ai compare** (also "compare ai", "compare models", "compare prompts"). It replaces A/B Test; "a b test" requests that only want answers side by side are AI Compare.

**Unique value**: Puts two or more AI answers side by side, from fresh read-only sessions that differ only in the prompt or model the user chose, with measured time, tokens and cost kept apart from the AI's judgment of quality. It compares what the AIs said; it does not prove which approach accomplishes more (that is Controlled A/B Test).

## Invocation Contract

The caller MUST include in the kickoff prompt:
- The variants, two or more: one prompt run N times, one prompt on several models, or a different prompt per variant.
- The session names, if the user gave any (default: "Compare A", "Compare B", and so on, plus the model when one was picked).
- The model for each variant, if the user named any (one for all, or one each).
- What the user wants compared, if they said (conclusions, reasoning, completeness, clarity, speed, cost); otherwise all of them.
- A timeout, if the user gave one (default 10 minutes).

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting.

## The Prompt

Run this directly in the session the user typed it in. It needs no subagent: the work is one script and a comparison.

1. Run the AI Compare script from the VibeNode repository folder in the foreground, with a shell timeout longer than its own `--timeout`:
   - one prompt, N read-only runs: `python scripts/ai_compare.py --prompt "<prompt>" --count <N>`
   - one prompt under names the user gave: `python scripts/ai_compare.py --prompt "<prompt>" --names "<name 1>" "<name 2>"`
   - a different prompt per variant: `python scripts/ai_compare.py --each "<prompt A>" "<prompt B>"`
   - a different prompt per variant under the user's names: `python scripts/ai_compare.py "<name 1>=<prompt 1>" "<name 2>=<prompt 2>"`
   - models: add `--models <model> …` (one for all, or one per variant in order: "opus", "sonnet", "haiku", "sonnet 5" or an exact id). One prompt plus `--models opus sonnet haiku` gives one read-only variant per model.
   - add `--timeout <seconds>` when the user gave a timeout.
   The prompts are the user's words, passed through byte for byte, spaces and blank lines included. The script records each prompt it sent and the prompt each session's transcript shows it received, and the report warns when they differ. Split prompts into variants only where the user did.
   Every session it starts is read-only: VibeNode's plan mode lets it read and answer but refuses file edits and commands. When the web server does not confirm read-only, the script stops that session through VibeNode at once, confirms the stop and reports the variant as not compared; the report also flags any variant whose transcript shows a change that went through.
2. The script prints `report.md` and its path under `data/ai_compare/<run id>/`. That folder is unique to the run: `run.json` plus one numbered folder per variant with its prompt, full reply, transcript copy and record. Read the full replies there; the printed table shows only short answers.
3. Compare the variants on what the user asked for: conclusions and recommendations, reasoning quality, completeness, important omissions, contradictions, clarity. Use the measured columns (status, time, model calls, tokens, cost) as measurements and say so; say plainly that the quality comparison is your judgment from the replies. Give a recommendation only when the replies support one.

Do not edit any code, do not answer a variant yourself, and do not remove the sessions afterwards: the user asked for them, and they stay in the sidebar. A comparison that has to produce files or change anything is not AI Compare: point the user to Controlled A/B Test, which gives each variant an isolated copy.

## Standing Criteria

- Every variant is a fresh read-only session started by the script, never an answer written by this session.
- The prompt a session received equals the user's prompt, checked against its transcript; nothing operational is appended to it.
- Each result comes from that session's own reply, with its status, time, model, tokens and cost; a cost the CLI did not report is "not reported", never estimated.
- A variant that failed, timed out or was cancelled is reported as such with any partial text marked partial, never dropped or counted as an answer; one that timed out or was cancelled is stopped through VibeNode and the report says whether the stop was confirmed.
- No quality score is invented; measured data and AI judgment are labelled apart.
- Each comparison and each of its variants has its own folder; no output lands on top of another comparison or variant.

## Escalate when

- The web server at port 5050 does not answer, or refuses read-only mode: report the script's raw error verbatim (an older web server needs a restart to load the read-only route).
- A session ends on an error or a usage limit: report it verbatim and do not retry on your own.
- A variant ran on a different model than asked, or the report says read-only was not enforced: report it as such and never present that variant as a result for the setup asked.

## Output Format

Return one report in this numbered structure:

1. **What was compared**: what differs between the variants (prompt, model), each variant's name and session ID, and the run folder.
2. **Measured results**: one row per variant: status, time, model asked and ran, model calls, tokens, cost.
3. **Side by side**: each variant's answer in short, and the important differences: conclusions, reasoning, completeness, omissions, contradictions, clarity.
4. **Judgment**: which answered better on what the user asked for and why, labelled as AI judgment, or that the replies do not support a pick.
5. **What was not validated or could not be fully verified**: unfinished variants, claims in a reply nobody checked, costs not reported.
6. **Obstacles encountered**: script errors verbatim, timeouts, read-only refusals, anything the next run should know.
