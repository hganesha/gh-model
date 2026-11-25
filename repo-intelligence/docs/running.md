# Running and testing

Commands run from `repo-intelligence/`.

## Install and test

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m pytest -q
```

The projection and API tests start a throwaway PostgreSQL cluster from the local `initdb` and remove it afterwards. They are skipped if PostgreSQL binaries are absent.

The Temporal test is opt-in because the SDK downloads a Temporal dev server on first use:

```bash
RKP_TEMPORAL_TESTS=1 .venv/bin/python -m pytest -q tests/test_workflow.py
```

Check what the compiler produces against the goldens:

```bash
.venv/bin/python -m repo_intel.compile.golden_check
```

Incremental/cold parity (every seeded commit compiled in a warm store and in a fresh one must match) and the sandbox self-check:

```bash
.venv/bin/python -m repo_intel.knowledge.parity
```

```bash
.venv/bin/python -m repo_intel.compile.step_worker --selfcheck
```

Live model checks send synthetic data only. They read provider settings from the environment, so load `.rkp-store/providers.env` first (Azure models also need `az login`); the Jev check needs `RKP_JEV_API_KEY`:

```bash
set -a; . .rkp-store/providers.env; set +a
```

```bash
.venv/bin/python -m repo_intel.decisions.live_check microsoft-decision
```

```bash
RKP_LLM_DEPLOYMENT=gpt-4-1-mini .venv/bin/python -m repo_intel.decisions.live_check llm
```

Score every fixture component with a decision model, then see which threshold and margin the labelled results support:

```bash
.venv/bin/python benchmarks/fixture_eval.py microsoft-decision benchmarks/results/component-kind-<model>-<date>.json
```

```bash
.venv/bin/python benchmarks/calibrate.py benchmarks/results/component-kind-<model>-<date>.json
```

The same for DM11 (deployment granularity), against the label each fixture's golden carries:

```bash
.venv/bin/python benchmarks/fixture_eval.py --decision deployment-granularity microsoft-decision benchmarks/results/deployment-granularity-<model>-<date>.json
```

After changing a contract, regenerate the schemas:

```bash
.venv/bin/python -m repo_intel.compile.export_schemas
```

## Run the API and the Explorer

Two processes, both on localhost only. The API starts its own private PostgreSQL under `.rkp-store/` and creates a random local token there; the Explorer's server side reads that token, so it never reaches the browser.

```bash
.venv/bin/python -m repo_intel.server.serve
```

```bash
npm --prefix explorer install && npm --prefix explorer run dev
```

Then open http://localhost:3100, choose **Connect repository** and enter a GitHub URL or `owner/name`. The repository is fetched through the GitHub CLI session on this machine (`gh auth login` first if needed). The default setup uses one local administrator token. Connection and webhook configuration is described below.

First real repository tried (public, `fastapi/full-stack-fastapi-template`, 239 files, 2026-11-23): 4 components, 23 endpoints all linked to handlers, 2 tables, 58 tests; imports resolved 671 of 685; data access linked 34 of 37.

## Browser verification

Run the browser suite from the Explorer directory after installing the Python dev dependencies. The suite starts isolated local API and PostgreSQL processes and compiles a bundled fixture.

~~~bash
npx playwright install chromium
npm run test:e2e
~~~

The suite also builds the Explorer for production and runs it twice with no API: once replaying the recorded fixture responses in `e2e/recorded` (every plane must render, and any write is refused), and once with neither sign-in nor `RKP_ALLOW_ANONYMOUS=1` (it must refuse every request). After a change to what the views request, record again:

~~~bash
RKP_RECORD=1 npx playwright test --project=record
~~~

`RKP_API_REPLAY=<dir>` runs any Explorer from recordings, for a demo without an API.
