---
id: execute-pipeline
name: Execute Pipeline
department: compose
source: vibenode
version: 2.0.0
depends_on: [code-pipeline]
type: prompt-template
---

# Execute Pipeline

Reusable prompt template. Invoke by typing: **execute pipeline**

**Unique value**: None of its own, by design. Execute Pipeline is an alias for **Code Pipeline** (`code-pipeline.md`), so the familiar invocation reaches the one programming workflow whose confidence grade is computed by a program from recorded evidence.

**Why this agent diverges** from the AGENT_BEST_PRACTICES required sections: an alias has no prompt, criteria or output of its own. Every rule lives in `code-pipeline.md`, so a copy here could only drift from it. Version 2.0.0 replaced the prose-enforced pipeline (1.x, in git history) after Code Pipeline passed its qualification run on 2026-10-27.

## Invocation Contract

Exactly the same as Code Pipeline. The caller MUST include in the kickoff prompt:
- the original request verbatim,
- the repository the work happens in (defaults to the current working directory),
- any constraints, deadlines or out-of-scope items the user has already stated,
- pointers to prior context that matter (a spec path, a conversation decision),
- whether this is new work, an unknown-cause bug, a production fire, or cleanup, if known.

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting.

## The Prompt

Run **Code Pipeline**: follow `code-pipeline.md` from Step 0 to Step 7, exactly as if the user had typed "code pipeline". Do not run the older prose-only workflow; it no longer exists. The grade you report is the one `python -m pipeline.harness` prints, and the work counts as verified only when `python -m pipeline.harness <run-dir> --expect HIGH` exits 0.

## Output Format

Return Code Pipeline's final report, in Code Pipeline's numbered structure, including its last two sections:

1. **Grade, Delivered, For you, and the rest of Code Pipeline's report** — as defined in `code-pipeline.md`.
2. **What was not validated or could not be fully verified** — as in Code Pipeline's report.
3. **Obstacles encountered** — as in Code Pipeline's report, including the line written to the ledger.
