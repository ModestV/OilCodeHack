"""Research-only checks. Run with requirements-modeling.txt installed."""
import numpy as np
import pandas as pd

from tools.modeling.improve_sulfur import (
    Kinetics, choose_threshold, extended_features, finite_metrics,
)
from tools.modeling.adaptive_sulfur import delayed_correction, anchored_kinetics


def fixture():
    times=pd.date_range('2025-01-02 11:30',periods=5,freq='15min')
    values={'T6':360.,'T11':365.,'P13':4.,'P8':.2,'F15':3400.,
            'F25':13000.,'F9':210.,'F14':2.,'F17':200.,'Q20':8000.}
    telemetry=pd.DataFrame({'242000__'+k:v for k,v in values.items()},index=times)
    labs=pd.DataFrame({'target_time':pd.to_datetime(['2025-01-01 12:00','2025-01-02 08:00','2025-01-02 09:00']),
                       'target':[7.,8.,12.]})
    pak=pd.DataFrame({'pak_sulfur':[7.,8.,9.,10.,11.]},index=times)
    return telemetry,labs,pak,pd.DatetimeIndex(['2025-01-02 12:00'])


def test_future_telemetry_pak_and_unpublished_lab_do_not_change_features():
    telemetry,labs,pak,origins=fixture()
    before=extended_features(telemetry,labs,origins,pak)
    telemetry.loc[telemetry.index>origins[0]]=1e9
    pak.loc[pak.index>origins[0]]=1e9
    labs.loc[labs.target_time+pd.Timedelta(hours=4)>origins[0],'target']=1e9
    after=extended_features(telemetry,labs,origins,pak)
    pd.testing.assert_frame_equal(before,after)
    assert after.previous_lab_available.iloc[0]==8.
    assert after.lab_median3.iloc[0]==7.5
    assert after.pak_sulfur.iloc[0]==9.


def test_nonpositive_physical_denominators_are_missing_not_infinite():
    telemetry,labs,pak,origins=fixture()
    telemetry[['242000__F15','242000__F9','242000__P13']]=0.
    x=extended_features(telemetry,labs,origins,pak)
    assert np.isnan(x.phys_gas_oil.iloc[0])
    assert np.isnan(x.phys_quench_feed_ratio.iloc[0])
    assert np.isnan(x.phys_pressure_drop_fraction.iloc[0])
    assert not np.isinf(x.to_numpy()).any()
    assert x.phys_kelvin.iloc[0]==633.15


def test_kinetic_direction_holds_other_inputs_fixed():
    base=np.array([[637.15,8000.,4.,3400.,4.]])
    p=np.array([1.9,7000.,.5,.2])
    reference=Kinetics.response(base,p)[0]
    for column in [0,2,4]:
        changed=base.copy(); changed[0,column]*=1.02
        assert Kinetics.response(changed,p)[0]<reference
    changed=base.copy();changed[0,3]*=1.1
    assert Kinetics.response(changed,p)[0]>reference


def test_alarm_threshold_respects_validation_false_positive_budget():
    y=np.array([1.,2.,3.,4.,11.,12.]);scores=np.array([.1,.3,.7,.8,.6,.9])
    result=choose_threshold(y,scores,.25)
    assert result['fpr']<=.25
    assert result['recall']==.5


def test_numerical_failure_preserves_denominator():
    m=finite_metrics(np.array([1.,2.]),np.array([1.,np.inf]))
    assert m['n']==2 and m['nonfinite_predictions']==1 and m['mae'] is None


def test_online_correction_only_uses_published_forecast_errors():
    samples=pd.date_range('2025-01-02 00:00',periods=5,freq='12h')
    origins=samples-pd.Timedelta(hours=3)
    p=np.full(5,8.);y=np.array([9.,9.,9.,100.,100.])
    result=delayed_correction(p,y,samples,origins,7,.5)
    assert result[2]==8.  # Fewer than three published feedback points.
    assert result[3]==8.5  # Its own target 100 is not yet known.
    y[3:]=10000.
    changed=delayed_correction(p,y,samples,origins,7,.5)
    np.testing.assert_array_equal(result[:4],changed[:4])


def test_anchored_kinetics_reproduces_last_lab_at_unchanged_conditions():
    telemetry,labs,pak,origins=fixture()
    x=extended_features(telemetry,labs,origins,pak)
    sf=pd.DataFrame({'242000__T6':[360.]*3,'242000__P13':[4.]*3,
                     '242000__F15':[3400.]*3,'242000__Q20':[8000.]*3})
    p,valid=anchored_kinetics(x,sf,labs,origins,60000,.5,8.5)
    assert valid[0]
    np.testing.assert_allclose(p,[8.],atol=1e-10)
    # Features and targets for the as-yet unpublished sample cannot affect it.
    sf.loc[2,:]=1e9;labs.loc[2,'target']=1e9
    changed,_=anchored_kinetics(x,sf,labs,origins,60000,.5,8.5)
    np.testing.assert_array_equal(p,changed)
