"""Сценарий: список шагов `{do, text?}` (docs/plan-scenarios.md §3.1). Контракт ядра и обвязки.

`do` — что сделать (по-английски: Jev на нём точнее), `text` — что напечатать дословно. Разбор и лимиты — здесь,
одно место для сервера, CLI, стенда и ядра; `render` — одна формулировка шага для Jev, лога и отчёта.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MAX_STEPS = 20
MAX_DO = 300
MAX_TEXT = 2000  # = model.MAX_TEXT_VALUE (tests/test_contract.py)
KEYS = frozenset({"do", "text"})


class ScenarioError(ValueError):
    """Сценарий не прошёл разбор; текст — одна строка с номером шага и причиной."""


@dataclass(slots=True, frozen=True)
class ScenarioStep:
    do: str
    text: str | None = None


def parse_steps(raw: object) -> list[ScenarioStep]:
    """Список `{do, text?}` → шаги; иначе `ScenarioError` («step 3: do is empty»).

    Принимает и готовые `ScenarioStep` — проверяет их заново (ядро зовёт это мимо сервера).
    """
    if not isinstance(raw, list):
        raise ScenarioError(f"steps: expected a list, got {type(raw).__name__}")
    if not raw:
        raise ScenarioError("steps: empty list")
    if len(raw) > MAX_STEPS:
        raise ScenarioError(f"steps: {len(raw)} steps, max {MAX_STEPS}")
    return [_parse_step(n, item) for n, item in enumerate(raw, start=1)]


def _parse_step(n: int, item: object) -> ScenarioStep:
    if isinstance(item, ScenarioStep):
        item = {"do": item.do, "text": item.text}
    if not isinstance(item, Mapping):
        raise ScenarioError(f"step {n}: expected an object with do and optional text")
    extra = sorted(str(key) for key in item if key not in KEYS)
    if extra:
        raise ScenarioError(f"step {n}: unknown keys {', '.join(extra)} (allowed: do, text)")
    if "do" not in item:
        raise ScenarioError(f"step {n}: do is missing")
    do = item["do"]
    if not isinstance(do, str):
        raise ScenarioError(f"step {n}: do must be a string")
    do = do.strip()
    if not do:
        raise ScenarioError(f"step {n}: do is empty")
    if len(do) > MAX_DO:
        raise ScenarioError(f"step {n}: do is longer than {MAX_DO} characters")
    text = item.get("text")
    if text is not None:
        if not isinstance(text, str):
            raise ScenarioError(f"step {n}: text must be a string or null")
        if not text:  # без strip: пробелы — часть текста
            raise ScenarioError(f"step {n}: text is empty")
        if len(text) > MAX_TEXT:
            raise ScenarioError(f"step {n}: text is longer than {MAX_TEXT} characters")
    return ScenarioStep(do=do, text=text)


def render(step_no: int, steps: Sequence[ScenarioStep]) -> str:
    """«Step 2 of 4: open the chat» — шаг `step_no` (с 1) для Jev, лога и отчёта."""
    if not 1 <= step_no <= len(steps):
        raise ValueError(f"step_no {step_no} out of range 1..{len(steps)}")
    return f"Step {step_no} of {len(steps)}: {steps[step_no - 1].do}"
