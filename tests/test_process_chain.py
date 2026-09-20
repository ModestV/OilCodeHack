from pathlib import Path
import json
import pytest
from pydantic import ValidationError
from backend import agents
from backend.agents import AgentContext, OptimizationAgent
from backend.scenarios import ScenarioRequest, calculate_scenario, select_sulfur
from backend.formulas import LIMS_ALIASES

AT = '2026-05-01T12:00:00'
FRAME = {'values': [{'metric_id':k,'value':v,'timestamp':AT,'available_at':AT,'flags':[],'freshness':'fresh'}
                    for k,v in [('ht.T6',360),('ht.F9',210),('ht.P13',4),('pak.ht.Mg.Sulfur',11)]]}


def request(**updates):
    data=dict(at=AT,current_sulfur=11,current_t95=350,current_cetane=52,
              batch_mass_t=100,production_rate_tph=40,
              tanks=[dict(name='new',kind='hydrotreated_batch',stock_t=0,share=80,sulfur=11,t95=350,cetane=52),
                     dict(name='stored',stock_t=100,share=20,sulfur=2,t95=300,cetane=53)],
              changes=dict(temperature=0),optimize_economics=False)
    data.update(updates)
    return ScenarioRequest(**data)


def calc(req):
    return calculate_scenario(Path('.'),req,frame=FRAME)


def gate(req):
    return OptimizationAgent._gate(calc(req),AgentContext(Path('.'),req,FRAME),
        {'can_recommend':True,'basis':'scenario_only','reasons':[]},{'evidence':{}})


def test_component_above_ten_is_allowed_when_final_blend_meets_spec():
    req=request();s=calc(req)
    assert s['predicted_sulfur']==11
    assert s['product_sulfur']==pytest.approx(9.2)
    assert gate(req)['passed']
    assert not gate(request(intermediate_sulfur_max=10))['passed']


def test_upstream_change_propagates_via_integrated_arriving_batch():
    base=calc(request());changed=calc(request(changes=dict(temperature=10)))
    # 90m ramp then 90m at 10.2: average 10.4, not the endpoint 10.2.
    assert changed['batch']['produced_sulfur']==pytest.approx(10.4)
    assert changed['product_sulfur']==pytest.approx(.8*10.4+.2*2)
    assert changed['product_sulfur']<base['product_sulfur']
    finer=calc(request(changes=dict(temperature=10),step_minutes=15))
    assert finer['product_sulfur']==changed['product_sulfur']


def test_stock_balance_includes_existing_inventory_and_new_arrivals():
    req=request();req.tanks[0].stock_t=120;req.tanks[0].sulfur=5
    s=calc(req)
    assert s['batch']['produced_t']==120
    assert s['blend']['components'][0]['sulfur']==8
    assert s['blend']['components'][0]['required_t']==80


def test_transport_delay_and_zero_horizon_cannot_invent_inventory():
    assert not gate(request(transport_delay_minutes=180))['passed']
    assert not gate(request(horizon_minutes=0))['passed']
    assert not gate(request(batch_mass_t=1000))['passed']


def test_dead_time_precedes_response_and_changes_batch_average():
    s=calc(request(changes=dict(temperature=10),parameters=dict(dead_time_minutes=120,lag_minutes=60)))
    assert s['trajectory'][4]['sulfur']==11  # 120min
    assert s['batch']['produced_sulfur']==pytest.approx((120*11+60*10.6)/180)


def test_additive_cannot_create_t95_improvement_or_unknown_sulfur_credit():
    a=calc(request());req=request(additive_pct=3)
    b=calc(req)
    assert b['blend']['t95']==a['blend']['t95']
    assert b['blend']['sulfur']==a['blend']['sulfur']
    assert not gate(req)['passed']
    known=request(additive_pct=3,parameters=dict(additive_sulfur_mgkg=0))
    assert calc(known)['blend']['sulfur']==pytest.approx(9.2*.97)
    assert calc(known)['blend']['additive_dose_kg_t']==30


def test_link_requires_explicit_mass_basis_not_suspect_csv_flow():
    with pytest.raises(ValidationError):request(production_rate_tph=None)
    with pytest.raises(ValidationError):request(batch_mass_t=None)


def test_additive_inventory_is_part_of_final_batch_mass_balance():
    req=request(additive_pct=1,additive_stock_t=.5,parameters=dict(additive_sulfur_mgkg=0))
    assert not gate(req)['passed']
    req.additive_stock_t=1
    result=calc(req)
    assert gate(req)['passed']
    assert sum(t['required_t'] for t in result['blend']['components'])+result['blend']['additive_inventory']['required_t']==100


def test_stored_tanks_do_not_change_when_upstream_changes():
    req=request();req.tanks[0].kind='stored';req.tanks[0].stock_t=100
    before=calc(req);req.changes.temperature=10
    assert calc(req)['product_sulfur']==before['product_sulfur']
    assert not gate(req)['passed']


def test_forecast_alarm_is_checked_after_blending_without_control_credit():
    req=request(current_sulfur=None);context=AgentContext(Path('.'),req,FRAME)
    quality={'evidence':{'model_forecast':{'prediction_risk_guard':11,'alarm_above_10':True}}}
    reliability={'can_recommend':True,'basis':'observed_and_forecast','reasons':[]}
    assert OptimizationAgent._gate(calc(req),context,reliability,quality)['passed']
    quality['evidence']['model_forecast']['prediction_risk_guard']=15
    assert not OptimizationAgent._gate(calc(req),context,reliability,quality)['passed']


def test_economic_variants_exist_and_small_gain_can_be_rejected(monkeypatch,tmp_path):
    monkeypatch.setattr(agents,'snapshot',lambda *a, **kw:FRAME)
    monkeypatch.setattr(agents,'forecast_sulfur',lambda *a, **kw:{'status':'ok','alarm_above_10':False})
    req=request(current_sulfur=8,tanks=[],optimize_economics=True,minimum_economic_gain=0)
    result=agents.make_decision(tmp_path,req)
    assert {'lower_heat','more_feed','lower_pressure'} <= {c['id'] for c in result['candidates']}
    assert result['selected_candidate']!='hold'
    req.minimum_economic_gain=10
    assert agents.make_decision(tmp_path,req)['selected_candidate']=='hold'


def test_recipe_candidates_respect_mass_shares_and_stock(monkeypatch,tmp_path):
    monkeypatch.setattr(agents,'snapshot',lambda *a, **kw:FRAME)
    monkeypatch.setattr(agents,'forecast_sulfur',lambda *a, **kw:{'status':'ok','alarm_above_10':False})
    req=request(optimize_recipe=True,parameters=dict(additive_sulfur_mgkg=0))
    result=agents.make_decision(tmp_path,req)
    assert any('_mix' in c['id'] for c in result['candidates'])
    for c in result['candidates']:
        if c['scenario']:
            assert sum(t['share'] for t in c['scenario']['applied_recipe']['tanks'])==100
    assert result['recommendation']['applied_recipe']==result['scenario']['applied_recipe']


def test_online_analyzer_priority_uses_quality_then_recency():
    values={'pak.ht.Mg.Sulfur':{'value':8,'freshness':'stale','timestamp':AT},
            'ht.Q21':{'value':9,'freshness':'fresh','timestamp':AT}}
    assert select_sulfur(values)==(9,'ht.Q21')


def test_updated_formulas_and_unresolved_lims_are_versioned():
    r=json.loads((Path(__file__).parents[1]/'backend/resources/registry.json').read_text(encoding='utf8'))
    f={f['id']:f for f in r['formulas']}
    assert 'F65/' in f['AVT6:240-350:D15']['expression']
    assert '+0.76664*T6' in f['AVT6:350:I350']['expression']
    assert f['24-2000:GODT:T95']['status']=='unresolved'
    assert f['AVT6:240-350:CFPP']['status']=='unresolved'
    assert not LIMS_ALIASES


def test_selected_recipe_round_trip_preserves_entire_request(monkeypatch,tmp_path):
    monkeypatch.setattr(agents,'snapshot',lambda *a, **kw:FRAME)
    monkeypatch.setattr(agents,'forecast_sulfur',lambda *a, **kw:{'status':'ok','alarm_above_10':False})
    req=request(optimize_recipe=True,parameters=dict(additive_sulfur_mgkg=0,dead_time_minutes=30),transport_delay_minutes=15)
    result=agents.make_decision(tmp_path,req)
    selected=result['scenario']
    replay=calc(ScenarioRequest(**selected['model_request']))
    assert replay['product_sulfur']==selected['product_sulfur']
    assert replay['batch']==selected['batch']
    assert replay['blend']==selected['blend']
    assert replay['controls']==selected['controls']


def test_other_horizon_does_not_borrow_h3_forecast_support(monkeypatch,tmp_path):
    monkeypatch.setattr(agents,'snapshot',lambda *a, **kw:FRAME)
    monkeypatch.setattr(agents,'forecast_sulfur',lambda *a, **kw:{'status':'ok','alarm_above_10':False,'forecast_horizon_minutes':180})
    result=agents.make_decision(tmp_path,request(current_sulfur=None,horizon_minutes=60))
    assert result['status']=='abstain'
    assert any('Горизонт' in s for s in result['agents']['reliability']['reasons'])


def test_fresh_online_analyzer_disagreement_blocks_observed_decision(monkeypatch,tmp_path):
    frame={'values':FRAME['values']+[dict(FRAME['values'][-1],metric_id='ht.Q21',value=5)]}
    monkeypatch.setattr(agents,'snapshot',lambda *a, **kw:frame)
    monkeypatch.setattr(agents,'forecast_sulfur',lambda *a, **kw:{'status':'ok','alarm_above_10':False})
    result=agents.make_decision(tmp_path,request(current_sulfur=None))
    assert result['status']=='abstain'
    assert result['agents']['quality']['evidence']['analyzer_comparison']['conflict']
