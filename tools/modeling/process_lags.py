"""Bounded pre-2026 horizon/source/lag diagnostic. See process-lags/PROTOCOL.md."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import duckdb
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits
from tools.modeling.sulfur_features import telemetry_features, available_lab
from tools.modeling.sulfur_first_iteration import StandardizedRidge, regression_metrics


def fresh_lab(labs,origins):
    out=available_lab(labs,origins)
    age=(pd.Series(origins)-out.previous_lab_sample_time).dt.total_seconds()/3600
    fresh=age.le(48)
    return out.previous_lab_available.where(fresh),age.where(fresh)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dataset',type=Path,default=ROOT/'storage/hackathon')
    parser.add_argument('--output',type=Path,default=ROOT/'reports/modeling/process-lags')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    tags=['ht.'+t for t in ['T6','T11','P13','P8','F9','F14','F17','F25','Q20','Q21']]
    pak='pak.ht.Mg.Sulfur';output='lims.ht.2.Mg.Sulfur';feed='lims.ht.1.Mass.Sulfur'
    path=args.dataset/'observations.parquet'
    with duckdb.connect() as db:
        df=db.execute("""SELECT metric_id,timestamp,value,flags FROM read_parquet(?)
          WHERE timestamp < '2026-01-01' AND metric_id IN (SELECT unnest(?))
          AND value IS NOT NULL AND isfinite(value)
          AND NOT regexp_matches(coalesce(flags,''),'invalid|conflict')
          ORDER BY timestamp""",[str(path),tags+[pak,output,feed]]).fetchdf()
    labs=df.loc[(df.metric_id==output)&df.value.ge(0),['timestamp','value']].rename(columns={'timestamp':'target_time','value':'target'}).drop_duplicates('target_time').reset_index(drop=True)
    feed_labs=df.loc[(df.metric_id==feed)&df.value.ge(0),['timestamp','value']].rename(columns={'timestamp':'target_time','value':'target'}).drop_duplicates('target_time').reset_index(drop=True)
    feed_labs['target']*=10000
    telemetry=df.loc[df.metric_id.isin(tags+[pak])&~df['flags'].fillna('').str.contains('suspect|flatline')]
    telemetry=telemetry.drop_duplicates(['timestamp','metric_id']).pivot(index='timestamp',columns='metric_id',values='value').reindex(columns=tags+[pak])
    labels_available=labs.target_time+pd.Timedelta(hours=4)
    results=[];predictions=[]
    for horizon in [0,60,120,180]:
        origins=pd.DatetimeIndex(labs.target_time-pd.Timedelta(minutes=horizon))
        x,_=telemetry_features(telemetry,origins)
        previous,age=fresh_lab(labs,origins)
        x['previous_lab_available']=previous
        core=[c for c in x if not c.startswith(pak)]
        with_pak=list(x.columns)
        for lag in [60,120,180]:
            shifted,_=telemetry_features(telemetry,origins-pd.Timedelta(minutes=lag))
            for tag in tags+[pak]: x[f'{tag}__lag{lag}m']=shifted[tag]
        with_lags=list(x.columns)
        x['feed_lab_mgkg'],x['feed_lab_age_h']=fresh_lab(feed_labs,origins)
        x['inverse_kelvin']=1000/(x['ht.T6']+273.15).where(x['ht.T6']>-273.15)
        x['pressure_drop_fraction']=x['ht.P8']/x['ht.P13'].where(x['ht.P13']>0)
        x=x.replace([np.inf,-np.inf],np.nan)
        train=labels_available.lt('2025-01-01').to_numpy()
        val=((origins>=pd.Timestamp('2025-01-01'))&labels_available.lt('2026-01-01')).to_numpy()
        common=val & x[tags].notna().mean(axis=1).ge(.8).to_numpy() & previous.notna().to_numpy()
        y=labs.target.to_numpy(float)
        for family,cols in [('core',core),('separate_pak',with_pak),('process_lags',with_lags),('feed_lims_and_shape',list(x.columns))]:
            model=StandardizedRidge(alpha=3000).fit(x.loc[train,cols],pd.Series(np.log1p(y[train])))
            pred=np.maximum(0,np.expm1(np.clip(model.predict(x[cols]),-20,20)))
            results.append(dict(horizon_minutes=horizon,family=family,train_n=int(train.sum()),
                validation_n=int(val.sum()),common_n=int(common.sum()),feature_count=len(cols),
                feed_lims_available=int((val&x.feed_lab_mgkg.notna().to_numpy()).sum()),
                raw_validation=regression_metrics(y[val],pred[val]),
                paired_validation=regression_metrics(y[common],pred[common]),
                paired_previous_lab=regression_metrics(y[common],previous[common])))
            predictions.extend(dict(horizon_minutes=horizon,family=family,origin=str(origins[i]),target_time=str(labs.target_time[i]),
                actual=float(y[i]),prediction=float(pred[i]),previous_lab=None if pd.isna(previous[i]) else float(previous[i]),common=bool(common[i])) for i in np.flatnonzero(val))
        print(f'H{horizon}: train={train.sum()}, validation={val.sum()}, paired={common.sum()}',flush=True)
    sha=lambda p:hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
    result=dict(protocol_sha256=sha(args.output/'PROTOCOL.md'),script_sha256=sha(Path(__file__)),
        observations_sha256=sha(path),results=results,production_changed=False)
    (args.output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    pd.DataFrame(predictions).to_csv(args.output/'predictions.csv',index=False)
    lines=['# Горизонты, лаги и ЛИМС сырья: проверка до 2026 года','',
      'Диагностическая validation-проверка, не независимый тест и не основание для замены production-модели. H0 — оценка текущего качества. Все сравнения внутри горизонта используют одни и те же пробы.','',
      '| Горизонт, мин | Вариант | n парных | MAE | MAE прошлой ЛИМС | Recall >10 | FPR |','|---|---|---|---|---|---|---|']
    for r in results:
        m=r['paired_validation'];b=r['paired_previous_lab']
        lines.append(f"| {r['horizon_minutes']} | {r['family']} | {r['common_n']} | {m['mae']:.3f} | {b['mae']:.3f} | {m['recall_above_10']} | {m['false_alarm_rate']:.3f} |")
    lines+=['','MAE в мг/кг. Все пробы validation, включая недоступные для парного сравнения, сохранены в results.json и predictions.csv. Маска доступности не заменяет runtime OOD-проверку.',
      'ЛИМС сырья переведена из массовых процентов в мг/кг (×10000), доступность через 4 часа, возраст пробы не более 48 часов. Объёмный расход со спорной шкалой не используется как LHSV.',
      'Обратная абсолютная температура и относительный перепад давления — дополнительные признаки. Это не идентификация кинетики и не доказательство причинного эффекта управления.']
    (args.output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf8')


if __name__=='__main__':
    with threadpool_limits(limits=2):main()
