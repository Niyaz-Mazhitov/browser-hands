"""Jev (systemone через OpenRouter) выбирает операцию и цель; маленькая модель пишет текст только для TYPE_TEXT.

Перенос `jev_ultrafast/model.py` (MIT, Browser Use). Один запрос к Jev на шаг: вопрос `operation` и спекулятивные
`*_target`-головы; исполняется только голова выбранной операции. Модель выбирает индекс наблюдаемого элемента —
никогда не селектор, координаты или код. Адреса, модели и ключи — из `ModelConfig`; один тёплый httpx-клиент.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import ModelConfig
from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 503, 529})
WARMUP_TIMEOUT_S = 2.0
KEEPALIVE_S = 300.0
HISTORY_FOR_CHOICE = 10
HISTORY_FOR_TEXT = 6
PAGE_TEXT_FOR_TEXT = 6000
MAX_TEXT_VALUE = 2000

LABELS = {
    "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
    "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
    "SELECT": "Select an observed dropdown value.",
}


class ModelTimeout(TimeoutError):
    """Модель не ответила до дедлайна или `request_timeout_s`; действие не исполнялось."""


@dataclass(slots=True)
class Decision:
    choice: str  # id действия из снимка ("e3", "scroll_down", "wait") или DONE / BLOCKED
    operation: str  # CLICK | TYPE_TEXT | SELECT | SCROLL_UP | SCROLL_DOWN | WAIT | DONE | BLOCKED
    target: str | None  # индекс элемента в голове операции ("3", "2:1" для SELECT)
    confidence: float
    probabilities: dict[str, float]
    usage: dict[str, Any] = field(default_factory=dict)
    cost: float | None = None
    latency_ms: int = 0
    model: str | None = None


@dataclass(slots=True)
class TextHelper:
    model: str
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    cost: float | None = None


def usage_cost(result: dict[str, Any]) -> float | None:
    """`usage.cost` ответа OpenRouter, если это конечное число."""
    usage = result.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(cost):
        return None
    return float(cost)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class ModelClients:
    """Один `httpx.Client(http2=True)` на процесс: TLS/HTTP2 к хостам моделей переиспользуется между шагами."""

    def __init__(self, config: ModelConfig, *, http: httpx.Client | None = None) -> None:
        self.config = config
        # keepalive 300 с (по умолчанию 5 с): прогретое при старте соединение доживает до первого browse.
        self.http = http or httpx.Client(
            http2=True,
            timeout=config.request_timeout_s,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=KEEPALIVE_S),
        )

    def post(self, url: str, key: str, body: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """POST JSON; повтор только при 429/503/529 (до исполнения, в пределах `timeout`), иначе ошибка."""
        limit = self.config.request_timeout_s if timeout is None else min(self.config.request_timeout_s, timeout)
        if limit <= 0:
            raise ModelTimeout("Run deadline reached before the model call; no action executed.")
        deadline = time.monotonic() + limit
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = self.http.post(url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=remaining)
            except httpx.TimeoutException:
                raise ModelTimeout(f"Model did not answer in {limit:.1f} s; no action executed.") from None
            except httpx.HTTPError:
                raise RuntimeError("Model connection failed; no action executed.") from None
            delay = 0.5 * 2**attempt
            if response.status_code in RETRY_STATUSES and attempt < 2 and time.monotonic() + delay < deadline:
                time.sleep(delay)
                continue
            if response.is_error:
                raise RuntimeError(
                    f"Model provider returned HTTP {response.status_code}{_error_detail(response)}; no action executed."
                )
            try:
                return response.json()
            except ValueError:
                raise RuntimeError("Model provider returned invalid JSON; no action executed.") from None
        raise ModelTimeout(f"Model unavailable within {limit:.1f} s; no action executed.")

    def warmup(self) -> None:
        """Заранее открыть TLS/HTTP2 к хостам Jev и текстовой модели; без ключа и платы, ошибки — в лог, ≤2 с."""
        deadline = time.monotonic() + WARMUP_TIMEOUT_S
        for origin in dict.fromkeys(_origin(u) for u in (self.config.jev_url, self.config.text_base_url)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                self.http.head(origin + "/", timeout=remaining)
            except httpx.HTTPError as exc:
                log.info("Прогрев %s не удался: %s", origin, type(exc).__name__)

    def close(self) -> None:
        self.http.close()


def _error_detail(response: httpx.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    return f" ({str(message)[:200]})" if message else ""


def validate_choice(answer: Any, ids: Any) -> dict[str, Any]:
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Invalid Jev response; no action executed.")
    return answer


def action_space(
    actions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Один индекс на наблюдаемый элемент; у каждой операции — свои допустимые цели."""
    elements: list[dict[str, Any]] = []
    indices: dict[Any, str] = {}
    targets: dict[str, dict[str, dict[str, Any]]] = {}
    controls: dict[str, dict[str, Any]] = {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def build_request(
    config: ModelConfig, state: dict[str, Any], goal: str, history: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Тело одного запроса к Jev: операция + спекулятивные цели для каждой доступной операции."""
    elements, targets, controls = action_space(state["actions"])
    operations = {key: LABELS[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions: dict[str, Any] = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": config.jev_model,
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-HISTORY_FOR_CHOICE:]
            ],
        },
        "questions": questions,
    }
    return body, operations, targets, controls


def choose(
    clients: ModelClients,
    state: dict[str, Any],
    goal: str,
    history: list[dict[str, Any]],
    *,
    timeout: float | None = None,
) -> Decision:
    """Один запрос к Jev; проверяется и исполняется только голова выбранной операции."""
    config = clients.config
    if not config.jev_api_key:
        raise ValueError("No Jev API key: set OPENROUTER_API_KEY or BROWSER_HANDS_JEV_API_KEY.")
    body, operations, targets, controls = build_request(config, state, goal, history)
    started = time.perf_counter()
    result = clients.post(config.jev_url, config.jev_api_key, body, timeout=timeout)
    latency_ms = round((time.perf_counter() - started) * 1000)
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("Invalid Jev response; no action executed.")
    operation_answer = validate_choice(answers.get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    probabilities: dict[str, float] = {}
    if operation in targets:
        # Неиспользованные головы не могут вызвать действие. Проверяется голова выбранной операции.
        target_answer = validate_choice(answers.get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    usage = result.get("usage")
    log.debug("Jev %s: %d ms, usage=%s", operation, latency_ms, usage)
    return Decision(
        choice=choice,
        operation=operation,
        target=target,
        confidence=float(operation_answer["confidence"]),
        probabilities=probabilities,
        usage=usage if isinstance(usage, dict) else {},
        cost=usage_cost(result),
        latency_ms=latency_ms,
        model=result.get("model"),
    )


def field_context(
    goal: str, action: dict[str, Any], page: dict[str, Any], history: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:PAGE_TEXT_FOR_TEXT]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-HISTORY_FOR_TEXT:]],
    }


def reasoning_options(config: ModelConfig) -> dict[str, Any]:
    if config.text_reasoning == "none":
        return {"reasoning": {"enabled": False}}
    return {"reasoning": {"effort": config.text_reasoning}}


def field_text(
    clients: ModelClients, context: dict[str, Any], *, timeout: float | None = None
) -> tuple[str, TextHelper]:
    """Значение поля от текстовой модели: строго `{"text": "..."}`, иначе ничего не печатаем."""
    config = clients.config
    if not config.text_api_key:
        raise ValueError(
            "TYPE_TEXT needs a text model key (OPENROUTER_API_KEY or BROWSER_HANDS_TEXT_API_KEY); "
            "no text is hardcoded or guessed by the executor."
        )
    started = time.perf_counter()
    result = clients.post(
        config.text_base_url.rstrip("/") + "/chat/completions",
        config.text_api_key,
        {
            "model": config.text_model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning_options(config),
            "usage": {"include": True},
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {"role": "user", "content": json.dumps(context)},
            ],
        },
        timeout=timeout,
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_VALUE:
            raise ValueError()
    except (ValueError, KeyError, TypeError, IndexError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    usage = result.get("usage")
    log.debug("text model %s: usage=%s", config.text_model, usage)
    return value, TextHelper(
        model=config.text_model,
        latency_ms=round((time.perf_counter() - started) * 1000),
        usage=usage if isinstance(usage, dict) else {},
        cost=usage_cost(result),
    )
