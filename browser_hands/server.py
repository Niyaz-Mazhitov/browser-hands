"""MCP-сервер browser-hands: один инструмент `browse` поверх ядра (Chrome по CDP + Jev).

Ядро (`browser_hands.chrome`, `.model`, `.agent`) импортируется лениво — при первом `browse`, не при старте:
сервер отвечает на `initialize`/`tools/list`, даже если ядро не готово, а `browse` вернёт понятную ошибку.
"""

import functools
import importlib
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from browser_hands.config import (
    MAX_STEPS_LIMIT,
    TIMEOUT_LIMIT_S,
    BrowserConfig,
    ModelConfig,
    RunConfig,
    Settings,
    apply_overrides,
)
from browser_hands.logging import get_logger, quiet_http_loggers
from browser_hands.scenario import KEYS, MAX_DO, MAX_STEPS, MAX_TEXT, ScenarioError, ScenarioStep, parse_steps
from browser_hands.types import AgentLike, ChromeLike, RunResult, Timing

INSTRUCTIONS = "Управляет Chrome: browse(url, goal|steps) сам кликает и печатает; результат проверяй по скриншоту."
BROWSE_DESCRIPTION = (
    "Выполняет goal или steps в Chrome: клики, ввод, выбор. В attach — в открытой вкладке, если задан только сайт или "
    "ровно эта страница, иначе в новой фоновой. DONE не гарантирует успех — смотри скриншот."
)
GOAL_DESCRIPTION = "что сделать и когда остановиться; необязателен при steps"
STEPS_DESCRIPTION = (
    "сценарий для многошаговых задач и точных текстов: список {do, text?}; do — что сделать, по-английски; "
    "text печатается дословно (иначе текст подберёт модель по goal)"
)
DO_DESCRIPTION = "что сделать на этом шаге, по-английски"
TEXT_DESCRIPTION = "что напечатать дословно"
NO_TASK = "нужен goal или steps"
URL_DESCRIPTION = (
    "стартовая страница: http(s) или about:blank; в открытой вкладке (без перехода), если задан только сайт или ровно "
    "эта страница"
)
NEW_TAB_DESCRIPTION = "attach: своя фоновая вкладка, даже если сайт открыт у пользователя"
CLOSE_WAIT_S = 3.0  # close() ждёт идущий browse не дольше, потом закрывает всё равно
CANCELLED = "cancelled"  # RunResult.error отменённого прогона (так же отвечает ядро)
ALLOWED_SCHEMES = frozenset({"http", "https"})  # и ещё ровно about:blank
# схема в начале адреса; «localhost:8000», «example.com:8080/x» — это хост с портом, а не схема
_SCHEME = re.compile(r"([a-z][a-z0-9+.-]*):(?!\d+(?:[/?#]|$))", re.IGNORECASE)

log = get_logger("server")


class ClientsLike(Protocol):
    """То, что обвязке нужно от `browser_hands.model.ModelClients`."""

    def warmup(self) -> None: ...

    def close(self) -> None: ...


ChromeFactory = Callable[[BrowserConfig], ChromeLike]
ClientsFactory = Callable[[ModelConfig], ClientsLike]
# (chrome, clients, url, goal, run, *, screenshot_quality, screenshot_scale, cancel[, steps]); steps — только сценарию
AgentFactory = Callable[..., AgentLike]


class CoreNotReady(RuntimeError):
    """Модуля ядра (Пакет 1) нет: ветка ещё не влита."""


class ServiceClosed(RuntimeError):
    """browse после close()."""


class UnsupportedUrl(ValueError):
    """Адрес не http/https/about:blank: вкладку не открываем."""


def import_core(name: str) -> ModuleType:
    """`browser_hands.<name>` или CoreNotReady, если нет именно этого модуля (прочие ImportError — как есть)."""
    module = f"browser_hands.{name}"
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == module:
            raise CoreNotReady(f"модуль {module} ещё не готов") from None
        raise


def default_chrome_factory(config: BrowserConfig) -> ChromeLike:
    return import_core("chrome").Chrome(config)


def default_clients_factory(config: ModelConfig) -> ClientsLike:
    return import_core("model").ModelClients(config)


def default_agent_factory(
    chrome: ChromeLike,
    clients: ClientsLike,
    url: str,
    goal: str,
    run: RunConfig,
    *,
    screenshot_quality: int,
    screenshot_scale: float,
    cancel: threading.Event,
    steps: list[ScenarioStep] | None = None,
) -> AgentLike:
    agent_cls = import_core("agent").Agent
    scenario = {} if steps is None else {"steps": steps}  # режим цели зовёт ядро как раньше
    return agent_cls(
        chrome,
        clients,
        url,
        goal,
        run,
        screenshot_quality=screenshot_quality,
        screenshot_scale=screenshot_scale,
        cancel=cancel,
        **scenario,
    )


class BrowseService:
    """Один Chrome и один ModelClients на процесс, создаются при первом прогоне; прогоны — строго по одному.

    `browse()` не бросает исключений: любая ошибка (нет ключа, нет ядра, Chrome недоступен) → RunResult `failed`.
    У каждого прогона своё событие отмены: выставлено — агент останавливается между шагами, а вызов, ещё ждущий
    лок, не начинается вовсе (`failed: cancelled`).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        chrome_factory: ChromeFactory | None = None,
        clients_factory: ClientsFactory | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self._settings = settings
        self._chrome_factory = chrome_factory or default_chrome_factory
        self._clients_factory = clients_factory or default_clients_factory
        self._agent_factory = agent_factory or default_agent_factory
        # модули ядра, нужные фабрикам по умолчанию; порядок = порядок сообщений «ещё не готов»
        self._core_modules = [
            name
            for name, factory in (("chrome", chrome_factory), ("model", clients_factory), ("agent", agent_factory))
            if factory is None
        ]
        self._lock = threading.Lock()  # второй browse ждёт первого (docs/decisions.md, решение 6)
        self._active: set[threading.Event] = set()  # события отмены идущего и ждущих в очереди прогонов
        self._chrome: ChromeLike | None = None
        self._clients: ClientsLike | None = None
        self._warmup_started = False
        self._closed = False

    @property
    def settings(self) -> Settings:
        return self._settings

    def browse(
        self,
        url: str,
        goal: str = "",
        *,
        steps: list[ScenarioStep] | None = None,
        max_steps: int | None = None,
        timeout_s: float | None = None,
        keep_open: bool | None = None,
        new_tab: bool | None = None,
        cancel: threading.Event | None = None,
    ) -> RunResult:
        """Прогон по `goal`, по сценарию `steps` или по обоим (тогда `goal` — общая цель для Jev)."""
        started = time.monotonic()
        try:
            url = normalize_url(url)
        except UnsupportedUrl as exc:  # до лока и Chrome: вкладка не открывается
            log.warning("browse: %s", exc)
            return failed_result(url.strip(), str(exc), started)
        if steps is not None:
            try:
                steps = parse_steps(steps)  # вызывающий уже разобрал; здесь — защита от прямого вызова
            except ScenarioError as exc:
                log.warning("browse: %s", exc)
                return failed_result(url, str(exc), started)
        elif not goal.strip():
            log.warning("browse: %s", NO_TASK)
            return failed_result(url, NO_TASK, started)
        cancel = threading.Event() if cancel is None else cancel
        self._active.add(cancel)
        try:
            with self._lock:
                if cancel.is_set():  # отменён, пока ждал в очереди: агента не создаём
                    log.info("browse: отменён до начала")
                    return failed_result(url, CANCELLED, started)
                try:
                    result = self._run(
                        url,
                        goal,
                        cancel,
                        started,
                        steps=steps,
                        max_steps=max_steps,
                        timeout_s=timeout_s,
                        keep_open=keep_open,
                        new_tab=new_tab,
                    )
                except Exception as exc:  # ответ инструмента вместо исключения
                    return failed_result(url, describe_error(exc), started)
        finally:
            self._active.discard(cancel)
        log.info(
            "browse: %s, шагов %d, %d мс, cost %s", result.status, len(result.steps), result.elapsed_ms, result.cost
        )
        return result

    def _run(
        self,
        url: str,
        goal: str,
        cancel: threading.Event,
        started: float,
        *,
        steps: list[ScenarioStep] | None,
        max_steps: int | None,
        timeout_s: float | None,
        keep_open: bool | None,
        new_tab: bool | None,
    ) -> RunResult:
        """Тело browse под локом; ошибки — исключениями (их превращает в `failed` вызывающий)."""
        if self._closed:
            raise ServiceClosed("сервер закрывается")
        run = apply_overrides(
            self._settings, max_steps=max_steps, timeout_s=timeout_s, keep_open=keep_open, new_tab=new_tab
        ).run
        if steps is not None and len(steps) > run.max_steps:  # на шаг сценария — хотя бы одно действие
            raise ScenarioError(f"steps: шагов сценария {len(steps)}, а max_steps={run.max_steps}")
        for name in self._core_modules:
            import_core(name)
        self._settings.validate()
        clients = self._ensure_clients()
        chrome = self._ensure_chrome()
        if cancel.is_set():  # отменили, пока подключались (attach ждёт «Разрешить» до 60 с)
            return failed_result(url, CANCELLED, started)
        scenario = {} if steps is None else {"steps": steps}  # фабрики режима цели зовутся как раньше
        agent = self._agent_factory(
            chrome,
            clients,
            url,
            goal,
            run,
            screenshot_quality=self._settings.browser.screenshot_quality,
            screenshot_scale=self._settings.browser.screenshot_scale,
            cancel=cancel,
            **scenario,
        )
        if steps is None:
            log.info("browse: %s (шагов ≤ %d, %.0f с)", url_host(url), run.max_steps, run.timeout_s)
        else:
            log.info(
                "browse: %s (сценарий из %d, шагов ≤ %d, %.0f с)",
                url_host(url),
                len(steps),
                run.max_steps,
                run.timeout_s,
            )
        log.debug("browse url: %s", url)  # полный адрес, цель и сценарий — только в DEBUG
        log.debug("goal: %s", goal)
        for n, step in enumerate(steps or (), start=1):
            log.debug("step %d: %s%s", n, step.do, "" if step.text is None else f" — {step.text!r}")
        return agent.run()

    def close(self, *, wait_s: float = CLOSE_WAIT_S) -> None:
        """Отменить прогоны, закрыть Chrome (attach — только соединение) и HTTP-клиенты. Идемпотентно.

        Идущий browse (он остановится между шагами) ждём не дольше `wait_s`, потом закрываем всё равно.
        """
        self._closed = True
        self._cancel_all()
        acquired = self._lock.acquire(timeout=wait_s) if wait_s > 0 else self._lock.acquire(blocking=False)
        if not acquired:
            log.warning("close: browse ещё идёт — закрываю соединения, не дожидаясь")
        try:
            chrome, clients = self._chrome, self._clients
            self._chrome = self._clients = None
            for name, resource in (("chrome", chrome), ("model clients", clients)):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception:  # закрываем всё, что можно
                    log.warning("close: %s не закрылся", name, exc_info=True)
        finally:
            if acquired:
                self._lock.release()

    def abort(self) -> None:
        """Для обработчика сигнала: лок не ждёт — сразу отменяет прогоны и закрывает Chrome и клиентов."""
        self.close(wait_s=0)

    def _cancel_all(self) -> None:
        for event in tuple(self._active):  # копия: потоки browse меняют множество (tuple(set) — под GIL целиком)
            event.set()

    def _ensure_clients(self) -> ClientsLike:
        if self._clients is None:
            self._clients = self._clients_factory(self._settings.models)
        if not self._warmup_started:  # TLS/HTTP2 к моделям — параллельно с подключением к Chrome, один раз
            self._warmup_started = True
            threading.Thread(target=_warmup, args=(self._clients,), name="browser-hands-warmup", daemon=True).start()
        return self._clients

    def _ensure_chrome(self) -> ChromeLike:
        if self._chrome is None:
            self._chrome = self._chrome_factory(self._settings.browser)
            browser = self._settings.browser
            if browser.mode == "attach" and not browser.ws_url:
                log.info("подключаюсь к Chrome (attach): если Chrome спросит — нажмите «Разрешить»")
        self._chrome.connect()  # идемпотентно; мёртвое соединение переподключает
        return self._chrome


def _warmup(clients: ClientsLike) -> None:
    try:
        clients.warmup()
    except Exception:  # прогрев необязателен
        log.warning("warmup не удался", exc_info=True)


def normalize_url(url: str) -> str:
    """http(s) — как есть; `about:blank`; голый хост (`example.com`, `localhost:8000`) → `https://…`.

    Прочие схемы (file:, chrome:, chrome-extension:, data:, javascript:, view-source:, …) → UnsupportedUrl.
    """
    url = url.strip()
    if not url:
        raise UnsupportedUrl("пустой url")
    if url.lower() == "about:blank":
        return "about:blank"
    match = _SCHEME.match(url)
    if match is None:
        return "https://" + url
    scheme = match.group(1).lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsupportedUrl(f"схема {scheme}: не поддерживается — только http://, https:// и about:blank")
    return url


def url_host(url: str) -> str:
    """Хост для INFO-лога (путь и query могут нести личные данные); about:blank — как есть."""
    if url == "about:blank":
        return url
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:  # кривой адрес вроде «https://[::1»
        return "?"


def describe_error(exc: Exception) -> str:
    """Свои ошибки (конфиг, ядро: ChromeUnavailable и т. п.) — текстом; чужие — с именем класса и трейсом в лог."""
    if type(exc).__module__.startswith("browser_hands."):  # ConfigError, CoreNotReady, ChromeUnavailable, …
        log.warning("browse: %s", exc)
        return str(exc) or type(exc).__name__
    log.exception("browse: неожиданная ошибка")
    return f"{type(exc).__name__}: {exc}"


def failed_result(url: str, error: str, started: float) -> RunResult:
    return RunResult(
        status="failed",
        steps=[],
        url=url,
        title="",
        screenshot_jpeg=None,
        cost=None,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        timing=Timing(),
        model_calls=0,
        error=error,
    )


def _s(ms: int) -> str:
    return f"{ms / 1000:.1f}"


def tab_line(result: RunResult) -> str | None:
    """Где работал агент: вкладка пользователя (с хостом) или своя; None — до вкладки не дошло, строки нет."""
    if result.tab == "user":  # keep_open к ней не относится: её не закрываем никогда
        return f"вкладка: твоя ({url_host(result.url)})"
    if result.tab == "new":
        return "вкладка: новая, оставлена открытой" if result.tab_kept else "вкладка: новая"
    return None


def scenario_lines(result: RunResult, steps: Sequence[ScenarioStep] | None = None) -> list[str]:
    """Итог сценария: «сценарий: k из M выполнено» и, если не `done`, «остановился на шаге k+1 из M: <do>»; при
    `unconfirmed` — «шаг k+1 из M не подтверждён (вероятно, выполнен — проверь скриншот): <do>».

    Режим цели (`scenario_total` None) — пусто. `do` берётся из `steps`, если это тот же сценарий (M шагов).
    """
    total, done = result.scenario_total, result.scenario_done or 0
    if total is None:
        return []
    lines = [f"сценарий: {done} из {total} выполнено"]
    if result.status != "done" and done < total:
        if result.status == "unconfirmed":
            stopped = f"шаг {done + 1} из {total} не подтверждён (вероятно, выполнен — проверь скриншот)"
        else:
            stopped = f"остановился на шаге {done + 1} из {total}"
        if steps is not None and len(steps) == total:
            stopped += f": {steps[done].do}"
        lines.append(stopped)
    return lines


def format_result(result: RunResult, *, verbose: bool = False, steps: Sequence[ScenarioStep] | None = None) -> str:
    """Текст ответа: статус, итог сценария, url, вкладка, шаги, стоимость, замеры.

    `verbose` — замеры и уверенность по шагам (CLI); `steps` — сценарий вызова (для «остановился на шаге N: <do>»).
    Режим цели — текст как до сценариев.
    """
    lines = [f"status: {result.status}"]
    if result.error:
        lines.append(f"error: {result.error}")
    lines.extend(scenario_lines(result, steps))
    lines.append(f"url: {result.url}")
    if result.title:
        lines.append(f"title: {result.title}")
    tab = tab_line(result)
    if tab is not None:
        lines.append(tab)
    lines.append(f"steps: {len(result.steps)}")
    for step in result.steps:
        line = f"{step.index}. {step.operation} {step.target}"
        if step.text is not None:
            line += f' — "{step.text}"'
        if step.scenario_step is not None:
            line += f" [шаг {step.scenario_step}]"
        if verbose:
            t = step.timing
            total = t.model_ms + t.text_ms + t.browser_ms + t.wait_ms
            changed = {True: "да", False: "нет", None: "?"}[step.page_changed]
            line += f"  [{_s(total)} с, conf {step.confidence:.2f}, изменилась: {changed}, {step.url}]"
        lines.append(line)
    cost = f"${result.cost:.4f}" if result.cost is not None else "n/a"
    lines.append(f"cost: {cost} (model calls: {result.model_calls})")
    t = result.timing
    lines.append(
        f"elapsed {_s(result.elapsed_ms)}s "
        f"(model {_s(t.model_ms)} / text {_s(t.text_ms)} / browser {_s(t.browser_ms)} / wait {_s(t.wait_ms)})"
    )
    if result.screenshot_jpeg is None:
        lines.append("screenshot: нет")
    return "\n".join(lines)


def to_content(result: RunResult, steps: Sequence[ScenarioStep] | None = None) -> list[str | Image]:
    """Ответ `browse`: текст + JPEG той же вкладки (FastMCP превращает в TextContent + ImageContent)."""
    content: list[str | Image] = [format_result(result, steps=steps)]
    if result.screenshot_jpeg:
        content.append(Image(data=result.screenshot_jpeg, format="jpeg"))
    return content


class StepIn(BaseModel):
    # Шаг сценария во входе MCP: только `do` и `text`, лимиты — из `scenario.py`, лишние ключи — ошибка.
    # Комментарий, а не docstring: docstring ушёл бы в схему `tools/list` лишними токенами.

    model_config = ConfigDict(extra="forbid")

    # do — без пробелов по краям (как parse_steps): «   » — пустой; text — как есть, пробелы — часть текста
    do: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_DO)] = Field(
        description=DO_DESCRIPTION
    )
    text: str | None = Field(None, min_length=1, max_length=MAX_TEXT, description=TEXT_DESCRIPTION)


def describe_validation(exc: ValidationError) -> str:
    """Ошибки pydantic во входе `browse` → одна строка без ссылок и без значений (в `text` может быть личное).

    Шаги нумеруются с 1, как в `parse_steps`: «step 2 url: Extra inputs are not permitted (allowed: do, text)».
    """
    parts = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = list(error["loc"])
        where = str(loc.pop(0)) if loc else "arguments"
        if where == "steps" and loc and isinstance(index := loc[0], int):
            where = f"step {index + 1}"
            loc.pop(0)
        where = " ".join([where, *map(str, loc)])
        message = error["msg"]
        if error["type"] == "extra_forbidden" and where.startswith("step "):
            message += f" (allowed: {', '.join(sorted(KEYS))})"
        parts.append(f"{where}: {message}")
    return "неверные аргументы browse — " + "; ".join(parts)


class BrowseMCP(FastMCP):
    """FastMCP, у которого ошибка разбора аргументов — короткий текст, а не дамп pydantic со ссылками."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                raise ToolError(describe_validation(exc.__cause__)) from None
            raise


def build_server(service: BrowseService) -> FastMCP:
    """FastMCP с инструментом `browse` поверх готового сервиса (закрывает сервис вызывающий — `cli serve`)."""
    run = service.settings.run
    debug = get_logger().getEffectiveLevel() <= logging.DEBUG
    server = BrowseMCP("browser-hands", instructions=INSTRUCTIONS, log_level="DEBUG" if debug else "WARNING")
    quiet_http_loggers()  # FastMCP мог поставить DEBUG корневому логгеру: hpack напечатал бы ключ

    @server.tool(description=BROWSE_DESCRIPTION, structured_output=False)
    async def browse(
        url: Annotated[str, Field(min_length=1, description=URL_DESCRIPTION)],
        goal: Annotated[str, Field(description=GOAL_DESCRIPTION)] = "",
        steps: Annotated[
            Annotated[list[StepIn], Field(min_length=1, max_length=MAX_STEPS)] | None,
            Field(description=STEPS_DESCRIPTION),
        ] = None,
        max_steps: Annotated[int, Field(ge=1, le=MAX_STEPS_LIMIT)] = run.max_steps,
        timeout_seconds: Annotated[float, Field(gt=0, le=TIMEOUT_LIMIT_S)] = run.timeout_s,
        keep_open: Annotated[bool, Field(description="не закрывать свою вкладку после прогона")] = run.keep_open,
        new_tab: Annotated[bool, Field(description=NEW_TAB_DESCRIPTION)] = run.new_tab,
    ) -> list[str | Image]:
        # форму и лимиты проверил pydantic; parse_steps (ошибка → `failed` текстом) — в service.browse
        scenario = None if steps is None else [ScenarioStep(step.do, step.text) for step in steps]
        cancel = threading.Event()  # своё на каждый вызов: отмена (Esc) касается только этого прогона
        call = functools.partial(
            service.browse,
            url,
            goal,
            steps=scenario,
            max_steps=max_steps,
            timeout_s=timeout_seconds,
            keep_open=keep_open,
            new_tab=new_tab,
            cancel=cancel,
        )
        try:
            # блокирующее ядро — в потоке: event loop отвечает на ping/cancel, второй вызов ждёт лок в своём потоке
            result = await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            cancel.set()  # поток брошен: агент встанет между шагами, а из очереди прогон уже не начнётся
            raise
        return to_content(result, scenario)

    return server


def create_server(
    settings: Settings,
    *,
    chrome_factory: ChromeFactory | None = None,
    agent_factory: AgentFactory | None = None,
    clients_factory: ClientsFactory | None = None,
) -> FastMCP:
    """Сервер с собственным BrowseService (для тестов и встраивания; `cli serve` строит сервис сам, чтобы закрыть)."""
    service = BrowseService(
        settings, chrome_factory=chrome_factory, clients_factory=clients_factory, agent_factory=agent_factory
    )
    return build_server(service)
