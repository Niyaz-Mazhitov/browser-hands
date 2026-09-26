"""Полный цикл агента: снимок → один запрос к Jev → исполнение только выбранной цели → снимок.

Перенос `jev_ultrafast/agent.py` (MIT, Browser Use) в блокирующий `Agent.run() -> RunResult`. Инварианты источника:
решение потребляется до любой мутации (повтор не кликнет дважды); `StalePage` → переснять и выбрать заново;
сгенерированный текст переиспользуется только при идентичном контексте; исполнение записывается до наблюдения;
3 действия подряд без изменения страницы (кроме WAIT) → blocked. Повторы — сетевые, внутри `ModelClients.post`, и один
запрос к текстовой модели при невалидном ответе, ошибке провайдера или таймауте (до ввода). BLOCKED переспрашивается
один раз после успокоения страницы (снова — после действия, изменившего её).
Отмена (`cancel`) проверяется перед каждым вызовом модели и каждым действием: `failed` / `cancelled`, без новых мутаций.

Сценарий (`steps`, docs/plan-scenarios.md §4.2): Jev ведёт текущий шаг и в том же запросе отвечает «шаг выполнен?»
(`step_done`). Шаг закрыт — P(yes) ≥ `STEP_DONE_MIN_P` или DONE операции с P(yes) ≥ `DONE_STEP_DONE_MIN_P` на свежей
странице (действие этого решения не исполняется: выбиралось под старый шаг) или TYPE_TEXT текста шага, после которого
поле на свежем снимке этот текст показывает (пропал — пометка в истории и новое решение Jev; после `RETYPE_LIMIT`
повторов — `blocked`). DONE без такого P(yes) — режим проверки: успокоение, свежий снимок и вопрос только с DONE, WAIT
и BLOCKED; снова DONE без подтверждения или второй WAIT — `unconfirmed`. Текст шага печатается дословно и только через
`Tab.act` в поле, которое выбрал Jev (проверка фокуса, не password/file); текстовая модель — только для шага без `text`
и только при `goal` (без него — `blocked`), тексты шагов ей не уходят. На шаг — не больше `STEP_ACTIONS_LIMIT` действий
(`step_limit`), «3 без изменений» — внутри шага; решений — `2 × max_steps + M`; последний шаг закрыт — `done`.

В обоих режимах CLICK/TYPE_TEXT/SELECT с уверенностью ниже `MIN_ACTION_CONFIDENCE` не исполняется: первый раз — как WAIT
(успокоение, свежий снимок, новый вопрос), второй подряд — `blocked` (docs/core-notes.md, «Живая проверка WhatsApp»).

Вкладка: в attach — открытая вкладка пользователя (`Chrome.find_user_tab`: тот же хост и порт, задан только сайт или
ровно эта страница), без навигации; в конце — только `release()`, никогда не закрывается. Все подходящие заняты или
в разных профилях — `failed` без своей вкладки. Иначе (launch, ws, `new_tab`, сайт не открыт или открыт на другой
странице) — своя фоновая с переходом, как раньше.
"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from typing import Any, NoReturn

from .browser import NAVIGATE_TIMEOUT_S, StalePage, Tab, url_host
from .cdp import CDPError, ChromeDisconnected, TabGone
from .chrome import Chrome, TabTaken, UserTabUnavailable
from .config import RunConfig
from .model import (
    Decision,
    InvalidTextValue,
    ModelClients,
    ModelTimeout,
    StepContext,
    TextHelper,
    choose,
    field_context,
    field_text,
)
from .scenario import ScenarioStep, parse_steps
from .types import AgentLike, RunResult, Status, Step, TabKind, Timing

log = logging.getLogger(__name__)

NO_PROGRESS_STEPS = 3
SCREENSHOT_TIMEOUT_S = 5.0
CANCELLED = "cancelled"
EMPTY_PAGE_WAIT_S = 25.0  # потолок ожидания элементов для действия до первого вызова Jev (не дольше дедлайна)
# То же после первого вызова: страница после действия уже успокоилась (SETTLE), пустой экран посреди работы (форма после
# Submit — «Thanks» без элементов) — дальше решает Jev.
EMPTY_PAGE_WAIT_LATER_S = 1.0
EMPTY_PAGE_POLL_S = 0.25
INTERACTIVE_KINDS = frozenset({"fill", "click", "select"})
TEXT_ATTEMPTS = 2  # текст переспрашивается один раз (невалидный ответ, таймаут): до ввода, ничего не напечатано
STEP_DONE_MIN_P = 0.7  # P(yes) головы `step_done`, с которой шаг сценария выполнен (docs/plan-scenarios.md §0.2)
DONE_STEP_DONE_MIN_P = 0.5  # DONE операции закрывает шаг, только если P(yes) в том же ответе не ниже (после ревью)
STEP_ACTIONS_LIMIT = 6  # действий на шаг сценария (WAIT считается); больше — step_limit (§0.4)
LOG_LABEL_MAX = 40  # подпись элемента в INFO — не длиннее, с «…» (имя чата, тема письма); полностью — в результате
# Живая проверка WhatsApp 26.09 (docs/core-notes.md):
RETYPE_LIMIT = 2  # повторных вводов текста шага, пропавшего из поля; пропал и после них — blocked
TEXT_VANISHED = "text vanished after typing (page re-rendered)"  # пометка в истории (Jev видит в recent_actions)
CHECK_WAITS = 2  # WAIT в режиме проверки после неподтверждённого DONE: второй — unconfirmed
MIN_ACTION_CONFIDENCE = 0.3  # уверенность (`conf` шага) CLICK/TYPE_TEXT/SELECT ниже — не исполняется
UNCERTAIN_LIMIT = 2  # неуверенных действий подряд — blocked
ACTING_OPERATIONS = frozenset({"CLICK", "TYPE_TEXT", "SELECT"})
# Эмодзи и прочие символы: WhatsApp может рисовать эмодзи в поле картинкой (<img alt>), в innerText их нет.
SYMBOL_CATEGORIES = frozenset({"So", "Sk", "Cf", "Mn", "Me", "Cs", "Co"})


def short_label(label: str) -> str:
    """Подпись элемента для INFO: до `LOG_LABEL_MAX` символов, обрезанная — с «…» в пределах лимита."""
    return label if len(label) <= LOG_LABEL_MAX else label[: LOG_LABEL_MAX - 1] + "…"


def interactive(page: dict[str, Any]) -> bool:
    """В снимке есть хотя бы один элемент для действия (`fill|click|select`); `wait`/`scroll_*` не считаются."""
    return any(a.get("kind") in INTERACTIVE_KINDS for a in page.get("actions") or ())


def typed_fields(page: dict[str, Any], action: dict[str, Any]) -> list[dict[str, Any]]:
    """Поле, куда печатали, на новом снимке: тот же узел, если он ещё есть; иначе — поля той же роли и подписи (сайт
    перерисовал поле — узел новый). Пусто — поля не видно."""
    fields = [a for a in page.get("actions") or () if a.get("kind") in {"fill", "click"} and "node" in a]
    same = [a for a in fields if a["node"] == action.get("node")]
    if same:
        return same
    return [
        a
        for a in fields
        if a["kind"] == "fill" and a.get("role") == action.get("role") and a.get("label") == action.get("label")
    ]


def _spaces(text: str) -> str:
    return " ".join(text.split())  # str.split() режет и по NBSP, переводам строк


def _bare(text: str) -> str:
    """Текст без эмодзи и символов (`SYMBOL_CATEGORIES`), пробелы нормализованы."""
    text = unicodedata.normalize("NFC", text)
    return _spaces("".join(c for c in text if unicodedata.category(c) not in SYMBOL_CATEGORIES))


def shows_text(value: Any, text: str) -> bool:
    """Значение поля содержит текст шага (пробелы нормализованы). Запасное сравнение — без эмодзи и символов с обеих
    сторон, если без них от текста что-то остаётся: эмодзи картинкой в innerText не попадает."""
    if not isinstance(value, str):
        return False
    if _spaces(text) in _spaces(value):
        return True
    bare = _bare(text)
    return bool(bare) and bare in _bare(value)


class RunTimeout(TimeoutError):
    """Общий дедлайн прогона исчерпан до очередного вызова модели."""


class _Stop(Exception):
    """Прогон закончен со статусом (DONE/BLOCKED модели, лимит шагов, нет прогресса)."""

    def __init__(self, status: Status, error: str | None = None) -> None:
        super().__init__(error or status)
        self.status: Status = status
        self.error = error


class Agent(AgentLike):
    """Один `browse`: вкладка пользователя (attach) или своя фоновая, цикл до DONE/BLOCKED/лимита/
    дедлайна, финальный скриншот."""

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
        steps: list[ScenarioStep] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        if not url.strip():
            raise ValueError("Supply a url")
        if not goal.strip() and not steps:
            raise ValueError("Supply a goal or steps")
        if run.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.chrome = chrome
        self.clients = clients
        self.url = url.strip()
        self.goal = goal.strip()  # при сценарии может быть пустой; иначе — общая цель для Jev
        # Сценарий проверяется заново и при прямом вызове мимо сервера (ScenarioError — ValueError); None — режим цели.
        self.scenario: tuple[ScenarioStep, ...] | None = None if steps is None else tuple(parse_steps(steps))
        self.config = run
        self.screenshot_quality = screenshot_quality
        self.screenshot_scale = screenshot_scale
        self.cancel = cancel
        self._begin()

    # --- состояние одного прогона ------------------------------------------------------------------------------

    def _begin(self) -> None:
        self.steps: list[Step] = []
        self._tab: Tab | None = None
        self._tab_kind: TabKind | None = None  # None — до вкладки не дошло
        self._tab_dead = False
        self._empty_since: float | None = (
            None  # monotonic: с какого момента ждём элементы (ожидание пережило StalePage)
        )
        self._page: dict[str, Any] | None = None
        self._decision: Decision | None = None
        self._history: list[dict[str, Any]] = []
        self._pending_text: tuple[dict[str, Any], str, TextHelper] | None = None
        self._second_chance_used = False  # BLOCKED переспрашивается раз; снова — после действия, сменившего страницу
        self._checking = False  # режим проверки шага после неподтверждённого DONE: Jev видит только DONE/WAIT/BLOCKED
        self._check_waits = 0  # WAIT в режиме проверки
        self._vanished = 0  # сколько раз на шаге текст шага пропал из поля после ввода
        self._uncertain = 0  # неуверенных действий подряд (уверенность ниже MIN_ACTION_CONFIDENCE)
        self._model_calls = 0
        self._jev_calls = 0
        self._scenario_no = 1  # текущий шаг сценария (с 1)
        self._scenario_done = 0
        self._step_actions = 0  # действий на текущем шаге
        self._step_history = 0  # len(self._history) на начале шага: «3 без изменений» — только внутри шага
        self._location: tuple[str, str] | None = None  # url/title вкладки, если последний снимок не удался (сценарий)
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
        url, title = self._location or (page.get("url") or self.url, page.get("title") or "")
        scenario = self.scenario
        result = RunResult(
            status=status,
            steps=self.steps,
            url=url,
            title=title,
            screenshot_jpeg=screenshot,
            cost=self._total_cost,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            timing=self._total,
            model_calls=self._model_calls,
            error=error,
            tab_kept=kept,
            tab=self._tab_kind,
            scenario_done=None if scenario is None else self._scenario_done,
            scenario_total=None if scenario is None else len(scenario),
            jev_calls=self._jev_calls,
        )
        if scenario is None:
            log.info(
                "browse %s: %d steps, %d model calls, %d ms, tab %s%s",
                status,
                len(self.steps),
                self._model_calls,
                result.elapsed_ms,
                self._tab_kind or "-",
                f" ({error})" if error else "",
            )
        else:
            log.info(
                "browse %s: %d/%d steps of scenario, %d actions, %d model calls (Jev %d), %d ms, tab %s%s",
                status,
                self._scenario_done,
                len(scenario),
                len(self.steps),
                self._model_calls,
                self._jev_calls,
                result.elapsed_ms,
                self._tab_kind or "-",
                f" ({error})" if error else "",
            )
        return result

    def _open_tab(self) -> Tab:
        """Вкладка пользователя (attach, без `new_tab`) или своя. Все подходящие заняты (`attached` или чужая метка
        `__bhOwner`) или в разных профилях, не удалось прицепиться к найденной — ошибка прогона, без тихого перехода в
        свою (он маскирует проблему). Чужая метка — следующая свободная вкладка; такая считается занятой."""
        taken: set[str] = set()
        try:
            while not self.config.new_tab and (target := self.chrome.find_user_tab(self.url, skip=taken)) is not None:
                try:
                    tab = self.chrome.attach_tab(
                        target, screenshot_quality=self.screenshot_quality, screenshot_scale=self.screenshot_scale
                    )
                except TabTaken:
                    taken.add(target)
                    continue
                self._tab, self._tab_kind = tab, "user"
                return tab
        except UserTabUnavailable as exc:
            raise _Stop("failed", str(exc)) from None
        self._tab = tab = self.chrome.new_tab(
            screenshot_quality=self.screenshot_quality, screenshot_scale=self.screenshot_scale
        )
        self._tab_kind = "new"
        return tab

    def _loop(self) -> tuple[Status, str | None]:
        try:
            self._check_cancel()
            tab = self._open_tab()
            tab.deadline, tab.cancel = self._deadline, self.cancel  # и для успокоения после действий
            self._check_cancel()
            if tab.owned:  # во вкладке пользователя — с того, что открыто: не переходим и не перезагружаем
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
        """Финальный кадр той же вкладки (≤5 с), затем закрыть свою — или оставить при keep_open. Вкладку
        пользователя — только отпустить (`release`), при любом исходе; `tab_kept` для неё False."""
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
            if not tab.owned:
                tab.release()
            elif self.config.keep_open and not self._tab_dead:
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

    def _await_interactive(self) -> None:
        """Пока в снимке нет элементов для действия (экран загрузки, логотип), Jev не зовём: ждём и переснимаем, не
        дольше потолка и дедлайна; отмена — сразу. Потолок — `EMPTY_PAGE_WAIT_S` до первого вызова Jev в прогоне,
        потом `EMPTY_PAGE_WAIT_LATER_S` (прокрутили за контролы, короткая перерисовка). Шаги и `model_calls` не растут,
        время — `wait_ms`. Потолок вышел — Jev решает по пустой странице (DONE/WAIT законны)."""
        tab = self._require_tab()
        page = self._page
        if page is None or interactive(page):
            self._empty_since = None
            return
        ceiling = EMPTY_PAGE_WAIT_S if self._jev_calls == 0 else EMPTY_PAGE_WAIT_LATER_S
        if self._empty_since is None:
            self._empty_since = time.monotonic()
            log.info("Нет элементов для действия, жду до %g с (%s)", ceiling, url_host(page.get("url") or self.url))
        until = min(self._empty_since + ceiling, self._deadline)
        while not interactive(page) and time.monotonic() < until:
            self._check_cancel()
            tab.pause(EMPTY_PAGE_POLL_S)
            self._page = page = tab.observe()  # StalePage — наружу, в _tick: ожидание продолжится с тем же потолком
        log.debug(
            "Ожидание элементов: %.1f с, %s",
            time.monotonic() - self._empty_since,
            "появились" if interactive(page) else "не появились",
        )
        self._empty_since = None

    # --- сценарий ------------------------------------------------------------------------------------------------

    def _scenario_step(self) -> ScenarioStep | None:
        """Текущий шаг сценария; None — режим цели."""
        return None if self.scenario is None else self.scenario[self._scenario_no - 1]

    def _step_context(self) -> StepContext | None:
        return None if self.scenario is None else StepContext(self.scenario, self._scenario_no, self.goal or None)

    def _step_name(self) -> str:
        """«step 2 of 4» — для ошибок прогона."""
        return f"step {self._scenario_no} of {len(self.scenario or ())}"

    def _text_goal(self) -> str:
        """Цель для текстовой модели (шаг без `text`): общая цель и текущий шаг; в режиме цели — просто `goal`."""
        current = self._scenario_step()
        if current is None:
            return self.goal
        line = f"Current {self._step_name()}: {current.do}"
        return f"{self.goal}\n{line}" if self.goal else line

    def _advance(self, how: str) -> None:
        """Текущий шаг сценария выполнен: счётчики, второй шанс снова доступен; последний шаг — `done`. INFO — номер,
        причина и число действий, без `do` и текста."""
        total = len(self.scenario or ())
        log.info("шаг %d/%d выполнен (%s, действий %d)", self._scenario_no, total, how, self._step_actions)
        self._scenario_done += 1
        self._step_actions = 0
        self._step_history = len(self._history)
        self._second_chance_used = False
        self._checking, self._check_waits, self._vanished, self._uncertain = False, 0, 0, 0
        if self._scenario_done >= total:
            raise _Stop("done")
        self._scenario_no += 1

    def _check_step_limit(self) -> None:
        """Сценарий: на шаге уже `STEP_ACTIONS_LIMIT` действий — `step_limit`, следующее не исполняется."""
        if self._step_actions >= STEP_ACTIONS_LIMIT:
            raise _Stop(
                "step_limit", f"{self._step_name().capitalize()} not completed after {self._step_actions} actions"
            )

    def _record_observation(self, page: dict[str, Any], new_page: dict[str, Any], step: Step) -> bool:
        """Снимок после действия: страница, запись истории и `Step` (изменилась ли, url); изменилась — второй шанс
        снова доступен. Возвращает «изменилась»."""
        self._page = new_page
        changed = new_page["fingerprint"] != page["fingerprint"]
        self._history[-1].update(page_changed=changed, url=new_page["url"])
        step.page_changed, step.url = changed, new_page["url"]
        if changed:
            self._second_chance_used = False
        return changed

    def _refresh_page(self) -> None:
        """Прогон кончается `unconfirmed`: url и title результата — со свежего снимка. Устарел — url и title вкладки
        (`Tab.location`, `Target.getTargetInfo`); нет и их — прошлый снимок. Ошибки CDP исход не меняют."""
        tab = self._require_tab()
        try:
            try:
                self._page = tab.observe()
            except StalePage:
                self._location = tab.location()
        except (TabGone, ChromeDisconnected) as exc:
            self._tab_dead = True
            log.debug("Снимок в конце не снят: %s", exc)
        except (CDPError, TimeoutError) as exc:
            log.debug("Снимок в конце не снят: %s", exc)

    def _unconfirmed(self, decision: Decision) -> NoReturn:
        """Режим проверки не подтвердил шаг (снова DONE без подтверждения или второй WAIT): `unconfirmed`, url и title
        — свежие, финальный кадр снимет `_finish`."""
        total = len(self.scenario or ())
        log.info(
            "шаг %d/%d не подтверждён при проверке (%s, p=%.2f)",
            self._scenario_no,
            total,
            decision.operation,
            decision.step_done or 0.0,
        )
        self._refresh_page()
        raise _Stop("unconfirmed", f"{self._step_name()}: probably done, not confirmed — check the screenshot")

    def _check_typed(self, action: dict[str, Any], text: str, new_page: dict[str, Any]) -> None:
        """Текст шага напечатан: шаг закрыт, только если поле на свежем снимке его показывает (`typed_fields`,
        `shows_text`). Нет — пометка `TEXT_VANISHED` в истории, решает Jev по этому снимку (повторный ввод допустим);
        пропал и после `RETYPE_LIMIT` повторов — `blocked`."""
        if any(shows_text(field.get("value"), text) for field in typed_fields(new_page, action)):
            self._advance("text typed")
            return
        self._vanished += 1
        self._history[-1]["note"] = TEXT_VANISHED
        total = len(self.scenario or ())
        log.info("шаг %d/%d: текста нет в поле после ввода (%d-й раз)", self._scenario_no, total, self._vanished)
        if self._vanished > RETYPE_LIMIT:
            raise _Stop("blocked", f"{self._step_name()}: typed text does not stay in the field")

    def _field_text(self, context: dict[str, Any]) -> tuple[str, TextHelper]:
        """Текст для TYPE_TEXT. Невалидный ответ или ошибка провайдера (`InvalidTextValue`) и таймаут (`ModelTimeout`,
        потолок `text_timeout_s` или остаток дедлайна) — ещё один запрос с тем же контекстом: браузер ещё не тронут,
        повтор безопасен. Всего `TEXT_ATTEMPTS` на поле, какой бы ни была причина. Отмена и дедлайн — перед каждым
        (дедлайн вышел — `timeout`, без второго запроса); `model_calls`, `text_ms` и цена — за каждый. Вторая неудача и
        прочие ошибки (нет ключа, сеть) — наружу."""
        for attempt in range(1, TEXT_ATTEMPTS + 1):
            self._check_cancel()
            remaining = self._remaining()
            started = time.perf_counter()
            try:
                text, helper = field_text(self.clients, context, timeout=remaining)
            except (InvalidTextValue, ModelTimeout) as exc:
                reason = exc.reason if isinstance(exc, InvalidTextValue) else "timeout"
                self._add(Timing(), exc.cost if isinstance(exc, InvalidTextValue) else None)
                if attempt == TEXT_ATTEMPTS:
                    raise
                log.info("Повторяю запрос к текстовой модели (%s)", reason)
                continue
            finally:
                self._model_calls += 1
                self._add(Timing(text_ms=round((time.perf_counter() - started) * 1000)))
            self._add(Timing(), helper.cost)
            return text, helper
        raise AssertionError("unreachable")

    def _second_look(self, page: dict[str, Any]) -> None:
        """Второй шанс перед BLOCKED (`_look_again`): один раз, пока действие не изменит страницу."""
        self._second_chance_used = True
        self._look_again(page, "BLOCKED")

    def _look_again(self, page: dict[str, Any], why: str) -> None:
        """Новое решение Jev без действия: успокоение, переснимок, вопрос — в следующем тике. Второй шанс перед BLOCKED,
        проверка шага после неподтверждённого DONE и WAIT в ней, неуверенное действие. Не шаг: `Step` не создаётся, в
        истории — честная запись ожидания (Jev видит, что ждали). Отмена и дедлайн — до ожидания; время — `wait_ms`."""
        tab = self._require_tab()
        self._check_cancel()
        self._remaining()
        log.info("%s: жду успокоения и спрашиваю ещё раз (%s)", why, url_host(page["url"]))
        self._history.append(
            {
                "step": len(self._history) + 1,
                "action": "Wait for the page to update",
                "kind": "wait",
                "text": None,
                "operation": "WAIT",
                "target": None,
                "page_changed": None,
                "url": page["url"],
            }
        )
        tab.settle({"kind": "retry"})
        new_page = tab.observe()  # StalePage — наружу, в _tick: переснимет, запись ожидания остаётся
        self._page = new_page
        self._history[-1].update(page_changed=new_page["fingerprint"] != page["fingerprint"], url=new_page["url"])

    def _predict(self) -> None:
        tab = self._require_tab()
        if self._page is None or not tab.fresh(self._page):
            self._page = tab.observe()
        self._await_interactive()
        self._decision = None
        # Сценарий: + решение на шаг — на границе шагов Jev спрашивается ещё раз (§0.6), устаревшие не съедят лимит.
        budget = 2 * self.config.max_steps + len(self.scenario or ())
        if self._jev_calls >= budget:
            raise _Stop("step_limit", f"Model-call budget exhausted ({self._jev_calls} decisions)")
        self._check_cancel()
        remaining = self._remaining()
        started = time.perf_counter()
        try:
            decision = choose(
                self.clients,
                self._page,
                self.goal,
                self._history,
                timeout=remaining,
                step=self._step_context(),
                verify=self._checking,
            )
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
        current = self._scenario_step()
        p_done = decision.step_done or 0.0
        uncertain = decision.operation in ACTING_OPERATIONS and decision.confidence < MIN_ACTION_CONFIDENCE
        if not uncertain:
            self._uncertain = 0
        if current is not None and (
            p_done >= STEP_DONE_MIN_P or (selected == "DONE" and p_done >= DONE_STEP_DONE_MIN_P)
        ):
            # Шаг выполнен по свежей странице; действие этого решения выбиралось под этот шаг — не исполняется,
            # следующий тик спросит Jev уже под новый шаг (docs/plan-scenarios.md §0.6).
            if not tab.fresh(page):
                raise StalePage("Page changed since the decision. Choose again.")
            self._advance(f"{'DONE, ' if selected == 'DONE' else ''}p={p_done:.2f}")
            return
        if self._checking:
            # Режим проверки — без действий: снова DONE без подтверждения или второй WAIT — `unconfirmed`, BLOCKED — как
            # обычно (второй шанс, потом blocked). Другое сюда не доходит (`build_request(verify=True)`), но и не
            # исполняется.
            if selected == "DONE":
                self._unconfirmed(decision)
            if decision.operation == "WAIT":
                self._check_waits += 1
                if self._check_waits >= CHECK_WAITS:
                    self._unconfirmed(decision)
                self._look_again(page, "WAIT при проверке шага")
                return
            if selected != "BLOCKED":
                raise ValueError(f"Decision {decision.operation} is not allowed while checking the step")
        elif current is not None and selected == "DONE":
            # DONE без подтверждения step_done — шаг не выполнен, решение отброшено без мутации. Режим проверки:
            # успокоение, свежий снимок и вопрос только с DONE/WAIT/BLOCKED (живая проверка 26.09: иначе Jev искал, что
            # ещё сделать, и кликнул мимо).
            total = len(self.scenario or ())
            log.info("шаг %d/%d: DONE без подтверждения (p=%.2f) — проверяю", self._scenario_no, total, p_done)
            self._checking = True
            self._look_again(page, "DONE")
            return
        if selected in {"DONE", "BLOCKED"}:
            if not tab.fresh(page):
                raise StalePage("Page changed since the decision. Choose again.")
            if selected == "DONE":
                raise _Stop("done")
            if not self._second_chance_used:
                self._second_look(page)
                return
            where = "" if current is None else f" on {self._step_name()}"
            raise _Stop(
                "blocked", f"Model chose BLOCKED{where} after a second look; no supported operation can progress."
            )
        action = next((a for a in page["actions"] if a["id"] == selected), None)
        if action is None:
            raise ValueError(f"Decision {selected!r} is not an observed action")
        if uncertain:
            # Неуверенное действие не исполняется (26.09: CLICK «00:31 Sent», conf 0.25): первый раз — как WAIT, второй
            # подряд — blocked.
            self._uncertain += 1
            if self._uncertain >= UNCERTAIN_LIMIT:
                raise _Stop(
                    "blocked",
                    f"uncertain action: {decision.operation} on {short_label(action['label'])} "
                    f"(conf {decision.confidence:.2f})",
                )
            self._look_again(page, f"{decision.operation} conf {decision.confidence:.2f}")
            return
        if len(self.steps) >= self.config.max_steps:
            raise _Stop("step_limit", f"Stopped at max_steps={self.config.max_steps}")
        if current is not None:
            self._check_step_limit()
        text: str | None = None
        if action["kind"] == "fill":
            if not tab.fresh(page):
                raise StalePage("Page changed before text generation. Choose again.")
            if current is not None and current.text is not None:
                text = current.text  # дословно из сценария, без текстовой модели; ввод — Tab.act с проверкой фокуса
            elif current is not None and not self.goal:
                # Текста нет ни в шаге, ни в цели: выдумывать нечего — стоп, без текстовой модели и нового вопроса Jev.
                raise _Stop("blocked", f"{self._step_name()}: no text given for typing")
            else:
                # Сценарий: история без напечатанных текстов — тексты шагов текстовой модели не уходят.
                context = field_context(self._text_goal(), action, page, self._history, texts=current is None)
                if self._pending_text and self._pending_text[0] == context:
                    _, text, _helper = self._pending_text
                else:
                    text, helper = self._field_text(context)
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
            scenario_step=None if current is None else self._scenario_no,
        )
        self.steps.append(step)
        # Текст шага напечатан (Tab.act прошёл свежесть и фокус): шаг закрывается, если поле его показывает на свежем
        # снимке (`_check_typed`), без вопроса к Jev (§0.3).
        typed_step_text = (
            current is not None and current.text is not None and action["kind"] == "fill" and text == current.text
        )
        if current is not None:
            self._step_actions += 1
        try:
            try:
                new_page = tab.observe()  # после успокоения (`after_input`)
            except StalePage:
                if not typed_step_text:
                    raise
                # Снимок после ввода текста шага устарел: ещё один после успокоения; снова — наружу, шаг не закрыт,
                # решит Jev по следующему снимку.
                log.info("Снимок после ввода устарел: жду успокоения и снимаю ещё раз (%s)", url_host(page["url"]))
                tab.settle({"kind": "retry"})
                new_page = tab.observe()
        finally:
            step.timing, step.cost = self._drain()
        changed = self._record_observation(page, new_page, step)
        # INFO — без персональных данных: метка элемента (≤ LOG_LABEL_MAX), длина текста, хост. Текст и полный url —
        # только DEBUG.
        log.info(
            "step %d %s %r%s @ %s → %s (model %d / text %d / browser %d / wait %d ms)%s",
            step.index,
            step.operation,
            short_label(step.target),
            f" len={len(text)}" if text is not None else "",
            url_host(new_page["url"]),
            "changed" if changed else "unchanged",
            step.timing.model_ms,
            step.timing.text_ms,
            step.timing.browser_ms,
            step.timing.wait_ms,
            "" if current is None else f" [шаг {self._scenario_no}/{len(self.scenario or ())}]",
        )
        log.debug("step %d text=%r url=%s", step.index, text, new_page["url"])
        if typed_step_text and text is not None:
            self._check_typed(action, text, new_page)
        # Только действия текущего шага (в режиме цели — все): закрытые шаги с неизменной страницей не копятся.
        repeated = self._history[max(self._step_history, len(self._history) - NO_PROGRESS_STEPS) :]
        if len(repeated) == NO_PROGRESS_STEPS and all(
            h["page_changed"] is False and h["kind"] != "wait" for h in repeated
        ):
            raise _Stop("blocked", f"No page change after {NO_PROGRESS_STEPS} consecutive actions")
