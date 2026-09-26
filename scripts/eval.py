"""Стенд надёжности: локальные страницы `tests/fixtures/`, N прогонов агента на задачу, проверка результата по DOM.

    uv run --env-file .env python scripts/eval.py --runs 10 --label before
    uv run --env-file .env python scripts/eval.py --mode scenario --tasks all --runs 10 --label scenario
    uv run --frozen python scripts/eval.py --fixtures-only        # бесплатно: настоящий Chrome, без моделей
    uv run --env-file .env python scripts/eval.py --sweep axes --runs 5 --mode goal,scenario --max-cost 0.25
    uv run --frozen python scripts/eval.py --fixtures-only --sweep axes   # бесплатно: каждая ячейка скриптом

`--mode goal` (по умолчанию) — агент получает цель задачи, `--mode scenario` — её сценарий `{do, text?}` без цели
(docs/plan-scenarios.md §5.3). Страница сама отчитывается стенду (`POST /report`, только то, что видно в DOM, и
разобранные параметры), поэтому `verified` не зависит от статуса агента. Исключение — `wiki` (внешняя сеть, только в
`--tasks all` или по имени): проверка по итоговому `url`. Chrome всегда свой: launch + headless + временный профиль
(выбора браузера нет; `--mode` — режим прогона). Итог — строка на прогон, сводка по задаче (verified/done k/N, медиана
и p95 elapsed/wait, вызовы Jev и текстовой модели, стоимость, причины неудач) и JSONL в `traces/eval-<ts>.jsonl`
(строки прогонов + `summary`). В строке — история отчётов страницы и запросов к `/api/*` с `t_ms` от того же старта,
что `t_ms` решений Jev. Выход 0, только если все прогоны verified. Платно, кроме `--fixtures-only`; в pytest не
запускается (там — `FakeCore`).

Задержки страниц (docs/plan-waits.md §0.6): `net=1` (по умолчанию) — поиск и подтверждение Send — настоящие запросы к
этому серверу (`/api/search`, `/api/send`, пауза на сервере), `net=0` — таймеры; пересоздание поля (`remount`, список)
— всегда таймер. `--sweep axes|grid` (§0.7, §7.3) — развёртка параметров app.html: ячейка = параметры в `start_url` и в
строке (`cell`, `params`, `axes`), прогоны по кругу (круг — все ячейки × режимы), `--mode goal,scenario` — оба режима
вперемешку под одним `--max-cost`. Отчёт по осям — `scripts/sweep_report.py`; выход развёртки 0, если сделаны все
прогоны (успехи — данные, не ошибка).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import itertools
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from browser_hands import agent as agent_module
from browser_hands.agent import interactive
from browser_hands.browser import StalePage, Tab
from browser_hands.chrome import Chrome
from browser_hands.cli import result_to_json
from browser_hands.config import ConfigError, Settings, apply_overrides
from browser_hands.scenario import ScenarioStep, parse_steps
from browser_hands.server import BrowseService
from browser_hands.types import RunResult

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:  # `bench` рядом: при загрузке из тестов каталог скрипта не в sys.path
    sys.path.insert(0, str(SCRIPTS))

from bench import fresh_profile, percentile  # noqa: E402

ROOT = SCRIPTS.parent
FIXTURES = ROOT / "tests" / "fixtures"
CHAT = "Рабочий"
MESSAGE = "это я через агента, проверка 👋"
CHAT_GOAL = f'Open the chat "{CHAT}" and send the message: {MESSAGE}'
FORM_GOAL = (
    "Fill the form: name Ivan Petrov, email ivan@example.test, country Kazakhstan, agree to the terms, and submit."
)
FORM_EXPECTED: dict[str, Any] = {
    "name": "Ivan Petrov",
    "email": "ivan@example.test",
    "country": "Kazakhstan",
    "agree": True,
}
SEARCH_LABEL = "Search or start a new chat"
SEARCH_DELAY_MS = 700  # задержка результатов поиска в задачах (в app.html по умолчанию — столько же)
SEND_DELAY_MS = 300  # app.html без sendstatus: подтверждение Send (`senddelay`)
SEARCH_QUERY = "Раб"  # --fixtures-only: совпадает с «Рабочий» и отвлекающим «Работа»
# search-remount: поле сообщения пересоздаётся через REMOUNT_MS после открытия чата (текст пропадает, как в WhatsApp
# 26.09), после Send — «Sending…» ещё SEND_STATUS_MS, у исходящих кнопки «HH:MM Sent» — есть куда кликнуть лишний раз
REMOUNT_MS = 1600  # 900 агент переживал (решение устаревало); 1600 воспроизводит WhatsApp 26.09 (0/5 до правок)
SEND_STATUS_MS = 1500
COMPOSER_TO = f"Type a message to {CHAT}"  # подпись поля при remount, как в WhatsApp
STATUS_SENT = re.compile(r"\d\d:\d\d Sent")
STATUS_SENDING = re.compile(r"\d\d:\d\d Sending…")
API_MAX_DELAY_MS = 30_000  # пауза `/api/*` не дольше: опечатка в параметре не вешает поток сервера
WIKI_URL = "https://en.wikipedia.org/wiki/Main_Page"
WIKI_GOAL = "Find and open the Wikipedia article about Gödel's incompleteness theorems."  # как bench/live_wikipedia
WIKI_QUERY = "Gödel's incompleteness theorems"
WIKI_ARTICLE = "/wiki/Gödel's_incompleteness_theorems"  # в url после unquote: …/wiki/G%C3%B6del%27s_incompleteness_…

# Сценарии задач (`--mode scenario`): do — по-английски (Jev на нём точнее), text — дословно (plan-scenarios §0.5)
CHAT_SCENARIO: tuple[dict[str, str], ...] = (
    {"do": "Type the chat name into the chat search box", "text": CHAT},
    {"do": f"Open the chat named «{CHAT}» in the chat list (done when its header shows «{CHAT}»)"},
    {"do": "Type the message into the message box of the open chat", "text": MESSAGE},
    {"do": "Send the message (done when it appears in the chat)"},
)
FORM_SCENARIO: tuple[dict[str, str], ...] = (
    {"do": "Fill the Name field", "text": FORM_EXPECTED["name"]},
    {"do": "Fill the Email field", "text": FORM_EXPECTED["email"]},
    {"do": f"Select {FORM_EXPECTED['country']} in the Country dropdown"},
    {"do": "Tick the checkbox «I agree to the terms»"},
    {"do": "Submit the form (done when a thank-you message is shown)"},
)
WIKI_SCENARIO: tuple[dict[str, str], ...] = (
    {"do": "Type the query into the Wikipedia search box", "text": WIKI_QUERY},
    {"do": "Open the matching article from the suggestions or search results (done when the article page is open)"},
)
MODES = ("goal", "scenario")

REPORT_QUIET_S = 0.3  # после browse: отчёт не менялся столько — последний
REPORT_WAIT_S = 2.0  # и не ждём дольше
REPORT_MISSING_S = 0.2  # отчётов нет вовсе (страница не открывалась) — не ждём
MAX_REPORT_BYTES = 64 * 1024
SEEN_TEXT = 400  # что видел Jev: символов видимого текста в JSONL
SEEN_CONTROLS = 30  # и элементов для действия


# --- проверка по отчёту страницы -----------------------------------------------------------------------------------


def chat_problem(state: Mapping[str, Any] | None) -> str | None:
    """None — в открытом чате «Рабочий» ровно одно новое сообщение, дословно MESSAGE; иначе причина."""
    if not state:
        return "нет отчёта со страницы"
    if not state.get("ready"):
        return "интерфейс не загрузился"
    open_chat, query = state.get("openChat"), state.get("query") or ""
    sent = list(state.get("sent") or [])
    if open_chat != CHAT:
        where = f"открыт «{open_chat}»" if open_chat else "чат не открыт"
        return where + (f", в поиске «{query}»" if query else ", поиск пуст")
    if not sent:
        return "чат открыт, сообщение не отправлено"
    if len(sent) > 1:
        return f"отправлено {len(sent)} сообщений"
    if sent[0] != MESSAGE:
        return f"текст отличается: {sent[0]!r}"
    return None


def form_problem(state: Mapping[str, Any] | None) -> str | None:
    """None — форма отправлена и все четыре поля совпали с FORM_EXPECTED; иначе причина."""
    if not state:
        return "нет отчёта со страницы"
    submitted = state.get("submitted")
    if not isinstance(submitted, Mapping):
        fields = state.get("fields") or {}
        wrong = [key for key, value in FORM_EXPECTED.items() if fields.get(key) != value]
        return "форма не отправлена" + (f" (не заполнено: {', '.join(wrong)})" if wrong else "")
    wrong = [f"{key}={submitted.get(key)!r}" for key, value in FORM_EXPECTED.items() if submitted.get(key) != value]
    return f"отправлено не то: {', '.join(wrong)}" if wrong else None


def wiki_problem(result: RunResult) -> str | None:
    """None — открыта статья о теоремах Гёделя (url в любом кодировании); иначе «открыт <url>»."""
    return None if WIKI_ARTICLE in unquote(result.url) else f"открыт {result.url}"


@dataclass(frozen=True)
class Task:
    name: str
    page: str  # страница в tests/fixtures/ с параметрами; `run=<id>` добавляется на прогон; у `url`-задач — ""
    goal: str
    problem: Callable[[Mapping[str, Any] | None], str | None] | None  # проверка по отчёту страницы
    scenario: tuple[Mapping[str, str], ...] = ()  # `--mode scenario`: шаги {do, text?}
    url: str | None = None  # абсолютный адрес внешнего сайта (сеть): отчёта нет, проверка — verify_result
    verify_result: Callable[[RunResult], str | None] | None = None

    @property
    def network(self) -> bool:
        return self.url is not None

    def check(self, state: Mapping[str, Any] | None) -> bool:
        return self.problem is not None and self.problem(state) is None

    def steps(self) -> list[ScenarioStep]:
        return parse_steps(list(self.scenario))

    def verify(self, result: RunResult, report: Mapping[str, Any] | None) -> str | None:
        """None — задача выполнена; иначе причина. Локальные — по отчёту страницы, внешние — по RunResult."""
        if self.verify_result is not None:
            return self.verify_result(result)
        assert self.problem is not None
        return self.problem(report)


TASKS: dict[str, Task] = {
    task.name: task
    for task in (
        Task("search", f"app.html?delay={SEARCH_DELAY_MS}&net=1", CHAT_GOAL, chat_problem, CHAT_SCENARIO),
        Task(
            "search-spinner",
            f"app.html?delay={SEARCH_DELAY_MS}&spinner=1&net=1",
            CHAT_GOAL,
            chat_problem,
            CHAT_SCENARIO,
        ),
        Task("boot", f"app.html?boot=4000&delay={SEARCH_DELAY_MS}&net=1", CHAT_GOAL, chat_problem, CHAT_SCENARIO),
        Task(
            "search-remount",
            f"app.html?delay={SEARCH_DELAY_MS}&remount={REMOUNT_MS}&sendstatus={SEND_STATUS_MS}&net=1",
            CHAT_GOAL,
            chat_problem,
            CHAT_SCENARIO,
        ),
        Task("form", "form.html", FORM_GOAL, form_problem, FORM_SCENARIO),
        Task("wiki", "", WIKI_GOAL, None, WIKI_SCENARIO, url=WIKI_URL, verify_result=wiki_problem),
    )
}
DEFAULT_TASKS = [name for name, task in TASKS.items() if not task.network]  # без сети; `--tasks all` — все


def with_params(page: str, **params: str) -> str:
    """`page` с добавленными/заменёнными параметрами query (порядок прежних сохраняется; запятая списка — как есть)."""
    parts = urlsplit(page)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit(parts._replace(query=urlencode(query, safe=",")))


def page_for(
    task: Task,
    *,
    delay: int | None,
    remount: int | str | None = None,
    sendstatus: int | None = None,
    net: int | None = None,
) -> str:
    """Страница задачи; `--delay` / `--remount` / `--sendstatus` / `--net` меняют параметр только там, где он задан
    (`remount` — мс или список «800,1600»)."""
    query = dict(parse_qsl(urlsplit(task.page).query))
    values = {"delay": delay, "remount": remount, "sendstatus": sendstatus, "net": net}
    changes = {key: str(value) for key, value in values.items() if value is not None and key in query}
    return with_params(task.page, **changes) if changes else task.page


@dataclass(frozen=True)
class PageParams:
    """Параметры app.html из адреса страницы — с теми же значениями по умолчанию и разбором, что в самой странице:
    тайминги скриптовых проверок и сверка с `params` из её отчёта."""

    delay: int = SEARCH_DELAY_MS
    remount: tuple[int, ...] = ()
    sendstatus: int = 0
    senddelay: int = SEND_DELAY_MS
    net: bool = True
    spinner: bool = False
    boot: int = 0

    @classmethod
    def of(cls, page: str) -> PageParams:
        query = dict(parse_qsl(urlsplit(page).query, keep_blank_values=True))

        def num(key: str, fallback: int) -> int:
            try:
                value = float(query[key])
            except (KeyError, ValueError):
                return fallback
            return int(value) if value >= 0 and value != float("inf") else fallback

        remount = []
        for part in query.get("remount", "").split(","):
            with contextlib.suppress(ValueError):
                if float(part or 0) > 0 and float(part) != float("inf"):
                    remount.append(int(float(part)))
        return cls(
            delay=num("delay", SEARCH_DELAY_MS),
            remount=tuple(sorted(remount)),
            sendstatus=num("sendstatus", 0),
            senddelay=num("senddelay", SEND_DELAY_MS),
            net=num("net", 1) > 0,
            spinner=query.get("spinner") == "1",
            boot=num("boot", 0),
        )

    def as_report(self) -> dict[str, Any]:
        """Как `params` в отчёте app.html (`PARAMS` в странице)."""
        return {
            "delay": self.delay,
            "remount": list(self.remount),
            "sendstatus": self.sendstatus,
            "senddelay": self.senddelay,
            "net": int(self.net),
            "spinner": int(self.spinner),
            "boot": self.boot,
        }


def params_problem(page: str, report: Mapping[str, Any] | None) -> str | None:
    """None — страница поняла параметры так же, как стенд (или отчёта с `params` нет: не app.html / не открылась);
    иначе — чем расходятся (ячейка развёртки была бы не той, что в строке)."""
    got = report.get("params") if report else None
    if not isinstance(got, Mapping):
        return None
    wrong = [
        f"{key}={got.get(key)!r}, ждали {value!r}"
        for key, value in PageParams.of(page).as_report().items()
        if got.get(key) != value
    ]
    return f"стенд: страница поняла параметры иначе ({'; '.join(wrong)})" if wrong else None


# --- развёртка (§7.3) ----------------------------------------------------------------------------------------------

SWEEP_KINDS = ("axes", "grid")
SWEEP_TASKS = ["search-remount"]  # развёртка по умолчанию — чат, похожий на WhatsApp
SWEEP_DELAYS = "0,500,1500,3000"  # × net 0,1 — 8 ячеек; вместе с осями remount и sendstatus — 18
SWEEP_REMOUNTS = "0:3000:500"
SWEEP_SENDSTATUS = "0,1500"
SWEEP_NETS = "0,1"
SWEEP_BASE: dict[str, int] = {"delay": SEARCH_DELAY_MS, "remount": 0, "sendstatus": 0, "net": 1}


def parse_values(text: str, *, name: str = "значения") -> list[int]:
    """«a:b:step» — от a до b включительно с шагом step; «v1,v2,…» — список. Целые ≥ 0, по возрастанию, без повторов.
    ValueError — с причиной по-русски."""
    text = text.strip()
    try:
        if ":" in text:
            start, stop, step = (int(part) for part in text.split(":"))
            if step <= 0 or stop < start:
                raise ValueError
            values = list(range(start, stop + 1, step))
        else:
            values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise ValueError(f"{name}: «{text}» — ожидается a:b:step (a ≤ b, step > 0) или список через запятую") from None
    if not values or min(values) < 0:
        raise ValueError(f"{name}: «{text}» — нужны целые ≥ 0")
    return sorted(set(values))


@dataclass(frozen=True)
class Cell:
    """Ячейка развёртки: параметры app.html и оси, на которых она лежит (`(ось, значение)`; одна ячейка может быть
    на двух осях — тогда её прогоны считаются в обеих)."""

    params: Mapping[str, int]
    axes: tuple[tuple[str, int], ...]

    @property
    def id(self) -> str:
        p = self.params
        return f"d{p['delay']}-r{p['remount']}-s{p['sendstatus']}-n{p['net']}"

    def query(self) -> dict[str, str]:
        return {key: str(value) for key, value in self.params.items()}


def sweep_cells(
    kind: str,
    *,
    delays: Sequence[int],
    remounts: Sequence[int],
    sendstatuses: Sequence[int],
    nets: Sequence[int],
) -> list[Cell]:
    """Ячейки развёртки (docs/plan-waits.md §0.7, §7.3).

    `axes`: delay-ось при remount 0, sendstatus 0 — отдельно для каждого `net` («delay net=1», «delay net=0»);
    remount-ось при delay 700, sendstatus SEND_STATUS_MS, net 1 (как `search-remount`); sendstatus × remount {0,
    REMOUNT_MS} при delay 700, net 1. Совпавшие ячейки объединяются (оси — обе). `grid` — полное произведение,
    ось ячейки — каждый её параметр.
    """
    found: dict[str, tuple[dict[str, int], list[tuple[str, int]]]] = {}

    def add(axis: str, value: int, **params: int) -> None:
        full = {**SWEEP_BASE, **params}
        cell = Cell(full, ())
        found.setdefault(cell.id, (full, []))[1].append((axis, value))

    if kind == "grid":
        for delay, remount, sendstatus, net in itertools.product(delays, remounts, sendstatuses, nets):
            params = {"delay": delay, "remount": remount, "sendstatus": sendstatus, "net": net}
            for axis, value in params.items():
                add(axis, value, **params)
    elif kind == "axes":
        for net in sorted(nets, reverse=True):
            for delay in delays:
                add(f"delay net={net}", delay, delay=delay, remount=0, sendstatus=0, net=net)
        for remount in remounts:
            add("remount", remount, remount=remount, sendstatus=SEND_STATUS_MS)
        for remount in (0, REMOUNT_MS):
            for sendstatus in sendstatuses:
                add(f"sendstatus remount={remount}", sendstatus, remount=remount, sendstatus=sendstatus)
    else:
        raise ValueError(f"развёртка: {kind!r}, есть {', '.join(SWEEP_KINDS)}")
    return [Cell(params, tuple(axes)) for params, axes in found.values()]


def sweep_of(args: argparse.Namespace) -> list[Cell]:
    return sweep_cells(
        args.sweep, delays=args.delays, remounts=args.remounts, sendstatuses=args.sendstatuses, nets=args.nets
    )


@dataclass(frozen=True)
class Job:
    """Что прогнать в одном круге: задача, режим, страница стенда с параметрами (без `run`; внешний сайт — "")."""

    task: Task
    mode: str
    page: str
    cell: Cell | None = None


def plan_jobs(tasks: Sequence[Task], args: argparse.Namespace) -> list[Job]:
    """Круг прогонов. Без `--sweep` — задачи по порядку со своими страницами (и `--delay`/`--remount`/…); с
    `--sweep` — ячейка × режим × задача (параметры ячейки поверх страницы задачи)."""
    if args.sweep:
        return [
            Job(task, mode, with_params(task.page, **cell.query()), cell)
            for cell in sweep_of(args)
            for mode in args.modes
            for task in tasks
        ]
    return [Job(task, args.mode, "" if task.network else page_for(task, **overrides(args))) for task in tasks]


def overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "delay": args.delay,
        "remount": args.remount,
        "sendstatus": getattr(args, "sendstatus", None),
        "net": getattr(args, "net", None),
    }


# --- сервер стенда -------------------------------------------------------------------------------------------------


class ReportStore:
    """По `run`: последний отчёт страницы (отчёт с меньшим `seq` пришёл позже — не затирает), время изменения, история
    отчётов `(monotonic, отчёт)` и журнал запросов страницы к `/api/*`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reports: dict[str, dict[str, Any]] = {}
        self._changed: dict[str, float] = {}
        self._history: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        self._calls: dict[str, list[dict[str, Any]]] = {}

    def put(self, report: dict[str, Any]) -> None:
        run = report.get("run")
        if not isinstance(run, str) or not run:
            return
        with self._lock:
            old = self._reports.get(run)
            if old is not None and _seq(old) > _seq(report):
                return
            now = time.monotonic()
            self._reports[run] = report
            self._changed[run] = now
            self._history.setdefault(run, []).append((now, report))

    def get(self, run: str) -> dict[str, Any] | None:
        with self._lock:
            return self._reports.get(run)

    def changed(self, run: str) -> float | None:
        with self._lock:
            return self._changed.get(run)

    def history(self, run: str) -> list[tuple[float, dict[str, Any]]]:
        with self._lock:
            return list(self._history.get(run, ()))

    def call(self, run: str, entry: dict[str, Any]) -> None:
        if not run:
            return
        with self._lock:
            self._calls.setdefault(run, []).append(entry)

    def calls(self, run: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._calls.get(run, ()))


def _seq(report: Mapping[str, Any]) -> int:
    seq = report.get("seq")
    return seq if isinstance(seq, int) else -1


def api_delay(value: str | None) -> int:
    """Пауза `/api/*` из параметра `delay` (мс): не число — 0, не больше API_MAX_DELAY_MS."""
    try:
        ms = int(float(value or 0))
    except (ValueError, OverflowError):
        return 0
    return min(max(ms, 0), API_MAX_DELAY_MS)


class _Handler(SimpleHTTPRequestHandler):
    """GET — файлы `tests/fixtures/`; POST /report — отчёт страницы в `ReportStore`; GET /api/search и POST /api/send —
    «сервер приложения» app.html (`net=1`): пауза `delay` мс здесь, ответ JSON, запись в журнал `run`. Без логов."""

    def __init__(self, *args: Any, store: ReportStore, **kwargs: Any) -> None:
        self.store = store  # до super().__init__: он сразу обрабатывает запрос
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/api/search":
            self._api("search", parts.query)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        if parts.path not in ("/report", "/api/send"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REPORT_BYTES:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        if parts.path == "/api/send":
            self._api("send", parts.query)
            return
        try:
            report = json.loads(body or b"null")
        except ValueError:
            self.send_error(400)
            return
        if isinstance(report, dict):
            self.store.put(report)
        self.send_response(204)
        self.end_headers()

    def _api(self, name: str, query: str) -> None:
        params = dict(parse_qsl(query))
        delay = api_delay(params.get("delay"))
        started = time.monotonic()
        time.sleep(delay / 1000)  # поток запроса: ThreadingHTTPServer, другие запросы не ждут
        entry = {"api": name, "q": params.get("q"), "delay_ms": delay, "t0": started, "t1": time.monotonic()}
        self.store.call(params.get("run", ""), entry)  # до ответа: страница увидит ответ после записи
        body = json.dumps({"ok": True, "api": name, "q": params.get("q")}, ensure_ascii=False).encode()
        with contextlib.suppress(OSError):  # страница прервала запрос (новый ввод, закрытая вкладка)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


class FixtureServer:
    """`ThreadingHTTPServer` на 127.0.0.1:<свободный порт> в своём потоке; контекстный менеджер."""

    def __init__(self, root: Path = FIXTURES) -> None:
        self.store = ReportStore()
        handler = functools.partial(_Handler, directory=str(root), store=self.store)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="eval-fixtures", daemon=True)

    @property
    def base(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}/"

    def url(self, page: str) -> str:
        return self.base + page.lstrip("/")

    def __enter__(self) -> FixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)

    def report(self, run: str) -> dict[str, Any] | None:
        return self.store.get(run)

    def history(self, run: str) -> list[tuple[float, dict[str, Any]]]:
        return self.store.history(run)

    def calls(self, run: str, api: str | None = None) -> list[dict[str, Any]]:
        return [call for call in self.store.calls(run) if api is None or call["api"] == api]

    def wait_report(
        self,
        run: str,
        *,
        quiet: float | None = None,
        timeout: float | None = None,
        missing: float | None = None,
    ) -> dict[str, Any] | None:
        """Отчёт, который не менялся `quiet` с (последний keepalive-POST успел дойти); не дольше `timeout`.
        Отчётов нет `missing` с — страница не открывалась, None."""
        quiet = REPORT_QUIET_S if quiet is None else quiet
        timeout = REPORT_WAIT_S if timeout is None else timeout
        missing = REPORT_MISSING_S if missing is None else missing
        started = time.monotonic()
        while True:
            now = time.monotonic()
            changed = self.store.changed(run)
            if changed is None and now - started >= missing:
                return None
            if changed is not None and now - changed >= quiet:
                break
            if now - started >= timeout:
                break
            time.sleep(0.02)
        return self.store.get(run)


def report_timeline(history: Sequence[tuple[float, Mapping[str, Any]]], started: float) -> list[dict[str, Any]]:
    """История отчётов для строки прогона: `t_ms` от `started` (как у решений Jev) и поля отчёта без служебных
    (`run`, `seq`, `params` — они в `start_url`/`params` строки)."""
    return [
        {"t_ms": _since(started, at), **{k: v for k, v in report.items() if k not in ("run", "seq", "params")}}
        for at, report in history
    ]


def call_timeline(calls: Sequence[Mapping[str, Any]], started: float) -> list[dict[str, Any]]:
    """Запросы страницы к `/api/*`: когда пришёл (`t_ms` от `started`), пауза сервера и сколько отвечали (`ms`)."""
    return [
        {
            "api": call["api"],
            "q": call.get("q"),
            "delay_ms": call["delay_ms"],
            "t_ms": _since(started, call["t0"]),
            "ms": round((call["t1"] - call["t0"]) * 1000),
        }
        for call in calls
    ]


# --- что видел Jev -------------------------------------------------------------------------------------------------


def _control(action: Mapping[str, Any]) -> str:
    text = f"{action.get('kind')}:{str(action.get('label') or '')[:48]}"
    value = action.get("value")
    if action.get("kind") == "fill" and value:
        text += f"={str(value)[:30]!r}"
    return text


def seen(state: Mapping[str, Any]) -> dict[str, Any]:
    controls = [a for a in state.get("actions") or () if a.get("kind") in ("fill", "click", "select")]
    return {
        "url": state.get("url"),
        "text": str(state.get("text") or "")[:SEEN_TEXT],
        "controls": [_control(a) for a in controls[:SEEN_CONTROLS]],
        "controls_total": len(controls),
    }


def chosen(state: Mapping[str, Any], decision: Any) -> dict[str, Any]:
    target = next((a for a in state.get("actions") or () if a.get("id") == decision.choice), None)
    step_done = getattr(decision, "step_done", None)  # режим сценария: p(«шаг выполнен»); ядро без него — None
    return {
        "op": decision.operation,
        "target": target.get("label") if target is not None else None,
        "confidence": round(float(decision.confidence), 3),
        "step_done": None if step_done is None else round(float(step_done), 3),
    }


def step_number(step: Any) -> dict[str, Any]:
    """Номер шага сценария, под который спрашивали Jev (`StepContext.number`); режим цели — пусто."""
    number = getattr(step, "number", None)
    return {} if number is None else {"scenario_step": number}


def loading_of(value: Any) -> dict[str, Any]:
    """Факт «страница ещё загружается», который видел Jev (`choose(loading=N)`: запросов последнего действия в
    полёте); 0 или ядро без него — пусто."""
    return {"loading": value} if type(value) is int and value > 0 else {}


@contextlib.contextmanager
def record_decisions(sink: list[dict[str, Any]], *, started: float | None = None) -> Iterator[None]:
    """На время прогона оборачивает `browser_hands.agent.choose`: на каждое решение Jev — что было на странице
    (видимый текст, элементы, `loading` — запросов последнего действия в полёте), под какой шаг сценария, что выбрано
    и `t_ms` — когда пришёл ответ (мс от `started`,
    по умолчанию — от входа в контекст, ≈ начало прогона). Ядро не меняется; нет такой функции — ничего не пишет."""
    original = getattr(agent_module, "choose", None)
    if original is None:
        yield
        return
    started = time.monotonic() if started is None else started

    def recording(clients: Any, state: dict[str, Any], goal: str, history: Any, *args: Any, **kwargs: Any) -> Any:
        where = {**seen(state), **step_number(kwargs.get("step")), **loading_of(kwargs.get("loading"))}
        try:
            decision = original(clients, state, goal, history, *args, **kwargs)
        except Exception as exc:
            sink.append({**where, "error": f"{type(exc).__name__}: {exc}", "t_ms": _since(started)})
            raise
        sink.append({**where, **chosen(state, decision), "t_ms": _since(started)})
        return decision

    setattr(agent_module, "choose", recording)
    try:
        yield
    finally:
        setattr(agent_module, "choose", original)


def _since(started: float, at: float | None = None) -> int:
    return round(((time.monotonic() if at is None else at) - started) * 1000)


def trail(decisions: Sequence[Mapping[str, Any]]) -> str:
    """Решения Jev одной строкой: `TYPE_TEXT «Search…» → CLICK «Clear search» → BLOCKED`."""
    parts = []
    for d in decisions:
        if "error" in d:
            parts.append(f"ошибка ({d['error'][:60]})")
            continue
        target = d.get("target")
        part = f"{d.get('op')} «{str(target)[:32]}»" if target else str(d.get("op"))
        if d.get("step_done") is not None:  # режим сценария: шаг и p(«шаг выполнен»)
            part += f" (шаг {d.get('scenario_step', '?')}, p={d['step_done']:.2f})"
        if d.get("loading"):  # Jev видел «страница ещё загружается»
            part += f" [грузится {d['loading']}]"
        parts.append(part)
    return " → ".join(parts)


# --- прогон и сводка -----------------------------------------------------------------------------------------------


@dataclass
class Evaluation:
    rows: list[dict[str, Any]] = field(default_factory=list)
    stopped: str | None = None  # почему прогонов меньше заказанного (бюджет)

    @property
    def cost(self) -> float:
        return sum(row["cost"] for row in self.rows if row.get("cost") is not None)


def run_all(
    service: BrowseService,
    server: FixtureServer,
    jobs: Sequence[Job],
    args: argparse.Namespace,
    trace: TextIO,
    *,
    head: str | None,
) -> Evaluation:
    """По кругу: прогон 1 всего круга (`plan_jobs`: задачи; при `--sweep` — ячейки × режимы × задачи), потом 2 и т. д. —
    медленная минута сети не достаётся одной задаче, обрыв по бюджету оставляет ячейки поровну.

    Режим `scenario` — вместо цели сценарий задачи (`browse(url, "", steps=…)`), цель агенту не даётся.
    """
    evaluation = Evaluation()
    sweep = any(job.cell is not None for job in jobs)
    print(table_header(sweep=sweep), flush=True)
    for index in range(1, args.runs + 1):
        for job in jobs:
            if args.max_cost is not None and evaluation.cost >= args.max_cost:
                evaluation.stopped = f"бюджет ${args.max_cost:g} исчерпан (${evaluation.cost:.4f})"
                return evaluation
            task = job.task
            run_id = f"{task.name}-{index}-{uuid.uuid4().hex[:6]}"
            # внешний сайт — как есть, отчёта страницы не будет
            url = task.url if task.url is not None else server.url(with_params(job.page, run=run_id))
            steps = task.steps() if job.mode == "scenario" else None
            decisions: list[dict[str, Any]] = []
            started = time.monotonic()  # общий ноль для t_ms решений, отчётов и запросов страницы
            with record_decisions(decisions, started=started):
                if steps is None:
                    result = service.browse(url, task.goal, max_steps=args.max_steps, timeout_s=args.timeout)
                else:
                    result = service.browse(url, "", steps=steps, max_steps=args.max_steps, timeout_s=args.timeout)
            report = None if task.network else server.wait_report(run_id)
            problem = (None if task.network else params_problem(job.page, report)) or task.verify(result, report)
            row: dict[str, Any] = {
                "task": task.name,
                "mode": job.mode,
                "run": index,
                "run_id": run_id,
                "start_url": url,
                "label": args.label,
                "head": head,
                "verified": problem is None,
                "problem": problem,
                "report": report,
                "decisions": decisions,
                **result_to_json(result, scenario=steps),
                "text_calls": text_calls(result),
            }
            if not task.network:
                row["reports"] = report_timeline(server.history(run_id), started)
                row["api_calls"] = call_timeline(server.calls(run_id), started)
            if job.cell is not None:
                row.update(cell=job.cell.id, params=dict(job.cell.params), axes=[list(a) for a in job.cell.axes])
            evaluation.rows.append(row)
            trace.write(json.dumps(row, ensure_ascii=False) + "\n")
            trace.flush()
            print(format_line(row), flush=True)
            if not row["verified"]:
                why = f"     → {problem}" + (f"; {row['error']}" if row.get("error") else "")
                print(why, flush=True)
                if decisions:
                    print(f"     Jev: {trail(decisions)}", flush=True)
    return evaluation


def text_calls(result: RunResult) -> int | None:
    """Вызовы текстовой модели: `model_calls − jev_calls`. Ядро, что не считает Jev отдельно (`jev_calls` 0 при
    ненулевых `model_calls`: без Jev текст не просят), — None, а не ложное «всё — текст»."""
    if result.jev_calls == 0 and result.model_calls > 0:
        return None
    return result.model_calls - result.jev_calls


def _n(value: Any) -> str:
    return "—" if value is None else str(value)


def scenario_progress(row: Mapping[str, Any]) -> str | None:
    """«2/4» — шагов сценария выполнено из скольких; режим цели — None."""
    total = row.get("scenario_total")
    return None if total is None else f"{row.get('scenario_done') or 0}/{total}"


def table_header(*, sweep: bool = False) -> str:
    head = f"{'cell':<22}{'mode':<9}" if sweep else ""
    return head + (
        f"{'task':<15}{'#':>3}  {'status':<10} {'ok':<4}{'steps':>5}  {'elapsed':>7} {'model':>6} {'text':>6} "
        f"{'browser':>7} {'wait':>6} {'calls':>5} {'jev':>4} {'txt':>4} {'scn':>5}  {'cost $':>7}"
    )


def format_line(row: Mapping[str, Any]) -> str:
    """Строка прогона; у развёртки — с ячейкой и режимом впереди."""
    line = format_row(row)
    return f"{row['cell']:<22}{row['mode']:<9}{line}" if "cell" in row else line


def format_row(row: Mapping[str, Any]) -> str:
    t = row["timing"]
    cost = f"{row['cost']:.4f}" if row.get("cost") is not None else "n/a"
    jev = row["jev_calls"] if row.get("text_calls") is not None else None  # ядро без счёта Jev — «—»
    return (
        f"{row['task']:<15}{row['run']:>3}  {row['status']:<10} {'yes' if row['verified'] else 'no':<4}"
        f"{len(row['steps']):>5}  {row['elapsed_ms']:>7} {t['model_ms']:>6} {t['text_ms']:>6} {t['browser_ms']:>7} "
        f"{t['wait_ms']:>6} {row['model_calls']:>5} {_n(jev):>4} {_n(row.get('text_calls')):>4} "
        f"{_n(scenario_progress(row)):>5}  {cost:>7}"
    )


def _stats(values: Sequence[float]) -> dict[str, float]:
    return {"median": statistics.median(values), "p95": percentile(values, 95)}


def failure_reason(row: Mapping[str, Any]) -> str:
    """«blocked: чат не открыт…»; в режиме сценария — со счётом шагов: «blocked [сценарий 1/4]: …»."""
    progress = scenario_progress(row)
    where = row["status"] if progress is None else f"{row['status']} [сценарий {progress}]"
    return f"{where}: {row['problem']}"


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def summarize_task(name: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    costs = [row["cost"] for row in rows if row.get("cost") is not None]
    failures = Counter(failure_reason(row) for row in rows if not row["verified"])
    counted = [row for row in rows if row.get("text_calls") is not None]  # ядро считает вызовы Jev отдельно
    return {
        "task": name,
        "runs": len(rows),
        "verified": sum(bool(row["verified"]) for row in rows),
        "done": sum(row["status"] == "done" for row in rows),
        "statuses": dict(Counter(row["status"] for row in rows)),
        "elapsed_ms": _stats([row["elapsed_ms"] for row in rows]),
        "wait_ms": _stats([row["timing"]["wait_ms"] for row in rows]),
        "steps_median": statistics.median(len(row["steps"]) for row in rows),
        "model_calls_median": statistics.median(row["model_calls"] for row in rows),
        "jev_calls_median": _median([row["jev_calls"] for row in counted]),
        "text_calls_median": _median([row["text_calls"] for row in counted]),
        "text_calls_total": sum(row["text_calls"] for row in counted) if counted else None,
        "cost_mean": round(sum(costs) / len(costs), 6) if costs else None,
        "cost_total": round(sum(costs), 6) if costs else None,
        "cost_runs": len(costs),
        "failures": dict(failures.most_common()),
    }


def summarize(
    evaluation: Evaluation, tasks: Sequence[Task], *, label: str, head: str | None, mode: str = "goal"
) -> dict[str, Any]:
    per_task = []
    for task in tasks:
        rows = [row for row in evaluation.rows if row["task"] == task.name]
        if rows:
            per_task.append(summarize_task(task.name, rows))
    return {
        "label": label,
        "mode": mode,
        "head": head,
        "runs": len(evaluation.rows),
        "verified": sum(bool(row["verified"]) for row in evaluation.rows),
        "cost_total": round(evaluation.cost, 6),
        "stopped": evaluation.stopped,
        "tasks": per_task,
    }


def summarize_sweep(
    evaluation: Evaluation, jobs: Sequence[Job], *, label: str, head: str | None, kind: str
) -> dict[str, Any]:
    """Сводка развёртки: по ячейке × режиму × задаче — k/N, медиана и p95 elapsed/wait, стоимость, причины неудач."""
    cells = []
    for job in jobs:
        assert job.cell is not None
        rows = [
            row
            for row in evaluation.rows
            if row.get("cell") == job.cell.id and row["mode"] == job.mode and row["task"] == job.task.name
        ]
        if not rows:
            continue
        costs = [row["cost"] for row in rows if row.get("cost") is not None]
        cells.append(
            {
                "cell": job.cell.id,
                "mode": job.mode,
                "task": job.task.name,
                "params": dict(job.cell.params),
                "axes": [list(a) for a in job.cell.axes],
                "runs": len(rows),
                "verified": sum(bool(row["verified"]) for row in rows),
                "elapsed_ms": _stats([row["elapsed_ms"] for row in rows]),
                "wait_ms": _stats([row["timing"]["wait_ms"] for row in rows]),
                "cost_total": round(sum(costs), 6) if costs else None,
                "failures": dict(Counter(failure_reason(row) for row in rows if not row["verified"]).most_common()),
            }
        )
    return {
        "label": label,
        "sweep": kind,
        "modes": sorted({job.mode for job in jobs}),
        "head": head,
        "runs": len(evaluation.rows),
        "verified": sum(bool(row["verified"]) for row in evaluation.rows),
        "cost_total": round(evaluation.cost, 6),
        "stopped": evaluation.stopped,
        "cells": cells,
    }


def format_sweep(summary: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    """Итог развёртки: таблицы по осям (`sweep_report.format_report`) и строка «итого»."""
    from sweep_report import format_report  # рядом, только stdlib

    lines = [format_report([(summary.get("label") or "sweep", list(rows))])]
    lines.append(
        f"итого (развёртка {summary['sweep']}, {', '.join(summary['modes'])}): "
        f"ячеек {len({cell['cell'] for cell in summary['cells']})}, "
        f"verified {summary['verified']}/{summary['runs']}; cost ${summary['cost_total']:.4f}"
    )
    if summary.get("stopped"):
        lines.append(f"остановлено: {summary['stopped']}")
    return "\n".join(lines)


def _calls(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def format_summary(summary: Mapping[str, Any]) -> str:
    lines = [
        f"{'task':<15}{'verified':>9}{'done':>7}   {'elapsed med / p95':>19}   {'wait med / p95':>16}"
        f"   {'jev':>4} {'text':>4}   {'$/run':>7}  {'$ total':>7}"
    ]
    for s in summary["tasks"]:
        e, w = s["elapsed_ms"], s["wait_ms"]
        mean = f"{s['cost_mean']:.4f}" if s["cost_mean"] is not None else "n/a"
        total = f"{s['cost_total']:.4f}" if s["cost_total"] is not None else "n/a"
        lines.append(
            f"{s['task']:<15}{s['verified']:>5}/{s['runs']:<3}{s['done']:>3}/{s['runs']:<3}"
            f"   {e['median']:>8.0f} / {e['p95']:>6.0f}   {w['median']:>6.0f} / {w['p95']:>6.0f}"
            f"   {_calls(s['jev_calls_median']):>4} {_calls(s['text_calls_median']):>4}"
            f"   {mean:>7}  {total:>7}"
        )
        for reason, count in s["failures"].items():
            lines.append(f"    ×{count} {reason}")
    lines.append("jev / text — медианы вызовов Jev и текстовой модели (model_calls − jev_calls) на прогон")
    lines.append(
        f"итого ({summary.get('mode', 'goal')}): verified {summary['verified']}/{summary['runs']}; "
        f"cost ${summary['cost_total']:.4f}"
    )
    if summary.get("stopped"):
        lines.append(f"остановлено: {summary['stopped']}")
    return "\n".join(lines)


def git_head() -> str | None:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def eval_settings(env: Mapping[str, str], profile: str | None, args: argparse.Namespace) -> Settings:
    """Всегда свой Chrome: launch, headless, временный профиль; `BROWSER_HANDS_WS_URL`/`MODE` из env не действуют."""
    return apply_overrides(
        Settings.from_env(env),
        mode="launch",
        ws_url="",
        headless=True,
        data_dir=profile,
        max_steps=getattr(args, "max_steps", None),
        timeout_s=getattr(args, "timeout", None),
    )


# --- --fixtures-only: настоящий Chrome, скриптовые действия, без моделей -------------------------------------------


class CheckFailed(AssertionError):
    pass


def need(condition: object, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def find_action(
    page: Mapping[str, Any], kind: str, *, label: str | None = None, prefix: str | None = None
) -> dict[str, Any] | None:
    for action in page.get("actions") or ():
        text = str(action.get("label") or "")
        if action.get("kind") == kind and (text == label or (prefix is not None and text.startswith(prefix))):
            return action
    return None


def settle_text(value: Any) -> str:
    """`Tab.last_settle` (после §4.1 ядра: причина и мс успокоения); у ядра без него — «—»."""
    if value is None:
        return "—"
    if isinstance(value, Mapping):
        return f"{value.get('reason')} {value.get('ms')} мс"
    return str(value)


class Driver:
    """Вкладка + последний снимок: действия по меткам из снимка, как у агента (`Tab.act` → `Tab.observe`)."""

    def __init__(self, tab: Tab) -> None:
        self.tab = tab
        self.page: dict[str, Any] = {}
        self.settles: list[tuple[str, str]] = []

    def open(self, url: str) -> dict[str, Any]:
        self.tab.navigate(url)
        return self.observe()

    def observe(self) -> dict[str, Any]:
        self.page = self.tab.observe()
        return self.page

    def do(
        self, kind: str, *, label: str | None = None, prefix: str | None = None, text: str | None = None
    ) -> dict[str, Any]:
        for _ in range(3):  # страница могла измениться между снимком и действием — переснять и повторить
            action = find_action(self.page, kind, label=label, prefix=prefix)
            need(action, f"нет {kind} «{label or prefix}» в снимке")
            assert action is not None
            try:
                self.tab.act(action, self.page, text=text)
            except StalePage:
                self.observe()
                continue
            self.observe()
            self.settles.append((f"{kind} «{label or prefix}»", settle_text(getattr(self.tab, "last_settle", None))))
            return self.page
        raise CheckFailed(f"{kind} «{label or prefix}»: страница меняется, действие не выполнено")


def has_chat(page: Mapping[str, Any]) -> bool:
    return find_action(page, "click", prefix=CHAT) is not None


def labels(page: Mapping[str, Any], pattern: re.Pattern[str]) -> list[str]:
    """Подписи кнопок-статусов сообщений в снимке («HH:MM Sent» / «HH:MM Sending…»)."""
    return [str(a.get("label")) for a in page.get("actions") or () if pattern.fullmatch(str(a.get("label") or ""))]


# Тайминги проверок — от параметров страницы (PageParams) плюс запасы ниже, не литералы задач.
CHECK_MARGIN_S = 0.4  # запас после ожидаемого момента (результаты, пересоздание, статус) до снимка
TYPE_ROOM_S = 0.45  # столько нужно до пересоздания, чтобы успеть напечатать (действие + снимок)
NO_RESULTS_MIN_MS = 500  # net=0: «результатов сразу после ввода нет» проверяем при задержке не короче
KEEP_S = 0.5  # повторный ввод после последнего пересоздания держится столько


def wait_until(deadline: float) -> None:
    time.sleep(max(0.0, deadline - time.monotonic()))


def search_part(driver: Driver, p: PageParams) -> str:
    typed_at = time.monotonic()
    first = driver.do("fill", label=SEARCH_LABEL, text=SEARCH_QUERY)
    typed_settle = driver.settles[-1][1]
    need(find_action(first, "click", label="Clear search"), "после ввода нет кнопки «Clear search»")
    if p.spinner:
        need("Searching" in first["text"] or has_chat(first), "после ввода нет ни «Searching», ни результатов")
        seen_first = "Searching" if "Searching" in first["text"] else f"уже «{CHAT}»"
    elif not p.net and p.delay >= NO_RESULTS_MIN_MS:  # таймер ожидание ядра не держит: результатов ещё нет
        need(not has_chat(first), f"«{CHAT}» виден сразу после ввода: задержка поиска не работает")
        seen_first = f"без «{CHAT}»"
    else:  # net=1: ядро вправе дождаться ответа /api/search — честны оба исхода
        seen_first = f"уже «{CHAT}»" if has_chat(first) else f"без «{CHAT}»"
    after = p.delay / 1000 + CHECK_MARGIN_S
    wait_until(typed_at + after)
    second = driver.observe()
    need(has_chat(second), f"через {after:.1f} с после ввода «{CHAT}» нет в списке")
    need(find_action(second, "click", prefix="Работа "), "нет отвлекающего чата «Работа»")
    return f"снимок после ввода «{SEARCH_QUERY}»: {seen_first} (settle {typed_settle}); через {after:.1f} с: есть"


def type_message(driver: Driver, label: str) -> dict[str, Any]:
    page = driver.do("fill", label=label, text=MESSAGE)
    field = find_action(page, "fill", label=label)
    need(field and field.get("value") == MESSAGE, f"после ввода в «{label}» текста в поле нет")
    need(find_action(page, "click", label="Send"), "после ввода нет Send")
    assert field is not None
    return field


def remount_part(driver: Driver, p: PageParams, clicked: float, opened: Mapping[str, Any]) -> str:
    """Каждое пересоздание из `remount` (мс от клика по чату): узел поля новый, пустой, Send спрятан, есть «Voice
    message»; напечатанное до пересоздания пропадает (печатаем, если до него успеть); после последнего повторный ввод
    остаётся."""
    field = find_action(opened, "fill", label=COMPOSER_TO)
    need(field, f"после открытия чата нет поля «{COMPOSER_TO}»")
    assert field is not None
    node, looked, typed = field["node"], time.monotonic(), False
    notes: list[str] = []
    if clicked + p.remount[0] / 1000 - time.monotonic() > TYPE_ROOM_S:
        node, typed = type_message(driver, COMPOSER_TO)["node"], True
        looked = time.monotonic()
        notes.append(f"ввод через {_since(clicked)} мс после клика — текст есть")
    else:
        notes.append(f"до {p.remount[0]} мс напечатать не успеть")
    for i, at in enumerate(p.remount):
        due = clicked + at / 1000
        following = p.remount[i + 1] if i + 1 < len(p.remount) else None
        margin = CHECK_MARGIN_S if following is None else min(CHECK_MARGIN_S, (following - at) / 4000)
        if looked >= due:  # пересоздание было раньше, чем мы увидели поле: сравнить не с чем
            notes.append(f"{at} мс: раньше снимка, не проверено")
            continue
        wait_until(due + margin)
        page = driver.observe()
        after = find_action(page, "fill", label=COMPOSER_TO)
        need(after, f"через {at} мс после клика нет поля «{COMPOSER_TO}»")
        assert after is not None
        need(after["node"] != node, f"через {at} мс поле не пересоздано: тот же узел")
        need(not after.get("value"), f"через {at} мс текст пережил пересоздание: {after.get('value')!r}")
        need(not find_action(page, "click", label="Send"), f"через {at} мс Send виден при пустом поле")
        need(find_action(page, "click", label="Voice message"), f"через {at} мс при пустом поле нет «Voice message»")
        notes.append(f"{at} мс: узел новый, {'текст пропал' if typed else 'пустой'}")
        node, looked, typed = after["node"], time.monotonic(), False
        if following is not None and clicked + following / 1000 - time.monotonic() > TYPE_ROOM_S:
            node, typed = type_message(driver, COMPOSER_TO)["node"], True
            looked = time.monotonic()
    type_message(driver, COMPOSER_TO)
    time.sleep(KEEP_S)
    kept = find_action(driver.observe(), "fill", label=COMPOSER_TO)
    need(kept and kept.get("value") == MESSAGE, "повторный ввод пропал: пересозданий больше, чем в remount")
    return f"пересоздания ×{len(p.remount)}: " + "; ".join(notes) + "; повторный ввод остался"


def send_part(driver: Driver, server: FixtureServer, run: str, p: PageParams) -> str:
    sent_at = time.monotonic()
    page = driver.do("click", label="Send")
    looked = time.monotonic() - sent_at  # снимок после Send — не позже этого
    if not p.sendstatus:
        wait_until(sent_at + p.senddelay / 1000 + CHECK_MARGIN_S)
        problem = chat_problem(server.wait_report(run))
        need(problem is None, f"отчёт страницы через {p.senddelay} мс после Send: {problem}")
        return f"отправлено (подтверждение {p.senddelay} мс)"
    sending = labels(page, STATUS_SENDING)
    if looked < p.sendstatus / 1000:
        need(len(sending) == 1, f"сразу после Send кнопок «HH:MM Sending…»: {len(sending)}, ожидалась 1")
        first = f"«{sending[0]}»"
    else:  # ядро ждало ответ /api/send дольше статуса: «Sending…» мог смениться до снимка
        need(len(sending) <= 1, f"после Send кнопок «HH:MM Sending…»: {len(sending)}")
        first = f"снимок через {round(looked * 1000)} мс"
    problem = chat_problem(server.wait_report(run))
    need(problem is None, f"отчёт страницы сразу после Send: {problem}")
    wait_until(sent_at + p.sendstatus / 1000 + CHECK_MARGIN_S)
    done = driver.observe()
    need(not labels(done, STATUS_SENDING), f"через {p.sendstatus} мс статус всё ещё «Sending…»")
    statuses = labels(done, STATUS_SENT)
    need(statuses, "у исходящих нет кнопок «HH:MM Sent»")
    info = driver.do("click", label=statuses[-1])
    need("Message info" in info["text"], "клик по «HH:MM Sent» не открыл «Message info»")
    problem = chat_problem(server.wait_report(run))
    need(problem is None, f"отчёт страницы после «Message info»: {problem}")
    return (
        f"{first} → «Sent» через {p.sendstatus} мс; кнопок «HH:MM Sent» {len(statuses)}; "
        "клик по статусу — «Message info»"
    )


def net_part(server: FixtureServer, run: str, p: PageParams) -> str:
    """net=1 — поиск и подтверждение Send прошли через сервер стенда с паузой из параметров; net=0 — сети нет."""
    calls = server.calls(run)
    if not p.net:
        need(not calls, f"net=0, а страница ходила в /api: {[call['api'] for call in calls]}")
        return "сеть: нет (таймеры)"
    searches = [call for call in calls if call["api"] == "search" and call.get("q") == SEARCH_QUERY]
    need(searches, f"net=1, а запроса /api/search?q={SEARCH_QUERY} не было")
    need(all(call["delay_ms"] == p.delay for call in searches), f"/api/search: пауза не {p.delay} мс")
    sends = [call for call in calls if call["api"] == "send"]
    confirm = p.sendstatus or p.senddelay
    need(len(sends) == 1, f"/api/send: запросов {len(sends)}, ждали 1")
    need(sends[0]["delay_ms"] == confirm, f"/api/send: пауза {sends[0]['delay_ms']} мс, ждали {confirm}")
    short = [call for call in searches + sends if (call["t1"] - call["t0"]) * 1000 < call["delay_ms"] - 2]
    need(not short, f"сервер ответил раньше паузы: {short}")
    return f"сеть: /api/search {p.delay} мс ×{len(searches)}, /api/send {confirm} мс"


def check_chat(driver: Driver, server: FixtureServer, run: str, page: str) -> str:
    """Чат на app.html с любыми параметрами: загрузка (boot), поиск с задержкой (net — запрос или таймер), открытие
    чата, пересоздания поля (remount), отправка и статусы (sendstatus); сверка отчётов и журнала /api."""
    p = PageParams.of(page)
    parts = []
    opened = time.monotonic()
    first = driver.open(server.url(with_params(page, run=run)))
    if p.boot:
        need(not interactive(first), f"boot={p.boot}: в первом снимке уже есть элементы для действия")
        after = p.boot / 1000 + CHECK_MARGIN_S
        wait_until(opened + after)
        need(interactive(driver.observe()), f"boot={p.boot}: через {after:.1f} с элементов для действия нет")
        parts.append(f"первый снимок без элементов, через {after:.1f} с есть")
    parts.append(search_part(driver, p))
    clicked = time.monotonic()
    chat = driver.do("click", prefix=CHAT)
    if p.remount:
        parts.append(remount_part(driver, p, clicked, chat))
    else:
        type_message(driver, "Type a message")
    parts.append(send_part(driver, server, run, p))
    parts.append(net_part(server, run, p))
    history = server.history(run)
    need(len(history) >= 3, f"отчётов страницы {len(history)}, ждали ≥ 3")
    need(any(report.get("composer") == MESSAGE for _, report in history), "в отчётах нет поля с напечатанным")
    mismatch = params_problem(page, server.report(run))
    need(mismatch is None, str(mismatch))
    parts.append(f"отчётов {len(history)}")
    return "; ".join(parts)


def check_form(driver: Driver, server: FixtureServer, run: str, page: str) -> str:
    driver.open(server.url(with_params(page, run=run)))
    driver.do("fill", label="Name", text=FORM_EXPECTED["name"])
    driver.do("fill", label="Email", text=FORM_EXPECTED["email"])
    driver.do("select", label=f"Country → {FORM_EXPECTED['country']}")
    driver.do("click", label="I agree to the terms")
    driver.do("click", label="Submit")
    problem = form_problem(server.wait_report(run))
    need(problem is None, f"отчёт страницы: {problem}")
    return "отправлено, все 4 поля совпали"


# скриптовые проверки страниц (`--fixtures-only`); не путать со сценариями задач (`--mode scenario`)
FIXTURE_CHECKS: dict[str, Callable[[Driver, FixtureServer, str, str], str]] = {
    "search": check_chat,
    "search-spinner": check_chat,
    "boot": check_chat,
    "search-remount": check_chat,
    "form": check_form,
}
# кроме страницы задачи — варианты параметров (поверх неё); boot — 2 с вместо 4: то же поведение, короче
FIXTURE_VARIANTS: dict[str, tuple[dict[str, str], ...]] = {
    "search": ({}, {"net": "0"}),
    "boot": ({"boot": "2000"},),
    "search-remount": ({}, {"remount": "800,1600"}, {"net": "0"}),
}


def variant_label(base: str, page: str) -> str:
    """Чем страница отличается от страницы задачи: «net=0», «remount=800,1600»; ничем — ""."""
    before = dict(parse_qsl(urlsplit(base).query))
    return ",".join(f"{key}={value}" for key, value in parse_qsl(urlsplit(page).query) if before.get(key) != value)


def fixture_pages(task: Task, args: argparse.Namespace) -> list[tuple[str, str]]:
    """`(метка, страница)` для скриптовой проверки задачи: с `--sweep` — каждая ячейка развёртки; с `--delay` /
    `--remount` / `--sendstatus` / `--net` — одна страница с ними; иначе — страница задачи и FIXTURE_VARIANTS."""
    if args.sweep:
        return [(cell.id, with_params(task.page, **cell.query())) for cell in sweep_of(args)]
    if any(value is not None for value in overrides(args).values()):
        pages = [page_for(task, **overrides(args))]
    else:
        pages = [
            with_params(task.page, **extra) if extra else task.page for extra in FIXTURE_VARIANTS.get(task.name, ({},))
        ]
    return [(variant_label(task.page, page), page) for page in pages]


def fixtures_only(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Страницы стенда ведут себя как задумано на настоящем Chrome: скриптовые действия без моделей (бесплатно)."""
    failures = 0
    with fresh_profile(True) as profile:
        try:
            browser = eval_settings(env, profile, args).browser
        except ConfigError as exc:
            print(f"eval: {exc}", file=sys.stderr)
            return 2
        if not browser.chrome_binary.is_file():
            print(
                f"eval: Chrome не найден: {browser.chrome_binary}; задайте BROWSER_HANDS_CHROME_BINARY", file=sys.stderr
            )
            return 2
        chrome = Chrome(browser)
        try:
            chrome.connect()
            with FixtureServer() as server:
                for name in args.tasks:
                    if name not in FIXTURE_CHECKS:  # wiki: внешний сайт, страниц стенда нет
                        print(f"skip  {name}: внешняя сеть — страниц стенда нет", flush=True)
                        continue
                    for label, page in fixture_pages(TASKS[name], args):
                        failures += not fixture_check(chrome, server, name, label, page)
        finally:
            chrome.close()
    return 0 if failures == 0 else 1


def fixture_check(chrome: Chrome, server: FixtureServer, name: str, label: str, page: str) -> bool:
    """Одна скриптовая проверка в новой вкладке: строка `ok`/`FAIL` и успокоения ядра по действиям."""
    title = f"{name} [{label}]" if label else name
    tab = chrome.new_tab()
    driver = Driver(tab)
    run = f"fixtures-{name}-{uuid.uuid4().hex[:6]}"
    passed = True
    try:
        detail = FIXTURE_CHECKS[name](driver, server, run, page)
        print(f"ok    {title}: {detail}", flush=True)
    except Exception as exc:  # любой сбой проверки печатаем и идём дальше
        passed = False
        print(f"FAIL  {title}: {type(exc).__name__}: {exc}", flush=True)
    finally:
        tab.close()
    if any(how != "—" for _, how in driver.settles):
        steps = "; ".join(f"{what} → {how}" for what, how in driver.settles)
        print(f"      settle: {steps}", flush=True)
    elif driver.settles:
        print("      settle: у ядра нет Tab.last_settle (до §4.1 плана надёжности)", flush=True)
    return passed


# --- вход ----------------------------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Стенд надёжности browser-hands: локальные страницы, проверка по DOM.")
    parser.add_argument("--runs", type=int, default=5, metavar="N", help="прогонов на задачу (ячейку) (по умолчанию 5)")
    parser.add_argument(
        "--mode",
        default="goal",
        metavar="MODE",
        help="goal — цель задачи (по умолчанию), scenario — её сценарий steps; с --sweep — можно оба: goal,scenario",
    )
    parser.add_argument(
        "--tasks",
        help=f"через запятую из: {', '.join(TASKS)}; all — все (wiki — внешняя сеть, по умолчанию не входит); "
        f"с --sweep по умолчанию {','.join(SWEEP_TASKS)}",
    )
    parser.add_argument("--label", default="", help="метка замера в JSONL (before, after, …)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "traces", help="куда писать eval-<ts>.jsonl")
    parser.add_argument("--timeout", type=float, default=60.0, metavar="S", help="дедлайн прогона (60)")
    parser.add_argument("--max-steps", type=int, default=12, metavar="N", help="шагов на прогон (12)")
    parser.add_argument("--delay", type=int, metavar="MS", help="задержка поиска на app.html вместо заданной")
    parser.add_argument(
        "--remount",
        metavar="MS[,MS…]",
        help=f"пересоздания поля в search-remount вместо {REMOUNT_MS} мс; список — несколько (800,1600)",
    )
    parser.add_argument(
        "--sendstatus",
        metavar="MS",
        help=f"«Sending…» в search-remount вместо {SEND_STATUS_MS} мс; с --sweep — значения оси ({SWEEP_SENDSTATUS})",
    )
    parser.add_argument(
        "--net",
        metavar="0|1",
        help=f"1 — задержки страниц запросами к стенду (по умолчанию), 0 — таймерами; с --sweep — значения на "
        f"delay-оси ({SWEEP_NETS})",
    )
    parser.add_argument(
        "--sweep", choices=SWEEP_KINDS, help="развёртка параметров app.html: axes — по осям, grid — полная сетка"
    )
    parser.add_argument(
        "--delay-range", default=SWEEP_DELAYS, metavar="A:B:STEP|V,…", help=f"--sweep: delay ({SWEEP_DELAYS})"
    )
    parser.add_argument(
        "--remount-range", default=SWEEP_REMOUNTS, metavar="A:B:STEP|V,…", help=f"--sweep: remount ({SWEEP_REMOUNTS})"
    )
    parser.add_argument("--max-cost", type=float, metavar="USD", help="не начинать новый прогон после этой суммы")
    parser.add_argument("--fixtures-only", action="store_true", help="без моделей: скриптовые действия (бесплатно)")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs: ожидается ≥ 1")
    modes = list(dict.fromkeys(mode.strip() for mode in args.mode.split(",") if mode.strip()))
    bad = [mode for mode in modes if mode not in MODES]
    if not modes or bad:
        parser.error(f"--mode: неизвестные {', '.join(bad) or '(пусто)'}; есть {', '.join(MODES)}")
    if len(modes) > 1 and not args.sweep:
        parser.error("--mode: несколько режимов — только с --sweep")
    args.modes, args.mode = modes, ",".join(modes)
    default_tasks = SWEEP_TASKS if args.sweep else DEFAULT_TASKS
    names = [name.strip() for name in (args.tasks or ",".join(default_tasks)).split(",") if name.strip()]
    if names == ["all"]:
        names = list(TASKS)
    unknown = [name for name in names if name not in TASKS]
    if not names or unknown:
        parser.error(f"--tasks: неизвестные {', '.join(unknown) or '(пусто)'}; есть {', '.join(TASKS)}")
    args.tasks = list(dict.fromkeys(names))
    try:
        remount = None if args.remount is None else parse_values(args.remount, name="--remount")
        sendstatus = args.sendstatus or (SWEEP_SENDSTATUS if args.sweep else None)
        sendstatuses = None if sendstatus is None else parse_values(sendstatus, name="--sendstatus")
        net = args.net or (SWEEP_NETS if args.sweep else None)
        nets = None if net is None else parse_values(net, name="--net")
        args.delays = parse_values(args.delay_range, name="--delay-range")
        args.remounts = parse_values(args.remount_range, name="--remount-range")
    except ValueError as exc:
        parser.error(str(exc))
    if nets is not None and not set(nets) <= {0, 1}:
        parser.error("--net: только 0 и 1")
    if args.sweep:
        if args.delay is not None or remount is not None:
            parser.error("--sweep: вместо --delay / --remount — --delay-range / --remount-range")
        outside = [name for name in args.tasks if not TASKS[name].page.startswith("app.html")]
        if outside:
            parser.error(f"--sweep: только задачи чата (app.html), не {', '.join(outside)}")
        args.sendstatuses, args.nets, args.sendstatus, args.net = sendstatuses, nets, None, None
        return args
    if len(sendstatuses or ()) > 1 or len(nets or ()) > 1:
        parser.error("--sendstatus / --net: список значений — только с --sweep")
    args.remount = None if remount is None else ",".join(map(str, remount))
    args.sendstatus = sendstatuses[0] if sendstatuses else None
    args.net = nets[0] if nets else None
    args.sendstatuses = args.nets = None
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    factories: Mapping[str, Any] | None = None,
) -> int:
    args = parse_args(argv)
    env = os.environ if env is None else env
    if args.fixtures_only:
        return fixtures_only(args, env)
    tasks = [TASKS[name] for name in args.tasks]
    jobs = plan_jobs(tasks, args)
    with fresh_profile(True) as profile:
        try:
            settings = eval_settings(env, profile, args)
            settings.validate()
        except ConfigError as exc:
            print(f"eval: {exc}", file=sys.stderr)
            return 2
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out = args.out_dir / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        head = git_head()
        service = BrowseService(settings, **(factories or {}))  # один Chrome и одни клиенты на все прогоны
        try:
            with FixtureServer() as server, out.open("w", encoding="utf-8") as trace:
                evaluation = run_all(service, server, jobs, args, trace, head=head)
                if args.sweep:
                    summary = summarize_sweep(evaluation, jobs, label=args.label, head=head, kind=args.sweep)
                else:
                    summary = summarize(evaluation, tasks, label=args.label, head=head, mode=args.mode)
                trace.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
        finally:
            service.close()
    print(format_sweep(summary, evaluation.rows) if args.sweep else format_summary(summary))
    print(f"trace: {out}")
    complete = not evaluation.stopped and summary["runs"] == args.runs * len(jobs)
    if args.sweep:  # развёртка — замер: неудачи в ней — данные, выход 1 — только недобор прогонов
        return 0 if complete else 1
    return 0 if complete and summary["verified"] == summary["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
