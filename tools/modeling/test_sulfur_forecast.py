"""Unit checks for the training pipeline pieces that pytest does not exercise."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.modeling import sulfur_forecast as sf  # noqa: E402


class ControlResponseTest(unittest.TestCase):
    def test_future_controls_and_analyser_cannot_change_fitted_response(self):
        index = pd.date_range("2025-12-01", "2026-02-01", freq="10min")
        rng = np.random.default_rng(42)
        analyser = pd.Series(8 + rng.normal(0, .2, len(index)), index=index)
        controls = {"T6": pd.Series(360., index=index), "F9": pd.Series(200., index=index), "P13": pd.Series(4., index=index)}
        controls["T6"].loc["2025-12-10":] += 5
        original = sf.estimate_control_response(analyser, controls)
        analyser.loc["2026-01-01":] = 90
        controls["T6"].loc["2026-01-01":] = 390
        self.assertEqual(original, sf.estimate_control_response(analyser, controls))

    def test_step_response_recovers_sign_and_lag(self):
        index = pd.date_range("2025-01-01", periods=24 * 6 * 20, freq="10min")
        rng = np.random.default_rng(0)
        t6 = pd.Series(360.0, index=index)
        f9 = pd.Series(200.0, index=index)
        p13 = pd.Series(3.9, index=index)
        ln_s = pd.Series(np.log(8.5), index=index)
        # Temperature steps of +5 °C every two days lower ln(S) by 0.2 after one hour.
        for start in range(12 * 6, len(index) - 12 * 6, 48 * 6):
            t6.iloc[start:] += 5.0
            ln_s.iloc[start + 6:] -= 0.2
        analyser = np.exp(ln_s + rng.normal(0, 0.01, len(index)))
        response = sf.estimate_control_response(analyser, {"T6": t6, "F9": f9, "P13": p13})
        self.assertGreater(response["T6"]["events"], 5)
        self.assertAlmostEqual(response["T6"]["coefficient"], -0.04, delta=0.01)
        self.assertLessEqual(response["T6"]["lag_minutes"], 120)
        self.assertEqual(response["F9"]["events"], 0)


class TemporalFoldsTest(unittest.TestCase):
    def test_folds_respect_publication_and_do_not_overlap(self):
        times = pd.Series(pd.date_range("2023-01-01", "2026-06-01", freq="D"))
        origins = times - pd.Timedelta(minutes=180)
        folds = sf.temporal_folds(times, origins, pd.Timedelta(minutes=240))
        for name, (fit, score) in folds.items():
            self.assertFalse((fit & score).any(), name)
            self.assertLess((times[fit] + pd.Timedelta(minutes=240)).max(), origins[score].min(), name)
        self.assertEqual(int(folds["final"][1].sum()), int((origins >= pd.Timestamp("2026-01-01")).sum()))


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
