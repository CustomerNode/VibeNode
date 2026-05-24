# E2E Test Setup

## Quick reference

| What | Where |
|---|---|
| Workflow | `.github/workflows/e2e-tests.yml` |
| Fixtures | `tests/e2e/conftest.py` |
| Test files | `tests/e2e/test_*.py` |
| Required secret | `ANTHROPIC_API_KEY_E2E` (recommended) or `ANTHROPIC_API_KEY` (fallback) |

## Configuring the API key

CI passes `ANTHROPIC_API_KEY` to pytest so tests can drive the Claude SDK.
Prefer a **dedicated low-budget test key** so a runaway test (infinite loop,
pathological prompt) can't drain the production wallet.

### Set it up

1. Create a budget-capped key at <https://console.anthropic.com/settings/keys>:
   - Click **Create Key**
   - Name it `vibenode-e2e` (or similar)
   - Under **Workspace**, set a low monthly spend limit (e.g., `$5`)
2. Add it to the repo as a GitHub secret named `ANTHROPIC_API_KEY_E2E`:
   - GitHub web UI: **Settings → Secrets and variables → Actions → New repository secret**
   - Or via `gh` CLI:

     ```bash
     gh secret set ANTHROPIC_API_KEY_E2E --body "sk-ant-..." --repo <owner>/<repo>
     ```

The workflow prefers `ANTHROPIC_API_KEY_E2E` and falls back to
`ANTHROPIC_API_KEY` with a CI warning if the dedicated key isn't set.

### Verify

After setting the secret, the next E2E run should not print:
> `::warning::ANTHROPIC_API_KEY_E2E secret not set ...`

If it still appears, double-check the secret name (case-sensitive) and the
scope (repo-level, not environment-level).

## Running E2E locally

```bash
pip install -r requirements-test.txt
pytest tests/e2e -m e2e -v --timeout=300
```

The fixture in `conftest.py` spins up a separate VibeNode server stack on
ports 5098/5099 (production runs on 5050/5051), so this is safe to run
against your live instance.

To skip E2E entirely:

```bash
SKIP_E2E=1 pytest tests/e2e -m e2e
```

## Artifacts

Failures save artifacts to `tests/screenshots/` (gitignored):

- `*.png` — full-page screenshot at the failure moment
- `*.log` — browser console output
- `test_server.log`, `test_daemon.log` — server-side logs

CI uploads these as workflow artifacts on failure.
