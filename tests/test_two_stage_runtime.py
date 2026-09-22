"""Runtime adapter of the repaired two-stage (v4r) forecast: parity, contract and opt-in dispatch."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend import forecast as forecast_module
from backend import forecast_two_stage as runtime
from backend.forecast import ForecastUnavailable
from backend.scenarios import _baseline_knots
from tools.modeling import two_stage_forecast as tsf

sys.path.insert(0, str(Path(__file__).parent))
from test_two_stage_temporal import SYN_FOLDS, synthetic_plant  # noqa: E402

FIT_ENDS = (SYN_FOLDS["B"][1].isoformat(), SYN_FOLDS["C"][1].isoformat())


def _write_observations(directory: Path, analysers, controls, labs) -> None:
    rows = []
    for name, series in analysers.items():
        rows.append(pd.DataFrame({"metric_id": tsf.ANALYSER_METRICS[name], "timestamp": series.index, "value": series.to_numpy(),
                                  "source": "kip"}))
    for name, series in controls.items():
        rows.append(pd.DataFrame({"metric_id": tsf.CONTROL_METRICS[name], "timestamp": series.index, "value": series.to_numpy(),
                                  "source": "kip"}))
    rows.append(pd.DataFrame({"metric_id": tsf.TARGET_METRIC, "timestamp": labs["target_time"], "value": labs["target"],
                              "source": "lims"}))
    frame = pd.concat(rows, ignore_index=True)
    frame["unit"] = ""
    frame["flags"] = ""
    frame["source_file"] = "synthetic"
    frame["source_row"] = np.arange(len(frame))
    directory.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(directory / "observations.parquet", index=False)


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    root = tmp_path_factory.mktemp("two_stage")
    data, output = root / "data", root / "model"
    _write_observations(data, *synthetic_plant())
    tsf.run(data, output, fit_ends=FIT_ENDS, folds=SYN_FOLDS)
    predictions = pd.read_csv(output / "predictions.csv")
    return data, output / "model.json", predictions[predictions.split.eq("walk_forward")]


def test_runtime_reproduces_offline_walk_forward_rows(trained):
    data, artifact, predictions = trained
    picks = []
    for (_, _), group in predictions.groupby(["model_fit_end", "horizon_minutes"]):
        picks.append(group.iloc[np.linspace(0, len(group) - 1, min(4, len(group)), dtype=int)])
    checked = 0
    for _, row in pd.concat(picks).iterrows():
        served = runtime.forecast_sulfur(data, row.prediction_origin, artifact, row.horizon_minutes)
        assert served["status"] == row.forecast_status
        assert served["model"]["fit_end"][:10] == row.model_fit_end[:10]
        if served["status"] == "ok":
            for key in ("prediction", "prediction_lower", "prediction_upper", "exceedance_probability"):
                assert served[key] == pytest.approx(row[key], rel=1e-9, abs=1e-9)
            checked += 1
    assert checked >= 16


def test_terminal_level_is_shared_by_point_interval_probability_and_trajectory(trained):
    data, artifact, predictions = trained
    origin = predictions[predictions.forecast_status.eq("ok")].prediction_origin.iloc[-1]
    served = runtime.forecast_sulfur(data, origin, artifact, 90)
    assert served["status"] == "ok" and served["path_supported"]
    by_h = {r["minutes"]: r for r in served["horizons"]}
    ln_mid = 0.5 * (np.log(by_h[60]["prediction"]) + np.log(by_h[120]["prediction"]))
    assert served["prediction"] == pytest.approx(np.exp(ln_mid), rel=1e-12)
    entry = runtime.select_model(runtime.load_artifact(artifact), origin)
    again = runtime.exceedance_at(entry, 90, ln_mid)
    assert (served["prediction_lower"], served["prediction_upper"], served["exceedance_probability"]) == pytest.approx(
        (again["lower"], again["upper"], again["exceedance_probability"]), rel=1e-12)
    knots = _baseline_knots(served, 8.0, 90)
    assert knots[-1] == (90.0, pytest.approx(served["prediction"], rel=1e-12))


def test_output_keeps_the_v3_contract_used_by_agents(trained):
    data, artifact, predictions = trained
    served = runtime.forecast_sulfur(data, predictions.prediction_origin.iloc[-1], artifact, 180)
    for key in ("status", "reasons", "nowcast", "nowcast_applicability", "path_supported", "prediction", "prediction_lower",
                "prediction_upper", "exceedance_probability", "alarm_probability", "alarm_above_10", "horizons", "leakage_check"):
        assert key in served
    assert served["alarm_probability"] == tsf.ALARM_PROBABILITY
    assert served["leakage_check"]["passed"]


def test_forecast_ignores_quality_freshness_settings(trained, tmp_path):
    """CN/T95 freshness belongs to the quality agent; changing it must not move the forecast."""
    data, artifact, predictions = trained
    origin = predictions.prediction_origin.iloc[-5]
    before = runtime.forecast_sulfur(data, origin, artifact, 120)
    (data / "settings.json").write_text(json.dumps({"freshness_minutes": {"lims": 60 * 24 * 60}}), encoding="utf-8")
    try:
        after = runtime.forecast_sulfur(data, origin, artifact, 120)
    finally:
        (data / "settings.json").unlink()
    assert before == after
    for module in (forecast_module, runtime):
        assert "settings(" not in Path(module.__file__).read_text(encoding="utf-8")


def test_dispatch_defaults_to_v3_and_opts_in_by_environment(monkeypatch):
    monkeypatch.delenv(forecast_module.FORECAST_MODEL_ENV, raising=False)
    assert forecast_module.selected_model() == "v3"
    calls = []
    monkeypatch.setattr(runtime, "forecast_sulfur", lambda *a: calls.append(a) or {"status": "ok"})
    monkeypatch.setattr(forecast_module, "forecast_sulfur_v3", lambda *a: {"status": "v3"})
    assert forecast_module.forecast_sulfur(Path("."), "2026-01-01T00:00")["status"] == "v3"
    monkeypatch.setenv(forecast_module.FORECAST_MODEL_ENV, forecast_module.TWO_STAGE_MODEL)
    assert forecast_module.forecast_sulfur(Path("."), "2026-01-01T00:00")["status"] == "ok"
    assert calls and calls[0][2] == runtime.ARTIFACT


def test_adapter_rejects_unrepaired_v4_and_undeployable_artifacts(trained, tmp_path):
    _, artifact, _ = trained
    content = json.loads(artifact.read_text(encoding="utf-8"))
    old = dict(content, version=4)
    (tmp_path / "v4.json").write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(ForecastUnavailable):
        runtime.load_artifact(tmp_path / "v4.json")
    content["walk_forward"][0]["deployable"] = False
    (tmp_path / "nodeploy.json").write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ForecastUnavailable):
        runtime.load_artifact(tmp_path / "nodeploy.json")
