"""The calibration tool refuses to recommend thresholds from too few labelled cases."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("calibrate", Path(__file__).resolve().parents[1] / "benchmarks" / "calibrate.py")
calibrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calibrate)


def _case(expected, *top):
    return {"expected": expected, "top": [list(t) for t in top]}


def test_outcome_applies_threshold_margin_and_decline():
    assert calibrate.outcome(_case("service", ("service", 0.9), ("worker", 0.05)), 0.8, 0.25) == "correct"
    assert calibrate.outcome(_case("service", ("worker", 0.9), ("service", 0.05)), 0.8, 0.25) == "wrong"
    assert calibrate.outcome(_case("service", ("service", 0.7), ("worker", 0.2)), 0.8, 0.0) == "abstained"
    assert calibrate.outcome(_case("service", ("service", 0.85), ("worker", 0.7)), 0.8, 0.25) == "abstained"
    assert calibrate.outcome(_case("service", ("none_of_these", 0.99)), 0.5, 0.0) == "abstained"


def test_wilson_bound_needs_many_cases_before_it_is_tight():
    assert calibrate.wilson_upper(0, 15) > 0.2
    assert calibrate.wilson_upper(0, 400) < 0.01
    assert calibrate.wilson_upper(0, 0) == 1.0


def test_sweep_recommends_only_with_enough_clean_cases():
    clean = [_case("service", ("service", 0.95), ("worker", 0.03))] * 200 + [_case("service", ("worker", 0.6), ("service", 0.3))] * 5
    rows = calibrate.sweep(clean)
    ok = [r for r in rows if r["error_upper_95"] <= 0.05]
    assert ok and all(r["threshold"] > 0.6 or r["margin"] > 0.3 for r in ok), "only pairs that reject the 0.6 wrong answers qualify"
