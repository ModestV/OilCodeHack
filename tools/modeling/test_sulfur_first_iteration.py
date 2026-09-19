"""Unit checks for the modelling contract; they do not require source data."""
import unittest

import numpy as np
import pandas as pd

from sulfur_first_iteration import (
    StandardizedRidge,
    add_past_features,
    align_target_features,
    regression_metrics,
    time_split,
)


class FirstIterationContractTests(unittest.TestCase):
    def test_alignment_never_uses_row_after_cutoff(self):
        index = pd.date_range("2025-01-01", periods=12, freq="10min")
        telemetry = pd.DataFrame({"sensor": np.arange(len(index), dtype=float)}, index=index)
        target = pd.DataFrame({
            "target_time": [pd.Timestamp("2025-01-01 02:00")],
            "target": [7.0],
        })
        aligned = align_target_features(target, telemetry, pd.Timedelta(hours=1))
        self.assertEqual(aligned.loc[0, "feature_time"], pd.Timestamp("2025-01-01 01:00"))
        self.assertLessEqual(aligned.loc[0, "feature_time"], aligned.loc[0, "feature_cutoff"])

    def test_time_splits_are_disjoint_and_exhaustive(self):
        frame = pd.DataFrame({"target_time": pd.date_range("2023-01-01", "2026-03-01", periods=30)})
        masks = time_split(frame)
        total = sum(int(mask.sum()) for mask in masks.values())
        self.assertEqual(total, len(frame))
        self.assertFalse((masks["train"] & masks["validation"]).any())
        self.assertFalse((masks["validation"] & masks["test"]).any())

    def test_ridge_handles_missing_values_without_fitting_imputation_on_test(self):
        train = pd.DataFrame({"x": [1.0, 2.0, np.nan], "z": [4.0, 5.0, 6.0]})
        target = pd.Series([2.0, 4.0, 6.0])
        model = StandardizedRidge(alpha=10.0).fit(train, target)
        prediction = model.predict(pd.DataFrame({"x": [1000.0, np.nan], "z": [4.0, 6.0]}))
        self.assertTrue(np.isfinite(prediction).all())
        self.assertEqual(model.medians_[0], 1.5)

    def test_metrics_preserve_threshold_counts(self):
        metrics = regression_metrics([8.0, 12.0], [9.0, 11.0], threshold=10.0)
        self.assertEqual(metrics["actual_above_10"], 1)
        self.assertEqual(metrics["predicted_above_10"], 1)
        self.assertEqual(metrics["recall_above_10"], 1.0)

    def test_fitted_model_exports_ordered_runtime_artifact(self):
        train = pd.DataFrame({"sensor": [1.0, 2.0, 3.0], "other": [4.0, 5.0, 6.0]})
        model = StandardizedRidge(alpha=10.0).fit(train, pd.Series([2.0, 4.0, 6.0]))
        artifact = model.artifact()
        self.assertEqual(artifact["feature_columns"], ["sensor", "other"])
        self.assertEqual(len(artifact["coef"]), 3)
        self.assertEqual(len(artifact["medians"]), 2)


if __name__ == "__main__":
    unittest.main()
