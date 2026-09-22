import numpy as np
import pandas as pd
import pytest

from tools.modeling.quality_targets import asof_value, cetane_index, residual_bound, metrics, features, TARGETS, LABS, CONTROLS


def test_lims_available_only_four_hours_after_sample():
    rows=pd.DataFrame({'timestamp':pd.to_datetime(['2026-01-01 10:00','2026-01-02 10:00']), 'value':[52.,50.]})
    result=asof_value(rows,pd.to_datetime(['2026-01-02 13:59','2026-01-02 14:00','2026-01-01 13:59']))
    assert result.value.iloc[:2].tolist()==[52.,50.]
    assert np.isnan(result.value.iloc[2])


def test_freshness_counts_sample_age_not_publication_age():
    rows=pd.DataFrame({'timestamp':pd.to_datetime(['2026-01-01 10:00']), 'value':[350.]})
    result=asof_value(rows,pd.to_datetime(['2026-01-03 10:00','2026-01-03 10:01']))
    assert result.value.iloc[0]==350
    assert np.isnan(result.value.iloc[1])


def test_index_uses_kg_to_g_conversion_and_base_ten_log():
    # Independent hand-evaluated expression for 850 kg/m3 and T50=280 C.
    expected=454.74-1641.416*.85+774.74*.85**2-.554*280+97.803*np.log10(280)**2
    assert cetane_index(850,280)==pytest.approx(expected)
    assert 45<float(cetane_index(850,280))<55
    assert np.isnan(cetane_index(.85,280))
    assert np.isnan(cetane_index(850,0))


def test_bound_refuses_small_samples_and_uses_finite_sample_rank():
    assert residual_bound(np.arange(29)) is None
    assert residual_bound(np.arange(30))==27


def test_quality_limits_include_boundary_and_missing_is_not_safe():
    c=metrics([51,50,52],[51,52,np.nan],'cetane')
    assert c['n']==2 and c['violations']==1 and c['missed']==1
    t=metrics([360,361],[360,362],'t95')
    assert t['violations']==1 and t['detected']==1


def test_future_labels_and_inputs_do_not_change_features():
    times=pd.to_datetime(['2026-01-01 10:00','2026-01-02 10:00','2026-01-03 10:00'])
    ids=set([*TARGETS.values(),*LABS.values(),*['ht.'+c for c in CONTROLS]])
    groups={k:pd.DataFrame({'timestamp':times,'value':[52.,53.,54.]}) for k in ids}
    groups[LABS['d15']]['value']=[830.,831.,832.]
    for k in LABS.values():
        if k!=LABS['d15']:groups[k]['value']=[270.,271.,272.]
    origins=pd.to_datetime(['2026-01-02 13:00'])
    before=features(groups,'cetane',origins)
    for g in groups.values():g.loc[g.timestamp>=pd.Timestamp('2026-01-03'),'value']=9999.
    after=features(groups,'cetane',origins)
    pd.testing.assert_frame_equal(before,after)
    assert before.previous.iloc[0]==52
