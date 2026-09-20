"""Separate exceedance diagnostic with pre-2026 temporal selection."""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.modeling.anchored_challengers import gate, fit_candidate, predict
from tools.modeling.anchored_features import MODEL_COLUMNS, HORIZONS_MINUTES, build_features, ln, residual_quantile_function, exceedance_probability
from tools.modeling.sulfur_forecast import load_dataset, temporal_folds, StandardizedRidge, probability_metrics, sha256


def fit(frame, y, c):
    normalizer = StandardizedRidge(10).fit(frame[MODEL_COLUMNS], ln(y)).artifact()
    values = frame[MODEL_COLUMNS].to_numpy(float)
    x = (np.where(np.isfinite(values), values, normalizer['medians']) - normalizer['mean']) / normalizer['scale']
    model = LogisticRegression(C=c, max_iter=2000).fit(x, np.asarray(y) > 10)
    normalizer.update(coef=[float(model.intercept_[0]), *model.coef_[0].tolist()], C=c,
                      link='logit', event='lab_sulfur_gt_10', observational_only=True)
    return normalizer


def probability(model, frame):
    from scipy.special import expit
    x = frame[MODEL_COLUMNS].to_numpy(float)
    x = (np.where(np.isfinite(x), x, model['medians']) - model['mean']) / model['scale']
    return expit(np.c_[np.ones(len(x)), x] @ model['coef'])


def run(dataset, output):
    analysers, controls, labs = load_dataset(dataset)
    result, models, records, selections = {}, {}, [], {}
    # Select and confirm all horizons before calculating any 2026 metrics.
    prepared = {}
    for h in HORIZONS_MINUTES:
        origins = pd.DatetimeIndex(labs.target_time) - pd.Timedelta(minutes=h)
        frame = build_features(analysers, controls, labs, origins)
        y = labs.target.to_numpy(float)
        folds = temporal_folds(labs.target_time, pd.Series(origins), pd.Timedelta(hours=4))
        fit24, score24 = folds['2024']
        fit25, score25 = folds['2025']
        _, ok24, _ = gate(frame, fit24, y)
        _, ok25, _ = gate(frame, fit25, y)
        accepted24, accepted25 = ok24 & score24, ok25 & score25
        candidates = {c: fit(frame.loc[fit24], y[fit24], c) for c in (.01, .1, 1., 10.)}
        scores = {c: probability_metrics(y[accepted24], probability(m, frame.loc[accepted24]), .3) for c,m in candidates.items()}
        c = min(scores, key=lambda k: scores[k]['brier'])
        model25 = fit(frame.loc[fit25], y[fit25], c)
        p25 = probability(model25, frame)
        ridge24 = fit_candidate('ridge_log_100', frame.loc[fit24], y[fit24])
        ridge25 = fit_candidate('ridge_log_100', frame.loc[fit25], y[fit25])
        pred24, pred25 = predict(ridge24, frame), predict(ridge25, frame)
        q24 = residual_quantile_function(ln(y[accepted24]) - pred24[accepted24])
        residual25 = [exceedance_probability(q24, v, np.log(10)) for v in pred25[accepted25]]
        score = probability_metrics(y[accepted25], p25[accepted25], .3)
        baseline = probability_metrics(y[accepted25], np.full(sum(accepted25), (y[fit25] > 10).mean()), .3)
        residual = probability_metrics(y[accepted25], residual25, .3)
        promoted = score['brier'] <= .98 * min(baseline['brier'], residual['brier']) and score['auc'] >= residual['auc']
        selections[str(h)] = {'C': c, 'promoted': bool(promoted), 'selection2024': scores,
                              'confirmation2025': score, 'prevalence2025': baseline, 'residual2025': residual}
        prepared[h] = (frame, y, folds, pred25, accepted25)
    (output / 'risk-selection.json').write_text(json.dumps(selections, indent=2), encoding='utf8')
    for h, (frame, y, folds, pred25, accepted25) in prepared.items():
        train, audit = folds['final']
        support, ok, _ = gate(frame, train, y)
        accepted = audit & ok
        model = fit(frame.loc[train], y[train], selections[str(h)]['C'])
        models[str(h)] = {**model, 'support': support, 'promoted': selections[str(h)]['promoted']}
        p = probability(model, frame)
        q25 = residual_quantile_function(ln(y[accepted25]) - pred25[accepted25])
        ridge = fit_candidate('ridge_log_100', frame.loc[train], y[train])
        pred = predict(ridge, frame)
        residual = np.array([exceedance_probability(q25, v, np.log(10)) for v in pred])
        result[str(h)] = {**selections[str(h)], 'audit_rows': int(audit.sum()), 'accepted_rows': int(sum(accepted)),
                         'audit2026': probability_metrics(y[accepted], p[accepted], .3),
                         'residual2026': probability_metrics(y[accepted], residual[accepted], .3),
                         'prevalence2026': probability_metrics(y[accepted], np.full(sum(accepted), (y[train] > 10).mean()), .3)}
        records.append(pd.DataFrame({'horizon': h, 'origin': frame.loc[audit, 'prediction_origin'],
                                    'target': y[audit], 'accepted': ok[audit], 'probability': p[audit],
                                    'residual_probability': residual[audit]}))
        print(json.dumps({'horizon': h, 'promoted': selections[str(h)]['promoted'],
                          'Brier2025': result[str(h)]['confirmation2025']['brier'],
                          'Brier2026': result[str(h)]['audit2026']['brier'],
                          'AUC2026': result[str(h)]['audit2026']['auc']}), flush=True)
    (output / 'risk-model.json').write_text(json.dumps(models, indent=2), encoding='utf8')
    pd.concat(records, ignore_index=True).to_csv(output / 'risk-predictions.csv', index=False)
    (output / 'risk-metrics.json').write_text(json.dumps({'horizons': result, 'observations_sha256': sha256(dataset / 'observations.parquet'),
        'script_sha256': sha256(Path(__file__)), 'protocol_sha256': sha256(output / 'RISK_PROTOCOL.md'),
        'model_sha256': sha256(output / 'risk-model.json')}, indent=2), encoding='utf8')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--output', type=Path, default=ROOT / 'reports/modeling/causal-anchored')
    args = p.parse_args()
    with threadpool_limits(limits=1):
        run(args.dataset, args.output)
