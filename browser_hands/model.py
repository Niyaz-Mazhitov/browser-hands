"""Jev (systemone через OpenRouter) выбирает операцию и цель; маленькая модель пишет текст только для TYPE_TEXT.

Перенос `jev_ultrafast/model.py` (MIT, Browser Use). Один запрос к Jev на шаг: вопрос `operation` и спекулятивные
`*_target`-головы; исполняется только голова выбранной операции. Модель выбирает индекс наблюдаемого элемента —
никогда не селектор, координаты или код. Адреса, модели и ключи — из `ModelConfig`; один тёплый httpx-клиент.
Режим сценариев (`step`, docs/plan-scenarios.md §4.1): в том же запросе — голова `step_done` («шаг выполнен?»), цель —
объект текущего шага; без `step` тело запроса прежнее байт в байт (tests/snapshots/jev_goal_request.json). Режим
проверки (`verify`, после неподтверждённого DONE): операции — только WAIT, DONE и BLOCKED, голов `*_target` нет.
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
from .questions import NEXT_ACTION, NEXT_ACTION_STEP, STEP_DONE, TARGET, TEXT_VALUE
from .scenario import ScenarioStep, render

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 503, 529})
WARMUP_TIMEOUT_S = 2.0
KEEPALIVE_S = 300.0
HISTORY_FOR_CHOICE = 10
HISTORY_FOR_TEXT = 6
PAGE_TEXT_FOR_TEXT = 2000  # явно заданному тексту контекст не нужен; выводимому значению хватает верха страницы
MAX_TEXT_VALUE = 2000
RAW_TEXT_LOG = 500  # сколько символов сырого ответа текстовой модели попадает в DEBUG

LABELS = {
    "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
    "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
    "SELECT": "Select an observed dropdown value.",
}
DONE_GOAL = "Every requirement is visibly satisfied."
BLOCKED_LABEL = "No supported operation can progress."
# Шаг с `text`: текст даёт сценарий, не текстовая модель. Шаг без `text` — прежняя подпись.
LABELS_STEP = {
    **LABELS,
    "TYPE_TEXT": "Enter or replace text in an editable field. The exact text is given in the current step.",
}
DONE_STEP = "The current step is visibly complete."
STEP_DONE_QUESTION = "Is the current step complete?"
STEP_DONE_CRITERIA = {
    "yes": "The current step is visibly complete on the CURRENT page.",
    "no": "The current step is not complete yet, or the page does not show it.",
}
# Факт для Jev: запросы вкладки, начатые действиями агента с последней смены страницы, ещё в полёте (фоновые тоже) —
# только число, без адресов и тел; одинаково в режиме цели, сценария и проверки. Нет таких запросов — ключа нет (тело
# запроса режима цели прежнее, tests/snapshots/jev_goal_request.json).
LOADING = "Page is still loading: {n} network request(s) started by recent actions have not finished yet."


class ModelTimeout(TimeoutError):
    """Модель не ответила до дедлайна прогона или своего потолка (`jev_timeout_s`/`text_timeout_s`) — считается по
    часам на весь запрос, а не на каждое чтение; действие не исполнялось."""


class ProviderError(RuntimeError):
    """Ответ 2xx, но в теле `error` и нет ответа модели (`choices`/`answers`): OpenRouter уже отдал 200, а провайдер
    упал (25.09: `{"error": {"code": 504, "message": "Upstream idle timeout exceeded"}}`). Действие не исполнялось."""

    def __init__(self, code: Any, message: str, *, cost: float | None = None) -> None:
        detail = f" ({message})" if message else ""
        super().__init__(f"Model provider returned error {code}{detail}; no action executed.")
        self.code = code
        self.message = message
        self.cost = cost


class InvalidTextValue(ValueError):
    """Текстовая модель не дала `{"text": "<непустая строка>"}`; ничего не напечатано. `reason` — почему:
    no-content | not-json | null | extra-keys | not-string | empty | too-long | provider-error (200 с `error` в теле).
    `cost` — цена неудачного запроса."""

    def __init__(self, reason: str, *, cost: float | None = None) -> None:
        super().__init__("Text helper returned no valid field value; nothing typed.")
        self.reason = reason
        self.cost = cost


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
    step_done: float | None = None  # вероятность `yes` головы `step_done`; None — режим цели


@dataclass(slots=True, frozen=True)
class StepContext:
    """Текущий шаг сценария в запросе к Jev: номер (с 1) в `steps` и общая цель, если дана вместе со сценарием.
    Выполненные шаги — те, что до текущего (шаги закрываются по порядку)."""

    steps: tuple[ScenarioStep, ...]
    number: int
    goal: str | None = None

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def do(self) -> str:
        return self.steps[self.number - 1].do

    @property
    def text(self) -> str | None:
        return self.steps[self.number - 1].text

    @property
    def done(self) -> list[str]:
        return [s.do for s in self.steps[: self.number - 1]]

    @property
    def remaining(self) -> list[str]:
        return [s.do for s in self.steps[self.number :]]

    def current(self) -> str:
        """«Step 2 of 4: open the chat» (`scenario.render`); номер вне 1…M — `ValueError`."""
        return render(self.number, self.steps)

    def instructions(self) -> dict[str, Any]:
        """Объект вместо строки `goal` в `instructions` операции и целей: шаг k из M явным полем, текст для ввода,
        `do` выполненных и оставшихся (тексты прошлых шагов и так в `recent_actions`)."""
        out: dict[str, Any] = {"goal": self.goal} if self.goal else {}
        out["current_step"] = self.current()
        if self.text is not None:
            out["text_to_type"] = self.text
        out.update(done_steps=self.done, next_steps=self.remaining)
        return out


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
        # Таймаут клиента — запасной: каждый запрос `post` задаёт свой потолок по часам.
        self.http = http or httpx.Client(
            http2=True,
            timeout=max(config.jev_timeout_s, config.text_timeout_s),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=KEEPALIVE_S),
        )

    def post(
        self,
        url: str,
        key: str,
        body: dict[str, Any],
        *,
        limit: float,
        timeout: float | None = None,
        name: str = "Model",
        retry_body_errors: bool = True,
    ) -> dict[str, Any]:
        """POST JSON с одним потолком по часам на всё — соединение, ожидание, тело и повторы: `limit` модели, но не
        дольше `timeout` (остаток дедлайна прогона); вышел — `ModelTimeout`. Повтор (до исполнения, в пределах
        потолка) — при 429/503/529 и, если `retry_body_errors`, при 2xx с `error` в теле (`ProviderError`)."""
        limit = limit if timeout is None else min(limit, timeout)
        if limit <= 0:
            raise ModelTimeout("Run deadline reached before the model call; no action executed.")
        deadline = time.monotonic() + limit
        for attempt in range(3):
            if time.monotonic() >= deadline:
                break
            try:
                response = self._send(url, key, body, deadline)
            except httpx.TimeoutException:
                raise ModelTimeout(f"{name} did not answer in {limit:.1f} s; no action executed.") from None
            except httpx.HTTPError:
                raise RuntimeError(f"{name} connection failed; no action executed.") from None
            delay = 0.5 * 2**attempt
            can_retry = attempt < 2 and time.monotonic() + delay < deadline
            if response.status_code in RETRY_STATUSES and can_retry:
                time.sleep(delay)
                continue
            if response.is_error:
                raise RuntimeError(
                    f"Model provider returned HTTP {response.status_code}{_error_detail(response)}; no action executed."
                )
            try:
                result = response.json()
            except ValueError:
                raise RuntimeError("Model provider returned invalid JSON; no action executed.") from None
            error = _body_error(result)
            if error is None:
                return result
            retry = retry_body_errors and can_retry
            # INFO: код и сообщение провайдера (≤200 символов), без ключа и заголовков.
            log.info(
                "%s: ошибка провайдера в ответе HTTP %d: %s%s%s",
                name,
                response.status_code,
                error.code,
                f" ({error.message})" if error.message else "",
                ", повторяю" if retry else "",
            )
            if retry:
                time.sleep(delay)
                continue
            raise error
        raise ModelTimeout(f"{name} unavailable within {limit:.1f} s; no action executed.")

    def _send(self, url: str, key: str, body: dict[str, Any], deadline: float) -> httpx.Response:
        """Один POST, ответ целиком не позже `deadline` по часам. Таймаут httpx — на каждое чтение, а OpenRouter, пока
        ждёт провайдера, держит соединение пробелами (25.09: 200 с `error` через ~120 с при таймауте 25 с). Поэтому
        тело читается кусками, между кусками — сверка с часами, а перед каждым чтением таймаут — остаток до
        `deadline`: HTTP/2 (OpenRouter) httpcore берёт его из `request.extensions` при каждом чтении сокета — потолок
        точный. HTTP/1.1 берёт таймаут один раз на тело: если пробелы шли и оборвались, последнее чтение может ждать ещё
        до остатка на начало тела (худший случай — 2 × потолок)."""
        request = self.http.build_request(
            "POST", url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=_left(deadline)
        )
        response = self.http.send(request, stream=True)
        chunks: list[bytes] = []
        try:
            raw = response.iter_raw()
            while True:
                request.extensions["timeout"] = _left(deadline).as_dict()
                chunk = next(raw, None)
                if chunk is None:
                    break
                chunks.append(chunk)
        finally:
            response.close()
        # Сырые байты → ответ с тем же статусом и заголовками: httpx сам снимет Content-Encoding.
        return httpx.Response(response.status_code, headers=response.headers, content=b"".join(chunks), request=request)

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


def _left(deadline: float) -> httpx.Timeout:
    """Таймаут httpx на следующую операцию — остаток до `deadline`; вышел — `ReadTimeout` сразу, без чтения."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpx.ReadTimeout("Model request deadline reached")
    return httpx.Timeout(remaining)


def _body_error(result: Any) -> ProviderError | None:
    """`error` в теле ответа 2xx без `choices`/`answers` — ошибка провайдера (код и сообщение ≤200 символов)."""
    if not isinstance(result, dict) or result.get("error") is None or {"choices", "answers"} & result.keys():
        return None
    error, cost = result["error"], usage_cost(result)
    if isinstance(error, dict):
        return ProviderError(error.get("code"), str(error.get("message") or "")[:200], cost=cost)
    return ProviderError(None, str(error)[:200], cost=cost)


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


def _recent_action(entry: dict[str, Any]) -> dict[str, Any]:
    """Запись истории для Jev; `note` (пометка агента: «text vanished after typing …») — только если есть: тело режима
    цели без пометок прежнее."""
    out = {k: entry.get(k) for k in ("action", "kind", "text", "page_changed")}
    if entry.get("note"):
        out["note"] = entry["note"]
    return out


def build_request(
    config: ModelConfig,
    state: dict[str, Any],
    goal: str,
    history: list[dict[str, Any]],
    *,
    step: StepContext | None = None,
    verify: bool = False,
    loading: int = 0,
) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Тело одного запроса к Jev: операция + спекулятивные цели для каждой доступной операции. При `step` — цель
    шага (объект), правила шага, DONE про шаг и голова `step_done`; `goal` тогда — внутри `step`. `loading` > 0 —
    в `state.page.loading` факт «страница ещё загружается» (`LOADING`: сколько запросов, начатых недавними действиями,
    в полёте), в любом режиме.

    `verify` (только со `step`) — режим проверки после неподтверждённого DONE: из операций — WAIT, DONE и BLOCKED;
    целей для CLICK, TYPE_TEXT, SELECT и прокрутки нет вовсе, у элементов нет списка операций — поля и значения видны
    как состояние страницы."""
    if verify and step is None:
        raise ValueError("verify needs a scenario step")
    elements, targets, controls = action_space(state["actions"])
    if verify:
        elements = [{k: v for k, v in e.items() if k != "operations"} for e in elements]
        targets = {}
        controls = {k: v for k, v in controls.items() if k == "WAIT"}
    labels = LABELS_STEP if step is not None and step.text is not None else LABELS
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE=DONE_GOAL if step is None else DONE_STEP, BLOCKED=BLOCKED_LABEL)
    goal_field: str | dict[str, Any] = goal if step is None else step.instructions()
    rules = NEXT_ACTION if step is None else NEXT_ACTION_STEP
    questions: dict[str, Any] = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal_field, "rules": rules}}
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
            "instructions": {"goal": goal_field, "operation": operation, "rules": [rules, TARGET]},
        }
    if step is not None:
        done_instructions: dict[str, Any] = {"question": STEP_DONE_QUESTION, "current_step": step.current()}
        if step.text is not None:
            done_instructions["text_to_type"] = step.text
        done_instructions["rules"] = STEP_DONE
        questions["step_done"] = {
            "type": "choice",
            "criteria": dict(STEP_DONE_CRITERIA),
            "instructions": done_instructions,
        }
    page: dict[str, Any] = {k: state[k] for k in ("url", "title", "text")}
    if loading > 0:
        page["loading"] = LOADING.format(n=loading)
    body = {
        "model": config.jev_model,
        "state": {
            "page": page,
            "elements": elements,
            "recent_actions": [_recent_action(h) for h in history[-HISTORY_FOR_CHOICE:]],
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
    step: StepContext | None = None,
    verify: bool = False,
    loading: int = 0,
) -> Decision:
    """Один запрос к Jev; проверяется и исполняется только голова выбранной операции. При `step` проверяется и голова
    `step_done` (нет или невалидна — `ValueError`, как у операции): `Decision.step_done` = вероятность `yes`.
    `verify` — режим проверки (`build_request`): ответ вне WAIT/DONE/BLOCKED невалиден. `loading` — запросов, начатых
    действиями с последней смены страницы, в полёте (факт в `state.page`, `build_request`)."""
    config = clients.config
    if not config.jev_api_key:
        raise ValueError("No Jev API key: set OPENROUTER_API_KEY or BROWSER_HANDS_JEV_API_KEY.")
    body, operations, targets, controls = build_request(
        config, state, goal, history, step=step, verify=verify, loading=loading
    )
    started = time.perf_counter()
    result = clients.post(
        config.jev_url, config.jev_api_key, body, limit=config.jev_timeout_s, timeout=timeout, name="Jev"
    )
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
    step_done: float | None = None
    if step is not None:
        step_done = float(validate_choice(answers.get("step_done", {}), STEP_DONE_CRITERIA)["probabilities"]["yes"])
    usage = result.get("usage")
    if step is None:
        log.debug("Jev %s: %d ms, usage=%s", operation, latency_ms, usage)
    else:
        log.debug("Jev %s, step_done=%.2f: %d ms, usage=%s", operation, step_done, latency_ms, usage)
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
        step_done=step_done,
    )


def field_context(
    goal: str, action: dict[str, Any], page: dict[str, Any], history: list[dict[str, Any]], *, texts: bool = True
) -> dict[str, Any]:
    """Контекст текстовой модели. `texts=False` (режим сценария) — история без напечатанных текстов: тексты шагов
    текстовой модели не уходят (docs/core-notes.md, «Сценарии» → «После ревью»)."""
    keys = ("action", "text") if texts else ("action",)
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:PAGE_TEXT_FOR_TEXT]},
        "recent_actions": [{k: h.get(k) for k in keys} for h in history[-HISTORY_FOR_TEXT:]],
    }


def reasoning_options(config: ModelConfig) -> dict[str, Any]:
    if config.text_reasoning == "none":
        return {"reasoning": {"enabled": False}}
    return {"reasoning": {"effort": config.text_reasoning}}


def _message_content(result: Any) -> str | None:
    """`choices[0].message.content` ответа chat/completions, если это строка."""
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, TypeError, IndexError):
        return None
    return content if isinstance(content, str) else None


def unfence_json(content: str) -> Any:
    """JSON-объект ответа, допуская обёртку в markdown-блок: ```json … ``` или только хвост ``` (mercury-2.5 изредка
    дописывает его после валидного JSON, ~1 из 12 ответов 25.09). Любой другой текст вокруг — `ValueError`."""
    body = content.strip()
    if body.startswith("```"):
        body = body.partition("\n")[2]  # строка ```json целиком
    output, end = json.JSONDecoder().raw_decode(body.lstrip())
    if body.lstrip()[end:].strip().strip("`").strip():
        raise ValueError("text after JSON")
    return output


def text_value(content: str | None) -> str:
    """Значение из `{"text": "<непустая строка ≤ MAX_TEXT_VALUE>"}`, иначе `InvalidTextValue` с причиной."""
    if content is None or not content.strip():
        raise InvalidTextValue("no-content")
    try:
        output = unfence_json(content)
    except ValueError:
        raise InvalidTextValue("not-json") from None
    if not isinstance(output, dict):
        raise InvalidTextValue("not-json")
    if set(output) != {"text"}:
        raise InvalidTextValue("extra-keys")
    value = output["text"]
    if value is None:
        raise InvalidTextValue("null")
    if not isinstance(value, str):
        raise InvalidTextValue("not-string")
    if not value.strip():
        raise InvalidTextValue("empty")
    if len(value) > MAX_TEXT_VALUE:
        raise InvalidTextValue("too-long")
    return value


def field_text(
    clients: ModelClients, context: dict[str, Any], *, timeout: float | None = None
) -> tuple[str, TextHelper]:
    """Значение поля от текстовой модели: строго `{"text": "..."}`, иначе `InvalidTextValue` и ничего не печатаем.
    Ошибка провайдера в теле 200 — тоже `InvalidTextValue` (`provider-error`), без повтора внутри `post`: один повтор
    делает агент. Потолок — `text_timeout_s`, не дольше `timeout`; вышел — `ModelTimeout`."""
    config = clients.config
    if not config.text_api_key:
        raise ValueError(
            "TYPE_TEXT needs a text model key (OPENROUTER_API_KEY or BROWSER_HANDS_TEXT_API_KEY); "
            "no text is hardcoded or guessed by the executor."
        )
    started = time.perf_counter()
    try:
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
            limit=config.text_timeout_s,
            timeout=timeout,
            name="Text model",
            retry_body_errors=False,
        )
    except ProviderError as exc:
        raise InvalidTextValue("provider-error", cost=exc.cost) from None  # код и сообщение — уже в INFO (`post`)
    content = _message_content(result)
    if content is not None:
        # Только тело ответа модели (без заголовков и ключа); напечатанный текст и так в DEBUG (agent.py).
        log.debug("text model raw (%d chars): %r", len(content), content[:RAW_TEXT_LOG])
    try:
        value = text_value(content)
    except InvalidTextValue as exc:
        # INFO — только причина и длина: сырой ответ может содержать текст со страницы.
        log.info("Текстовая модель: невалидный ответ (%s), len=%d", exc.reason, len(content or ""))
        exc.cost = usage_cost(result) if isinstance(result, dict) else None
        raise
    usage = result.get("usage")
    log.debug("text model %s: usage=%s", config.text_model, usage)
    return value, TextHelper(
        model=config.text_model,
        latency_ms=round((time.perf_counter() - started) * 1000),
        usage=usage if isinstance(usage, dict) else {},
        cost=usage_cost(result),
    )
