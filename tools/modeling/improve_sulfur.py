"""Chronological challenger study; test is audit-only, never selection input.

See reports/modeling/model-improvement/PROTOCOL.md. Does not modify production.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from dataclasses import dataclass
import warnings

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import duckdb
import numpy as np
import pandas as pd
from scipy.optimize import least_squares
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge, HuberRegressor, LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import average_precision_score, roc_auc_score
from threadpoolctl import threadpool_limits
from tools.modeling.sulfur_first_iteration import load_telemetry, load_lab_target, regression_metrics, StandardizedRidge
from tools.modeling.sulfur_features import telemetry_features, available_lab, applicability


def extended_features(telemetry, labs, origins, pak):
    """Past-only features; LIMS history is selected by publication, not sample."""
    x, _ = telemetry_features(telemetry, origins)
    lab = available_lab(labs, origins)
    x['previous_lab_available'] = lab.previous_lab_available
    x['lab_age_h'] = (pd.Series(origins) - lab.previous_lab_sample_time).dt.total_seconds()/3600
    ordered = labs.sort_values('target_time')
    time_ns = pd.DatetimeIndex(ordered.target_time + pd.Timedelta(hours=4)).as_unit('ns').asi8
    right = np.searchsorted(time_ns, origins.as_unit('ns').asi8, side='right')
    values = ordered.target.to_numpy(float)
    medians=[]; means=[]; changes=[]; counts=[]
    for origin, end in zip(origins, right):
        begin = np.searchsorted(time_ns, (origin-pd.Timedelta(days=7)).value, side='right')
        hist = values[begin:end]; recent = hist[-3:]
        medians.append(np.median(recent) if len(recent) else np.nan)
        means.append(np.median(hist) if len(hist) else np.nan)
        changes.append(recent[-1]-recent[-2] if len(recent)>1 else np.nan)
        counts.append(len(hist))
    x['lab_median3']=medians; x['lab_median7d']=means; x['lab_delta']=changes; x['lab_count7d']=counts
    pak_features, pak_time = telemetry_features(pak, origins)
    x = pd.concat([x, pak_features], axis=1)
    x['pak_age_min']=(pd.Series(origins)-pak_time).dt.total_seconds()/60
    for column in telemetry.columns:
        if column.startswith('242000__'):
            x[column+'__delta_1h']=x[column]-x[column+'__mean_1h']
            x[column+'__delta_6h']=x[column]-x[column+'__mean_6h']
    # Kelvin conversion and ratios have explicit positivity domains.
    for suffix in ('','__mean_1h'):
        def col(c):return x['242000__'+c+suffix].where(x['242000__'+c+suffix]>0)
        temp=col('T6')+273.15; pressure=col('P13'); flow=col('F15'); gas=col('F25')
        x['phys_kelvin'+suffix]=temp
        x['phys_feed_sulfur'+suffix]=col('Q20')
        x['phys_pressure'+suffix]=pressure
        x['phys_flow'+suffix]=flow
        x['phys_gas_oil'+suffix]=gas/flow
        x['phys_inverse_kelvin'+suffix]=1000/temp
        x['phys_log_pressure'+suffix]=np.log(pressure)
        x['phys_log_flow'+suffix]=np.log(flow)
        x['phys_log_gas_oil'+suffix]=np.log(gas/flow)
    x['phys_delta_reactor_temp']=x['242000__T11']-x['242000__T6']
    x['phys_pressure_drop_fraction']=x['242000__P8']/x['242000__P13'].where(x['242000__P13']>0)
    x['phys_quench_feed_ratio']=x['242000__F14']/x['242000__F9'].where(x['242000__F9']>0)
    x['phys_output_feed_ratio']=x['242000__F17']/x['242000__F9'].where(x['242000__F9']>0)
    return x.replace([np.inf,-np.inf],np.nan)


def read_data(source, dataset):
    telemetry=load_telemetry(source); labs=load_lab_target(source)
    origins=pd.DatetimeIndex(labs.target_time-pd.Timedelta(hours=3))
    path=str(dataset/'observations.parquet').replace("'","''")
    with duckdb.connect() as db:
        pak=db.execute(f"""SELECT timestamp,value FROM read_parquet('{path}')
            WHERE metric_id='pak.ht.Mg.Sulfur' AND isfinite(value) AND value>=0
            AND NOT regexp_matches(coalesce(flags,''),'invalid|conflict|flatline|suspect') ORDER BY timestamp""").fetchdf()
    pak=pak.drop_duplicates('timestamp').set_index('timestamp').rename(columns={'value':'pak_sulfur'})
    x=extended_features(telemetry,labs,origins,pak)
    old=pd.read_csv(ROOT/'reports/modeling/sulfur-first-iteration/predictions.csv')
    assert np.array_equal(pd.to_datetime(old.target_time).to_numpy(),labs.target_time.to_numpy())
    labels_available=pd.to_datetime(labs.target_time)+pd.Timedelta(hours=4)
    splits={
        'train':np.asarray(labels_available<pd.Timestamp('2025-01-01')),
        'validation':np.asarray((origins>=pd.Timestamp('2025-01-01'))&(labels_available<pd.Timestamp('2026-01-01'))),
        'test':np.asarray(origins>=pd.Timestamp('2026-01-01')),
    }
    return x,labs,origins,old,splits


class Kinetics:
    """Constrained pseudo-first-order HDS with lumped catalyst/exposure term."""
    def __init__(self,suffix=''):self.suffix=suffix
    def inputs(self,x,fit=False):
        names=['phys_kelvin','phys_feed_sulfur','phys_pressure','phys_flow','phys_gas_oil']
        a=x[[n+self.suffix for n in names]].to_numpy(float)
        a=np.where(a>0,a,np.nan)
        if fit:self.medians=np.nanmedian(a,axis=0)
        return np.where(np.isfinite(a),a,self.medians)
    @staticmethod
    def response(a,p):
        T,S,P,F,H=a.T
        log_conversion=(p[0]+p[1]*(1/637.15-1/T)+p[2]*np.log(P/4)
                        -np.log(F/3400)+p[3]*np.log(H/4))
        conversion=np.exp(np.clip(log_conversion,-15,15))
        return np.logaddexp(0,np.log(S)-conversion)
    def fit(self,x,y):
        a=self.inputs(x,True)
        result=least_squares(lambda p:self.response(a,p)-np.log1p(y),[1.9,7000.,.5,.1],
                             bounds=([-5,0,0,0],[5,25000,3,2]),loss='soft_l1',f_scale=.2,max_nfev=1500)
        self.params=result.x; self.converged=bool(result.success)
        return self
    def predict_log(self,x):return self.response(self.inputs(x),self.params)


@dataclass
class Spec:
    name:str
    family:str
    features:str='tech'
    parameter:float=3000.
    leaves:int=7
    iterations:int=100
    loss:str='absolute_error'
    residual:str='none'


def columns(x,which):
    if which=='all':return list(x.columns)
    if which=='ht':return [c for c in x if not c.startswith('avt__')]
    tags=['T6','T11','P13','P8','F9','F15','F25','F14','Q20','Q21','F17','F26']
    return [c for c in x if c.startswith(('phys_','lab_','pak_','previous_lab')) or any(c.startswith('242000__'+t+'__') or c=='242000__'+t for t in tags)]


class Challenger:
    def __init__(self,spec):self.spec=spec
    def fit(self,x,y):
        s=self.spec;self.cols=columns(x,s.features);self.train_median=float(np.median(y))
        if s.family=='baseline':return self
        self.kinetics=None
        if s.family=='kinetic' or s.residual=='kinetic':
            self.kinetics=Kinetics('__mean_1h' if s.features=='mean1h' else '').fit(x,y)
            if s.family=='kinetic':return self
        target=np.log1p(y) if s.loss!='raw_absolute' else np.asarray(y)
        if s.residual=='lab':target=target-np.log1p(x.previous_lab_available.fillna(self.train_median))
        if s.residual=='kinetic':target=target-self.kinetics.predict_log(x)
        if s.family=='ridge':
            estimator=Ridge(alpha=s.parameter)
        elif s.family=='huber':
            estimator=HuberRegressor(epsilon=1.5,alpha=s.parameter,max_iter=1500)
        elif s.family=='trees':
            estimator=ExtraTreesRegressor(n_estimators=200,min_samples_leaf=15,max_depth=10,random_state=42,n_jobs=2)
        else:
            estimator=HistGradientBoostingRegressor(loss='absolute_error' if s.loss in ('absolute_error','raw_absolute') else 'squared_error',
                         max_leaf_nodes=s.leaves,min_samples_leaf=int(s.parameter),max_iter=s.iterations,
                         learning_rate=.05,l2_regularization=10,early_stopping=False,random_state=42)
        self.model=make_pipeline(SimpleImputer(strategy='median',add_indicator=True,keep_empty_features=True),StandardScaler(),estimator)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.model.fit(x[self.cols],target)
        self.fit_warnings = [str(w.message) for w in caught]
        return self
    def predict(self,x):
        s=self.spec
        if s.family=='baseline':
            if s.name=='train_median':return np.full(len(x),self.train_median)
            return x[s.name].fillna(self.train_median).to_numpy()
        if s.family=='kinetic':p=self.kinetics.predict_log(x)
        else:
            p=self.model.predict(x[self.cols])
            if s.residual=='lab':p+=np.log1p(x.previous_lab_available.fillna(self.train_median))
            if s.residual=='kinetic':p+=self.kinetics.predict_log(x)
            if s.loss=='raw_absolute':return np.maximum(0,p)
        return np.maximum(0,np.expm1(np.clip(p,-20,20)))


def specs():
    result=[Spec(n,'baseline') for n in ['train_median','previous_lab_available','lab_median3','lab_median7d']]
    result += [Spec('kinetic_current','kinetic'),Spec('kinetic_mean1h','kinetic','mean1h')]
    for feature in ['tech','all']:
        for alpha in [300,3000,10000]:
            result.append(Spec(f'ridge_{feature}_{alpha}','ridge',feature,alpha))
        result.append(Spec(f'huber_{feature}','huber',feature,.1))
        result.append(Spec(f'trees_{feature}','trees',feature))
        for leaves,leafsize,it in [(7,60,100),(7,30,200),(15,30,100)]:
            result.append(Spec(f'hgb_{feature}_{leaves}_{leafsize}_{it}','hgb',feature,leafsize,leaves,it))
        result.append(Spec(f'hgb_raw_{feature}','hgb',feature,60,7,100,'raw_absolute'))
    result += [Spec('lab_residual_ridge','ridge','tech',3000,residual='lab'),
               Spec('kinetic_residual_ridge','ridge','tech',3000,residual='kinetic'),
               Spec('kinetic_residual_hgb','hgb','tech',60,7,100,residual='kinetic')]
    return result


def alarm_metrics(y,scores,threshold):
    y=np.asarray(y)>10;pred=np.asarray(scores)>=threshold
    tp=int((y&pred).sum());fp=int((~y&pred).sum());fn=int((y&~pred).sum());tn=int((~y&~pred).sum())
    return {'n':len(y),'tp':tp,'fp':fp,'fn':fn,'tn':tn,'recall':tp/max(1,tp+fn),
            'precision':tp/max(1,tp+fp),'fpr':fp/max(1,fp+tn),'threshold':float(threshold),
            'average_precision':float(average_precision_score(y,scores)),
            'roc_auc':float(roc_auc_score(y,scores)) if y.any() and (~y).any() else None}


def choose_threshold(y,scores,max_fpr):
    candidates=np.r_[np.nextafter(np.max(scores),np.inf),np.unique(scores)]
    metrics=[alarm_metrics(y,scores,t) for t in candidates]
    eligible=[m for m in metrics if m['fpr']<=max_fpr]
    return max(eligible,key=lambda m:(m['recall'],m['precision'],m['threshold']))


def block_interval(y,challenger,baseline,times):
    difference=np.abs(challenger-y)-np.abs(baseline-y)
    block=pd.DatetimeIndex(times).to_period('W').astype(str)
    groups=[np.flatnonzero(block==key) for key in np.unique(block)]
    rng=np.random.default_rng(20260920)
    draws=[float(np.mean(difference[np.concatenate([groups[i] for i in rng.integers(0,len(groups),len(groups))])])) for _ in range(1500)]
    return {'mae_difference_challenger_minus_baseline':float(difference.mean()),'weekly_block_bootstrap_95pct':np.quantile(draws,[.025,.975]).tolist(),'blocks':len(groups)}


def finite_metrics(y, predictions):
    """Do not silently discard numerical failures from the scoring denominator."""
    failures = int((~np.isfinite(predictions)).sum())
    if failures:
        return {'n': len(y), 'nonfinite_predictions': failures, 'mae': None,
                'rmse': None, 'status': 'numerical_failure_on_full_denominator'}
    return {**regression_metrics(y, predictions), 'nonfinite_predictions': 0}


def run(source,dataset,out):
    out.mkdir(parents=True,exist_ok=True)
    x,labs,origins,old,splits=read_data(source,dataset);y=labs.target.to_numpy(float)
    common=old.forecast_status.eq('ok').to_numpy()
    train=splits['train'];validation=splits['validation'];test=splits['test'];selection=validation&common
    print(f'Data ready: {x.shape}, train/val/test={[int(m.sum()) for m in splits.values()]}',flush=True)
    predictions={'incumbent':old.prediction_ridge.to_numpy()}; fitted={};selection_rows=[]
    for spec in specs():
        model=Challenger(spec).fit(x.loc[train],y[train]);p=model.predict(x)
        predictions[spec.name]=p;fitted[spec.name]=model
        m=regression_metrics(y[selection],p[selection]);selection_rows.append({'model':spec.name,'validation_mae':m['mae'],'spec':spec.__dict__})
        print(f"fit {spec.name}: validation MAE={m['mae']:.4f}",flush=True)
    # Freeze winner BEFORE any 2026 scoring. Baselines are eligible winners.
    winner=min(selection_rows,key=lambda m:m['validation_mae'])['model']
    frozen={'regression_winner':winner,'selection':'validation MAE on unchanged common mask','candidates':selection_rows}
    (out/'selection.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    scores=[]
    for name,p in predictions.items():
        record={'model':name}
        for split,mask in splits.items():
            record[split]={'all':regression_metrics(y[mask],p[mask]),'common':regression_metrics(y[mask&common],p[mask&common])}
        scores.append(record)
    # Separate alarm models; all thresholds/selection use validation only.
    alarm_scores={'incumbent_guard':old.prediction_risk_guard.to_numpy(),
                  'previous_lab':x.previous_lab_available.fillna(np.median(y[train])).to_numpy(),
                  'pak':x.pak_sulfur.fillna(np.median(y[train])).to_numpy(),
                  'selected_regression':predictions[winner]}
    for kind,param in [('logistic',.1),('logistic',1),('hgb',7),('hgb',15)]:
        estimator=(LogisticRegression(C=param,max_iter=2000,class_weight='balanced',random_state=42)
                   if kind=='logistic' else HistGradientBoostingClassifier(max_leaf_nodes=param,min_samples_leaf=40,
                       max_iter=150,learning_rate=.05,l2_regularization=10,early_stopping=False,random_state=42))
        model=make_pipeline(SimpleImputer(strategy='median',add_indicator=True,keep_empty_features=True),StandardScaler(),estimator)
        model.fit(x.loc[train,columns(x,'tech')],y[train]>10)
        alarm_scores[f'{kind}_{param}']=model.predict_proba(x[columns(x,'tech')])[:,1]
    alarms=[]
    for fpr in [.1,.2]:
        chosen=[]
        for name,p in alarm_scores.items():
            threshold=choose_threshold(y[selection],p[selection],fpr)['threshold']
            v=alarm_metrics(y[selection],p[selection],threshold)
            chosen.append((name,p,threshold,v))
        best=max(chosen,key=lambda item:(item[3]['recall'],item[3]['precision'],item[3]['average_precision']))[0]
        for name,p,threshold,v in chosen:
            alarms.append({'model':name,'validation_fpr_cap':fpr,'selected':name==best,'validation':v,
                           'test_common':alarm_metrics(y[test&common],p[test&common],threshold),
                           'test_all':alarm_metrics(y[test],p[test],threshold)})
    # Same fixed winner, expanding-window checks. Not independent selection data.
    folds=[]
    for begin,end in [('2024-01-01','2024-07-01'),('2024-07-01','2025-01-01'),('2025-01-01','2025-07-01')]:
        a=np.asarray(labs.target_time+pd.Timedelta(hours=4)<pd.Timestamp(begin))
        b=np.asarray((origins>=pd.Timestamp(begin))&(labs.target_time+pd.Timedelta(hours=4)<pd.Timestamp(end)))
        spec=next(s for s in specs() if s.name==winner)
        candidate=Challenger(spec).fit(x.loc[a],y[a]);p=candidate.predict(x.loc[b])
        old_columns=list(json.loads((ROOT/'reports/modeling/sulfur-first-iteration/model.json').read_text(encoding='utf-8'))['feature_columns'])
        incumbent=StandardizedRidge(3000).fit(x.loc[a,old_columns],pd.Series(np.log1p(y[a])))
        with np.errstate(over='ignore'):
            q=np.maximum(0,np.expm1(incumbent.predict(x.loc[b,old_columns])))
        artifact=incumbent.artifact()
        accepted=np.asarray([applicability(row,artifact)['status']=='ok'
                             for row in x.loc[b,old_columns].to_numpy(float)])
        folds.append({'begin':begin,'end':end,'train_rows':int(a.sum()),'rows':int(b.sum()),
                      'common_rows':int(accepted.sum()),'coverage':float(accepted.mean()),
                      'all':{'challenger':finite_metrics(y[b],p),'incumbent_retrained':finite_metrics(y[b],q)},
                      'common':{'challenger':finite_metrics(y[b][accepted],p[accepted]),
                                'incumbent_retrained':finite_metrics(y[b][accepted],q[accepted])}})
    comparable=test&common
    uncertainty=block_interval(y[comparable],predictions[winner][comparable],predictions['incumbent'][comparable],origins[comparable])
    table=pd.DataFrame({'origin':origins,'target_time':labs.target_time,'target':y,'common_eligible':common})
    table['split']='purged'
    for name,mask in splits.items():table.loc[mask,'split']=name
    for name,p in predictions.items():table[name]=p
    table.to_csv(out/'predictions.csv',index=False)
    result={'winner':winner,'features':x.shape[1],'common_test_n':int(comparable.sum()),'all_test_n':int(test.sum()),
            'selection':frozen,'regression':scores,'alarms':alarms,'expanding_checks':folds,'uncertainty':uncertainty,
            'kinetic_parameters':{n:{'params':m.kinetics.params.tolist(),'converged':m.kinetics.converged}
                                    for n,m in fitted.items() if getattr(m,'kinetics',None) is not None},
            'fit_warnings':{n:m.fit_warnings for n,m in fitted.items() if getattr(m,'fit_warnings',[])},
            'test_status':'previously inspected retrospective audit; not a fresh blind test'}
    (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'winner':winner,'uncertainty':uncertainty,'output':str(out)},ensure_ascii=False),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'reports/modeling/model-improvement')
    args=parser.parse_args()
    with threadpool_limits(limits=2),warnings.catch_warnings():
        warnings.simplefilter('ignore',DeprecationWarning)
        run(args.source,args.dataset,args.output)
