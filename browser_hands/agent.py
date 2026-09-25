"""Полный цикл агента: снимок → один запрос к Jev → исполнение только выбранной цели → снимок.

Перенос `jev_ultrafast/agent.py` (MIT, Browser Use) в блокирующий `Agent.run() -> RunResult`. Инварианты источника:
решение потребляется до любой мутации (повтор не кликнет дважды); `StalePage` → переснять и выбрать заново;
сгенерированный текст переиспользуется только при идентичном контексте; исполнение записывается до наблюдения;
3 действия подряд без изменения страницы (кроме WAIT) → blocked. Повторы — только сетевые, внутри `ModelClients.post`.
Отмена (`cancel`) проверяется перед каждым вызовом модели и каждым действием: `failed` / `cancelled`, без новых мутаций.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .browser import NAVIGATE_TIMEOUT_S, StalePage, Tab, url_host
from .cdp import ChromeDisconnected, TabGone
from .chrome import Chrome
from .config import RunConfig
from .model import Decision, ModelClients, TextHelper, choose, field_context, field_text
from .types import AgentLike, RunResult, Status, Step, Timing

log = logging.getLogger(__name__)

NO_PROGRESS_STEPS = 3
SCREENSHOT_TIMEOUT_S = 5.0
CANCELLED = "cancelled"


class RunTimeout(TimeoutError):
    """Общий дедлайн прогона исчерпан до очередного вызова модели."""


class _Stop(Exception):
    """Прогон закончен со статусом (DONE/BLOCKED модели, лимит шагов, нет прогресса)."""

    def __init__(self, status: Status, error: str | None = None) -> None:
        super().__init__(error or status)
        self.status: Status = status
        self.error = error


class Agent(AgentLike):
    """Один `browse`: своя фоновая вкладка, цикл до DONE/BLOCKED/лимита/дедлайна, финальный скриншот."""

    def __init__(
        self,
        chrome: Chrome,
        clients: ModelClients,
        url: str,
        goal: str,
        run: RunConfig,
        *,
        screenshot_quality: int = 60,
        screenshot_scale: float = 1.0,
        cancel: threading.Event | None = None,
    ) -> None:
        if not url.strip():
            raise ValueError("Supply a url")
        if not goal.strip():
            raise ValueError("Supply a goal")
        if run.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.chrome = chrome
        self.clients = clients
        self.url = url.strip()
        self.goal = goal.strip()
        self.config = run
        self.screenshot_quality = screenshot_quality
        self.screenshot_scale = screenshot_scale
        self.cancel = cancel
        self._begin()

    # --- состояние одного прогона ------------------------------------------------------------------------------

    def _begin(self) -> None:
        self.steps: list[Step] = []
        self._tab: Tab | None = None
        self._tab_dead = False
        self._page: dict[str, Any] | None = None
        self._decision: Decision | None = None
        self._history: list[dict[str, Any]] = []
        self._pending_text: tuple[dict[str, Any], str, TextHelper] | None = None
        self._model_calls = 0
        self._jev_calls = 0
        self._deadline = time.monotonic() + self.config.timeout_s
        self._acc = Timing()
        self._acc_cost: float | None = None
        self._total = Timing()
        self._total_cost: float | None = None

    def _add(self, timing: Timing, cost: float | None = None) -> None:
        self._acc = self._acc + timing
        if cost is not None:
            self._acc_cost = (self._acc_cost or 0.0) + cost

    def _drain(self) -> tuple[Timing, float | None]:
        """Замеры и стоимость с прошлой границы (шаг, старт); всё попадает и в итог прогона."""
        timing = self._acc + (self._tab.take_timing() if self._tab is not None else Timing())
        cost = self._acc_cost
        self._acc, self._acc_cost = Timing(), None
        self._total = self._total + timing
        if cost is not None:
            self._total_cost = (self._total_cost or 0.0) + cost
        return timing, cost

    def _check_cancel(self) -> None:
        """Перед вызовом модели и перед действием: отменённый прогон не мутирует страницу и не платит за модель."""
        if self.cancel is not None and self.cancel.is_set():
            raise _Stop("failed", CANCELLED)

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RunTimeout(f"Run timed out after {self.config.timeout_s:g} s")
        return remaining

    # --- публичный вход ----------------------------------------------------------------------------------------

    def run(self) -> RunResult:
        """Блокирующий прогон. Исключения наружу — только ошибки конфигурации (нет ключа Jev)."""
        if not self.clients.config.jev_api_key:
            raise ValueError("No Jev API key: set OPENROUTER_API_KEY or BROWSER_HANDS_JEV_API_KEY.")
        self._begin()
        started = time.perf_counter()
        completed = False
        status: Status = "failed"
        error: str | None = None
        screenshot: bytes | None = None
        kept = False
        try:
            status, error = self._loop()
            completed = error != CANCELLED  # отменённому финальный кадр не нужен: освобождаем вкладку сразу
        finally:
            screenshot, kept = self._finish(take_screenshot=completed)
        self._drain()
        page = self._page or {}
        result = RunResult(
            status=status,
            steps=self.steps,
            url=page.get("url") or self.url,
            title=page.get("title") or "",
            screenshot_jpeg=screenshot,
            cost=self._total_cost,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            timing=self._total,
            model_calls=self._model_calls,
            error=error,
            tab_kept=kept,
        )
        log.info(
            "browse %s: %d steps, %d model calls, %d ms%s",
            status,
            len(self.steps),
            self._model_calls,
            result.elapsed_ms,
            f" ({error})" if error else "",
        )
        return result

    def _loop(self) -> tuple[Status, str | None]:
        try:
            self._check_cancel()
            self._tab = tab = self.chrome.new_tab(
                screenshot_quality=self.screenshot_quality, screenshot_scale=self.screenshot_scale
            )
            tab.deadline = self._deadline
            self._check_cancel()
            tab.navigate(self.url, timeout=min(NAVIGATE_TIMEOUT_S, max(0.0, self._deadline - time.monotonic())))
            try:
                self._page = tab.observe()
            except StalePage:
                self._page = None  # следующий тик переснимет
            self._drain()  # старт: вкладка, навигация, первый снимок
            while True:
                if time.monotonic() >= self._deadline:
                    return "timeout", f"Run timed out after {self.config.timeout_s:g} s"
                self._check_cancel()  # не переснимать страницу зря; главная проверка — прямо перед моделью
                self._tick()
        except _Stop as stop:
            return stop.status, stop.error
        except TimeoutError as exc:  # RunTimeout, ModelTimeout, CDPTimeout
            if time.monotonic() >= self._deadline:
                return "timeout", str(exc) or f"Run timed out after {self.config.timeout_s:g} s"
            return "failed", f"{type(exc).__name__}: {exc}"
        except (TabGone, ChromeDisconnected) as exc:
            self._tab_dead = True
            return "failed", f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            log.debug("browse failed", exc_info=True)
            return "failed", f"{type(exc).__name__}: {exc}"

    def _finish(self, *, take_screenshot: bool) -> tuple[bytes | None, bool]:
        """Финальный кадр той же вкладки (≤5 с), затем закрыть её — или оставить при keep_open."""
        tab = self._tab
        if tab is None:
            return None, False
        tab.deadline = None
        screenshot = None
        if take_screenshot and not self._tab_dead:
            try:
                screenshot = tab.screenshot(timeout=SCREENSHOT_TIMEOUT_S)
            except Exception as exc:
                log.warning("Скриншот не снят: %s", exc)
        kept = False
        try:
            if self.config.keep_open and not self._tab_dead:
                tab.release()
                kept = True
            else:
                tab.close()
        except Exception as exc:
            log.warning("Вкладка не закрыта: %s", exc)
        return screenshot, kept

    # --- цикл (tick / predict / act из источника) ------------------------------------------------------------

    def _tick(self) -> None:
        try:
            self._predict()
            self._act()
        except StalePage:
            self._decision = None
            try:
                self._page = self._require_tab().observe()
            except StalePage:
                pass  # страница ещё грузится: следующий тик проверит свежесть снова

    def _require_tab(self) -> Tab:
        if self._tab is None:
            raise RuntimeError("No tab: run() creates it")
        return self._tab

    def _predict(self) -> None:
        tab = self._require_tab()
        if self._page is None or not tab.fresh(self._page):
            self._page = tab.observe()
        self._decision = None
        if self._jev_calls >= 2 * self.config.max_steps:
            raise _Stop("step_limit", f"Model-call budget exhausted ({self._jev_calls} decisions)")
        self._check_cancel()
        remaining = self._remaining()
        started = time.perf_counter()
        try:
            decision = choose(self.clients, self._page, self.goal, self._history, timeout=remaining)
        finally:
            self._model_calls += 1
            self._jev_calls += 1
            self._add(Timing(model_ms=round((time.perf_counter() - started) * 1000)))
        self._add(Timing(), decision.cost)
        self._decision = decision

    def _act(self) -> None:
        tab = self._require_tab()
        decision, page = self._decision, self._page
        if decision is None or page is None:
            raise RuntimeError("Observe and choose before acting")
        # Потребить один раз, до любой мутации и вызова модели: повтор не кликнет дважды.
        self._decision = None
        self._check_cancel()
        selected = decision.choice
        if selected in {"DONE", "BLOCKED"}:
            if not tab.fresh(page):
                raise StalePage("Page changed since the decision. Choose again.")
            if selected == "DONE":
                raise _Stop("done")
            raise _Stop("blocked", "Model chose BLOCKED: no supported operation can progress.")
        action = next((a for a in page["actions"] if a["id"] == selected), None)
        if action is None:
            raise ValueError(f"Decision {selected!r} is not an observed action")
        if len(self.steps) >= self.config.max_steps:
            raise _Stop("step_limit", f"Stopped at max_steps={self.config.max_steps}")
        text: str | None = None
        if action["kind"] == "fill":
            if not tab.fresh(page):
                raise StalePage("Page changed before text generation. Choose again.")
            context = field_context(self.goal, action, page, self._history)
            if self._pending_text and self._pending_text[0] == context:
                _, text, _helper = self._pending_text
            else:
                self._check_cancel()
                remaining = self._remaining()
                started = time.perf_counter()
                try:
                    text, helper = field_text(self.clients, context, timeout=remaining)
                finally:
                    self._model_calls += 1
                    self._add(Timing(text_ms=round((time.perf_counter() - started) * 1000)))
                self._add(Timing(), helper.cost)
                self._pending_text = (context, text, helper)
        # Tab.act проверяет свежесть прямо перед вводом, в том числе после генерации текста.
        self._check_cancel()
        tab.act(action, page, text=text)
        self._pending_text = None
        # Исполнение записано до наблюдения: устаревший снимок после действия не сотрёт его.
        self._history.append(
            {
                "step": len(self._history) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "text": text,
                "operation": decision.operation,
                "target": decision.target,
                "page_changed": None,
                "url": page["url"],
            }
        )
        step = Step(
            index=len(self.steps) + 1,
            operation=decision.operation,
            target=action["label"],
            text=text,
            page_changed=None,
            url=page["url"],
            confidence=decision.confidence,
            timing=Timing(),
            cost=None,
        )
        self.steps.append(step)
        try:
            new_page = tab.observe()
        finally:
            step.timing, step.cost = self._drain()
        self._page = new_page
        changed = new_page["fingerprint"] != page["fingerprint"]
        self._history[-1].update(page_changed=changed, url=new_page["url"])
        step.page_changed, step.url = changed, new_page["url"]
        # INFO — без персональных данных: метка элемента, длина текста, хост. Текст и полный url — только DEBUG.
        log.info(
            "step %d %s %r%s @ %s → %s (model %d / text %d / browser %d / wait %d ms)",
            step.index,
            step.operation,
            step.target,
            f" len={len(text)}" if text is not None else "",
            url_host(new_page["url"]),
            "changed" if changed else "unchanged",
            step.timing.model_ms,
            step.timing.text_ms,
            step.timing.browser_ms,
            step.timing.wait_ms,
        )
        log.debug("step %d text=%r url=%s", step.index, text, new_page["url"])
        repeated = self._history[-NO_PROGRESS_STEPS:]
        if len(repeated) == NO_PROGRESS_STEPS and all(
            h["page_changed"] is False and h["kind"] != "wait" for h in repeated
        ):
            raise _Stop("blocked", f"No page change after {NO_PROGRESS_STEPS} consecutive actions")
