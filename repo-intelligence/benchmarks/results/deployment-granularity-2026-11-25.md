# DM11 deployment granularity against a live decision model (2026-11-25)

`benchmarks/fixture_eval.py --decision deployment-granularity microsoft-decision`: every fixture whose golden has a granularity label (11 of 19; the rest deploy no application), with all four labels offered. A compile offers only the candidates the rules leave open, so this is harder than production. Only structured features are sent; the fixtures are synthetic.

| Run | Criteria | Answered | Wrong | Top choice right | Ambiguous cases (top choice) |
|---|---|---|---|---|---|
| `deployment-granularity-microsoft-decision-1.1-2026-11-25.json` | v1 | 8 of 11 | 0 | 9 of 11 | F02 microservices (expected service-based), F17 monolithic at 0.79 (expected microservices) |
| `deployment-granularity-microsoft-decision-1.1-criteria-v2-2026-11-25.json` | v2 | 6 of 11 | 0 | 10 of 11 | F02 service-based at 0.69, F17 declined (none_of_these) |

The first run was wrong on both cases the rules leave ambiguous, the only ones a compile asks the model about. F17's wrong answer, monolithic at 0.79, missed the 0.80 accept threshold by 0.01. Its two serverless functions reach the model as `unlinked_functions`, which v1's instructions never explained. Criteria v2 (spec 1.1.0) explains every feature. After that F02's top choice is right, and the model declines F17 instead of guessing. Confidence on the clear microservices fixtures fell (F07 0.64, F16 0.68), but the rules resolve those and never ask the model.

`calibrate.py` on either run recommends nothing: the tightest 95% error bound is 32% (v1) and 30% (v2). The accept threshold (0.80) and margin (0.25) stay until the reviewed corpus (GI-026) has enough cases.
