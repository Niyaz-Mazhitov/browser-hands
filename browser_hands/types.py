"""Общие типы ядра и обвязки (контракт, docs/plan.md §4). Правит только Пакет 1."""

from dataclasses import dataclass
from typing import Literal, Protocol

Status = Literal["done", "blocked", "failed", "timeout", "step_limit"]


@dataclass(slots=True)
class Timing:  # миллисекунды, суммируемые
    model_ms: int = 0  # Jev (systemone)
    text_ms: int = 0  # текстовая модель
    browser_ms: int = 0  # CDP: снимок + исполнение (без ожиданий)
    wait_ms: int = 0  # ожидания: settle после ввода, WAIT, загрузка

    def __add__(self, other: "Timing") -> "Timing":
        if not isinstance(other, Timing):
            return NotImplemented
        return Timing(
            model_ms=self.model_ms + other.model_ms,
            text_ms=self.text_ms + other.text_ms,
            browser_ms=self.browser_ms + other.browser_ms,
            wait_ms=self.wait_ms + other.wait_ms,
        )


@dataclass(slots=True)
class Step:
    index: int  # с 1
    operation: str  # CLICK | TYPE_TEXT | SELECT | SCROLL_UP | SCROLL_DOWN | WAIT
    target: str  # подпись элемента ("Search", "Open Search", "Scroll down")
    text: str | None  # что напечатали (TYPE_TEXT)
    page_changed: bool | None
    url: str  # url после шага
    confidence: float
    timing: Timing
    cost: float | None  # usage.cost этого шага (Jev + текст), если пришёл


@dataclass(slots=True)
class RunResult:
    status: Status
    steps: list[Step]
    url: str
    title: str
    screenshot_jpeg: bytes | None  # та же вкладка, в конце; None если вкладка погибла
    cost: float | None  # сумма по шагам, None если ни один usage.cost не пришёл
    elapsed_ms: int
    timing: Timing  # сумма по шагам + старт (навигация)
    model_calls: int
    error: str | None = None  # текст причины для failed/timeout
    tab_kept: bool = False


class AgentLike(Protocol):
    def run(self) -> RunResult: ...


class ChromeLike(Protocol):
    def connect(self) -> None: ...  # идемпотентно; переподключается, если соединение мертво

    def alive(self) -> bool: ...

    def close(self) -> None: ...  # launch: гасит свой процесс; attach: только закрывает ws
