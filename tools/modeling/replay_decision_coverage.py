"""Replay real runtime decisions on frozen test origins; no manual quality overrides.

This measures coverage, not causal benefit or recommendation correctness.
The sampled origins were selected by LIMS target availability, not uniform time.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pandas as pd
from backend.agents import make_decision
from backend.scenarios import ScenarioRequest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, default=ROOT/'storage/hackathon')
    parser.add_argument('--output', type=Path, default=ROOT/'reports/verification/process-corrections/coverage.json')
    args = parser.parse_args()
    origins_file = ROOT/'reports/modeling/sulfur-first-iteration/predictions.csv'
    origins = pd.read_csv(origins_file).query("split == 'test'").prediction_origin.tolist()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    started = time.perf_counter()
    for i, at in enumerate(origins):
        result = make_decision(args.dataset, ScenarioRequest(at=at))
        evidence = result['agents']['quality']['evidence']
        rows.append(dict(at=at, status=result['status'], selected=result['selected_candidate'],
            forecast_status=(result.get('forecast') or {}).get('status'),
            cetane_fresh=evidence['other_quality']['cetane'].get('freshness') == 'fresh',
            t95_fresh=evidence['other_quality']['t95'].get('freshness') == 'fresh',
            sulfur_fresh=evidence['sulfur'].get('freshness') == 'fresh',
            analyzer_conflict=evidence['analyzer_comparison']['conflict'],
            reasons=result['safety_gate']['reasons']))
        if (i+1) % 10 == 0 or i == len(origins)-1:
            args.output.with_suffix('.partial.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
            print(f'{i+1}/{len(origins)}: {time.perf_counter()-started:.1f}s', flush=True)
    sha = lambda p: hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
    result = dict(protocol='Frozen 2026 LIMS origins, +180min, no manual quality, direct product, default controls and fixed freshness. Diagnostic replay only.',
        input_sha256=sha(args.dataset/'observations.parquet'), origins_sha256=sha(origins_file),
        model_sha256=sha(ROOT/'reports/modeling/sulfur-first-iteration/model.json'),
        script_sha256=sha(Path(__file__)), n=len(rows),
        recommendations=sum(r['status']=='recommendation' for r in rows),
        forecast_accepted=sum(r['forecast_status']=='ok' for r in rows),
        fresh_cetane=sum(r['cetane_fresh'] for r in rows), fresh_t95=sum(r['t95_fresh'] for r in rows),
        analyzer_conflicts=sum(r['analyzer_conflict'] for r in rows),
        reason_counts=dict(Counter(reason for r in rows for reason in set(r['reasons']))), rows=rows)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows','reason_counts')},ensure_ascii=False),flush=True)


if __name__ == '__main__': main()
