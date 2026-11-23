# Business capability placement benchmark

The [recorded run](business-placement-v1.json) labels 21 components from ten fixture bases and three pinned public repositories. Fifteen components had a kind eligible for the default policy. Thirteen of those had a positive reference label on the 12-capability sample map.

| Check | Result |
|---|---:|
| Expected capability in the local top five | 9 of 13 eligible positive cases |
| Rules-only accepted and correct | 0 |
| Rules-only accepted and wrong | 0 |
| Rules-only abstained | 15 |

The four shortlist misses put an upper bound of 9 correct placements out of 13 eligible positive cases for this sample with the local top-five shortlist. Human labels select a primary capability for each component and represent one review of a small fixture-heavy sample.

No decision-model provider was configured in the benchmark environment. There is no calibrated automatic-placement threshold from this run. Use **suggestions only** when first evaluating a live provider. Compare accepted-and-wrong, accepted-and-correct, and abstentions at several thresholds on reviewed labels before relying on automatic decisions. The configured threshold remains provisional at 0.88 with a minimum margin of 0.20 and two option permutations.

The public samples were [Full Stack FastAPI Template](https://github.com/fastapi/full-stack-fastapi-template), [Express ES6 REST API](https://github.com/developit/express-es6-rest-api), and [React Native NW React Calculator](https://github.com/benoitvallon/react-native-nw-react-calculator). The result file contains the exact commit IDs. An earlier candidate public sample tripped the publish gate on an example environment file. The three recorded samples completed with the normal publish gate.

~~~bash
.venv/bin/python -m repo_intel.decisions.placement_benchmark --public
.venv/bin/python -m repo_intel.decisions.placement_benchmark --public --model microsoft-decision --threshold 0.88
~~~

The second command requires a configured model alias and an explicit `RKP_ALLOW_PROVIDERS` entry. It sends bounded features from the public samples and the synthetic fixtures to that provider.
