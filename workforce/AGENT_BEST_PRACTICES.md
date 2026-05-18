# Agent Authoring Best Practices

This file is the standard for every prompt-template agent in `workforce/`. When you create a new agent here, or edit an existing one, your output MUST follow these rules. They are derived from real subagent failure modes documented by the Claude Code team and adapted to VibeNode's workforce mechanism (catalog-injected prompts via `/api/workforce/assets`).

Read this file before touching any `workforce/*.md` file. If a rule conflicts with a specific agent's needs, document the divergence in that agent's file with a clear "Why this agent diverges" note — do not silently violate the standard.

---

## Why these rules exist

Every workforce agent is loaded into the parent Claude session's system prompt at runtime. The parent uses the agent's body to decide **when** to spawn a subagent and **what** to write in the kickoff prompt. The subagent then operates in an isolated context window, returns a summary, and that summary is the only artifact the parent sees.

This architecture has four reliable failure modes:

1. **Vague descriptions** → parent writes vague kickoff prompts → subagent wastes context re-deriving what it should have been told.
2. **No defined output format** → subagent has no stopping point → runs long, returns shapeless prose the parent cannot parse.
3. **No obstacle reporting** → workarounds and env quirks die in the subagent's context → the next stage rediscovers them at cost.
4. **Test/result summarization** → diagnostic detail compresses into "tests failed" → parent must rerun to debug.

Every rule below targets one of these failure modes.

---

## Required sections (in order)

Every prompt-template agent in `workforce/` MUST contain these sections, in this order:

```
---
id: <kebab-case-id>
name: <Human Name>
department: compose
source: vibenode
version: <semver>
depends_on: [<other-agent-ids>]
type: prompt-template
---

# <Human Name>

Reusable prompt template. Invoke by typing: **<lowercase invocation>**

**Unique value**: <one sentence — what failure class this agent catches that no other agent catches>

## Invocation Contract

The caller MUST include in the kickoff prompt:
- <thing 1>
- <thing 2>
- <thing 3 …>

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting.

## The Prompt

<the actual instructions to the subagent — what to do, how to think>

## Standing Criteria  (or equivalent — "Standing Review Criteria", "Standing Hotfix Criteria", etc.)

<the non-negotiable checks this agent always performs>

## Fix Policy  (or "Escalate when" — whichever fits)

<when to auto-fix vs. when to escalate>

## Output Format

Return one combined team report in this numbered structure:

1. <section>
2. <section>
…
N-1. **What was not validated or could not be fully verified** — Blind spots, skipped areas, env limits.
N.   **Obstacles encountered** — Setup issues, workarounds discovered, commands that needed special flags or configuration, dependencies or imports that caused problems, env quirks. Report anything the next stage would otherwise have to rediscover.
```

Confidence rating (`HIGH / MEDIUM / LOW` with one-sentence rationale) is required for any agent that produces a judgment about whether work is shippable.

---

## Rule 1: Unique value statement (one sentence)

Every agent's body must open with a `**Unique value**:` line that names the specific failure class this agent catches. If you cannot complete the sentence "No other agent catches X," the agent does not earn its slot — merge it into an existing agent or delete it.

**Good:** "Catches integration failures, workflow breakage, concurrency issues, and performance regressions through broad validation."
**Bad:** "Helps with testing." (Vague. Does not identify a unique failure class.)

---

## Rule 2: Invocation Contract (mandatory, top of agent body)

Every agent must have an `## Invocation Contract` section listing exactly what the caller must include in the kickoff prompt. This is the single biggest lever for steering the parent agent's behavior — the catalog injects the agent body into the parent's system prompt, so the contract is read every time the parent decides to delegate.

**The contract must:**
- Be 3–6 specific bullet points. Fewer is too vague; more is unread.
- Name concrete inputs, not abstract qualities. "the spec path" beats "context about the spec."
- End with a fallback sentence: "If any of these are missing and cannot be inferred reliably from conversation context, request them before starting."

**Anti-patterns:**
- "The caller should provide enough context." (Useless. Specify what counts as enough.)
- "Include any relevant files." (Useless. Which files?)
- A contract that overlaps with `## The Prompt`. The contract is about **what to pass in**; The Prompt is about **what to do with it**.

---

## Rule 3: Output format = numbered sections (mandatory)

Every agent must have an `## Output Format` section that defines the return structure as a **numbered list**. Numbered sections create explicit stopping points — the agent knows it's done when every section is filled.

**Required final two sections** (in this order, at the end of every agent's output format):
- **What was not validated or could not be fully verified** — Blind spots and non-completion.
- **Obstacles encountered** — Setup issues, workarounds discovered, commands that needed special flags or configuration, dependencies or imports that caused problems, env quirks. Report anything the next stage would otherwise have to rediscover.

These are different concerns. "What was not validated" is about **scope coverage**. "Obstacles encountered" is about **practical hacks the next stage needs to know about**. Do not collapse them into one section.

---

## Rule 4: Raw output discipline for any agent that runs tests, queries, or external commands

When an agent runs a command whose output is **diagnostic** (test failures, query results, log greps), the report MUST include the raw verbatim output for failures or unexpected results. Summarizing diagnostic output is the single worst subagent failure mode — the parent cannot debug what is hidden.

Rules for diagnostic output:
1. **Failures or errors → include verbatim.** Full stack trace, full assertion message, full stderr. Do not paraphrase.
2. **Successes → summarize freely.** One-line counts are fine.
3. **Flaky results → include at least one failing run's raw output AND note the flake.** Do not silently retry until green.
4. **Document non-default commands.** If you ran `pytest -x --tb=long`, say so in Obstacles so the next stage can reproduce.

This rule is enforced explicitly in Test Team. Any new agent that runs diagnostic commands MUST adopt the same rule.

---

## Rule 5: Don't claim expertise — claim a frame

The "expert persona" anti-pattern (e.g. "you are a Python expert") adds no value. Claude already has the knowledge. What an agent CAN add is a **distinct frame** that changes the questions being asked.

**Bad:** "You are an expert in performance optimization."
**Good:** "You evaluate every change from the perspective of someone who uses VibeNode 8 hours a day." (Compose Expert User does this — different lens, different questions.)

Agents with multiple internal roles ("Run the Plan Team: Spec Analyst, Product Strategist, Architect…") are fine. Those are **role frames within one agent**, not separate subagents. The anti-pattern is spawning a subagent whose only differentiator is "expert."

---

## Rule 6: Avoid sequential subagent pipelines unless artifacts are on disk

Multi-stage pipelines where each stage depends on the previous stage's discoveries lose information at every handoff — the summary returned to the parent strips out detail the next stage needs.

**Mitigation pattern** (used in Execute Pipeline):
- Each stage writes its full findings to a numbered file in `docs/plans/runs/<run-id>/`.
- The next stage reads the prior artifact in full, not just the in-context summary.

If you build a new pipeline-style agent, you MUST adopt this pattern. The artifact directory should be:

```
docs/plans/runs/<YYYY-MM-DD-HHMM>-<short-slug>/
  00-request.md
  01-<stage>.md
  02-<stage>.md
  ...
```

`docs/plans/` is gitignored — these artifacts are safe to write freely. Reference them by path in the final pipeline report so the user can dig into any stage without rerunning.

---

## Rule 7: Tool restrictions are encoded in prose

The VibeNode workforce mechanism does not have a YAML `tools:` field, so any restriction on what a subagent should touch must be written explicitly in The Prompt. Examples:

- Read-only agents: "Do not modify code files. Read and analyze only."
- Plan-stage agents: "Write to `docs/plans/` only. Do not modify source code."
- Audit agents: "Fix problems that are clear, low-risk, and unambiguous. For anything requiring judgment or with broader risk, do not fix — escalate."

State the restriction once, in clear language, near the top of The Prompt or in the Fix Policy. Repeating it in three places is noise.

---

## Rule 8: Versioning and depends_on

Every agent file has a YAML `version:` field. Bump it when you make a substantive change:
- Patch (1.0.0 → 1.0.1): typo fix, wording tightening, no behavior change.
- Minor (1.0.0 → 1.1.0): added section, added rule, expanded scope.
- Major (1.0.0 → 2.0.0): changed output format, removed required behavior, breaking change for callers that depend on the prior structure.

The `depends_on` field lists other agent IDs this agent invokes or builds on. Keep it accurate — Execute Pipeline reads this to construct its workflow.

---

## Rule 9: Standing criteria belong in the agent, not in the kickoff prompt

If an agent always evaluates the same checklist (CLAUDE.md compliance, PERF-CRITICAL preservation, public-repo safety, lifecycle states), encode that checklist in a `## Standing Criteria` section within the agent body. Do not require the caller to remember these — the catalog injects the agent body, so standing criteria are always in context.

Standing criteria should be **non-negotiable**. If a check is optional, it belongs in The Prompt or the Fix Policy, not Standing Criteria.

---

## Rule 10: One agent = one failure class

Every agent must remove a **distinct class of failure** the other agents cannot remove. If two agents repeatedly catch the same issues, one of them is redundant — merge, narrow, or delete the weaker one.

This is enforced at the top of Execute Pipeline's TEAM UNIQUE VALUE block. When you add a new agent to `workforce/`, you must be able to add it to that list with a one-sentence "catches X failures that nothing else catches" statement.

---

## When you edit an existing agent

1. Read this file first.
2. Read the existing agent file in full — do not edit blind.
3. Bump the `version:` field per Rule 8.
4. Preserve the agent's distinct frame (Rule 5) and unique value (Rule 10).
5. If your edit changes the output format, also update any agent in `depends_on` that consumes the output.
6. After saving, re-read the file to confirm the edit landed correctly and the structure is intact.

## When you create a new agent

1. Read this file first.
2. State the unique value in one sentence (Rule 1). If you can't, stop — the agent doesn't earn a slot.
3. Use the template in "Required sections" above. Every section is required unless you have a documented reason to diverge.
4. Add the new agent to Execute Pipeline's TEAM UNIQUE VALUE list and routing rules if applicable.
5. Test the agent by invoking it on a real task and reading the output. If the output is shapeless or runs too long, the format is wrong.

---

## What this file is not

- Not a place to list all agents. The runtime catalog (built from `workforce/*.md` by `/api/workforce/assets`) is the authoritative list.
- Not the spec for the workforce loader. That lives in `app/routes/live_api.py` (`get_workforce_assets`).
- Not advice for one-off Agent tool invocations in the middle of a session. Those don't go through this catalog. This file is specifically for the reusable prompt-template agents stored in `workforce/`.
