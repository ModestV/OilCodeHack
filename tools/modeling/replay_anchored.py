"""Compare complete offline features to causally truncated runtime on every audit origin."""
import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.modeling.anchored_features import ANALYSER_METRICS, FEATURE_COLUMNS, build_features, clean_analyser
from tools.modeling.sulfur_forecast import load_dataset, sha256
from backend import forecast as rt


def run(dataset, output):
    _, controls, labs = load_dataset(dataset)
    with duckdb.connect() as db:
        rows = db.execute("""SELECT metric_id,timestamp,value,coalesce(flags,'') AS flags FROM read_parquet(?)
          WHERE metric_id IN (SELECT unnest(?)) AND value IS NOT NULL AND isfinite(value)
          AND NOT contains(coalesce(flags,''),'invalid') AND NOT contains(coalesce(flags,''),'conflict')
          ORDER BY timestamp""", [str(dataset / "observations.parquet"), list(ANALYSER_METRICS.values())]).fetchdf()
    raw = {name: rows[rows.metric_id == mid].drop_duplicates('timestamp', keep='last').set_index('timestamp')
           for name, mid in ANALYSER_METRICS.items()}
    def memory_inputs(directory, origin, artifact):
        origin = pd.Timestamp(origin)
        parts = {k: v.loc[(v.index > origin - pd.Timedelta(days=rt.ANALYSER_HISTORY_DAYS)) & (v.index <= origin)] for k,v in raw.items()}
        online = {k: clean_analyser(p.value, p['flags']) for k,p in parts.items()}
        ctl = {k: v.loc[(v.index > origin - pd.Timedelta(hours=rt.CONTROL_HISTORY_HOURS)) & (v.index <= origin)] for k,v in controls.items()}
        lab = labs.loc[(labs.target_time >= origin - pd.Timedelta(days=rt.LAB_HISTORY_DAYS)) &
                       (labs.target_time <= origin - pd.Timedelta(minutes=artifact['lims_publication_delay_minutes']))]
        return online, ctl, lab

    results, totals = [], {}
    for variant, folder in [('corrected_claude', output / 'corrected-claude'), ('frozen_selection', output)]:
        path = folder / 'model.json'
        artifact = rt._load_artifact(path)
        offline = pd.read_csv(folder / 'predictions.csv')
        offline = offline[offline.split.isin(['test', 'audit_2026'])]
        checks = []
        # Two known feature mismatches plus evenly spaced actual Parquet checks.
        spot_indices = set(np.linspace(0, len(offline)-1, 12).astype(int))
        for count, (_, row) in enumerate(offline.iterrows()):
            origin, h = pd.Timestamp(row.prediction_origin), int(row.horizon_minutes)
            inputs = memory_inputs(dataset, origin, artifact)
            features = build_features(*inputs, pd.DatetimeIndex([origin])).iloc[0]
            left, right = features[FEATURE_COLUMNS].to_numpy(float), row[FEATURE_COLUMNS].to_numpy(float)
            np.testing.assert_allclose(left, right, rtol=1e-8, atol=1e-7, equal_nan=True)
            with patch.object(rt, '_runtime_inputs', memory_inputs):
                result = rt.forecast_sulfur(dataset, origin.isoformat(), path, horizon_minutes=h)
            assert result['status'] == row.forecast_status, (variant, h, origin)
            delta = None
            if result['status'] == 'ok':
                delta = abs(result['prediction'] - row.prediction)
                np.testing.assert_allclose(result['prediction'], row.prediction, rtol=1e-9, atol=1e-8)
                np.testing.assert_allclose(result['prediction_lower'], row.prediction_lower, rtol=1e-9, atol=1e-8)
                np.testing.assert_allclose(result['prediction_upper'], row.prediction_upper, rtol=1e-9, atol=1e-8)
                np.testing.assert_allclose(result['exceedance_probability'], row.exceedance_probability, atol=1e-8)
            if count in spot_indices or origin in pd.DatetimeIndex(['2026-05-08 23:50', '2026-01-09 19:00']):
                actual = rt.forecast_sulfur(dataset, origin.isoformat(), path, horizon_minutes=h)
                assert actual['status'] == result['status']
                if actual['status'] == 'ok':
                    np.testing.assert_allclose(actual['prediction'], result['prediction'], atol=1e-9)
                checks.append({'origin': str(origin), 'horizon': h, 'status': actual['status']})
            results.append({'variant': variant, 'origin': str(origin), 'horizon': h, 'status': result['status'],
                            'path_supported': result['path_supported'], 'prediction_delta': delta,
                            'max_feature_delta': float(np.nanmax(abs(left-right)))})
            if (count+1) % 250 == 0:
                print(variant, 'prefix replay', count+1, flush=True)
        totals[variant] = {'origins': len(offline), 'database_checks': checks, 'model_sha256': sha256(path)}
    table = pd.DataFrame(results)
    table.to_csv(output / 'runtime-parity.csv', index=False)
    totals.update(passed=True, max_prediction_delta=float(table.prediction_delta.max()),
                  max_feature_delta=float(table.max_feature_delta.max()),
                  observations_sha256=sha256(dataset / 'observations.parquet'), script_sha256=sha256(Path(__file__)))
    (output / 'runtime-parity.json').write_text(json.dumps(totals, indent=2), encoding='utf8')
    print(json.dumps({k:v for k,v in totals.items() if k not in ('corrected_claude', 'frozen_selection')}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/modeling/causal-anchored')
    args = parser.parse_args()
    run(args.dataset, args.output)
