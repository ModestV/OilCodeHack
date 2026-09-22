"""Post-hoc availability check with raw PAK density; not a promoted cetane model."""
import argparse
from pathlib import Path
import sys
import duckdb
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.modeling.quality_targets import asof_value,cetane_index,metrics,save,digest


def run(dataset,output):
    with duckdb.connect() as db:
        rows=db.execute("""select timestamp,value from read_parquet(?) where metric_id='pak.ht.D15'
            and isfinite(value) and not contains(coalesce(flags,''),'invalid')
            and not contains(coalesce(flags,''),'conflict') order by timestamp""",[str(dataset/'observations.parquet')]).fetchdf()
    rows=rows.drop_duplicates('timestamp',keep='last')
    result,records={},[]
    for h in (0,180):
        f=pd.read_csv(output/f'features-cetane_h{h}.csv',parse_dates=['prediction_origin','target_time'])
        density=asof_value(rows,f.prediction_origin,delay_hours=0,max_age_hours=.5)
        pred=cetane_index(density.value,f.t50)
        f['pak_density'],f['pak_density_time'],f['pak_index']=density.value,density.timestamp,pred
        f['horizon']=h
        records.append(f[['horizon','prediction_origin','target_time','target','previous','t50','pak_density','pak_density_time','pak_index']])
        for year in (2025,2026):
            selected=(f.prediction_origin.dt.year==year).to_numpy()
            common=selected & np.isfinite(pred) & np.isfinite(f.previous)
            result[f'{year}_h{h}']=dict(total=int(selected.sum()),
                pak_index=metrics(f.target[selected],pred[selected],'cetane'),
                common_index=metrics(f.target[common],pred[common],'cetane'),
                common_previous=metrics(f.target[common],f.previous[common],'cetane'))
    pd.concat(records,ignore_index=True).to_csv(output/'pak-index-predictions.csv',index=False)
    save(output/'pak-index-diagnostic.json',dict(results=result,post_hoc=True,
        observations_sha256=digest(dataset/'observations.parquet'),script_sha256=digest(Path(__file__)),
        protocol_sha256=digest(output/'PAK_DIAGNOSTIC_PROTOCOL.md')))
    print(result)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'reports/modeling/cetane-t95')
    a=p.parse_args();run(a.dataset,a.output)
