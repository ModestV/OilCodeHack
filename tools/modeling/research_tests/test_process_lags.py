import pandas as pd
from tools.modeling.process_lags import fresh_lab


def test_lab_result_cannot_enter_features_before_publication():
    labs=pd.DataFrame({'target_time':pd.to_datetime(['2025-01-01T00:00']), 'target':[8000.]})
    origins=pd.DatetimeIndex(['2025-01-01T03:59','2025-01-01T04:00'])
    values,age=fresh_lab(labs,origins)
    assert pd.isna(values[0])
    assert values[1]==8000
    assert age[1]==4


def test_lab_freshness_is_measured_from_sample_not_publication():
    labs=pd.DataFrame({'target_time':pd.to_datetime(['2025-01-01T00:00']), 'target':[8000.]})
    origins=pd.DatetimeIndex(['2025-01-03T00:00','2025-01-03T00:01'])
    values,age=fresh_lab(labs,origins)
    assert values[0]==8000
    assert age[0]==48
    assert pd.isna(values[1]) and pd.isna(age[1])
