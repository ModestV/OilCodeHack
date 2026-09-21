import numpy as np
import pandas as pd
import pytest

from tools.modeling.cost_sensitive_risk import metrics, paired_interval, select_threshold


def test_hard_limit_is_strict_and_cost_counts_only_errors():
    result = metrics([10, 11, 12, 7], [True, False, True, False], 5)
    assert (result['tp'], result['fp'], result['fn'], result['tn']) == (1, 1, 1, 1)
    assert result['total_cost'] == 6
    assert result['cost_per_100'] == 150


def test_cost_tie_prefers_fewer_missed_events():
    # A constant risk cannot distinguish the event: at equal error cost, alarm wins.
    selected = select_threshold([8, 12], [.2, .2], 1)
    assert selected['selection2024']['fn'] == 0
    assert selected['selection2024']['fp'] == 1


def test_never_alarm_includes_probability_one():
    selected = select_threshold([8, 9], [0., 1.], 10)
    assert selected['threshold'] > 1
    assert selected['selection2024']['total_cost'] == 0


def test_policy_choice_matches_exhaustive_possible_alarm_sets():
    p = np.array([.1, .2, .2, .7, .9])
    y = np.array([8, 11, 8, 12, 8])
    for ratio in (1, 5, 20):
        chosen = select_threshold(y, p, ratio)
        cost = min(metrics(y, p >= t, ratio)['total_cost'] for t in [0, .1, .2, .7, .9, 2])
        assert chosen['selection2024']['total_cost'] == cost


def test_paired_blocks_preserve_sample_weight_and_identical_policy_zero():
    y = [11, 11, 11, 8]
    origins = pd.to_datetime(['2025-01-01']*3+['2025-02-01'])
    result = paired_interval(y, [False]*4, [True]*4, origins, 5, iterations=200)
    assert result['delta_cost_per_100'] == 350
    same = paired_interval(y, [True]*4, [True]*4, origins, 5, iterations=200)
    assert same['ci95_per_100'] == [0., 0.]


@pytest.mark.parametrize('p', [[], [np.nan], [-.1], [1.1]])
def test_bad_probabilities_cannot_silently_choose_a_policy(p):
    with pytest.raises(ValueError):
        select_threshold([8]*len(p), p, 5)
