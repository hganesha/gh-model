# Business capability placement benchmark

The [latest recorded run](business-placement-v2.json) labels 21 components from ten fixture bases and three pinned public repositories. Fifteen components had a kind eligible for the default policy. Thirteen of those had a positive reference label on the 12-capability sample map.

| Check | [First run](business-placement-v1.json) | [Latest run](business-placement-v2.json) |
|---|---:|---:|
| Expected capability in the local top five | 9 of 13 | 12 of 13 |
| Expected capability ranked first | 6 of 13 | 12 of 13 |
| Rules-only accepted and correct | 0 | 0 |
| Rules-only accepted and wrong | 0 | 0 |
| Rules-only abstained | 15 | 15 |

Between the two runs the shortlist text changed. It now describes what a component does (its name, routes, data and repository description) and leaves out field names and technical capabilities, which every component shares. Words are stemmed and words about how a component is built or addressed ("api", "service", "get") are ignored. The commits and labels are the same in both runs.

The one remaining miss is the `frontend` component of the Full Stack FastAPI Template, whose facts carry no wording about users or sign-in. The shortlist is still lexical: it matches words, not meaning, so a capability described in different vocabulary from the code will be missed. Human labels select a primary capability for each component and represent one review of a small fixture-heavy sample.

## With a decision model

Run on 2026-11-23 against Microsoft-Decision-1.1 on Azure, on the same 15 eligible components ([result at 0.88](business-placement-v3-microsoft-decision.json)). Two of the 15 have no fitting capability on the sample map, so declining is the correct answer for them.

| Threshold | Placed and correct | Placed and wrong | Left for review |
|---|---:|---:|---:|
| 0.88 (the default) | 7 | 0 | 8 |
| 0.75 | 7 | 0 | 8 |
| 0.60 | 10 | 1 | 4 |
| 0.45 | 10 | 1 | 4 |

At 0.88 the model declined both components that have no fitting capability. Of the six labelled components it left for review, four scored below the threshold and two it judged to fit none of the candidates. The single wrong placement at 0.60 was `@acme/web` under Service monitoring at 0.61; its only route is a health check, and the reference label is Order fulfilment.

The default threshold of 0.88 is kept. On this sample it gave no wrong placements, and lowering it to 0.75 gained nothing. This is 15 components and one reviewer's labels: enough to show the path works and the threshold is not reckless, not enough to state an accuracy.

The public samples were [Full Stack FastAPI Template](https://github.com/fastapi/full-stack-fastapi-template), [Express ES6 REST API](https://github.com/developit/express-es6-rest-api), and [React Native NW React Calculator](https://github.com/benoitvallon/react-native-nw-react-calculator). The result file contains the exact commit IDs. An earlier candidate public sample tripped the publish gate on an example environment file. The three recorded samples completed with the normal publish gate.

~~~bash
.venv/bin/python -m repo_intel.decisions.placement_benchmark --public
.venv/bin/python -m repo_intel.decisions.placement_benchmark --public --model microsoft-decision --threshold 0.88
~~~

The second command requires a configured model alias and an explicit `RKP_ALLOW_PROVIDERS` entry. It sends bounded features from the public samples and the synthetic fixtures to that provider. For Azure, set `RKP_MSDECISION_ENDPOINT`, `RKP_MSDECISION_DEPLOYMENT` and `RKP_MSDECISION_AUTH=azure-cli`, and install the `azure` extra.
