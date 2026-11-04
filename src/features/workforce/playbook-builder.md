---
id: playbook-builder
name: Playbook Builder
department: compose
source: vibenode
version: 2.1.2
depends_on: []
type: prompt-template
playbook: true
playbook_risk: medium
playbook_capabilities: [read, write, run_commands]
playbook_line: Builds and fixes playbooks to the standard
playbook_what: Drafts a new playbook or a change, checks it against VibeNode's Playbook Standard, and registers it only when it passes.
playbook_say: playbook builder make a playbook that drafts release notes | playbook builder fix the AI Compare playbook
playbook_how: Reads the Playbook Standard and any playbook it is changing. | Lists the playbook's capabilities and sets its risk from them. | Writes a draft outside the library. | Runs VibeNode's check on the draft and fixes every failure. | Registers it through VibeNode, which checks it again. | Reports the playbook, its risk and the check's output.
playbook_use: Use playbook builder
---

# Playbook Builder

Reusable prompt template. Invoke by typing: **playbook builder** (also "build a playbook", "fix the … playbook")

**Unique value**: Creates and changes VibeNode playbooks so they pass the Playbook Standard before they reach the library, with the check's own output as proof. No other playbook writes playbooks.

The standard lives in VibeNode, not here: `src/shared/playbook_standard.py`. This playbook holds no rules of its own; it runs that program. VibeNode refuses to register a playbook that fails it, serves a failing one to no session, and fails its test suite on one, whether or not this playbook wrote it. This playbook is the easy way to pass; it is not the gate.

A playbook may be instructions only (a way to think about a task) or it may coordinate scripts and tools. Both are built the same way; the risk level decides how much the standard asks for, so a simple talk-only playbook stays short.

## Invocation Contract

The caller MUST include in the kickoff prompt:
- what the playbook should do, in the user's words, or the playbook to change (its name or id);
- the words the user wants to type to start it, if the user gave any;
- anything it must never touch or send, if the user said;
- for a change: what is wrong with it now or what should be different, and the new name if it is a rename.

If any of these are missing and cannot be inferred reliably from conversation context, request them before starting.

## The Prompt

Run this in the session the user typed it in, from the VibeNode repository folder.

1. Print the standard: `python -m shared.playbook_standard --explain`. It lists the seven parts, the capabilities and the lowest risk each allows. Read `src/features/workforce/AGENT_BEST_PRACTICES.md` too. For a change, read the playbook in full first.
2. Work out the playbook's parts with the user's request in front of you: its identity (id, name, the words that start it), its purpose (the one thing no other playbook does), the inputs a caller must give, the steps it follows, what it hands back, how it checks its own result, and when it stops and asks.
3. List every capability its steps need in `playbook_capabilities`, using only the names `--explain` prints. Then set `playbook_risk` to at least the highest risk among them. The check refuses a risk below the declared capabilities, and refuses a step that shows a capability not declared. Write a prohibition right before the action it forbids ("Do not delete …", "Never push …"). A sentence that only says to skip asking first is an instruction to act, and is counted as one.
4. Write the draft as `docs/plans/playbook-drafts/<id>.md`, where `<id>` is the kebab-case name. That folder is outside the library and is not published. Copy the layout of `src/features/workforce/ai-compare.md`. Keep what Settings shows short: `playbook_line` at most 8 words, `playbook_what` one sentence of at most 22, `playbook_how` 3 to 6 steps of at most 14 words each, separated by `|`. `playbook_say` holds at least two phrases the user would really type, separated by `|`. For a change, start the draft from the current file and bump `version:` (patch for wording, minor for a new section or rule, major for a changed output format).
5. Run `python -m shared.playbook_standard docs/plans/playbook-drafts/<id>.md`. It checks the draft against the standard and against every playbook already in the library (no shared id, name or starting words). Fix every line it prints and run it again until it says `ok`. Fix a failure by changing the playbook so it is true, never by lowering its risk, dropping a capability it uses, or removing a check it needs.
6. Register it: `python -m shared.playbook_standard --register docs/plans/playbook-drafts/<id>.md`. The program checks it again and copies it into `src/features/workforce/` only when it passes; on a failure nothing in the library changes. To rename a playbook (a new id taking over from an old one), add `--replaces <old id>`: the old playbook leaves the library in the same step, only when the draft passes, and the draft's version must be higher than the old one's.
7. Run `python -m shared.playbook_standard` with no file to confirm every playbook in the library still passes.

A playbook is not a Code Pipeline team, so the authoring standard's step of adding a new agent to Code Pipeline's list of teams does not apply.

Do not edit any file except the draft and, through `--register`, the playbook it registers. Never edit `src/shared/playbook_standard.py` to make a playbook pass, never write into `src/features/workforce/` by hand, and never remove a playbook unless the user asked by name.

## Standing Criteria

- The final check's output is quoted verbatim in the report; "it should pass" is not a result.
- Every capability the playbook's steps need is declared, and the risk is at least the highest of them.
- The playbook names one thing no other playbook in the library does (Rule 10 of the authoring standard); if one already does it, say so instead of writing a duplicate.
- No secrets, personal paths or user data in the playbook: the library ships with the public repository.

## Escalate when

- the playbook would duplicate one that exists: ask whether to extend that one instead;
- the user asks for a rule the standard does not hold (a new required part or capability): the standard is VibeNode's code and changes go through a code change, not through this playbook;
- the check fails in a way no true edit to the playbook can fix: report its output verbatim.

## Output Format

Return one report in this numbered structure:

1. **Playbook**: its name, id, version and the words to type.
2. **Capabilities and risk**: what it declares and why each is needed.
3. **Changes**: created, or what changed and why.
4. **Standard check**: the output of the registration and the library check, verbatim.
5. **What was not validated or could not be fully verified**: for example, the playbook has not yet been run on a real task.
6. **Obstacles encountered**: anything the next build should know.
