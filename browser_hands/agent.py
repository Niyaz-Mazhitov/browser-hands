"""Полный цикл агента: снимок → один запрос к Jev → исполнение только выбранной цели → снимок.

Перенос `jev_ultrafast/agent.py` (MIT, Browser Use) в блокирующий `Agent.run() -> RunResult`. Инварианты источника:
решение потребляется до любой мутации (повтор не кликнет дважды); `StalePage` → переснять и выбрать заново;
сгенерированный текст переиспользуется только при идентичном контексте; исполнение записывается до наблюдения;
3 действия подряд без изменения страницы (кроме WAIT) → ожидание изменения и свежий снимок (как второй шанс перед
BLOCKED; снова — после действия, сменившего страницу, или пока в полёте запросы, начатые этими действиями), иначе
blocked. Запросы, начатые действиями с последней смены страницы, пережили ожидание после действия — перед вопросом Jev
ещё одно ожидание изменения (одно на такое действие); остались и тогда — Jev видит факт «страница ещё загружается»:
сколько их в полёте (`Tab.in_flight`, фоновые тоже; только число) — в любом режиме. Повторы — сетевые, внутри
`ModelClients.post`, и один запрос к текстовой модели при невалидном ответе, ошибке провайдера или таймауте (до ввода).
BLOCKED переспрашивается один раз после ожидания изменения страницы (снова — после действия, изменившего её).
Отмена (`cancel`) проверяется перед каждым вызовом модели и каждым действием: `failed` / `cancelled`, без новых мутаций.

Ожидания (docs/plan-waits.md §2): после действия — `Tab.await_ready(action)` (итог — в `Step.wait_reason` и
`pending_requests`), WAIT Jev, второй взгляд, проверка шага и повтор снимка — `Tab.await_change()`; своих пауз по
времени у агента нет.

Сценарий (`steps`, docs/plan-scenarios.md §4.2): Jev ведёт текущий шаг и в том же запросе отвечает «шаг выполнен?»
(`step_done`). Шаг закрыт — P(yes) ≥ `step_done_min_p` или DONE операции с P(yes) ≥ `done_step_done_min_p` на свежей
странице (действие этого решения не исполняется: выбиралось под старый шаг) или TYPE_TEXT текста шага, после которого
поле показывает этот текст (`matches`: начало значения или, по буквам и цифрам, всё значение, без учёта регистра; не
видно — ещё одно наблюдение после изменения страницы; пропал — пометка в истории и новое решение Jev). Текст шага,
закрытого кодом, — инвариант (docs/plan-waits.md §6.1): перед каждым решением (кроме подтверждения шага) и перед `done`
одним `Tab.field_values` проверяется, что поле ещё не пусто (`kept`: другое непустое значение — сайт отформатировал
текст); пусто или поля нет — откат к этому шагу (решение отброшено). Инвариант снимают
подтверждение Jev более позднего шага и исполненный клик не по полю (текст использован: Send очистил поле). Пропаж
текста шага — не больше `RETYPE_LIMIT` (сразу после ввода и позже — один счёт), дальше `blocked`. DONE без такого
P(yes) — режим проверки: ожидание изменения, свежий снимок и вопрос только с DONE, WAIT и BLOCKED; снова DONE без
подтверждения или второй WAIT на свежей странице — `unconfirmed`. Текст шага печатается дословно и только через
`Tab.act` в поле, которое выбрал Jev (проверка фокуса, не password/file); текстовая модель — только для шага без `text`
и только при `goal` (без него — `blocked`), тексты шагов ей не уходят. На шаг — не больше `STEP_ACTIONS_LIMIT` действий
(`step_limit`), «3 без изменений» — внутри шага; решений — `2 × max_steps + M`; последний шаг закрыт — `done`.

В обоих режимах CLICK/TYPE_TEXT/SELECT с уверенностью ниже `min_action_confidence` не исполняется: первый раз — как WAIT
(ожидание изменения, свежий снимок, новый вопрос), второй подряд — `blocked` (docs/core-notes.md, «Живая проверка
WhatsApp»). Режим цели: DONE с уверенностью ниже `done_min_confidence` — один второй взгляд, снова такой DONE —
`unconfirmed`; напечатанный текст не виден в поле на следующем снимке — пометка в истории (Jev видит). Виден — он
инвариант, пока его не использовали (клик не по полю, новый ввод в то же поле; docs/core-notes.md, «Медленные ответы и
текст в режиме цели»): перед каждым решением и перед `done` — `Tab.field_values`; пропал — решение отброшено, запись
ввода убрана из истории для Jev, повторный ввод — тем же текстом без текстовой модели; пропаж одного текста в поле
больше `RETYPE_LIMIT` — `blocked`.
Пороги — `Agent.thresholds` (`config.Thresholds`, docs/calibration.md); одноимённые константы модуля — алиасы
значений по умолчанию.

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
from dataclasses import dataclass
from typing import Any, NoReturn

from .browser import NAVIGATE_TIMEOUT_S, StalePage, Tab, url_host
from .cdp import CDPError, ChromeDisconnected, TabGone
from .chrome import Chrome, TabTaken, UserTabUnavailable
from .config import RunConfig, Thresholds
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
INTERACTIVE_KINDS = frozenset({"fill", "click", "select"})
TEXT_ATTEMPTS = 2  # текст переспрашивается один раз (невалидный ответ, таймаут): до ввода, ничего не напечатано
# Пороги модели — `Agent.thresholds` (`config.Thresholds`); константы ниже — алиасы значений по умолчанию (тесты, docs).
DEFAULT_THRESHOLDS = Thresholds()
STEP_DONE_MIN_P = DEFAULT_THRESHOLDS.step_done_min_p  # P(yes) головы `step_done`, с которой шаг сценария выполнен
DONE_STEP_DONE_MIN_P = DEFAULT_THRESHOLDS.done_step_done_min_p  # DONE операции закрывает шаг при P(yes) не ниже
STEP_ACTIONS_LIMIT = 6  # действий на шаг сценария (WAIT считается); больше — step_limit (§0.4)
LOG_LABEL_MAX = 40  # подпись элемента в INFO — не длиннее, с «…» (имя чата, тема письма); полностью — в результате
# Живая проверка WhatsApp 26.09 (docs/core-notes.md):
RETYPE_LIMIT = 2  # повторных вводов текста шага, пропавшего из поля (после ввода или позже); пропал снова — blocked
TEXT_VANISHED = "text vanished after typing (page re-rendered)"  # пометка в истории (Jev видит в recent_actions)
INVARIANT_BROKEN = "text of step {step} vanished (field re-rendered)"  # пометка при откате к шагу (§6.1)
CHECK_WAITS = 2  # WAIT в режиме проверки после неподтверждённого DONE: второй — unconfirmed
MIN_ACTION_CONFIDENCE = DEFAULT_THRESHOLDS.min_action_confidence  # `conf` CLICK/TYPE_TEXT/SELECT ниже — не исполняется
# Режим цели: DONE с уверенностью ниже — второй взгляд, снова такой — unconfirmed (docs/plan-waits.md §0.5).
DONE_MIN_CONFIDENCE = DEFAULT_THRESHOLDS.done_min_confidence
UNCERTAIN_LIMIT = 2  # неуверенных действий подряд — blocked
ACTING_OPERATIONS = frozenset({"CLICK", "TYPE_TEXT", "SELECT"})
GOAL_UNCONFIRMED = "goal probably done, not confirmed — check the screenshot"


def short_label(label: str) -> str:
    """Подпись элемента для INFO: до `LOG_LABEL_MAX` символов, обрезанная — с «…» в пределах лимита."""
    return label if len(label) <= LOG_LABEL_MAX else label[: LOG_LABEL_MAX - 1] + "…"


def _is_field(page: dict[str, Any], action: dict[str, Any]) -> bool:
    """Элемент действия — поле ввода (у его узла есть `fill`): клик в поле — фокус, а не использование текста."""
    return any(a.get("kind") == "fill" and a.get("node") == action.get("node") for a in page.get("actions") or ())


def interactive(page: dict[str, Any]) -> bool:
    """В снимке есть хотя бы один элемент для действия (`fill|click|select`); `wait`/`scroll_*` не считаются."""
    return any(a.get("kind") in INTERACTIVE_KINDS for a in page.get("actions") or ())


def _field_key(action: dict[str, Any]) -> str:
    """Ключ поля без роли: подпись (имя, `<label>`, title, placeholder). У поля без имени подпись в снимке — сама роль
    (`name || role`): ключ пустой. Такие поля ключом не различить (клетки кода из SMS): узнаём по узлу, а без узла —
    только единственное поле без имени на странице (`_same_field`, `typed_fields`)."""
    label = str(action.get("label") or "")
    return "" if label == action.get("role") else label


def _nameless(page: dict[str, Any]) -> list[Any]:
    """Узлы полей ввода (`fill`) без доступного имени на снимке."""
    return [a.get("node") for a in page.get("actions") or () if a.get("kind") == "fill" and not _field_key(a)]


def typed_fields(page: dict[str, Any], action: dict[str, Any]) -> list[dict[str, Any]]:
    """Поле, куда печатали, на новом снимке: тот же узел, если он ещё есть; иначе — поля `fill` с тем же ключом
    (`_field_key`), роль не сравнивается: сайт перерисовал поле новым узлом, Википедия — ещё и с другой ролью
    (searchbox → combobox). Поле без имени без своего узла — только если оно на странице одно. Текст сверяет
    вызывающий (`matches`). Пусто — поля не видно."""
    fields = [a for a in page.get("actions") or () if a.get("kind") in {"fill", "click"} and "node" in a]
    same = [a for a in fields if a["node"] == action.get("node")]
    if same:
        return same
    key = _field_key(action)
    found = [a for a in fields if a["kind"] == "fill" and _field_key(a) == key]
    return found if key or len(found) == 1 else []


def _spaces(text: str) -> str:
    return " ".join(text.split())  # str.split() режет и по NBSP, переводам строк


def _alnum(text: str) -> str:
    """Только буквы и цифры (после NFC): без масок телефона, карты, даты и без эмодзи."""
    return "".join(c for c in unicodedata.normalize("NFC", text) if c.isalnum())


def matches(value: Any, text: str) -> bool:
    """Поле показывает напечатанный текст (docs/plan-waits.md §0.3) — по нему код закрывает шаг с текстом: значение
    начинается с текста, пробелы нормализованы, регистр не важен (`selectAll` + `insertText` заменяет всё,
    inline-подсказки дописывают хвост, поле может писать заглавными). Запасное — буквы и цифры значения равны буквам и
    цифрам текста (маска `+7 (777) 123-45-67`, эмодзи картинкой `<img alt>` — в `innerText` его нет), если в тексте они
    есть. Текст в середине чужого значения — не совпадение.

    Строго намеренно: совпадение закрывает шаг без Jev, а ложное закрытие пропускает шаг (Send с чужим текстом).
    Поле, которое меняет цифры при вводе (8… → +7 …), код не узнаёт — пометка, шаг закрывает `step_done` Jev."""
    if not isinstance(value, str):
        return False
    if _spaces(value).casefold().startswith(_spaces(text).casefold()):
        return True
    bare = _alnum(text).casefold()
    return bool(bare) and _alnum(value).casefold() == bare


def kept(value: Any) -> bool:
    """Инвариант напечатанного текста держится: поле есть и не пусто (docs/core-notes.md, «После ревью ожиданий»).
    Непустое изменённое значение — нормализация сайтом (1000 → 1,000.00, abc → ABC, 2.1.2024 → 02.01.2024, маска
    телефона), не пропажа; пропал — только когда поле пусто (пробелы и символы нулевой ширины не в счёт) или поля нет
    (пересоздано без текста, как у WhatsApp)."""
    return isinstance(value, str) and any(not c.isspace() and unicodedata.category(c) != "Cf" for c in value)


def _count(value: Any) -> int:
    """Число запросов из `Tab.in_flight`; не целое (вкладка без учёта сети, заглушка) — 0."""
    return value if type(value) is int and value > 0 else 0


def _same_field(typed: Invariant, action: dict[str, Any], page: dict[str, Any]) -> bool:
    """Поле действия (`fill` на снимке `page`) — то, куда печатали `typed`: тот же узел или тот же ключ (`_field_key`;
    узел мог смениться). Поле без имени (ключ "") — только тот же узел, а если узла `typed` на снимке нет (поле
    пересоздано) — когда поле действия — единственное поле без имени."""
    if typed.node is not None and typed.node == action.get("node"):
        return True
    if typed.label:
        return typed.label == _field_key(action)
    nameless = _nameless(page)
    return not _field_key(action) and typed.node not in nameless and nameless == [action.get("node")]


def _same_entry(a: Invariant, b: Invariant) -> bool:
    """Два запомненных ввода — в одно поле: тот же узел или тот же непустой ключ (поля без имени — только по узлу)."""
    return a.node == b.node or (bool(a.label) and a.label == b.label)


def wait_fields(result: Any) -> tuple[str | None, int]:
    """`Step.wait_reason` и `pending_requests` из итога `Tab.await_ready`/`await_change`: `wait_reason` (иначе `reason`)
    и число запросов в полёте (`pending_requests`, иначе `pending`: число или их список). Не ждали (`{}`) — None и 0."""
    if not isinstance(result, dict):
        return None, 0
    reason = result.get("wait_reason", result.get("reason"))
    pending = result.get("pending_requests", result.get("pending", 0))
    if isinstance(pending, list | tuple | set | dict):
        pending = len(pending)
    return (reason if isinstance(reason, str) else None), (pending if type(pending) is int else 0)


@dataclass(frozen=True, slots=True)
class Invariant:
    """Текст шага сценария, закрытого кодом, должен оставаться в поле, пока его не использовали (docs/plan-waits.md
    §6.1): узел и ключ поля (`_field_key`, "" — поле без имени) — для `Tab.field_values`, текст — для `matches`."""

    step: int
    node: Any
    label: str
    text: str


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
        thresholds: Thresholds | None = None,
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
        self.thresholds = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
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
        self._vanished: dict[int, int] = {}  # шаг → сколько раз его текст пропал из поля (после ввода и позже)
        self._invariants: list[Invariant] = []  # тексты шагов, закрытых кодом, которые должны оставаться в полях
        self._uncertain = 0  # неуверенных действий подряд (уверенность ниже thresholds.min_action_confidence)
        self._done_look_used = False  # режим цели: неуверенный DONE уже получил второй взгляд
        # «Нет изменений после NO_PROGRESS_STEPS действий»: ожидание перед blocked уже было (снова — после действия,
        # сменившего страницу, или пока в полёте запросы, начатые этими действиями)
        self._stall_look_used = False
        self._loading_waited: int | None = None  # якорь, чьи запросы уже ждали перед вопросом Jev (`_await_loading`)
        # Режим цели: запись истории исполненного TYPE_TEXT по `Step.index` (её инвариант — там же): текст пропал —
        # запись убирается из истории для Jev; тексты, пропавшие из полей, — для повторного ввода без текстовой модели;
        # пропажи по (ключ поля, текст)
        self._typed_entries: dict[int, dict[str, Any]] = {}
        self._retype: list[Invariant] = []
        self._typed_vanished: dict[tuple[str, str], int] = {}
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
            tab.deadline, tab.cancel = self._deadline, self.cancel  # и для ожиданий после действий
            # Предохранитель одного ожидания — из порогов этого прогона. `Tab.fuse_s` вводит пакет «события»
            # (feat/waits-events); до слияния в контракте его нет — отсюда ignore.
            tab.fuse_s = self.thresholds.wait_fuse_s
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
        """Пока в снимке нет элементов для действия (экран загрузки, логотип), Jev не зовём: ждём следующего изменения
        страницы (`Tab.await_change`, не опрос) и переснимаем, не дольше `EMPTY_PAGE_WAIT_S` и дедлайна; отмена — сразу.
        После первого вызова Jev (прокрутили за контролы, форма после Submit — «Thanks» без элементов) страница после
        действия уже дождалась готовности: одно ожидание следующего изменения (событие; нет — предохранитель) и свежий
        снимок, без потолка по времени. Шаги и `model_calls` не растут, время — `wait_ms`. Элементов нет и тогда — Jev
        решает по пустой странице (DONE/WAIT законны)."""
        tab = self._require_tab()
        page = self._page
        if page is None or interactive(page):
            self._empty_since = None
            return
        if self._jev_calls:
            self._check_cancel()
            self._remaining()
            log.debug("Нет элементов для действия посреди работы — жду изменения страницы")
            tab.await_change()
            self._page = tab.observe()  # StalePage — наружу, в _tick: переснимет
            return
        ceiling = EMPTY_PAGE_WAIT_S
        if self._empty_since is None:
            self._empty_since = time.monotonic()
            log.info("Нет элементов для действия, жду до %g с (%s)", ceiling, url_host(page.get("url") or self.url))
        until = min(self._empty_since + ceiling, self._deadline)
        while not interactive(page) and time.monotonic() < until:
            self._check_cancel()
            tab.await_change()
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

    def _advance(self, how: str, *, confirmed: bool) -> None:
        """Текущий шаг сценария выполнен: счётчики, второй шанс снова доступен; последний шаг — `done`. `confirmed` —
        выполнение увидел Jev: тексты прошлых шагов использованы, их инварианты сняты. INFO — номер, причина и число
        действий, без `do` и текста."""
        total = len(self.scenario or ())
        log.info("шаг %d/%d выполнен (%s, действий %d)", self._scenario_no, total, how, self._step_actions)
        if confirmed:
            self._invariants.clear()
        self._scenario_done += 1
        self._step_actions = 0
        self._step_history = len(self._history)
        self._second_chance_used = self._stall_look_used = False
        self._checking, self._check_waits, self._uncertain = False, 0, 0
        if self._scenario_done >= total:
            raise _Stop("done")
        self._scenario_no += 1

    def _invariants_hold(self, invariants: list[Invariant]) -> bool:
        """Напечатанные тексты ещё в полях: один `Tab.field_values` (по узлу, иначе по подписи), `kept` — поле есть и
        не пусто (другое непустое значение — сайт его отформатировал). Пропал — сценарий: откат к самому раннему такому
        шагу (`_roll_back`); режим цели: ввод отброшен (`_roll_back_typed`); итог False. Документ сменяется —
        `StalePage` наружу."""
        values = self._require_tab().field_values([{"node": i.node, "label": i.label} for i in invariants])
        if not isinstance(values, list) or len(values) != len(invariants):
            raise StalePage("Document is navigating")
        broken = [i for i, value in zip(invariants, values, strict=True) if not kept(value)]
        if not broken:
            return True
        if self.scenario is None:
            self._roll_back_typed(broken)
        else:
            self._roll_back(min(broken, key=lambda i: i.step))
        return False

    def _roll_back_typed(self, broken: list[Invariant]) -> None:
        """Режим цели: напечатанный текст пропал из поля до использования (сайт пересоздал поле). Решение отброшено,
        запись этого TYPE_TEXT убрана из истории для Jev — он напечатает снова, тем же текстом (`_retype`, без текстовой
        модели). Пропажа — в счёт (ключ поля, текст) вместе с пропажей сразу после ввода; больше `RETYPE_LIMIT` —
        `blocked`."""
        gone = [self._typed_entries.pop(i.step) for i in broken if i.step in self._typed_entries]
        self._history = [h for h in self._history if not any(h is entry for entry in gone)]
        self._invariants = [i for i in self._invariants if i not in broken]
        self._second_chance_used = self._done_look_used = self._stall_look_used = False
        self._uncertain = 0
        for typed in broken:
            count = self._typed_vanish(typed)
            label = short_label(typed.label)
            log.info("текст пропал из поля %r — ввод отброшен, Jev напечатает снова (%d-й раз)", label, count)

    def _typed_vanish(self, typed: Invariant) -> int:
        """Режим цели: текст `typed` пропал из поля (сразу после ввода или позже). Запомнить его для повторного ввода в
        то же поле; пропаж того же текста в том же поле больше `RETYPE_LIMIT` — `blocked`. Итог — сколько раз пропал."""
        self._retype = [r for r in self._retype if not _same_entry(r, typed)] + [typed]
        key = (typed.label, typed.text)
        self._typed_vanished[key] = count = self._typed_vanished.get(key, 0) + 1
        if count > RETYPE_LIMIT:
            raise _Stop("blocked", f"typed text does not stay in the field {short_label(typed.label)!r}")
        return count

    def _typed_goal(self, action: dict[str, Any], text: str, new_page: dict[str, Any], step: Step) -> None:
        """Режим цели, TYPE_TEXT исполнен: поле показывает текст — он инвариант, пока его не использовали (клик не по
        полю, новый ввод в то же поле): перед каждым решением и перед `done` — `_invariants_hold`. Не показывает —
        пометка `TEXT_VANISHED` для Jev (решает он), текст — для повторного ввода, пропажа — в счёт."""
        field = self._shown(new_page, action, text)
        if field is not None:
            key = _field_key(field if field.get("label") else action)  # без имени — "": найдётся при любой роли
            self._invariants.append(Invariant(step.index, field.get("node"), key, text))
            self._typed_entries[step.index] = self._history[-1]
            return
        log.info("step %d: текста нет в поле после ввода — пометка в истории", step.index)
        self._history[-1]["note"] = TEXT_VANISHED
        self._typed_vanish(Invariant(step.index, action.get("node"), _field_key(action), text))

    def _retyped(self, action: dict[str, Any], page: dict[str, Any]) -> str | None:
        """Режим цели: текст, пропавший из этого поля (`_typed_vanish`), — ввести снова его же, без текстовой модели."""
        return next((r.text for r in reversed(self._retype) if _same_field(r, action, page)), None)

    def _anchor(self) -> int | None:
        """Эпоха действия, с которого считаются «запросы действий»: последнего действия (или ожидания), после которого
        страница изменилась; изменений не было — первого действия; действий не было — None."""
        anchor = next((h for h in reversed(self._history) if h.get("page_changed") is not False), None)
        if anchor is None and self._history:
            anchor = self._history[0]
        since = None if anchor is None else anchor.get("epoch")
        return since if type(since) is int else None

    def _in_flight(self) -> int:
        """Запросов вкладки в полёте, начатых действиями с последней смены страницы (`_anchor`) — последним действием,
        после которого страница изменилась, и всеми следующими; фоновые тоже (`Tab.in_flight`). Трасса delay 3000:
        после ввода в поиск Jev кликает по полю (клик запросов не начинает) — ответ поиска всё ещё в пути."""
        return _count(self._require_tab().in_flight(self._anchor()))

    def _await_loading(self) -> None:
        """Страница с незаконченной работой действия не выглядит готовой: запросы, начатые действиями с последней
        смены страницы, пережили ожидание после действия (его предохранитель), — перед вопросом Jev ещё одно ожидание
        изменения (`Tab.await_change`: ответ пришёл — будит сразу) и свежий снимок. Одно на якорь (`_anchor`, действие,
        сменившее страницу): long-poll держит не больше одного лишнего ожидания. Не шаг и не запись истории — это
        ожидание готовности после того же действия; время — `wait_ms`. Отмена и дедлайн — до ожидания."""
        anchor = self._anchor()
        if anchor is None or anchor == self._loading_waited:
            return
        loading = self._in_flight()
        if not loading:
            return
        self._loading_waited = anchor
        self._check_cancel()
        self._remaining()
        tab = self._require_tab()
        log.info("страница ещё загружается (запросов действий в полёте: %d) — жду изменения", loading)
        tab.await_change()
        self._page = tab.observe()  # StalePage — наружу, в _tick: переснимет, второй раз ждать не будем

    def _stalled(self, page: dict[str, Any]) -> None:
        """`NO_PROGRESS_STEPS` действий подряд без изменения страницы. Перед `blocked` — ожидание изменения и свежий
        снимок, как второй шанс перед BLOCKED (`_look_again`, решение — в следующем тике): один раз, пока действие не
        сменит страницу, и снова — пока в полёте запросы, начатые действиями с последней смены страницы (`_in_flight`,
        фоновые тоже: предохранитель их пережил, ответ ещё придёт — действия шли по недогруженной странице). Запись в
        истории разрывает серию: следующая проверка — не раньше ещё `NO_PROGRESS_STEPS` действий без изменений (их
        ограничивают `max_steps`, бюджет решений и `STEP_ACTIONS_LIMIT`)."""
        in_flight = self._in_flight()
        if self._stall_look_used and not in_flight:
            raise _Stop("blocked", f"No page change after {NO_PROGRESS_STEPS} consecutive actions")
        self._stall_look_used = True
        loading = f", {in_flight} requests of these actions in flight" if in_flight else ""
        self._look_again(page, f"no page change after {NO_PROGRESS_STEPS} actions{loading}")

    def _roll_back(self, broken: Invariant) -> None:
        """Текст шага j пропал из поля (сайт пересоздал поле): текущим снова становится шаг j, шаги после него — не
        выполнены, их инварианты сняты; пометка в истории, решение Jev — под шаг j. Пропажа — в общий счёт шага j
        (`_vanished`); больше `RETYPE_LIMIT` — `blocked`."""
        j, was, total = broken.step, self._scenario_no, len(self.scenario or ())
        self._vanished[j] = count = self._vanished.get(j, 0) + 1
        self._scenario_no, self._scenario_done = j, j - 1
        self._invariants = [i for i in self._invariants if i.step < j]
        self._step_actions = 0
        self._step_history = len(self._history)
        self._second_chance_used = self._stall_look_used = False
        self._checking, self._check_waits, self._uncertain = False, 0, 0
        if self._history:
            self._history[-1]["note"] = INVARIANT_BROKEN.format(step=j)
        log.info("шаг %d/%d: текст шага %d пропал — возвращаюсь к шагу %d (%d-й раз)", was, total, j, j, count)
        if count > RETYPE_LIMIT:
            raise _Stop("blocked", f"{self._step_name()}: typed text does not stay in the field")

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
            self._second_chance_used = self._done_look_used = self._stall_look_used = False
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

    def _unconfirmed(self, decision: Decision, page: dict[str, Any]) -> NoReturn:
        """Проверка не подтвердила шаг (снова DONE без подтверждения или второй WAIT) или неуверенный DONE цели
        повторился после второго взгляда: `unconfirmed`, url и title — свежие, финальный кадр снимет `_finish`. Вердикт
        — только по свежей странице: устарела — `StalePage` (переснимок и ещё один вопрос)."""
        if not self._require_tab().fresh(page):
            raise StalePage("Page changed since the decision. Choose again.")
        if self.scenario is None:
            log.info("DONE не подтверждён и после второго взгляда (conf %.2f)", decision.confidence)
            self._refresh_page()
            raise _Stop("unconfirmed", GOAL_UNCONFIRMED)
        total = len(self.scenario)
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
        """Текст шага напечатан: шаг закрыт, только если поле его показывает (`typed_fields`, `matches`). Не видно —
        ещё одно наблюдение после изменения страницы (`await_change`: поле могло появиться позже или новым узлом).
        Нет и тогда — пометка `TEXT_VANISHED` в истории, решает Jev по этому снимку (повторный ввод допустим); пропаж
        больше `RETYPE_LIMIT` (общий счёт с откатами) — `blocked`."""
        field = self._shown(new_page, action, text)
        if field is None:
            tab = self._require_tab()
            log.info("шаг %d: текста не видно в поле — жду изменения и смотрю ещё раз", self._scenario_no)
            tab.await_change()
            self._page = new_page = tab.observe()  # StalePage — наружу: шаг не закрыт, решит Jev по переснимку
            field = self._shown(new_page, action, text)
        if field is not None:
            self._close_typed(field, action, text)
            return
        self._vanished[self._scenario_no] = count = self._vanished.get(self._scenario_no, 0) + 1
        self._history[-1]["note"] = TEXT_VANISHED
        total = len(self.scenario or ())
        log.info("шаг %d/%d: текста нет в поле после ввода (%d-й раз)", self._scenario_no, total, count)
        if count > RETYPE_LIMIT:
            raise _Stop("blocked", f"{self._step_name()}: typed text does not stay in the field")

    @staticmethod
    def _shown(page: dict[str, Any], action: dict[str, Any], text: str) -> dict[str, Any] | None:
        """Поле на снимке, которое показывает напечатанный текст; None — не видно."""
        return next((f for f in typed_fields(page, action) if matches(f.get("value"), text)), None)

    def _close_typed(self, field: dict[str, Any], action: dict[str, Any], text: str) -> None:
        """Шаг закрыт кодом: его текст — инвариант. Последний шаг — перед `done` дождаться готовности страницы (сеть
        после ввода: подсказки, автосохранение) и проверить все живые инварианты вместе с этим; пропал — откат."""
        key = _field_key(field if field.get("label") else action)  # без имени — "": найдётся при любой роли
        invariant = Invariant(self._scenario_no, field.get("node"), key, text)
        if self._scenario_no == len(self.scenario or ()):
            self._require_tab().await_ready()
            if not self._invariants_hold([*self._invariants, invariant]):
                return
        self._invariants.append(invariant)
        self._advance("text typed", confirmed=False)

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
        """Новое решение Jev без действия: ожидание следующего изменения страницы (`Tab.await_change`), переснимок,
        вопрос — в следующем тике. Второй шанс перед BLOCKED, проверка шага после неподтверждённого DONE и WAIT в ней,
        неуверенное действие, неуверенный DONE цели. Не шаг: `Step` не создаётся, в истории — честная запись ожидания
        (Jev видит, что ждали). Отмена и дедлайн — до ожидания; время — `wait_ms`."""
        tab = self._require_tab()
        self._check_cancel()
        self._remaining()
        log.info("%s: жду изменения страницы и спрашиваю ещё раз (%s)", why, url_host(page["url"]))
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
                "epoch": tab.action_epoch,  # эпоха последнего действия (`_in_flight`): ожидание запросов не начинает
            }
        )
        tab.await_change()
        new_page = tab.observe()  # StalePage — наружу, в _tick: переснимет, запись ожидания остаётся
        self._page = new_page
        self._history[-1].update(page_changed=new_page["fingerprint"] != page["fingerprint"], url=new_page["url"])

    def _predict(self) -> None:
        tab = self._require_tab()
        if self._page is None or not tab.fresh(self._page):
            self._page = tab.observe()
        self._await_interactive()
        self._await_loading()
        self._decision = None
        # Сценарий: + решение на шаг — на границе шагов Jev спрашивается ещё раз (§0.6), устаревшие не съедят лимит.
        budget = 2 * self.config.max_steps + len(self.scenario or ())
        if self._jev_calls >= budget:
            raise _Stop("step_limit", f"Model-call budget exhausted ({self._jev_calls} decisions)")
        self._check_cancel()
        # Факт для Jev (любой режим): запросы, начатые действиями с последней смены страницы, ещё в полёте.
        loading = self._in_flight()
        if loading:
            log.debug("страница загружается: запросов действий в полёте — %d", loading)
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
                loading=loading,
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
        limits = self.thresholds
        uncertain = decision.operation in ACTING_OPERATIONS and decision.confidence < limits.min_action_confidence
        if not uncertain:
            self._uncertain = 0
        # Шаг с `text` DONE не закрывает: его результат проверяет код (поле показывает текст, `_check_typed`).
        done_counts = current is not None and current.text is None
        if current is not None and (
            p_done >= limits.step_done_min_p
            or (selected == "DONE" and done_counts and p_done >= limits.done_step_done_min_p)
        ):
            # Шаг выполнен по свежей странице; действие этого решения выбиралось под этот шаг — не исполняется,
            # следующий тик спросит Jev уже под новый шаг (docs/plan-scenarios.md §0.6).
            if not tab.fresh(page):
                raise StalePage("Page changed since the decision. Choose again.")
            self._advance(f"{'DONE, ' if selected == 'DONE' else ''}p={p_done:.2f}", confirmed=True)
            return
        # Тексты шагов, закрытых кодом, ещё в полях (§6.1)? Пропал — откат к шагу, это решение (выбрано по странице без
        # текста: Send пропал, BLOCKED) отброшено.
        if self._invariants and not self._invariants_hold(self._invariants):
            return
        if self._checking:
            # Режим проверки — без действий: снова DONE без подтверждения или второй WAIT — `unconfirmed`, BLOCKED — как
            # обычно (второй шанс, потом blocked). Другое сюда не доходит (`build_request(verify=True)`), но и не
            # исполняется.
            if selected == "DONE":
                self._unconfirmed(decision, page)
            if decision.operation == "WAIT":
                self._check_waits += 1
                if self._check_waits >= CHECK_WAITS:
                    self._unconfirmed(decision, page)
                self._look_again(page, "WAIT при проверке шага")
                return
            if selected != "BLOCKED":
                raise ValueError(f"Decision {decision.operation} is not allowed while checking the step")
        elif current is not None and selected == "DONE" and current.text is not None:
            # DONE на шаге с текстом до ввода (Википедия 26.09: p=0.36 до ввода запроса) — не повод для проверки и
            # `unconfirmed`: текст не напечатан, это код знает сам. Решение отброшено, новый вопрос по свежей странице.
            self._look_again(page, "DONE до ввода текста шага")
            return
        elif current is not None and selected == "DONE":
            # DONE без подтверждения step_done — шаг не выполнен, решение отброшено без мутации. Режим проверки:
            # ожидание изменения, свежий снимок и вопрос только с DONE/WAIT/BLOCKED (живая проверка 26.09: иначе Jev
            # искал, что ещё сделать, и кликнул мимо).
            total = len(self.scenario or ())
            log.info("шаг %d/%d: DONE без подтверждения (p=%.2f) — проверяю", self._scenario_no, total, p_done)
            self._checking = True
            self._look_again(page, "DONE")
            return
        if selected in {"DONE", "BLOCKED"}:
            if not tab.fresh(page):
                raise StalePage("Page changed since the decision. Choose again.")
            if selected == "DONE":
                if current is None and decision.confidence < limits.done_min_confidence:
                    # Режим цели, неуверенный DONE (трасса search-remount: 0.32 и 0.35, сообщение не отправлено) —
                    # один второй взгляд; снова такой же — `unconfirmed` (docs/plan-waits.md §0.5).
                    if self._done_look_used:
                        self._unconfirmed(decision, page)
                    self._done_look_used = True
                    self._look_again(page, f"DONE conf {decision.confidence:.2f}")
                    return
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
            elif current is None and (retype := self._retyped(action, page)) is not None:
                text = retype  # режим цели: текст пропал из этого поля — тот же, без текстовой модели
            else:
                # Сценарий: история без напечатанных текстов — тексты шагов текстовой модели не уходят.
                context = field_context(self._text_goal(), action, page, self._history, texts=current is None)
                if self._pending_text and self._pending_text[0] == context:
                    _, text, _helper = self._pending_text
                else:
                    text, helper = self._field_text(context)
                    self._pending_text = (context, text, helper)
        # Tab.act проверяет свежесть прямо перед вводом, в том числе после генерации текста. WAIT — не ввод: только
        # свежесть, ожидание изменения — ниже (`await_change`).
        self._check_cancel()
        if action["kind"] == "wait":
            if not tab.fresh(page):
                raise StalePage("Page changed since the decision. Choose again.")
        else:
            tab.act(action, page, text=text)
        self._pending_text = None
        if action["kind"] == "fill":
            self._retype = [r for r in self._retype if not _same_field(r, action, page)]  # ввели снова — забыть
        if self._invariants and action["kind"] == "click" and not _is_field(page, action):
            # Клик не по полю использует напечатанное (Send, строка чата, Submit): дальше поле может опустеть законно.
            log.debug(
                "клик %r: инварианты шагов %s сняты", short_label(action["label"]), [i.step for i in self._invariants]
            )
            self._invariants.clear()
            self._typed_entries.clear()
        elif self._invariants and action["kind"] == "fill":
            # Ввод в то же поле заменяет прежний текст (selectAll + insertText) — так задумано сценарием.
            self._invariants = [i for i in self._invariants if not _same_field(i, action, page)]
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
                # эпоха действия (`Tab.action_epoch`; у WAIT — прежняя): чьи запросы ещё в полёте (`_in_flight`)
                "epoch": tab.action_epoch,
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
            if action["kind"] == "wait":
                ready = tab.await_change()
            else:
                ready = tab.await_ready(action)
                tab.after_input = None  # ждали сами: observe() второй раз не ждёт
            step.wait_reason, step.pending_requests = wait_fields(ready)
            try:
                new_page = tab.observe()
            except StalePage:
                if not typed_step_text:
                    raise
                # Снимок после ввода текста шага устарел: ещё один после следующего изменения; снова — наружу, шаг не
                # закрыт, решит Jev по следующему снимку.
                log.info("Снимок после ввода устарел: жду изменения и снимаю ещё раз (%s)", url_host(page["url"]))
                tab.await_change()
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
        elif current is None and action["kind"] == "fill" and text is not None:
            self._typed_goal(action, text, new_page, step)  # режим цели: инвариант или пометка для Jev
        # Только действия текущего шага (в режиме цели — все): закрытые шаги с неизменной страницей не копятся.
        repeated = self._history[max(self._step_history, len(self._history) - NO_PROGRESS_STEPS) :]
        if len(repeated) == NO_PROGRESS_STEPS and all(
            h["page_changed"] is False and h["kind"] != "wait" for h in repeated
        ):
            self._stalled(new_page)
