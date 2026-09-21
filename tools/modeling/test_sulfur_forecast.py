"""Unit checks for the training pipeline pieces that pytest does not exercise."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.modeling import sulfur_forecast as sf  # noqa: E402
from tools.modeling.sulfur_features import DYNAMICS_COLUMNS  # noqa: E402


def _stepped_history(days: int = 20, seed: int = 0):
    index = pd.date_range("2025-01-01", periods=24 * 6 * days, freq="10min")
    rng = np.random.default_rng(seed)
    t6 = pd.Series(360.0, index=index)
    f9 = pd.Series(200.0, index=index)
    p13 = pd.Series(3.9, index=index)
    ln_s = pd.Series(np.log(8.5), index=index)
    # Temperature ramps of +5 °C (over 30 minutes) every two days lower ln(S) by 0.2 after one hour.
    for start in range(12 * 6, len(index) - 12 * 6, 48 * 6):
        for k in range(3):
            t6.iloc[start + k:] += 5.0 / 3
        ln_s.iloc[start + 6:] -= 0.2
    analyser = pd.Series(np.exp(ln_s + rng.normal(0, 0.01, len(index))), index=index)
    return analyser, {"T6": t6, "F9": f9, "P13": p13}, index


class ControlResponseTest(unittest.TestCase):
    def test_step_response_recovers_sign_lag_and_uncertainty(self):
        analyser, controls, _ = _stepped_history()
        response = sf.estimate_control_response(analyser, controls, bootstrap=50)
        self.assertGreater(response["T6"]["events"], 5)
        self.assertAlmostEqual(response["T6"]["coefficient"], -0.04, delta=0.01)
        self.assertLessEqual(response["T6"]["lag_minutes"], 120)
        self.assertLessEqual(response["T6"]["ci_80"][0], response["T6"]["coefficient"])
        self.assertGreaterEqual(response["T6"]["ci_80"][1], response["T6"]["coefficient"])
        self.assertIsNone(response["F9"]["coefficient"])

    def test_overlapping_step_events_are_counted_once(self):
        analyser, controls, index = _stepped_history()
        response = sf.estimate_control_response(analyser, controls, bootstrap=10)
        # One ramp per two days, not one event per 10-minute index of the ramp.
        ramps = len(range(12 * 6, len(index) - 12 * 6, 48 * 6))
        self.assertLessEqual(response["T6"]["events"], ramps)

    def test_control_response_ignores_data_after_end(self):
        analyser, controls, index = _stepped_history()
        end = index[len(index) // 2]
        limited = sf.estimate_control_response(analyser, controls, end, bootstrap=10)
        # Reverse the response after ``end``: the estimate before ``end`` must not change.
        flipped = analyser.copy()
        flipped[flipped.index >= end] = 8.5 * np.exp(0.5)
        reversed_response = sf.estimate_control_response(flipped, controls, end, bootstrap=10)
        self.assertEqual(limited["T6"]["events"], reversed_response["T6"]["events"])
        self.assertAlmostEqual(limited["T6"]["coefficient"], reversed_response["T6"]["coefficient"])
        self.assertEqual(limited["T6"]["end"], str(end))


class Stage1Test(unittest.TestCase):
    def test_stage1_target_uses_only_future_of_same_analyser(self):
        index = pd.date_range("2025-01-01", periods=24 * 6 * 3, freq="10min")
        q21 = pd.Series(np.linspace(6.0, 9.0, len(index)), index=index)
        pak = pd.Series(8.0, index=index)
        controls = {"T6": pd.Series(350.0, index=index), "F9": pd.Series(200.0, index=index), "P13": pd.Series(3.8, index=index)}
        data = sf.DynamicsData({"q21": q21, "pak": pak}, controls)
        i = 10
        origin = data.origins[i]
        target = data.targets[("q21", 60)][i]
        window = q21[(q21.index > origin + pd.Timedelta(minutes=30)) & (q21.index <= origin + pd.Timedelta(minutes=90))]
        expected = np.log(window.mean()) - np.log(q21[q21.index <= origin].iloc[-1])
        self.assertAlmostEqual(target, expected, places=9)
        self.assertAlmostEqual(data.targets[("pak", 60)][i], 0.0, places=9)
        # A row whose target window ends after ``before`` is not a training row.
        mask = data.rows("q21", 60, "all", before=origin + pd.Timedelta(minutes=60))
        self.assertFalse(mask[i])
        self.assertTrue(data.rows("q21", 60, "all", before=origin + pd.Timedelta(minutes=91))[i])

    def test_stage1_apply_falls_back_to_persistence_when_inputs_sparse(self):
        model = {"feature_columns": DYNAMICS_COLUMNS, "medians": [0.0] * len(DYNAMICS_COLUMNS), "mean": [0.0] * len(DYNAMICS_COLUMNS),
                 "scale": [1.0] * len(DYNAMICS_COLUMNS), "coef": [0.1] + [0.0] * len(DYNAMICS_COLUMNS), "analyser_column": "ln_q21_raw"}
        full = np.zeros(len(DYNAMICS_COLUMNS))
        delta, fallback = sf.stage1_apply(model, full)
        self.assertAlmostEqual(delta[0], 0.1)
        self.assertFalse(fallback[0])
        sparse = np.full(len(DYNAMICS_COLUMNS), np.nan)
        sparse[DYNAMICS_COLUMNS.index("ln_q21_raw")] = 2.0
        delta, fallback = sf.stage1_apply(model, sparse)
        self.assertEqual(delta[0], 0.0)
        self.assertTrue(fallback[0])

    def test_selection_folds_end_before_fit_end(self):
        for fit_end in sf.WALK_FORWARD_FIT_ENDS:
            folds = [name for name, (_, end) in sf.SELECTION_FOLDS.items() if pd.Timestamp(end) <= pd.Timestamp(fit_end)]
            self.assertTrue(folds, fit_end)
            for name in folds:
                self.assertLessEqual(pd.Timestamp(sf.SELECTION_FOLDS[name][1]), pd.Timestamp(fit_end))
        self.assertGreaterEqual(pd.Timestamp(sf.TEST_START), pd.Timestamp(sf.WALK_FORWARD_FIT_ENDS[-1]))


class Stage2Test(unittest.TestCase):
    def test_convex_stage2_weights_sum_to_one_and_prefer_informative_input(self):
        rng = np.random.default_rng(1)
        y = rng.normal(2.1, 0.2, 400)
        inputs = np.column_stack([y + rng.normal(0, 0.02, 400), y + rng.normal(0, 0.5, 400), rng.normal(2.1, 0.01, 400)])
        model = sf.fit_convex(inputs, y, ["a", "b", "c"], step=0.1)
        self.assertAlmostEqual(sum(model["weights"]), 1.0)
        self.assertTrue(all(w >= 0 for w in model["weights"]))
        self.assertGreaterEqual(model["weights"][0], 0.8)

    def test_huber_ridge_downweights_single_outlier(self):
        rng = np.random.default_rng(2)
        x = rng.normal(0, 1, 300)
        y = 2.0 + 0.5 * x + rng.normal(0, 0.05, 300)
        frame = pd.DataFrame({"x": x})
        clean = sf.StandardizedRidge(1.0).fit(frame, y).coef_[0]
        y[0] += 8.0
        ridge = sf.StandardizedRidge(1.0).fit(frame, y)
        huber = sf.HuberRidge(1.0).fit(frame, y)
        self.assertLess(abs(huber.coef_[0] - clean), abs(ridge.coef_[0] - clean) / 5)


class MetricsTest(unittest.TestCase):
    def test_probability_metrics_are_calibrated_for_perfect_and_climatology(self):
        actual = np.array([12.0, 8.0, 8.0, 11.0, 7.0, 9.0])
        perfect = (actual > 10).astype(float)
        result = sf.probability_metrics(actual, perfect, 0.5)
        self.assertEqual(result["brier"], 0.0)
        self.assertEqual(result["auc"], 1.0)
        self.assertEqual(result["recall_above_10"], 1.0)
        climatology = np.full(len(actual), (actual > 10).mean())
        self.assertAlmostEqual(sf.probability_metrics(actual, climatology, 0.5)["brier_skill"], 0.0)


if __name__ == "__main__":
    unittest.main()
