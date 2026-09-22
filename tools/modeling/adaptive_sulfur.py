"""Exploratory delayed-feedback and locally anchored physics study.

See the follow-up protocol: 2026 is already inspected, never a fresh holdout.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits
from tools.modeling.improve_sulfur import read_data, Challenger, Spec, block_interval
from tools.modeling.sulfur_first_iteration import load_telemetry, regression_metrics
from tools.modeling.sulfur_features import telemetry_features


def delayed_correction(predictions, y, sample_times, origins, days, strength):
    """Only post-training forecast errors published by this origin can adapt it."""
    publication=pd.DatetimeIndex(sample_times)+pd.Timedelta(hours=4)
    errors=np.asarray(y)-np.asarray(predictions)
    result=np.asarray(predictions).copy()
    for i,origin in enumerate(origins):
        prior=(publication<=origin)&(publication>origin-pd.Timedelta(days=days))
        prior&=pd.DatetimeIndex(origins)>=pd.Timestamp('2025-01-01')
        prior&=np.isfinite(errors)
        if prior.sum()>=3: result[i]+=strength*np.median(errors[prior])
    return np.maximum(0,result)


def anchored_kinetics(x, sample_features, labs, origins, energy, pressure_order, fallback):
    """Estimate the unknown exposure locally from the last published sample."""
    samples=pd.DatetimeIndex(labs.target_time)
    publication=samples+pd.Timedelta(hours=4)
    indexes=np.searchsorted(publication.as_unit('ns').asi8,origins.as_unit('ns').asi8,side='right')-1
    predictions=np.full(len(x),fallback,dtype=float)
    valid_anchor=np.zeros(len(x),dtype=bool)
    for i,j in enumerate(indexes):
        if j<0 or origins[i]-publication[j]>pd.Timedelta(hours=48):continue
        last=float(labs.target.iloc[j])
        if last<0:continue
        predictions[i]=last
        T0=sample_features['242000__T6'].iloc[j]+273.15
        P0=sample_features['242000__P13'].iloc[j]
        F0=sample_features['242000__F15'].iloc[j]
        S0=sample_features['242000__Q20'].iloc[j]
        T=x.phys_kelvin.iloc[i]; P=x.phys_pressure.iloc[i]
        F=x.phys_flow.iloc[i]; S=x.phys_feed_sulfur.iloc[i]
        values=np.asarray([T0,P0,F0,S0,T,P,F,S,last])
        if not np.isfinite(values).all() or not (values>0).all() or S0<=last:continue
        exposure=np.log(S0/last)
        severity=np.exp(np.clip(energy/8.314*(1/T0-1/T),-15,15))*(P/P0)**pressure_order*(F0/F)
        predictions[i]=S*np.exp(-exposure*severity)
        valid_anchor[i]=True
    return predictions,valid_anchor


def run(source,dataset,out):
    x,labs,origins,old,splits=read_data(source,dataset)
    y=labs.target.to_numpy(float); common=old.forecast_status.eq('ok').to_numpy()
    base=pd.read_csv(out/'predictions.csv'); predictions={}; diagnostics={}
    for name in ['incumbent','hgb_raw_tech']:
        for days in [7,30]:
            for strength in [.5,1.]:
                key=f'{name}_correction_{days}d_{strength}'
                predictions[key]=delayed_correction(base[name].to_numpy(),y,labs.target_time,origins,days,strength)
    sf,_=telemetry_features(load_telemetry(source),pd.DatetimeIndex(labs.target_time))
    for energy in [0,30000,60000,90000]:
        for order in [0,.5,1.]:
            key=f'anchored_kinetic_E{energy}_P{order}'
            predictions[key],valid=anchored_kinetics(x,sf,labs,origins,energy,order,np.median(y[splits['train']]))
            diagnostics[key]={'physical_anchor_coverage':{s:float(valid[m].mean()) for s,m in splits.items()}}
    published=pd.DatetimeIndex(labs.target_time)+pd.Timedelta(hours=4)
    months=origins.to_period('M')
    for history_days in [None,365]:
        label='all' if history_days is None else str(history_days)
        p=np.full(len(x),np.median(y[splits['train']]));median=p.copy(); refits=[]
        for month in sorted(set(months[origins>=pd.Timestamp('2025-01-01')])):
            cutoff=month.start_time; train=published<cutoff
            if history_days:train&=published>=cutoff-pd.Timedelta(days=history_days)
            evaluate=months==month
            model=Challenger(Spec('monthly','hgb','tech',60,7,100,'raw_absolute')).fit(x.loc[train],y[train])
            p[evaluate]=model.predict(x.loc[evaluate]);median[evaluate]=np.median(y[train])
            refits.append({'cutoff':str(cutoff),'train_rows':int(train.sum()),'latest_training_publication':str(published[train].max())})
        predictions[f'monthly_hgb_{label}']=p
        predictions[f'monthly_median_{label}']=median
        diagnostics[f'monthly_hgb_{label}']={'refits':refits,'policy':'prequential; only labels published before each month'}
    eligible=splits['validation']&common
    selection=[{'model':n,'validation_mae':regression_metrics(y[eligible],p[eligible])['mae']} for n,p in predictions.items()]
    winner=min(selection,key=lambda m:m['validation_mae'])['model']
    (out/'adaptive-selection.json').write_text(json.dumps({'winner':winner,'candidates':selection},indent=2),encoding='utf8')
    scores=[]
    for name,p in predictions.items():
        row={'model':name}
        for split,mask in splits.items():
            if split=='train':continue  # Online policies are evaluated only after training.
            row[split]={'all':regression_metrics(y[mask],p[mask]),'common':regression_metrics(y[mask&common],p[mask&common])}
        scores.append(row)
    test=splits['test']&common
    result={'winner':winner,'regression':scores,'diagnostics':diagnostics,
            'uncertainty':block_interval(y[test],predictions[winner][test],base.incumbent.to_numpy()[test],origins[test]),
            'status':'exploratory follow-up after first retrospective audit; selection uses 2025 only'}
    (out/'adaptive-results.json').write_text(json.dumps(result,indent=2,allow_nan=False),encoding='utf8')
    table=base[['origin','target_time','target','split','common_eligible']].copy()
    for name,p in predictions.items():table[name]=p
    table.to_csv(out/'adaptive-predictions.csv',index=False)
    print(json.dumps({'winner':winner,'uncertainty':result['uncertainty']}),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'reports/modeling/model-improvement')
    args=parser.parse_args()
    with threadpool_limits(limits=2):run(args.source,args.dataset,args.output)
