"""Офлайн-контракты политики операция/цель и текстовой модели. Без сети и платных вызовов."""

import json
from unittest.mock import Mock

import httpx
import pytest

from browser_hands import model
from browser_hands.browser import fingerprint
from browser_hands.config import ModelConfig
from browser_hands.model import ModelClients, ModelTimeout


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def make_clients(post=None, **config):
    config.setdefault("jev_api_key", "test-jev")
    config.setdefault("text_api_key", "test-text")
    clients = ModelClients(ModelConfig(**config), http=Mock())
    if post is not None:
        clients.post = post
    return clients


def response(status=200, payload=None):
    return httpx.Response(
        status, json=payload if payload is not None else {}, request=httpx.Request("POST", "https://x")
    )


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid Jev"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes():
    calls = []

    def post(_url, _key, body, *, timeout=None):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    d = model.choose(make_clients(post), page(), "Find a book", [])
    assert len(calls) == 1
    assert d.operation == "TYPE_TEXT" and d.target == "1" and d.choice == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target():
    def post(_url, _key, body, *, timeout=None):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    with pytest.raises(ValueError, match="Invalid Jev"):
        model.choose(make_clients(post), page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules():
    p = page()
    p["actions"].insert(
        0,
        {
            "id": "toggle",
            "kind": "click",
            "label": "Free cancellation",
            "node": 30,
            "role": "checkbox",
            "checked": "true",
            "selected": False,
        },
    )

    def post(_url, _key, body, *, timeout=None):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    d = model.choose(make_clients(post), p, "Search with free cancellation", [])
    assert d.choice == "e3"


def test_quoted_task_text_still_uses_the_llm():
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(make_clients(post), context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing():
    post = Mock()
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        model.field_text(make_clients(post, text_api_key=""), {"goal": 'Enter "Zurich"'})
    post.assert_not_called()


def test_missing_jev_credential_stops_before_the_request():
    post = Mock()
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        model.choose(make_clients(post, jev_api_key=""), page(), "Find a book", [])
    post.assert_not_called()


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(content):
    post = Mock(return_value={"choices": [{"message": {"content": content}}]})
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text(make_clients(post), {"goal": "Find a flight"})


def test_urls_models_and_keys_come_from_model_config():
    post = Mock(
        return_value={
            "model": "jev-x",
            "usage": {"cost": 0.0012},
            "answers": {"operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "WAIT")},
        }
    )
    clients = make_clients(post, jev_url="https://jev.test/api/v1/systemone", jev_model="jev-x", jev_api_key="kj")
    d = model.choose(clients, page(), "Find a book", [], timeout=7.5)
    url, key, body = post.call_args.args
    assert (url, key, body["model"]) == ("https://jev.test/api/v1/systemone", "kj", "jev-x")
    assert post.call_args.kwargs["timeout"] == 7.5
    assert d.choice == "wait" and d.operation == "WAIT" and d.cost == pytest.approx(0.0012)

    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"book"}'}}], "usage": {"cost": 0.0001}})
    clients = make_clients(post, text_base_url="https://text.test/v1/", text_model="m-1", text_api_key="kt")
    value, helper = model.field_text(clients, {"goal": "Find a book"})
    url, key, body = post.call_args.args
    assert (url, key, body["model"]) == ("https://text.test/v1/chat/completions", "kt", "m-1")
    assert body["reasoning"] == {"enabled": False} and body["usage"] == {"include": True}
    assert value == "book" and helper.cost == pytest.approx(0.0001) and helper.model == "m-1"

    clients = make_clients(post, text_reasoning="low")
    model.field_text(clients, {"goal": "Find a book"})
    assert post.call_args.args[2]["reasoning"] == {"effort": "low"}
    assert "thinking" not in post.call_args.args[2]


@pytest.mark.parametrize("usage", [None, {}, {"cost": None}, {"cost": "0.1"}, {"cost": True}, {"cost": float("nan")}])
def test_cost_is_none_without_numeric_usage_cost(usage):
    result = {} if usage is None else {"usage": usage}
    assert model.usage_cost(result) is None


def test_post_reuses_one_http_client_and_retries_only_overload(monkeypatch):
    sleeps = []
    monkeypatch.setattr(model.time, "sleep", sleeps.append)
    http = Mock()
    http.post.side_effect = [response(429), response(200, {"ok": 1}), response(200, {"ok": 2})]
    clients = ModelClients(ModelConfig(request_timeout_s=5), http=http)
    assert clients.post("https://x/a", "k", {"a": 1}) == {"ok": 1}
    assert clients.post("https://x/b", "k", {"b": 1}) == {"ok": 2}
    assert http.post.call_count == 3 and sleeps == [0.5]
    assert http.post.call_args.kwargs["headers"] == {"Authorization": "Bearer k"}


def test_post_does_not_retry_client_errors_and_hides_the_key():
    http = Mock()
    http.post.return_value = response(402, {"error": {"message": "Insufficient credits"}})
    clients = ModelClients(ModelConfig(request_timeout_s=5), http=http)
    with pytest.raises(RuntimeError, match="HTTP 402 \\(Insufficient credits\\); no action executed") as info:
        clients.post("https://x", "sk-secret", {})
    assert http.post.call_count == 1
    assert "sk-secret" not in str(info.value)


def test_post_timeout_is_bounded_by_the_run_deadline():
    http = Mock()
    http.post.return_value = response(200, {})
    clients = ModelClients(ModelConfig(request_timeout_s=25), http=http)
    clients.post("https://x", "k", {}, timeout=3)
    assert http.post.call_args.kwargs["timeout"] <= 3
    with pytest.raises(ModelTimeout):
        clients.post("https://x", "k", {}, timeout=0)
    assert http.post.call_count == 1

    http.post.side_effect = httpx.ReadTimeout("slow")
    with pytest.raises(ModelTimeout):
        clients.post("https://x", "k", {}, timeout=1)
    http.post.side_effect = httpx.ConnectError("down")
    with pytest.raises(RuntimeError, match="connection failed"):
        clients.post("https://x", "k", {}, timeout=1)


def test_warmup_opens_each_model_host_once_and_swallows_errors():
    http = Mock()
    http.head.side_effect = httpx.ConnectError("offline")
    clients = ModelClients(ModelConfig(), http=http)
    clients.warmup()
    assert [c.args[0] for c in http.head.call_args_list] == ["https://openrouter.ai/"]
    assert "headers" not in http.head.call_args.kwargs  # без ключа
    clients.close()
    http.close.assert_called_once()


def test_one_http_client_keeps_warm_connections_for_five_minutes(monkeypatch):
    created = Mock()
    monkeypatch.setattr(model.httpx, "Client", created)
    ModelClients(ModelConfig(jev_api_key="k", text_api_key="k"))
    kwargs = created.call_args.kwargs
    assert kwargs["http2"] is True
    assert kwargs["limits"].keepalive_expiry == 300  # прогрев при старте не остывает за 5 с по умолчанию
    assert kwargs["limits"].max_keepalive_connections == 20
