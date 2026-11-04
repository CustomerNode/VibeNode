<!-- vibenode: public project file. Keep it short; project notes go in the private notes file below. -->
# VibeNode: rules for AI sessions

VibeNode is an open-source **public repository**. Everything committed is published, often by a one-click Update that nobody reviews first.

## What may be published
- Code, tests, the README, CONTRIBUTING, the agent guides in `src/features/workforce/`, the API docs in `docs/api/`, the screenshots in `docs/screenshots/`.
- **Never**: secrets, keys, tokens or passwords; personal names, paths, emails or machine names; user data, logs, databases, test screenshots or local state; planning docs, specs, notes or working documents.
- **Project notes are private.** Write them in `docs/plans/PROJECT-NOTES.md` (gitignored, loaded below), never in this file. This file holds only these public rules.
- `.gitignore` is the one definition of what is not public. Its top block lists everything allowed at the top level, in `docs/`, and which notes and pictures may be published; anything else is never committed. Do not add an exception to publish a note.
- In code, comments, docs and tests, write the rule and its technical reason. Never quote a person, record who decided or picked something, or date a change (git records when).
- Update's security scan (`src/web/git_scanner.py`) refuses secrets, personal identifiers, swearing, quotes and decision or dated build notes, a tracked file the ignore rules cover, and this file growing past its limit. A session writing such text into the repository is refused at once (`src/engine/publish_guard.py`). Use made-up names in tests and examples.
- If something is even borderline, ask before committing it.

## Servers
- Never restart a server unless the user explicitly asked in this session.
- With permission, restart only the web server: `curl -s -X POST http://localhost:5050/api/restart -H "Content-Type: application/json" -d '{"scope":"web"}'`.
- **Never restart, stop or kill the Session Engine (port 5051)** by any means: it runs every session. If asked, warn the user and point them to System → Developer Tools → Restart Server → Session Engine.
- No direct process management (no kill, taskkill, spawning terminals).

## Files
- The shape is fixed (`tests/test_repo_shape.py`): at the top only `docs/`, `scripts/`, `src/`, `tests/` and eleven files. `session_manager.py` and `reviver.py` at the top are how every installed copy starts VibeNode and its recovery page (shortcuts, sign-in entries, boot tasks): never move them. Code lives in `src/`: `engine` (the Session Engine), `web` (the web server, its pages and browser code), `shared` (code both use), `launcher` (start and stop), `features` (Code Pipeline, experiments, the agent library). Run code from `src/` or with it on the path (`python -m engine.x` from `src/`); start the app with `python vibenode.py`.
- Plans, specs and working docs go in `docs/plans/` (gitignored), never the repo root.
- Code near a `PERF-CRITICAL` marker was profiled: understand the reason in its comment before changing it, and ask if unsure.
- Before editing a `.md` file in `src/features/workforce/`, read `src/features/workforce/AGENT_BEST_PRACTICES.md`.

## Private project notes (loaded when present)
@docs/plans/PROJECT-NOTES.md
