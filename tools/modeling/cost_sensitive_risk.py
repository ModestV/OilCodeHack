"""Frozen temporal alarm-policy audit. No runtime changes; see the adjacent protocol."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.modeling.anchored_risk import fit, probability
from tools.modeling.anchored_challengers import gate, fit_candidate, predict
from tools.modeling.anchored_features import HORIZONS_MINUTES, ln, residual_quantile_function, exceedance_probability
from tools.modeling.sulfur_forecast import temporal_folds, probability_metrics, sha256

RATIOS = (1, 2, 5, 10, 20, 50)
C_GRID = (.01, .1, 1., 10.)


def loss_vector(target, alarm, ratio):
    event, alarm = np.asarray(target) > 10, np.asarray(alarm, bool)
    return np.where(event & ~alarm, ratio, np.where(~event & alarm, 1., 0.))


def metrics(target, alarm, ratio):
    event, alarm = np.asarray(target) > 10, np.asarray(alarm, bool)
    tp, fp = int((event & alarm).sum()), int((~event & alarm).sum())
    fn, tn = int((event & ~alarm).sum()), int((~event & ~alarm).sum())
    return dict(n=len(event), events=int(event.sum()), tp=tp, fp=fp, fn=fn, tn=tn,
                recall=tp/(tp+fn) if tp+fn else None,
                precision=tp/(tp+fp) if tp+fp else None,
                false_alarm_rate=fp/(fp+tn) if fp+tn else None,
                alarm_share=float(alarm.mean()) if len(alarm) else None,
                total_cost=float(loss_vector(target, alarm, ratio).sum()),
                cost_per_100=float(loss_vector(target, alarm, ratio).mean()*100) if len(event) else None)


def select_threshold(target, p, ratio):
    """Enumerate every distinct decision, including all/none; deterministic cost ties."""
    p = np.asarray(p, float)
    if not len(p) or not np.isfinite(p).all() or (p < 0).any() or (p > 1).any():
        raise ValueError('Threshold selection requires nonempty finite probabilities in [0,1]')
    candidates = np.r_[0., np.unique(p), np.nextafter(p.max(), np.inf)]
    scored = [(float(t), metrics(target, p >= t, ratio)) for t in candidates]
    t, score = min(scored, key=lambda item: (item[1]['total_cost'], item[1]['fn'], item[1]['fp'], -item[0]))
    return dict(threshold=t, selection2024=score)


def paired_interval(target, left, right, origins, ratio, days=7, iterations=2000):
    """Calendar-block bootstrap; ratio of sampled losses to sampled observations."""
    delta = loss_vector(target, left, ratio) - loss_vector(target, right, ratio)
    blocks = pd.to_datetime(origins).to_numpy(dtype='datetime64[D]').astype('int64') // days
    grouped = pd.DataFrame({'block': blocks, 'delta': delta}).groupby('block').delta.agg(['sum', 'count'])
    rng = np.random.default_rng(42)
    indices = rng.integers(0, len(grouped), size=(iterations, len(grouped)))
    samples = grouped['sum'].to_numpy()[indices].sum(axis=1) / grouped['count'].to_numpy()[indices].sum(axis=1)
    return dict(delta_cost_per_100=float(delta.mean()*100),
                ci95_per_100=(np.quantile(samples, [.025, .975])*100).tolist(),
                block_days=days, blocks=len(grouped), iterations=iterations)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')


def score_period(frame, mask, p, residual, ratios, year, horizon, active=None):
    selected = frame.loc[mask].reset_index(drop=True)
    y, origins = selected.target.to_numpy(), selected.prediction_origin
    p, residual = p[mask], residual[mask]
    results, months, comparisons = [], [], {}
    for ratio in RATIOS:
        threshold = ratios[str(ratio)]['threshold']
        alarms = {'logistic_empirical': p >= threshold,
                  'logistic_bayes': p >= 1/(1+ratio), 'logistic_fixed_030': p >= .3,
                  'residual_bayes': residual >= 1/(1+ratio),
                  'always_alarm': np.ones(len(y), bool), 'never_alarm': np.zeros(len(y), bool)}
        thresholds = dict(logistic_empirical=threshold, logistic_bayes=1/(1+ratio),
                          logistic_fixed_030=.3, residual_bayes=1/(1+ratio), always_alarm=0., never_alarm=2.)
        if active is not None:
            active_p, active_threshold, active_point = active
            alarms.update(active_runtime=(active_p[mask] >= active_threshold) | (active_point[mask] > 10),
                          active_bayes=active_p[mask] >= 1/(1+ratio))
            thresholds.update(active_runtime=active_threshold, active_bayes=1/(1+ratio))
        for policy, alarm in alarms.items():
            results.append(dict(year=year, horizon=horizon, cost_ratio=ratio, policy=policy,
                                threshold=thresholds[policy], **metrics(y, alarm, ratio)))
            month_labels = origins.dt.strftime('%Y-%m')
            for month in sorted(month_labels.unique()):
                index = (month_labels == month).to_numpy()
                months.append(dict(month=month, horizon=horizon, cost_ratio=ratio, policy=policy,
                                   **metrics(y[index], alarm[index], ratio)))
        comparisons[str(ratio)] = {
            reference: {str(days): paired_interval(y, alarms['logistic_empirical'], alarms[reference],
                                                   origins, ratio, days) for days in (7, 28)}
            for reference in ('residual_bayes', 'always_alarm', 'never_alarm')}
    return results, months, comparisons


def run(source, output):
    output.mkdir(parents=True, exist_ok=True)
    cache_path = source/'corrected-claude/predictions.csv'
    cache = pd.read_csv(cache_path, parse_dates=['prediction_origin', 'target_time'])
    parity = json.loads((source/'runtime-parity.json').read_text(encoding='utf8'))
    if not parity['passed']:
        raise ValueError('Raw feature cache requires the preceding successful parity audit')
    prepared, selections = {}, {}
    # This stage reads raw features/targets only. Never read saved future-fitted OOF probabilities.
    for h in HORIZONS_MINUTES:
        frame = cache.loc[cache.horizon_minutes == h].reset_index(drop=True)
        if frame.target_time.duplicated().any():
            raise ValueError('Each target must be a distinct laboratory sample')
        y = frame.target.to_numpy(float)
        folds = temporal_folds(frame.target_time, frame.prediction_origin, pd.Timedelta(hours=4))
        train, score = folds['2024']
        _, ok, _ = gate(frame, train, y)
        accepted = score.to_numpy() & ok
        candidates = {c: fit(frame.loc[train], y[train], c) for c in C_GRID}
        p = {c: probability(m, frame) for c, m in candidates.items()}
        brier = {c: probability_metrics(y[accepted], v[accepted], .3)['brier'] for c, v in p.items()}
        c = min(C_GRID, key=lambda k: brier[k])
        thresholds = {str(r): select_threshold(y[accepted], p[c][accepted], r) for r in RATIOS}
        selections[str(h)] = dict(C=c, brier_selection2024=brier, thresholds=thresholds,
                                  selected_rows=int(accepted.sum()), frozen_before='2025-01-01')
        ridge = fit_candidate('ridge_log_100', frame.loc[train], y[train])
        residual24 = ln(y[accepted])-predict(ridge, frame)[accepted]
        prepared[h] = dict(frame=frame, y=y, folds=folds, q=residual_quantile_function(residual24))
    write_json(output/'selection.json', selections)

    all_scores, all_months, predictions, diagnostics, confirmations, comparisons = [], [], [], {}, {}, {}
    active_artifact = json.loads((source/'corrected-claude/model.json').read_text(encoding='utf8'))
    for year, fold in ((2025, '2025'), (2026, 'final')):
        for h, data in prepared.items():
            frame, y = data['frame'], data['y']
            train, score = data['folds'][fold]
            _, ok, _ = gate(frame, train, y)
            accepted = score.to_numpy() & ok
            model = fit(frame.loc[train], y[train], selections[str(h)]['C'])
            p = probability(model, frame)
            ridge = fit_candidate('ridge_log_100', frame.loc[train], y[train])
            ridge_pred = predict(ridge, frame)
            residual_p = np.array([exceedance_probability(data['q'], v, np.log(10)) for v in ridge_pred])
            active = None
            if year == 2026:
                assert np.array_equal(ok[score], (frame.loc[score, 'forecast_status'] == 'ok').to_numpy())
                active = (frame.exceedance_probability.to_numpy(),
                          active_artifact['models'][str(h)]['alarm_probability'], frame.prediction.to_numpy())
            scores, months, ci = score_period(frame, accepted, p, residual_p,
                                              selections[str(h)]['thresholds'], year, h, active)
            all_scores.extend(scores); all_months.extend(months)
            key = f'{year}_h{h}'
            comparisons[key] = ci
            diagnostics[key] = dict(rows=int(score.sum()), accepted=int(accepted.sum()),
                events=int((y[score] > 10).sum()), accepted_events=int((y[accepted] > 10).sum()),
                manual_review=int((score.to_numpy() & ~ok).sum()),
                events_in_manual_review=int(((y > 10) & score.to_numpy() & ~ok).sum()),
                logistic=probability_metrics(y[accepted], p[accepted], .3),
                residual=probability_metrics(y[accepted], residual_p[accepted], .3),
                latest_training_label_available=str((frame.loc[train,'target_time']+pd.Timedelta(hours=4)).max()),
                first_scored_origin=str(frame.loc[score,'prediction_origin'].min()))
            predictions.append(pd.DataFrame(dict(year=year, horizon=h,
                origin=frame.loc[score,'prediction_origin'], target_time=frame.loc[score,'target_time'],
                target=y[score], accepted=ok[score], logistic_probability=p[score], residual_probability=residual_p[score])))
            if year == 2025:
                eligibility = {}
                for r in RATIOS:
                    rows = {v['policy']:v for v in scores if v['cost_ratio'] == r}
                    eligibility[str(r)] = all(
                        rows['logistic_empirical']['total_cost'] <= .95*rows[reference]['total_cost']
                        and ci[str(r)][reference]['7']['ci95_per_100'][1] < 0
                        for reference in ('residual_bayes', 'always_alarm', 'never_alarm'))
                confirmations[str(h)] = eligibility
                # Annual residual calibration moves forward only after the scored year's labels exist.
                data['q'] = residual_quantile_function(ln(y[accepted])-ridge_pred[accepted])
            print(f'{year} H{h}: {int(accepted.sum())}/{int(score.sum())} accepted', flush=True)
        if year == 2025:
            write_json(output/'confirmation2025.json', confirmations)
    pd.DataFrame(all_scores).to_csv(output/'policy-metrics.csv', index=False)
    pd.DataFrame(all_months).to_csv(output/'monthly-metrics.csv', index=False)
    pd.concat(predictions, ignore_index=True).to_csv(output/'predictions.csv', index=False)
    write_json(output/'paired-cost-intervals.json', comparisons)
    write_json(output/'diagnostics.json', diagnostics)
    write_json(output/'provenance.json', dict(protocol_sha256=sha256(output/'PROTOCOL.md'),
        script_sha256=sha256(Path(__file__)), cache_sha256=sha256(cache_path),
        observations_sha256=parity['observations_sha256'],
        source_parity_sha256=sha256(source/'runtime-parity.json'),
        active_model_sha256=sha256(source/'corrected-claude/model.json'),
        dependency_sha256={name: sha256(ROOT/'tools/modeling'/name) for name in
            ('anchored_risk.py', 'anchored_challengers.py', 'anchored_features.py', 'sulfur_forecast.py')},
        costs_are_assumptions=True, runtime_changed=False))
    print('2025 eligibility:', json.dumps(confirmations), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'reports/modeling/causal-anchored')
    parser.add_argument('--output', type=Path, default=ROOT/'reports/modeling/cost-sensitive-risk')
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        run(args.source, args.output)
