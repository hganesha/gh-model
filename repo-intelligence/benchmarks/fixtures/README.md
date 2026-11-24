# Benchmark fixtures

Small synthetic repositories the compiler is tested against. Each is `base/`
plus ordered overlays under `commits/`, with a `golden.json` saying what the
compiler must produce. `repo_intel/compile/fixtures.py` turns one into a real Git
repository with stable commit SHAs.

## Planted test data: nothing here is real

Some fixtures contain adversarial content on purpose. It exists so the tests
can prove the compiler handles it safely.

| What | Where | Why |
|---|---|---|
| Fake AWS access key ID (`AKIAIOSFODNN7EXAMPLE`, the example value from AWS documentation) | `F01-python-api/base/.env.example`, repeated in that fixture's `golden.json` under `secret_probes` and in `tests/test_secrets.py` | Must be detected at admission and never appear in compiler output or evidence excerpts |
| Made-up Anthropic-style key (`sk-ant-fixture-EXAMPLE-000…`) | `F08-ai-agent/base/.env.example`, repeated in its `golden.json` | Same |
| Prompt-injection text addressed to AI tools | `F01` and `F06` READMEs, `F08` `prompts/system.md`, a file name in `F10` | Must be treated as data and change nothing |
| Symlinks pointing outside the repository | `F01` and `F10` (created when the fixture is materialized) | Must be recorded and never followed |
| A submodule entry with a dummy commit ID | `F10` | Must be recorded and never fetched |

These paths are excluded from GitHub secret scanning in
`.github/secret_scanning.yml`. If another scanner flags them, they are safe to
allow as test data. Do not put real credentials in a fixture.
