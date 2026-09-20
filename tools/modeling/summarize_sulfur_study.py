"""Generate supplemental diagnostics and an environment/artifact manifest."""
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import pandas as pd
from tools.modeling.improve_sulfur import block_interval


def run():
    out=ROOT/'reports/modeling/model-improvement'
    base=pd.read_csv(out/'predictions.csv')
    adaptive=pd.read_csv(out/'adaptive-predictions.csv')
    assert base[['origin','target_time','target','split','common_eligible']].equals(
        adaptive[['origin','target_time','target','split','common_eligible']])
    accepted=base.split.eq('test')&base.common_eligible
    intervals={}
    for name in ['monthly_hgb_all','monthly_hgb_365','incumbent_correction_30d_0.5','incumbent_correction_7d_0.5']:
        intervals[name]=block_interval(base.target[accepted].to_numpy(),adaptive[name][accepted].to_numpy(),
                                      base.incumbent[accepted].to_numpy(),pd.to_datetime(base.origin[accepted]))
    adaptive_results=json.loads((out/'adaptive-results.json').read_text())
    dates=[]
    for name,details in adaptive_results['diagnostics'].items():
        for row in details.get('refits',[]):
            assert pd.Timestamp(row['latest_training_publication'])<pd.Timestamp(row['cutoff'])
            dates.append({'model':name,**row})
    extra={'pairing_verified':True,'refits_publication_check_passed':len(dates),
           'paired_intervals':intervals,'distributions':{s:base.loc[base.split.eq(s),'target'].describe().to_dict()
                                                       for s in ['train','validation','test']}}
    (out/'additional-diagnostics.json').write_text(json.dumps(extra,indent=2),encoding='utf8')
    files=[ROOT/'tools/modeling'/name for name in ['improve_sulfur.py','adaptive_sulfur.py','summarize_sulfur_study.py',
           'sulfur_first_iteration.py','sulfur_features.py','research_tests/test_improvement.py']]
    files += [ROOT/'reports/modeling/sulfur-first-iteration'/name for name in ['model.json','predictions.csv']]
    files += [ROOT/'requirements-modeling.txt']
    files += [p for p in out.iterdir() if p.name!='manifest.json']
    manifest={'base_commit':'d8afb0b9803352ccf0e69808ae5d730a35cc0e27','python':platform.python_version(),
              'platform':platform.platform(),'packages':{n:importlib.metadata.version(n) for n in
                ['numpy','pandas','duckdb','scipy','scikit-learn','threadpoolctl','joblib','pytest']},
              'hash_normalization':'UTF-8 text with LF newlines, to survive Git autocrlf',
              'sha256_text_lf':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_text(encoding='utf8').encode('utf8')).hexdigest() for p in files}}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    print(json.dumps({'paired_rows':int(accepted.sum()),'verified_refits':len(dates),'packages':manifest['packages']}))


if __name__=='__main__':run()
