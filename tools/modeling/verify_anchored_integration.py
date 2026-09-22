"""Real API contract and observed batch round-trip checks on imported hackathon data."""
from pathlib import Path
from copy import deepcopy
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from backend.app import app
from backend.forecast import ARTIFACT


def main():
    output = ROOT / 'reports/verification/anchored-integration'
    output.mkdir(parents=True, exist_ok=True)
    at = '2026-05-01T12:00:00'
    evidence = {'at': at, 'requests': []}
    with TestClient(app) as client:
        for horizon in [0, 60, 120, 180]:
            response = client.get('/api/datasets/hackathon/forecast', params={'at': at, 'horizon_minutes': horizon})
            assert response.status_code == 200, response.text
            f = response.json()
            assert f['horizon_minutes'] == horizon
            assert f['leakage_check']['passed']
            assert f['status'] == 'ok'
            evidence['requests'].append({'horizon': horizon, 'prediction': f['prediction'],
                                         'path_supported': f['path_supported'], 'target_time': f['target_time']})
        invalid = client.get('/api/datasets/hackathon/forecast', params={'at': at, 'horizon_minutes': 181})
        assert invalid.status_code == 422
        # Tank properties/stocks below are explicit what-if inputs, not measured inventory.
        req = dict(at=at, horizon_minutes=60, step_minutes=15, optimize_economics=False,
                   production_rate_tph=40, batch_mass_t=30, transport_delay_minutes=15, changes={},
                   tanks=[dict(name='receiving', kind='hydrotreated_batch', share=80, stock_t=100,
                               sulfur=5, t95=350, cetane=52),
                          dict(name='stock', share=20, stock_t=100, sulfur=2, t95=320, cetane=53)])
        response = client.post('/api/datasets/hackathon/decision', json=req)
        assert response.status_code == 200, response.text
        d = response.json()
        assert d['status'] == 'recommendation', d['safety_gate']
        selected = d['scenario']
        assert selected['baseline']['sulfur_source'] == 'model.nowcast'
        replay = client.post('/api/datasets/hackathon/scenario', json=selected['model_request'])
        assert replay.status_code == 200, replay.text
        r = replay.json()
        for key in ('product_sulfur', 'predicted_sulfur', 'batch', 'blend', 'controls'):
            assert r[key] == selected[key], key
        evidence['linked_batch'] = {'request': deepcopy(req), 'status': d['status'],
                                    'selected': d['selected_candidate'], 'batch': selected['batch'],
                                    'product_sulfur': selected['product_sulfur'],
                                    'forecast_horizon': d['forecast']['horizon_minutes'],
                                    'round_trip_equal': True,
                                    'basis': 'observed forecast with supplied scenario inventory/properties'}
        req['tanks'][0]['stock_t'] = 0
        req['transport_delay_minutes'] = 60
        blocked = client.post('/api/datasets/hackathon/decision', json=req)
        assert blocked.status_code == 200
        blocked = blocked.json()
        assert blocked['status'] == 'abstain'
        assert any('запаса' in x for x in blocked['safety_gate']['reasons'])
        evidence['zero_arrival_stock_gate'] = blocked['safety_gate']['reasons']
    evidence.update(passed=True, model_sha256=hashlib.sha256(ARTIFACT.read_bytes()).hexdigest(),
                    script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (output / 'api-integration.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
