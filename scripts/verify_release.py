"""Read-only API acceptance checks with compact, reproducible JSON evidence.

HTTP mode uses only the Python standard library. ``--local`` uses the existing
backend development dependencies and TestClient; it never imports or mutates data.
Case timestamps are versioned separately so the evidence can be repeated against
the same imported dataset. This is a contract/safety check, not a model benchmark.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONTROL_IDS = ("ht.T6", "ht.F9", "ht.P13")
BAD_FLAGS = {"invalid", "conflict", "suspect", "flatline", "gap"}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def parse_time(value):
    require(isinstance(value, str), f"Missing timestamp: {value!r}")
    result = datetime.fromisoformat(value)
    require(result.tzinfo is None, "Dataset time must remain timezone-naive source time")
    return result


def reject_nonfinite(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def check_finite(value, path="response"):
    if isinstance(value, float):
        require(math.isfinite(value), f"Non-finite value at {path}")
    elif isinstance(value, dict):
        for key, item in value.items():
            check_finite(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_finite(item, f"{path}[{index}]")


class API:
    def __init__(self, base_url, local=False, timeout=120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.calls = []
        self.client = None
        if local:
            sys.path.insert(0, str(ROOT))
            from fastapi.testclient import TestClient

            from backend.app import app

            # Do not run import-recovery lifespan: all verification is read-only.
            self.client = TestClient(app)

    def request(self, method, path, body=None, expected=200):
        started = time.perf_counter()
        if self.client:
            response = self.client.request(method, path, json=body)
            status, raw = response.status_code, response.content
            content_type = response.headers.get("content-type", "")
        else:
            data = None if body is None else json.dumps(body, allow_nan=False).encode()
            request = Request(self.base_url + path, data=data, method=method,
                              headers={"Content-Type": "application/json", "Accept": "application/json"})
            try:
                response = urlopen(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                status, raw = response.status, response.read()
                content_type = response.headers.get("Content-Type", "")
        self.calls.append({"method": method, "path": path, "status": status,
                           "elapsed_seconds": round(time.perf_counter() - started, 3)})
        require(status == expected, f"{method} {path}: HTTP {status}, expected {expected}; {raw[:500]!r}")
        require("application/json" in content_type, f"{method} {path}: not JSON ({content_type})")
        result = json.loads(raw, parse_constant=reject_nonfinite)
        check_finite(result)
        require(isinstance(result, dict), f"{method} {path}: expected object")
        if expected == 200:
            require("detail" not in result and "error" not in result, f"Unexpected API error: {result}")
        return result


def assert_availability(item, origin, label, lims=False, usable=False):
    if item.get("timestamp") is not None:
        measured = parse_time(item["timestamp"])
        available = parse_time(item.get("available_at"))
        require(measured <= origin and available <= origin, f"{label}: future information")
        delay = timedelta(minutes=240) if lims else timedelta()
        require(available == measured + delay, f"{label}: incorrect publication delay")
    if usable:
        require(item.get("value") is not None, f"{label}: absent value")
        require(item.get("freshness") == "fresh", f"{label}: stale/missing value")
        require(not BAD_FLAGS.intersection(item.get("flags") or []), f"{label}: bad flags")
        require(item.get("timestamp") is not None, f"{label}: absent source time")


def assert_forecast(forecast, origin):
    require(forecast.get("status") in {"ok", "abstain"}, "Forecast status contract")
    require(parse_time(forecast.get("prediction_origin")) == origin, "Forecast origin differs from request")
    require(parse_time(forecast.get("target_time")) == origin + timedelta(minutes=180), "Forecast target is not origin + 3h")
    require(parse_time(forecast.get("feature_cutoff")) == origin, "Forecast feature cutoff differs from origin")
    require(forecast.get("horizon_minutes") == 180, "Forecast horizon must be 180 min")
    require(forecast.get("lims_publication_delay_minutes") == 240, "LIMS publication delay must be separate 240 min")
    require(forecast.get("target", {}).get("metric_id") == "lims.ht.2.Mg.Sulfur", "Wrong model target")
    if forecast.get("feature_time") is not None:
        require(parse_time(forecast["feature_time"]) <= origin, "Forecast used future telemetry")
    previous = forecast.get("previous_lab")
    if previous:
        sample = parse_time(previous.get("sample_time"))
        available = parse_time(previous.get("available_at"))
        require(available == sample + timedelta(minutes=240), "Previous LIMS availability is not sample + 4h")
        require(available <= origin, "Unpublished LIMS leaked into forecast")
    leakage = forecast.get("leakage_check", {})
    require(leakage.get("passed") is True and leakage.get("violations") == 0, "Forecast leakage check failed/missing")
    if forecast["status"] == "abstain":
        require(forecast.get("reasons"), "Forecast abstain requires reasons")
        for key in ("prediction", "prediction_lower", "prediction_upper", "exceedance_probability", "nowcast", "alarm_above_10"):
            require(forecast.get(key) is None, f"Abstain forecast exposes {key} as usable prediction")
    else:
        for key in ("prediction", "prediction_lower", "prediction_upper", "exceedance_probability"):
            require(isinstance(forecast.get(key), (int, float)), f"Forecast has no numeric {key}")
        require(forecast["prediction"] >= 0, "Negative predicted sulfur")
        require(forecast["prediction_lower"] <= forecast["prediction"] <= forecast["prediction_upper"], "Interval does not contain prediction")
        require(0 <= forecast["exceedance_probability"] <= 1, "Exceedance probability outside [0, 1]")
        nowcast = forecast.get("nowcast") or {}
        require(isinstance(nowcast.get("prediction"), (int, float)) and nowcast["prediction"] >= 0, "Forecast has no numeric nowcast")
        require(forecast.get("alarm_above_10") == (forecast["exceedance_probability"] >= forecast["alarm_probability"]
                                                    or forecast["prediction"] > forecast["hard_limit"]), "Incorrect sulfur alarm")
        require(forecast.get("lab_anchor", {}).get("pairs", 0) >= 3, "Nowcast without laboratory anchoring")


def assert_decision(decision, request, expected):
    origin = parse_time(request["at"])
    require(decision.get("status") == expected, f"Expected {expected}, got {decision.get('status')}: {decision.get('abstain')}")
    require([step.get("role") for step in decision.get("trace", [])] == ["quality", "reliability", "optimization", "orchestrator"], "Incomplete ordered agent trace")
    require(all(step.get("consumes") and step.get("produces") for step in decision.get("trace", [])), "Agent trace does not show the information exchange")
    require(isinstance(decision.get("explanation"), dict) and decision["explanation"].get("text"), "Decision has no operator explanation")
    require(isinstance(decision.get("consistency"), list) and isinstance(decision.get("conflicts"), list), "Decision lacks orchestrator checks")
    require(isinstance(decision.get("confidence"), dict) and 0 <= (decision["confidence"].get("score") or 0) <= 1, "Decision lacks a bounded confidence score")
    agents = decision.get("agents", {})
    reliability = agents.get("reliability", {})
    evidence = agents.get("quality", {}).get("evidence", {})
    manual = request.get("current_sulfur") is not None
    require(reliability.get("basis") == ("scenario_only" if manual else "observed_and_forecast"), "Decision evidence basis incorrect")
    sulfur = evidence.get("sulfur", {})
    if not manual:
        source = sulfur.get("source")
        require(source is None or source in {"model.nowcast", "lims.ht.2.Mg.Sulfur", "pak.ht.Mg.Sulfur", "vak.ht.Mg.Sulfur", "ht.Q21"}, "Unexpected sulfur source")
        if source == "model.nowcast":
            require(decision.get("forecast", {}).get("status") == "ok", "Nowcast baseline without usable forecast")
            require(abs(sulfur.get("value") - decision["forecast"]["nowcast"]["prediction"]) < 1e-9, "Baseline differs from forecast nowcast")
        else:
            assert_availability(sulfur, origin, "sulfur", lims=bool(source and source.startswith("lims.")), usable=expected == "recommendation")
    for metric in CONTROL_IDS:
        assert_availability(evidence.get("controls", {}).get(metric, {}), origin, metric, usable=expected == "recommendation")
    candidates = decision.get("candidates") or []
    for candidate in candidates:
        require(candidate.get("feasible") == candidate.get("safety_gate", {}).get("passed"), "Candidate feasibility differs from safety gate")
        if not candidate.get("feasible"):
            require(candidate.get("safety_gate", {}).get("reasons"), "Failed candidate gate has no reasons")
        scenario = candidate.get("scenario")
        if scenario:
            trajectory = scenario.get("trajectory") or []
            require(trajectory, "Candidate has no trajectory")
            require(trajectory[-1].get("minute") == request.get("horizon_minutes", 180), "Trajectory misses exact horizon")
            require(abs(trajectory[-1]["sulfur"] - candidate["predicted_sulfur"]) < 1e-9, "Candidate prediction is not horizon value")
            if candidate.get("feasible"):
                require(candidate["predicted_sulfur"] <= min(10, request.get("targets", {}).get("sulfur_max", 9)) + 1e-9, "Feasible candidate exceeds hard sulfur limit")
                if candidate.get("exceedance_probability") is not None:
                    require(candidate["exceedance_probability"] <= request.get("targets", {}).get("max_exceedance_probability", 0.3) + 1e-9,
                            "Feasible candidate exceeds allowed exceedance probability")
    if expected == "abstain":
        for key in ("recommendation", "selected_candidate", "scenario"):
            require(decision.get(key) is None, f"Unsafe decision exposes {key}")
        require(decision.get("abstain", {}).get("reason"), "Abstain has no reason")
        require(decision.get("safety_gate", {}).get("passed") is False, "Abstain safety gate is not failed")
    else:
        require(reliability.get("can_recommend") is True, "Recommendation bypassed reliability gate")
        require(decision.get("safety_gate", {}).get("passed") is True, "Recommendation bypassed safety gate")
        selected = [item for item in candidates if item.get("id") == decision.get("selected_candidate")]
        require(len(selected) == 1 and selected[0].get("feasible") is True, "Selected candidate is absent/infeasible")
        recommendation = decision.get("recommendation", {})
        require(recommendation.get("action") in {"review_controls", "hold"}, "Prototype must require operator review")
        require(decision.get("selected_candidate") in (decision.get("pareto_front") or []) or decision.get("selected_candidate") == "hold",
                "Selected candidate is not on the Pareto front")
        require(all(check.get("passed") for check in decision.get("consistency", [])), "Recommendation issued with failed consistency checks")
        require(not any(c.get("resolution") == "abstain" for c in decision.get("conflicts", [])), "Recommendation issued despite an abstain-level conflict")
        require(recommendation.get("requires_operator_review") is True, "Operator review flag missing")
        require(recommendation.get("operational_safety_validated") is False, "Unvalidated process safety claimed")
        if not manual:
            require(decision.get("forecast", {}).get("status") == "ok", "Observed recommendation lacks usable forecast")
            if (decision.get("problem") or {}).get("requires_action"):
                # A detected quality problem must be answered by a corrective candidate, never by "hold".
                require(decision.get("selected_candidate") != "hold", "Quality problem answered by hold")
                require(any(abs(c.get("change", 0)) > 0 for c in recommendation.get("controls", {}).values()), "Corrective recommendation changes nothing")
            else:
                require(decision.get("selected_candidate") == "hold", "Stable period answered by an unnecessary control action")
    return candidates


def compact_decision(decision):
    result = {key: decision.get(key) for key in ("status", "selected_candidate", "selection_rule", "safety_gate", "abstain", "trace", "forecast",
                                                 "problem", "confidence", "risk", "constraints", "conflicts", "consistency", "pareto_front",
                                                 "alternatives", "explanation")}
    result["quality_evidence"] = decision.get("agents", {}).get("quality", {}).get("evidence")
    result["reliability"] = decision.get("agents", {}).get("reliability")
    result["candidates"] = [{key: item.get(key) for key in ("id", "status", "feasible", "predicted_sulfur", "exceedance_probability", "effort", "objectives", "safety_gate")} for item in decision.get("candidates") or []]
    result["recommendation"] = {key: value for key, value in (decision.get("recommendation") or {}).items() if key != "model_forecast"} or None
    if result["quality_evidence"]:
        result["quality_evidence"] = {key: value for key, value in result["quality_evidence"].items() if key != "model_forecast"}
    return result


def run_case(api, prefix, case):
    request = {"at": case["at"], **case.get("request", {})}
    origin = parse_time(request["at"])
    frame = api.request("GET", prefix + "/snapshot?" + urlencode({"at": request["at"]}))
    for item in frame.get("values", []):
        assert_availability(item, origin, item["metric_id"], lims=item["metric_id"].startswith("lims.") or item["metric_id"].endswith(".lims"))
    forecast = api.request("GET", prefix + "/forecast?" + urlencode({"at": request["at"]}))
    assert_forecast(forecast, origin)
    if case.get("forecast_status"):
        require(forecast["status"] == case["forecast_status"], f"Expected forecast {case['forecast_status']}, got {forecast['status']}")
    decision = api.request("POST", prefix + "/decision", request)
    candidates = assert_decision(decision, request, case["decision_status"])
    require(decision.get("forecast") == forecast, "Standalone forecast differs from decision forecast")
    if case.get("min_candidates"):
        require(len(candidates) >= case["min_candidates"], "Missing diagnostic candidates")
    if case.get("all_candidates_infeasible"):
        require(candidates and not any(candidate.get("feasible") for candidate in candidates), "Expected all candidates infeasible")
    sulfur = decision.get("agents", {}).get("quality", {}).get("evidence", {}).get("sulfur", {})
    if case.get("observed_sulfur_above_10"):
        require(sulfur.get("value") is not None and sulfur["value"] > 10, "Danger case does not demonstrate observed sulfur exceedance")
        last_lab = decision.get("agents", {}).get("quality", {}).get("evidence", {}).get("last_lab_sulfur", {})
        require(last_lab.get("source", "").startswith("lims.") and (last_lab.get("value") or 0) > 10, "Danger case needs a published LIMS exceedance")
    if case.get("published_lab_above_10"):
        last_lab = decision.get("agents", {}).get("quality", {}).get("evidence", {}).get("last_lab_sulfur", {})
        require(last_lab.get("source", "").startswith("lims.") and (last_lab.get("value") or 0) > 10, "Danger case needs a published LIMS exceedance")
    if case.get("selected_candidate"):
        require(decision.get("selected_candidate") == case["selected_candidate"], f"Expected selected candidate {case['selected_candidate']}")
    if case.get("forecast_alarm") is not None:
        require(forecast.get("alarm_above_10") is case["forecast_alarm"], "Case does not demonstrate expected forecast alarm state")
    if case.get("requires_action") is not None:
        require((decision.get("problem") or {}).get("requires_action") is case["requires_action"], "Case does not demonstrate the expected problem state")
    if case.get("model_fit_end"):
        require((forecast.get("model") or {}).get("fit_end", "").startswith(case["model_fit_end"]), f"Expected walk-forward model {case['model_fit_end']}")
    if case.get("conflict"):
        require(any(c.get("code") == case["conflict"] for c in decision.get("conflicts", [])), f"Case does not demonstrate conflict {case['conflict']}")
    if case.get("risk_class"):
        require((decision.get("risk") or {}).get("class") == case["risk_class"], f"Case does not demonstrate risk class {case['risk_class']}")
    if case.get("selected_candidate_not"):
        require(decision.get("selected_candidate") != case["selected_candidate_not"], f"Selected candidate must not be {case['selected_candidate_not']}")
    if case.get("controls_state"):
        controls = decision["agents"]["quality"]["evidence"]["controls"]
        require(all(value.get("freshness") == case["controls_state"] for value in controls.values()), "Case does not demonstrate expected controls state")
    return {"id": case["id"], "passed": True, "purpose": case["purpose"], "request": request,
            "observed": compact_decision(decision)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--local", action="store_true", help="Use current backend via TestClient; requires existing dev dependencies")
    parser.add_argument("--dataset", default="hackathon")
    parser.add_argument("--cases", type=Path, default=ROOT / "reports/verification/cases.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/verification/latest.json")
    parser.add_argument("--expected-revision")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    api = API(args.base_url, args.local, args.timeout)
    report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
              "transport": "local_testclient" if args.local else "http", "base_url": None if args.local else args.base_url,
              "dataset": args.dataset, "expected_revision": args.expected_revision,
              "scope": "API contract, temporal availability and refusal to recommend unsafe candidates; not causal or forecasting accuracy validation",
              "passed": False, "cases": [], "calls": api.calls, "failures": []}
    try:
        cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
        health = api.request("GET", "/api/health")
        require(health.get("status") == "ok", "Health is not ok")
        if args.expected_revision:
            require(health.get("revision") == args.expected_revision, f"Server revision {health.get('revision')} != expected {args.expected_revision}")
        report["health"] = health
        prefix = "/api/datasets/" + quote(args.dataset, safe="")
        manifest = api.request("GET", prefix)
        require(manifest.get("status") == "ready", "Dataset not ready")
        report["dataset_manifest"] = {key: manifest.get(key) for key in ("id", "name", "status", "start", "end", "telemetry_start", "telemetry_end")}
        for case in cases:
            try:
                result = run_case(api, prefix, case)
                report["cases"].append(result)
                print(f"PASS {case['id']}", flush=True)
            except Exception as error:
                failure = {"id": case["id"], "passed": False, "error": f"{type(error).__name__}: {error}"}
                report["cases"].append(failure)
                report["failures"].append(failure)
                print(f"FAIL {case['id']}: {error}", flush=True)
        invalid_blend = {"at": cases[0]["at"], "current_sulfur": 8, "tanks": [
            {"name": "invalid total 90%", "share": 90, "sulfur": 8, "t95": 350, "cetane": 52}]}
        invalid_result = api.request("POST", prefix + "/scenario", invalid_blend, expected=422)
        require(bool(invalid_result.get("detail")), "Invalid blend returned no explanation")
        report["invalid_blend"] = {"passed": True, "status": 422, "detail": invalid_result["detail"]}
        report["passed"] = not report["failures"]
    except Exception as error:
        report["failures"].append({"error": f"{type(error).__name__}: {error}"})
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "case_count": len(report["cases"]), "failures": report["failures"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
