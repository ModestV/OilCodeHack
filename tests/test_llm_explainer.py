"""Optional local LLM explainer: paraphrases the decision, never changes it.

A fake OpenAI-compatible server stands in for Ollama / vLLM / the organisers'
gateway.  Any failure or any claim not grounded in the decision falls back to
the deterministic template.
"""

import json
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from test_decision_matrix import decide, frame

from backend import llm


class FakeLLM:
    def __init__(self):
        self.reply = "Ок"
        self.status = 200
        self.delay = 0.0
        self.raw = None
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                fake.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": json.loads(self.rfile.read(length))})
                time.sleep(fake.delay)
                body = fake.raw if fake.raw is not None else json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": fake.reply}}]})
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *_args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake(monkeypatch):
    server = FakeLLM()
    monkeypatch.setenv("OILCODE_LLM_BASE_URL", server.url)
    monkeypatch.setenv("OILCODE_LLM_MODEL", "qwen-test")
    monkeypatch.setenv("OILCODE_LLM_TIMEOUT_S", "0.5")
    yield server
    server.close()


def recommendation(monkeypatch, tmp_path):
    return decide(monkeypatch, tmp_path, frame())


def test_disabled_by_default_makes_no_call(monkeypatch, tmp_path):
    monkeypatch.delenv("OILCODE_LLM_BASE_URL", raising=False)
    assert llm.settings()["enabled"] is False
    decision = recommendation(monkeypatch, tmp_path)
    assert decision["explanation"]["source"] == "template"
    assert "llm" not in decision["explanation"]


def test_grounded_answer_is_used_and_audited(fake, monkeypatch, tmp_path):
    fake.reply = "Режим стабилен: удерживаем текущие T6, F9 и P13. Прогноз серы 8.0 мг/кг ниже предела 10 мг/кг."
    decision = recommendation(monkeypatch, tmp_path)
    explanation = decision["explanation"]
    assert explanation["source"] == "llm", explanation.get("fallback_reason")
    assert explanation["text"].startswith(fake.reply)
    assert "технолог" in explanation["text"]
    assert explanation["template_text"]
    assert explanation["llm"]["model"] == "qwen-test"
    assert explanation["llm"]["validation"]["passed"] is True
    request = fake.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["body"]["model"] == "qwen-test"
    assert request["body"]["temperature"] == 0
    facts = json.loads(request["body"]["messages"][-1]["content"])
    assert facts["status"] == "recommendation" and facts["selected_candidate"] == "hold"


def test_invented_number_falls_back_to_template(fake, monkeypatch, tmp_path):
    fake.reply = "Удерживаем режим, сера снизится до 6.3 мг/кг."
    explanation = recommendation(monkeypatch, tmp_path)["explanation"]
    assert explanation["source"] == "template"
    assert "6.3" in explanation["fallback_reason"]


def test_advice_during_abstain_falls_back(fake, monkeypatch, tmp_path):
    fake.reply = "Рекомендуется повысить T6, чтобы снизить серу."
    decision = decide(monkeypatch, tmp_path, frame(ht__F9={"flags": ["flatline"]}))
    assert decision["status"] == "abstain"
    assert decision["explanation"]["source"] == "template"
    assert "abstain" in decision["explanation"]["fallback_reason"]


def test_changed_control_must_be_named_and_hold_must_say_hold():
    controls = {"ht.T6": {"current": 360.0, "change": 2.0, "recommended": 362.0},
                "ht.F9": {"current": 210.0, "change": 0, "recommended": 210.0},
                "ht.P13": {"current": 3.9, "change": 0, "recommended": 3.9}}
    decision = {"status": "recommendation", "selected_candidate": "automatic",
                "recommendation": {"candidate_id": "automatic", "controls": controls}}
    assert not llm.validate_semantics("Повышаем температуру на входе реактора.", decision)["passed"]
    assert llm.validate_semantics("Повышаем T6 с 360 до 362 °C.", decision)["passed"]
    hold = {"status": "recommendation", "selected_candidate": "hold",
            "recommendation": {"candidate_id": "hold", "controls": {k: {**v, "change": 0} for k, v in controls.items()}}}
    assert not llm.validate_semantics("Предлагается изменить режим.", hold)["passed"]
    assert llm.validate_semantics("Удерживаем текущий режим.", hold)["passed"]


@pytest.mark.parametrize("failure", ["timeout", "http_500", "bad_json", "empty"])
def test_transport_failures_fall_back(fake, monkeypatch, tmp_path, failure):
    if failure == "timeout":
        fake.delay = 1.0
    elif failure == "http_500":
        fake.status = 500
    elif failure == "bad_json":
        fake.raw = "not json"
    else:
        fake.reply = "   "
    explanation = recommendation(monkeypatch, tmp_path)["explanation"]
    assert explanation["source"] == "template"
    assert explanation["fallback_reason"]


def test_decision_is_identical_with_and_without_llm(fake, monkeypatch, tmp_path):
    fake.reply = "Удерживаем текущий режим T6, F9 и P13."
    with_llm = deepcopy(recommendation(monkeypatch, tmp_path))
    monkeypatch.delenv("OILCODE_LLM_BASE_URL")
    without = recommendation(monkeypatch, tmp_path)
    with_llm.pop("explanation"), without.pop("explanation")
    assert with_llm == without


def test_api_key_is_sent_only_as_bearer_header(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("OILCODE_LLM_API_KEY", "secret-token")
    fake.reply = "Удерживаем текущий режим."
    decision = recommendation(monkeypatch, tmp_path)
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in json.dumps(decision, ensure_ascii=False)
    assert "secret-token" not in json.dumps(llm.settings())


def test_validator_accepts_roundings_and_rejects_new_numbers():
    facts = {"prediction": 8.4293, "probability": 0.1206, "limit": 10}
    assert llm.validate_numbers("Сера 8.4 мг/кг, предел 10", facts, "")["passed"]
    assert llm.validate_numbers("P(>10) = 12%", facts, "P(>10) = 12%")["passed"]
    result = llm.validate_numbers("Сера 7,9 мг/кг", facts, "")
    assert not result["passed"] and result["unknown_numbers"] == ["7,9"]
