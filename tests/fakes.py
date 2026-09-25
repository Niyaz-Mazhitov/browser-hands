"""Фейки ядра для тестов обвязки (Пакет 2): без Chrome, сети и моделей."""

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from browser_hands.config import BrowserConfig, ModelConfig, RunConfig
from browser_hands.scenario import ScenarioStep
from browser_hands.types import RunResult, Status, Step, TabKind, Timing

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32 + b"\xff\xd9"


def make_result(
    status: Status = "done",
    steps: int = 2,
    screenshot: bytes | None = JPEG,
    *,
    cost: float | None = 0.0021,
    url: str = "https://example.test/done",
    title: str = "Example",
    error: str | None = None,
    tab_kept: bool = False,
    tab: TabKind | None = "new",
    scenario: tuple[int, int] | None = None,
    jev_calls: int = 0,
) -> RunResult:
    """RunResult с `steps` шагами: нечётные — CLICK, чётные — TYPE_TEXT; `cost` — цена одного шага.

    `scenario=(done, total)` — режим сценария: `scenario_done/total`, у шага i `scenario_step = min(i, total)`.
    """
    step_list = [
        Step(
            index=i,
            operation="CLICK" if i % 2 else "TYPE_TEXT",
            target=f"Target {i}",
            text=None if i % 2 else f"text {i}",
            page_changed=True,
            url=f"https://example.test/{i}",
            confidence=0.9,
            timing=Timing(model_ms=600, text_ms=0 if i % 2 else 300, browser_ms=40, wait_ms=50),
            cost=cost,
            scenario_step=None if scenario is None else min(i, scenario[1]),
        )
        for i in range(1, steps + 1)
    ]
    timing = sum((s.timing for s in step_list), Timing())
    return RunResult(
        status=status,
        steps=step_list,
        url=url,
        title=title,
        screenshot_jpeg=screenshot,
        cost=None if cost is None or not step_list else round(cost * len(step_list), 6),
        elapsed_ms=timing.model_ms + timing.text_ms + timing.browser_ms + timing.wait_ms,
        timing=timing,
        model_calls=len(step_list),
        error=error,
        tab_kept=tab_kept,
        tab=tab,
        scenario_done=None if scenario is None else scenario[0],
        scenario_total=None if scenario is None else scenario[1],
        jev_calls=jev_calls,
    )


class FakeChrome:
    """ChromeLike: считает connect/close; `alive` задаётся флагом, успешный connect его поднимает."""

    def __init__(self, config: BrowserConfig | None = None, *, alive: bool = True) -> None:
        self.config = config or BrowserConfig()
        self.is_alive = alive
        self.connect_calls = 0
        self.close_calls = 0
        self.connect_error: Exception | None = None

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.is_alive = True

    def alive(self) -> bool:
        return self.is_alive

    def close(self) -> None:
        self.close_calls += 1
        self.is_alive = False


class FakeClients:
    """ModelClients без сети: считает warmup/close."""

    def __init__(self, config: ModelConfig | None = None) -> None:
        self.config = config or ModelConfig()
        self.warmup_calls = 0
        self.close_calls = 0
        self.warmed = threading.Event()

    def warmup(self) -> None:
        self.warmup_calls += 1
        self.warmed.set()

    def close(self) -> None:
        self.close_calls += 1


def cancelled_result() -> RunResult:
    """Что возвращает ядро на выставленный `cancel`: failed, error="cancelled"."""
    return make_result("failed", steps=0, screenshot=None, error="cancelled")


class FakeAgent:
    """AgentLike: возвращает заданный RunResult или бросает; `gate` — держать run(), пока не отпустят.

    Как ядро: выставленный `cancel` останавливает прогон «между шагами» — `failed`, error="cancelled".
    """

    def __init__(
        self,
        result: RunResult | None = None,
        *,
        error: Exception | None = None,
        started: threading.Event | None = None,
        gate: threading.Event | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.result = result if result is not None else make_result()
        self.error = error
        self.started = started
        self.gate = gate
        self.cancel = cancel
        self.run_calls = 0

    def run(self) -> RunResult:
        self.run_calls += 1
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            deadline = time.monotonic() + 5
            while not self.gate.wait(timeout=0.01):
                if self._cancelled():
                    return cancelled_result()
                if time.monotonic() > deadline:
                    raise TimeoutError("gate не открыт за 5 с")
        if self._cancelled():
            return cancelled_result()
        if self.error is not None:
            raise self.error
        return self.result

    def _cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()


@dataclass
class FakeCore:
    """Фабрики ядра для BrowseService/create_server/CLI; записывает, что создано и с какими аргументами."""

    result: RunResult = field(default_factory=make_result)
    error: Exception | None = None
    chrome_alive: bool = True
    gate: threading.Event | None = None
    chromes: list[FakeChrome] = field(default_factory=list)
    clients: list[FakeClients] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    started: list[threading.Event] = field(default_factory=list)

    def chrome_factory(self, config: BrowserConfig) -> FakeChrome:
        chrome = FakeChrome(config, alive=self.chrome_alive)
        self.chromes.append(chrome)
        return chrome

    def clients_factory(self, config: ModelConfig) -> FakeClients:
        clients = FakeClients(config)
        self.clients.append(clients)
        return clients

    def agent_factory(
        self,
        chrome: FakeChrome,
        clients: FakeClients,
        url: str,
        goal: str,
        run: RunConfig,
        *,
        screenshot_quality: int,
        screenshot_scale: float,
        steps: list[ScenarioStep] | None = None,
        cancel: threading.Event | None = None,
    ) -> FakeAgent:
        self.agents.append(
            {
                "chrome": chrome,
                "clients": clients,
                "url": url,
                "goal": goal,
                "run": run,
                "screenshot_quality": screenshot_quality,
                "screenshot_scale": screenshot_scale,
                "steps": steps,
                "cancel": cancel,
            }
        )
        started = threading.Event()
        self.started.append(started)
        return FakeAgent(self.result, error=self.error, started=started, gate=self.gate, cancel=cancel)

    def factories(self) -> dict[str, Any]:
        return {
            "chrome_factory": self.chrome_factory,
            "clients_factory": self.clients_factory,
            "agent_factory": self.agent_factory,
        }
