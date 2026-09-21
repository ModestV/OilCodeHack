"""Past-only cetane/T95 audit; research estimates cannot authorize product release."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.modeling.sulfur_forecast import StandardizedRidge, temporal_folds

TARGETS = {'cetane': 'lims.ht.2.CetaneNumber', 't95': 'lims.ht.2.95%.T'}
LABS = {'d15': 'lims.ht.2.D15', 't50': 'lims.ht.2.50%.T', 't90': 'lims.ht.2.90%.T',
        'feed_t95': 'lims.ht.1.95%.T', 'product_t95': TARGETS['t95']}
CONTROLS = ['T6', 'F9', 'P13', 'F2']
COLUMNS = {'cetane': ['previous', 'd15', 't50', 't90'],
           't95': ['previous', 'feed_t95', 't90', 'T6', 'F9', 'P13']}


def save(path, value):
    def default(v):
        if isinstance(v, np.generic): return v.item()
        if isinstance(v, pd.Timestamp): return v.isoformat()
        raise TypeError(type(v))
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=default, allow_nan=False), encoding='utf8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cetane_index(density_kgm3, t50_c):
    """D976 equation as published by NREL/SR-510-36242; not measured cetane number."""
    d, t = np.asarray(density_kgm3, float)/1000, np.asarray(t50_c, float)
    valid = np.isfinite(d) & np.isfinite(t) & (d >= .7) & (d <= 1.) & (t >= 100) & (t <= 450)
    safe_t = np.where(valid, t, 1.)
    value = 454.74-1641.416*d+774.74*d*d-.554*safe_t+97.803*np.log10(safe_t)**2
    return np.where(valid, value, np.nan)


def asof_value(rows, origins, *, delay_hours=4, max_age_hours=48):
    """Match by publication, then check age measured from sampling (not publication)."""
    left = pd.DataFrame({'origin': pd.to_datetime(origins), 'position': np.arange(len(origins))})
    right = rows[['timestamp', 'value']].copy().sort_values('timestamp')
    right['available_at'] = right.timestamp + pd.Timedelta(hours=delay_hours)
    joined = pd.merge_asof(left.sort_values('origin'), right, left_on='origin', right_on='available_at')
    joined = joined.sort_values('position').reset_index(drop=True)
    joined['age_hours'] = (joined.origin-joined.timestamp).dt.total_seconds()/3600
    joined.loc[joined.age_hours > max_age_hours, 'value'] = np.nan
    assert (joined.available_at.dropna() <= joined.loc[joined.available_at.notna(), 'origin']).all()
    return joined


def features(groups, name, origins):
    f = pd.DataFrame({'prediction_origin': pd.to_datetime(origins)})
    target = groups[TARGETS[name]]
    prior = asof_value(target, origins, max_age_hours=60*24 if name == 'cetane' else 48)
    f['previous'], f['previous_age_hours'] = prior.value, prior.age_hours
    f['previous_sample_time'], f['previous_available_at'] = prior.timestamp, prior.available_at
    smoothed = target.copy()
    smoothed['value'] = smoothed.value.rolling(3, min_periods=1).median()
    f['median3'] = asof_value(smoothed, origins, max_age_hours=60*24 if name == 'cetane' else 48).value
    for col, metric in LABS.items():
        joined = asof_value(groups[metric], origins)
        f[col] = joined.value
        lo, hi = (700, 1000) if col == 'd15' else (100, 450)
        f.loc[~f[col].between(lo, hi), col] = np.nan
        f[col+'_sample_time'], f[col+'_available_at'] = joined.timestamp, joined.available_at
    for col in CONTROLS:
        joined = asof_value(groups['ht.'+col], origins, delay_hours=0, max_age_hours=.5)
        f[col] = joined.value
        f[col+'_sample_time'] = joined.timestamp
    f['index'] = cetane_index(f.d15, f.t50)
    return f


def candidates(name):
    return ['previous', 'median3', 'ridge1', 'ridge10', 'ridge100'] + (
        ['index', 'index_offset'] if name == 'cetane' else ['vak_feed_hypothesis', 'vak_product_hypothesis'])


def predict_candidates(frame, train, name):
    predictions = {'previous': frame.previous.to_numpy(), 'median3': frame.median3.to_numpy()}
    complete = frame[COLUMNS[name]].notna().all(axis=1)
    for alpha in (1, 10, 100):
        valid = train & complete
        out = np.full(len(frame), np.nan)
        if valid.sum() >= len(COLUMNS[name])+2:
            model = StandardizedRidge(alpha).fit(frame.loc[valid,COLUMNS[name]], frame.loc[valid,'target'])
            out[complete] = model.predict(frame.loc[complete,COLUMNS[name]])
        predictions[f'ridge{alpha}'] = out
    if name == 'cetane':
        predictions['index'] = frame['index'].to_numpy()
        valid = train & frame['index'].notna()
        offset = np.median(frame.loc[valid,'target']-frame.loc[valid,'index']) if valid.sum() >= 5 else np.nan
        predictions['index_offset'] = frame['index'].to_numpy()+offset
    else:
        for suffix, col in [('feed', 'feed_t95'), ('product', 'product_t95')]:
            predictions[f'vak_{suffix}_hypothesis'] = (.03814*frame.F9-9.201-.00002*frame.F2+.50*frame.T6+.48321*frame[col]).to_numpy()
    return predictions


def metrics(y, pred, name):
    y, pred = np.asarray(y), np.asarray(pred)
    ok = np.isfinite(y) & np.isfinite(pred)
    y, pred = y[ok], pred[ok]
    if not len(y): return {'n': 0}
    actual_bad = y < 51 if name == 'cetane' else y > 360
    alarm = pred < 51 if name == 'cetane' else pred > 360
    return dict(n=len(y), mae=float(np.mean(abs(pred-y))), rmse=float(np.sqrt(np.mean((pred-y)**2))),
                bias=float(np.mean(pred-y)), violations=int(actual_bad.sum()),
                detected=int((actual_bad & alarm).sum()), missed=int((actual_bad & ~alarm).sum()),
                false_alarms=int((~actual_bad & alarm).sum()))


def paired_ci(y, pred, base, dates):
    delta = abs(pred-y)-abs(base-y)
    blocks = pd.to_datetime(dates).to_numpy(dtype='datetime64[D]').astype('int64')//7
    g = pd.DataFrame({'block': blocks, 'delta': delta}).groupby('block').delta.agg(['sum','count'])
    if len(g) < 2: return None
    ix = np.random.default_rng(42).integers(0,len(g),(2000,len(g)))
    sample = g['sum'].to_numpy()[ix].sum(axis=1)/g['count'].to_numpy()[ix].sum(axis=1)
    return np.quantile(sample,[.025,.975]).tolist()


def residual_bound(residuals):
    values = np.asarray(residuals,float)
    values = values[np.isfinite(values)]
    if len(values) < 30: return None
    return float(np.sort(values)[min(len(values), math.ceil((len(values)+1)*.9))-1])


def run(dataset, output):
    output.mkdir(parents=True,exist_ok=True)
    ids = list(set([*TARGETS.values(),*LABS.values(),*['ht.'+c for c in CONTROLS]]))
    with duckdb.connect() as db:
        raw = db.execute("SELECT * FROM read_parquet(?) WHERE metric_id IN (SELECT unnest(?)) ORDER BY timestamp",
                         [str(dataset/'observations.parquet'),ids]).fetchdf()
    clean = raw.loc[np.isfinite(raw.value) & ~raw['flags'].fillna('').str.contains('invalid|conflict')]
    groups = {k:v.sort_values('timestamp').drop_duplicates('timestamp',keep='last').reset_index(drop=True)
              for k,v in clean.groupby('metric_id')}
    audit = {}
    for name, mid in TARGETS.items():
        g=groups[mid]; days=g.timestamp.diff().dt.total_seconds()/86400
        audit[name]=dict(metric_id=mid,raw_rows=int((raw.metric_id==mid).sum()),usable_rows=len(g),
            years={str(k):int(v) for k,v in g.groupby(g.timestamp.dt.year).size().items()},
            yearly_median={str(k):float(v) for k,v in g.groupby(g.timestamp.dt.year).value.median().items()},
            first=str(g.timestamp.min()),last=str(g.timestamp.max()),
            median_gap_days=float(days.median()),max_gap_days=float(days.max()),
            min=float(g.value.min()),max=float(g.value.max()),units=g.unit.unique().tolist(),
            violations=int((g.value<51).sum() if name=='cetane' else (g.value>360).sum()))
    # Same-sample physical index audit: not available at forecast origin.
    pivot=clean[clean.metric_id.isin([TARGETS['cetane'],LABS['d15'],LABS['t50']])].pivot_table(index='timestamp',columns='metric_id',values='value',aggfunc='last')
    pairs=pivot.loc[pivot[TARGETS['cetane']].notna()].copy()
    pairs['calculated_index']=cetane_index(pairs[LABS['d15']],pairs[LABS['t50']])
    audit['same_sample_index'] = metrics(pairs[TARGETS['cetane']],pairs.calculated_index,'cetane')
    pairs.to_csv(output/'same-sample-index.csv')
    save(output/'data-audit.json',audit)
    prepared,selection={},{}
    for name in TARGETS:
        labs=groups[TARGETS[name]]
        for h in (0,180):
            frame=features(groups,name,labs.timestamp-pd.Timedelta(minutes=h))
            frame['target_time'],frame['target']=labs.timestamp,labs.value
            folds=temporal_folds(frame.target_time,frame.prediction_origin,pd.Timedelta(hours=4))
            train,score=folds['2024']; predictions=predict_candidates(frame,train,name)
            common=score.to_numpy() & np.isfinite(np.column_stack(list(predictions.values()))).all(axis=1)
            scores={k:metrics(frame.target[common],p[common],name) for k,p in predictions.items()}
            winner=min(scores,key=lambda k:scores[k].get('mae',float('inf')))
            key=f'{name}_h{h}'
            selection[key]=dict(winner=winner,common_n=int(common.sum()),eligible_selection=bool(common.sum()>=20),scores=scores)
            prepared[key]=(name,h,frame,folds)
            frame.to_csv(output/f'features-{key}.csv',index=False)
    save(output/'selection2024.json',selection)
    results,confirmation,records,calibration={}, {}, [], {}
    for year,fold in [(2025,'2025'),(2026,'final')]:
        for key,(name,h,frame,folds) in prepared.items():
            train,score=folds[fold]; predictions=predict_candidates(frame,train,name)
            common=score.to_numpy() & np.isfinite(np.column_stack(list(predictions.values()))).all(axis=1)
            y=frame.target.to_numpy();winner=selection[key]['winner'];p=predictions[winner]
            native={k:metrics(y[score],v[score],name) for k,v in predictions.items()}
            shared={k:metrics(y[common],v[common],name) for k,v in predictions.items()}
            comparison={b:paired_ci(y[common],p[common],predictions[b][common],frame.target_time[common]) for b in ['previous','median3']}
            entry=dict(total=int(score.sum()),common_n=int(common.sum()),native=native,common=shared,
                training_labels=int(train.sum()),
                training_complete_ridge=int((train & frame[COLUMNS[name]].notna().all(axis=1)).sum()),
                missing_predictors={c:int(frame.loc[score,c].isna().sum()) for c in COLUMNS[name]},
                winner=winner,paired_mae_ci=comparison,
                fresh_previous=metrics(y[common & (frame.previous_age_hours<=48)],p[common & (frame.previous_age_hours<=48)],name),
                stale_previous=metrics(y[common & (frame.previous_age_hours>48)],p[common & (frame.previous_age_hours>48)],name),
                latest_train_available=str((frame.target_time[train]+pd.Timedelta(hours=4)).max()),
                first_origin=str(frame.prediction_origin[score].min()))
            if year==2025:
                enough=selection[key]['eligible_selection'] and common.sum()>=30
                eligible=bool(enough and all(shared[winner]['mae']<=.95*shared[b]['mae'] and
                    shared[winner]['rmse']<=1.05*shared[b]['rmse'] and comparison[b] is not None and comparison[b][1]<0
                    for b in ['previous','median3']))
                confirmation[key]=dict(eligible=eligible,enough_samples=bool(enough),winner=winner)
                residual=(p-y) if name=='cetane' else (y-p)
                calibration[key]=dict(n=int(common.sum()),q=residual_bound(residual[common]))
            else:
                q=calibration[key]['q']; bound=p-q if name=='cetane' and q is not None else p+q if q is not None else np.full(len(p),np.nan)
                claimed=(bound>=51 if name=='cetane' else bound<=360)&common&np.isfinite(bound)
                bad=y<51 if name=='cetane' else y>360
                entry['bound']=dict(**calibration[key],nominal_one_sided=.9,
                    empirical_coverage=float(np.mean((y[common]>=bound[common]) if name=='cetane' else (y[common]<=bound[common]))) if q is not None and common.any() else None,
                    predicted_compliant=int(claimed.sum()),violations_among_claimed=int((claimed&bad).sum()))
            results[f'{year}_{key}']=entry
            out=frame.loc[score,['prediction_origin','target_time','target','previous_age_hours']].copy()
            out['year'],out['target_name'],out['horizon'],out['common']=year,name,h,common[score]
            for k,v in predictions.items():out[k]=v[score]
            records.append(out)
            print(f'{year} {key}: {int(common.sum())}/{int(score.sum())} common, winner={winner}',flush=True)
        if year==2025:save(output/'confirmation2025.json',confirmation)
    # Coverage at actual sulfur decision origins; no synthetic target labels.
    origins_file=ROOT/'reports/modeling/sulfur-first-iteration/predictions.csv'
    origins=pd.read_csv(origins_file).query("split == 'test'").prediction_origin
    coverage={}
    for name in TARGETS:
        f=features(groups,name,pd.to_datetime(origins).reset_index(drop=True))
        coverage[name]=dict(n=len(f),fresh_previous=int((f.previous.notna()&(f.previous_age_hours<=48)).sum()),
            research_previous=int(f.previous.notna().sum()),complete_ridge_inputs=int(f[COLUMNS[name]].notna().all(axis=1).sum()),
            index_inputs=int(f['index'].notna().sum()) if name=='cetane' else None)
    save(output/'sulfur-origin-coverage.json',coverage)
    save(output/'metrics.json',results)
    pd.concat(records,ignore_index=True).to_csv(output/'predictions.csv',index=False)
    save(output/'provenance.json',dict(observations_sha256=digest(dataset/'observations.parquet'),
        protocol_sha256=digest(output/'PROTOCOL.md'),script_sha256=digest(Path(__file__)),
        origins_sha256=digest(origins_file),ridge_module_sha256=digest(ROOT/'tools/modeling/sulfur_forecast.py'),
        runtime_changed=False))
    print(json.dumps(confirmation),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'reports/modeling/cetane-t95')
    a=p.parse_args();run(a.dataset,a.output)
