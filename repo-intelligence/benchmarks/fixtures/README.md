# Benchmark fixtures

Small synthetic repositories the compiler is tested against. Each is `base/`
plus ordered overlays under `commits/`, with a `golden.json` saying what the
compiler must produce. `repo_intel/compile/fixtures.py` turns one into a real Git
repository with stable commit SHAs, and `python -m repo_intel.compile.golden_check`
compares a compile with the golden.

## The golden

| Key | Meaning |
|---|---|
| `entities` | Entities a correct compile of the base declares: `kind`, `natural_key`, optional `attributes` (each must equal the compiled value), `since` (the milestone from which it is due) |
| `edges` | Assertions: `subject` and `object` (each `kind` and `natural_key`), `predicate`, `since` |
| `absent_entities` | Entities the base must not declare, each with `why` |
| `forbidden_kinds` | Kinds no commit may declare |
| `commits` | One entry per overlay under `commits/`, in order: `dir`, `expect`, and the `added_entities` and `removed_entities` that commit causes |
| `exclusions`, `oversize_not_parsed` | Paths the inventory must report as excluded or too large to parse |
| `granularity` | The deployment granularity label the fixture's design implies, or `null` |
| `expected_unknowns`, `expected_conflicts`, `secret_probes`, `decision_cases` | Read by the tests that need them |

## Multi-repository fixtures

A fixture whose `base/` holds `.rkp/solution.json` (the `rkp-solution/1` format of a
solution's `.rkp/solution.json`) is multi-repository. `base/` is the solution root, laid
out as an owner would lay it out: the manifest, the solution's inputs under
`.rkp/inputs/`, and one directory per member at the `path` the manifest gives it. Members
must be repositories (`path`), not delivered bundles. F23 is the first.

`golden_check` materializes the fixture as usual, then for each commit copies every
member's directory into a Git repository of its own, compiles it into its bundle, loads
the solution from those bundles and resolves identity across them
(`repo_intel.solution`). The golden is read as above, with these differences:

- Any entity, absent entity, edge or commit entry may name a member: `"member": "web"`.
  The solution's own inputs are the member `inputs`. An entry that names a member must be
  in that member; one that does not may be in any member. An edge is checked within one
  member, both ends in it.
- `links`: identity links and candidates across members, each
  `{"link": {"rule", "from", "to"}, "since"}`, where `from` and `to` are
  `{"member", "kind", "natural_key"}` and `rule` is a rule of
  `repo_intel/solution/identity.py` (`same_table`, `endpoint_path_unique`,
  `config_key_name_only`, ...). The rule's name says whether it is exact (E3) or a
  candidate (E4).
- `absent_links`: links that must not be found, each with `why`, for example a shared
  configuration key name that must stay a candidate rather than an exact link.
- `unmatched`: references no member resolves, `{"member", "kind", "reference", "reason", "since"}`
  as `rkp solution links` reports them (`http_call`, `no_endpoint`).
- `solution_coverage`: `{"domain", "scope", "numerator", "denominator", "since"}`, each
  matching the resolution's coverage entry exactly (`solution_data_refs`, `solution_http_calls`).
- A commit entry may also list `added_links`, `removed_links`, `added_unmatched` and
  `removed_unmatched`.
- A problem the loader reports for a member or input fails the check.

`tests/test_fixtures.py` compiles every fixture's tree as one repository, so a
multi-repository fixture must also hold for that: its `granularity` and its
`absent_entities` without a member are checked against the whole tree, and items due at
`M0` are checked there, so multi-repository goldens use `M2` or later. When that test
hands `golden_check.check` its one-repository compile, `check` compiles the members
itself. Entities in `entities` must be unique by kind and natural key across members,
because that test checks for duplicates without members: list the same key in a second
member through `links` instead.

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
