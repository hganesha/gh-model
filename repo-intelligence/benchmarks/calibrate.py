"""Choose a decision model's accept threshold and margin from labelled results.

    python benchmarks/calibrate.py results.json [results.json ...] [--max-error 0.05]

Each input is a list of labelled cases in the format `fixture_eval.py` writes: `expected`
(the reviewed label) and `top` (the model's per-option scores, highest first). For every
(threshold, margin) pair on a grid it counts accepted-and-correct, accepted-and-wrong and
abstained, and gives a 95% Wilson upper bound on the error rate of accepted answers.

It recommends the pair that accepts the most correct answers while that upper bound
stays under `--max-error`. With too few labelled cases no pair qualifies, and it says so
instead of recommending one: zero errors in 15 cases still allows an error rate near 20%.
Thresholds in the decision specs should change only on a recommendation from the
reviewed corpus (GI-026), not from synthetic fixtures.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

DECLINE = "none_of_these"


def wilson_upper(errors: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 1.0
    p = errors / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + spread) / (1 + z * z / n))


def outcome(case: dict, threshold: float, margin: float) -> str:
    top = case.get("top") or []
    if not top:
        return "abstained"
    label, score = top[0]
    second = top[1][1] if len(top) > 1 else 0.0
    if label == DECLINE or score < threshold or score - second < margin:
        return "abstained"
    return "correct" if label == case["expected"] else "wrong"


def sweep(cases: list[dict]) -> list[dict]:
    rows = []
    for t in [x / 100 for x in range(50, 100, 5)]:
        for m in [x / 100 for x in range(0, 55, 5)]:
            counts = {"correct": 0, "wrong": 0, "abstained": 0}
            for case in cases:
                counts[outcome(case, t, m)] += 1
            accepted = counts["correct"] + counts["wrong"]
            rows.append({"threshold": t, "margin": m, **counts, "error_upper_95": round(wilson_upper(counts["wrong"], accepted), 3)})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--max-error", type=float, default=0.05, help="largest acceptable 95%% upper bound on the error rate of accepted answers")
    args = parser.parse_args()
    cases = [c for path in args.results for c in json.loads(path.read_text()) if c.get("expected")]
    rows = sweep(cases)
    print(f"{len(cases)} labelled case(s)")
    print("threshold margin  correct wrong abstained  error<=(95%)")
    for r in rows:
        if r["margin"] in (0.0, 0.1, 0.25):
            print(f"  {r['threshold']:.2f}    {r['margin']:.2f}     {r['correct']:>3}    {r['wrong']:>3}    {r['abstained']:>3}       {r['error_upper_95']:.3f}")
    ok = [r for r in rows if r["error_upper_95"] <= args.max_error]
    if not ok:
        best = min(rows, key=lambda r: (r["error_upper_95"], -r["correct"]))
        print(f"\nNo pair keeps the error bound under {args.max_error:.0%}; the tightest is {best['error_upper_95']:.0%} "
              f"(threshold {best['threshold']:.2f}, margin {best['margin']:.2f}). More labelled cases are needed; keep the current thresholds.")
        return 1
    best = max(ok, key=lambda r: (r["correct"], r["threshold"], r["margin"]))
    print(f"\nRecommended: accept_threshold {best['threshold']:.2f}, min_margin {best['margin']:.2f} "
          f"({best['correct']} correct, {best['wrong']} wrong, {best['abstained']} abstained; error <= {best['error_upper_95']:.1%} at 95%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
