"""Стенд надёжности: локальные страницы `tests/fixtures/`, N прогонов агента на задачу, проверка результата по DOM.

    uv run --env-file .env python scripts/eval.py --runs 10 --label before
    uv run --env-file .env python scripts/eval.py --mode scenario --tasks all --runs 10 --label scenario
    uv run --frozen python scripts/eval.py --fixtures-only        # бесплатно: настоящий Chrome, без моделей

`--mode goal` (по умолчанию) — агент получает цель задачи, `--mode scenario` — её сценарий `{do, text?}` без цели
(docs/plan-scenarios.md §5.3). Страница сама отчитывается стенду (`POST /report`, только то, что видно в DOM), поэтому
`verified` не зависит от статуса агента. Исключение — `wiki` (внешняя сеть, только в `--tasks all` или по имени):
проверка по итоговому `url`. Chrome всегда свой: launch + headless + временный профиль (выбора браузера нет; `--mode`
— режим прогона). Итог — строка на прогон, сводка по задаче (verified/done k/N, медиана и p95 elapsed/wait, вызовы Jev
и текстовой модели, стоимость, причины неудач) и JSONL в `traces/eval-<ts>.jsonl` (строки прогонов + `summary`).
Выход 0, только если все прогоны verified. Платно, кроме `--fixtures-only`; в pytest не запускается (там — `FakeCore`).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
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
SEARCH_QUERY = "Раб"  # --fixtures-only: совпадает с «Рабочий» и отвлекающим «Работа»
# search-remount: поле сообщения пересоздаётся через REMOUNT_MS после открытия чата (текст пропадает, как в WhatsApp
# 26.09), после Send — «Sending…» ещё SEND_STATUS_MS, у исходящих кнопки «HH:MM Sent» — есть куда кликнуть лишний раз
REMOUNT_MS = 900
SEND_STATUS_MS = 1500
COMPOSER_TO = f"Type a message to {CHAT}"  # подпись поля при remount, как в WhatsApp
STATUS_SENT = re.compile(r"\d\d:\d\d Sent")
STATUS_SENDING = re.compile(r"\d\d:\d\d Sending…")
WIKI_URL = "https://en.wikipedia.org/wiki/Main_Page"
WIKI_GOAL = "Find and open the Wikipedia article about Gödel's incompleteness theorems."  # как bench/live_wikipedia
WIKI_QUERY = "Gödel's incompleteness theorems"
WIKI_ARTICLE = "/wiki/Gödel's_incompleteness_theorems"  # в url после unquote: …/wiki/G%C3%B6del%27s_incompleteness_…

# Сценарии задач (`--mode scenario`): do — по-английски (Jev на нём точнее), text — дословно (plan-scenarios §0.5)
CHAT_SCENARIO: tuple[dict[str, str], ...] = (
    {"do": "Type the chat name into the chat search box", "text": CHAT},
    {"do": f"Open the chat named «{CHAT}» in the chat list"},
    {"do": "Type the message into the message box of the open chat", "text": MESSAGE},
    {"do": "Send the message"},
)
FORM_SCENARIO: tuple[dict[str, str], ...] = (
    {"do": "Fill the Name field", "text": FORM_EXPECTED["name"]},
    {"do": "Fill the Email field", "text": FORM_EXPECTED["email"]},
    {"do": f"Select {FORM_EXPECTED['country']} in the Country dropdown"},
    {"do": "Tick the checkbox «I agree to the terms»"},
    {"do": "Submit the form"},
)
WIKI_SCENARIO: tuple[dict[str, str], ...] = (
    {"do": "Type the query into the Wikipedia search box", "text": WIKI_QUERY},
    {"do": "Open the matching article from the suggestions or search results"},
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
        Task("search", "app.html?delay=700", CHAT_GOAL, chat_problem, CHAT_SCENARIO),
        Task("search-spinner", "app.html?delay=700&spinner=1", CHAT_GOAL, chat_problem, CHAT_SCENARIO),
        Task("boot", "app.html?boot=4000&delay=700", CHAT_GOAL, chat_problem, CHAT_SCENARIO),
        Task(
            "search-remount",
            f"app.html?delay=700&remount={REMOUNT_MS}&sendstatus={SEND_STATUS_MS}",
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
    """`page` с добавленными/заменёнными параметрами query (порядок прежних сохраняется)."""
    parts = urlsplit(page)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit(parts._replace(query=urlencode(query)))


def page_for(task: Task, *, delay: int | None) -> str:
    """Страница задачи; `--delay` меняет задержку поиска только там, где она задана."""
    if delay is not None and "delay" in dict(parse_qsl(urlsplit(task.page).query)):
        return with_params(task.page, delay=str(delay))
    return task.page


# --- сервер стенда -------------------------------------------------------------------------------------------------


class ReportStore:
    """Последний отчёт страницы по `run` (отчёт с меньшим `seq` пришёл позже — не затирает) и время изменения."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reports: dict[str, dict[str, Any]] = {}
        self._changed: dict[str, float] = {}

    def put(self, report: dict[str, Any]) -> None:
        run = report.get("run")
        if not isinstance(run, str) or not run:
            return
        with self._lock:
            old = self._reports.get(run)
            if old is not None and _seq(old) > _seq(report):
                return
            self._reports[run] = report
            self._changed[run] = time.monotonic()

    def get(self, run: str) -> dict[str, Any] | None:
        with self._lock:
            return self._reports.get(run)

    def changed(self, run: str) -> float | None:
        with self._lock:
            return self._changed.get(run)


def _seq(report: Mapping[str, Any]) -> int:
    seq = report.get("seq")
    return seq if isinstance(seq, int) else -1


class _Handler(SimpleHTTPRequestHandler):
    """GET — файлы `tests/fixtures/`; POST /report — отчёт страницы в `ReportStore`. Без логов в stderr."""

    def __init__(self, *args: Any, store: ReportStore, **kwargs: Any) -> None:
        self.store = store  # до super().__init__: он сразу обрабатывает запрос
        super().__init__(*args, **kwargs)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/report":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REPORT_BYTES:
            self.send_error(413)
            return
        try:
            report = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            self.send_error(400)
            return
        if isinstance(report, dict):
            self.store.put(report)
        self.send_response(204)
        self.end_headers()

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


@contextlib.contextmanager
def record_decisions(sink: list[dict[str, Any]]) -> Iterator[None]:
    """На время прогона оборачивает `browser_hands.agent.choose`: на каждое решение Jev — что было на странице
    (видимый текст, элементы), под какой шаг сценария, что выбрано и `t_ms` — когда пришёл ответ (мс от входа в
    контекст, ≈ начало прогона). Ядро не меняется; нет такой функции — ничего не пишет."""
    original = getattr(agent_module, "choose", None)
    if original is None:
        yield
        return
    started = time.monotonic()

    def recording(clients: Any, state: dict[str, Any], goal: str, history: Any, *args: Any, **kwargs: Any) -> Any:
        where = {**seen(state), **step_number(kwargs.get("step"))}
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


def _since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


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
    tasks: Sequence[Task],
    args: argparse.Namespace,
    trace: TextIO,
    *,
    head: str | None,
) -> Evaluation:
    """По кругу: прогон 1 всех задач, потом 2 и т. д. — медленная минута сети не достаётся одной задаче.

    `--mode scenario` — вместо цели сценарий задачи (`browse(url, "", steps=…)`), цель агенту не даётся.
    """
    evaluation = Evaluation()
    scenarios = {task.name: task.steps() for task in tasks} if args.mode == "scenario" else {}
    print(table_header(), flush=True)
    for index in range(1, args.runs + 1):
        for task in tasks:
            if args.max_cost is not None and evaluation.cost >= args.max_cost:
                evaluation.stopped = f"бюджет ${args.max_cost:g} исчерпан (${evaluation.cost:.4f})"
                return evaluation
            run_id = f"{task.name}-{index}-{uuid.uuid4().hex[:6]}"
            if task.url is not None:  # внешний сайт: как есть, отчёта страницы не будет
                url = task.url
            else:
                url = server.url(with_params(page_for(task, delay=args.delay), run=run_id))
            steps = scenarios.get(task.name)
            decisions: list[dict[str, Any]] = []
            with record_decisions(decisions):
                if steps is None:
                    result = service.browse(url, task.goal, max_steps=args.max_steps, timeout_s=args.timeout)
                else:
                    result = service.browse(url, "", steps=steps, max_steps=args.max_steps, timeout_s=args.timeout)
            report = None if task.network else server.wait_report(run_id)
            problem = task.verify(result, report)
            row = {
                "task": task.name,
                "mode": args.mode,
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
            evaluation.rows.append(row)
            trace.write(json.dumps(row, ensure_ascii=False) + "\n")
            trace.flush()
            print(format_row(row), flush=True)
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


def table_header() -> str:
    return (
        f"{'task':<15}{'#':>3}  {'status':<10} {'ok':<4}{'steps':>5}  {'elapsed':>7} {'model':>6} {'text':>6} "
        f"{'browser':>7} {'wait':>6} {'calls':>5} {'jev':>4} {'txt':>4} {'scn':>5}  {'cost $':>7}"
    )


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


def chat_flow(driver: Driver, server: FixtureServer, run: str, *, spinner: bool) -> str:
    first = driver.do("fill", label=SEARCH_LABEL, text=SEARCH_QUERY)
    typed_settle = driver.settles[-1][1]
    need(find_action(first, "click", label="Clear search"), "после ввода нет кнопки «Clear search»")
    if spinner:
        need("Searching" in first["text"] or has_chat(first), "после ввода нет ни «Searching», ни результатов")
        seen_first = "Searching" if "Searching" in first["text"] else f"уже «{CHAT}»"
    else:
        need(not has_chat(first), f"«{CHAT}» виден сразу после ввода: задержка поиска не работает")
        seen_first = f"без «{CHAT}»"
    time.sleep(1.0)
    second = driver.observe()
    need(has_chat(second), f"через 1 с после ввода «{CHAT}» нет в списке")
    need(find_action(second, "click", prefix="Работа "), "нет отвлекающего чата «Работа»")
    driver.do("click", prefix=CHAT)
    driver.do("fill", label="Type a message", text=MESSAGE)
    driver.do("click", label="Send")
    time.sleep(0.5)
    problem = chat_problem(server.wait_report(run))
    need(problem is None, f"отчёт страницы: {problem}")
    return f"снимок после ввода «{SEARCH_QUERY}»: {seen_first} (settle {typed_settle}); через 1 с: есть; отправлено"


def check_search(driver: Driver, server: FixtureServer, run: str) -> str:
    driver.open(server.url(with_params(TASKS["search"].page, run=run)))
    return chat_flow(driver, server, run, spinner=False)


def check_spinner(driver: Driver, server: FixtureServer, run: str) -> str:
    driver.open(server.url(with_params(TASKS["search-spinner"].page, run=run)))
    return chat_flow(driver, server, run, spinner=True)


def check_boot(driver: Driver, server: FixtureServer, run: str) -> str:
    first = driver.open(server.url(with_params("app.html?boot=2000&delay=700", run=run)))
    need(not interactive(first), "boot=2000: в первом снимке уже есть элементы для действия")
    time.sleep(2.5)
    need(interactive(driver.observe()), "boot=2000: через 2,5 с элементов для действия нет")
    return "первый снимок без элементов, через 2,5 с есть; " + chat_flow(driver, server, run, spinner=False)


def labels(page: Mapping[str, Any], pattern: re.Pattern[str]) -> list[str]:
    """Подписи кнопок-статусов сообщений в снимке («HH:MM Sent» / «HH:MM Sending…»)."""
    return [str(a.get("label")) for a in page.get("actions") or () if pattern.fullmatch(str(a.get("label") or ""))]


def check_remount(driver: Driver, server: FixtureServer, run: str) -> str:
    """Поле сообщения пересоздаётся через REMOUNT_MS после открытия чата (новый узел, текст пропал, Send спрятан);
    повторный ввод остаётся; после Send — «Sending…», через SEND_STATUS_MS — «Sent»; клик по «HH:MM Sent» открывает
    «Message info», отчёт при этом прежний."""
    driver.open(server.url(with_params(TASKS["search-remount"].page, run=run)))
    driver.do("fill", label=SEARCH_LABEL, text=SEARCH_QUERY)
    time.sleep(1.0)
    need(has_chat(driver.observe()), f"через 1 с после ввода «{CHAT}» нет в списке")
    clicked = time.monotonic()
    driver.do("click", prefix=CHAT)
    typed = driver.do("fill", label=COMPOSER_TO, text=MESSAGE)
    typed_ms = round((time.monotonic() - clicked) * 1000)
    before = find_action(typed, "fill", label=COMPOSER_TO)
    need(before and before.get("value") == MESSAGE, f"ввод через {typed_ms} мс после клика: текста в поле нет")
    need(find_action(typed, "click", label="Send"), "после ввода нет Send")
    assert before is not None
    time.sleep(max(0.0, REMOUNT_MS / 1000 - (time.monotonic() - clicked)) + 0.4)
    wiped = driver.observe()
    after = find_action(wiped, "fill", label=COMPOSER_TO)
    need(after, f"после пересоздания нет поля «{COMPOSER_TO}»")
    assert after is not None
    need(after["node"] != before["node"], "поле не пересоздано: тот же узел")
    need(not after.get("value"), f"текст пережил пересоздание: {after.get('value')!r}")
    need(not find_action(wiped, "click", label="Send"), "Send виден при пустом поле")
    need(find_action(wiped, "click", label="Voice message"), "при пустом поле нет «Voice message»")
    past = labels(wiped, STATUS_SENT)
    need(past, "у прошлых сообщений нет кнопок «HH:MM Sent»")
    driver.do("fill", label=COMPOSER_TO, text=MESSAGE)
    time.sleep(1.0)
    kept = find_action(driver.observe(), "fill", label=COMPOSER_TO)
    need(kept and kept.get("value") == MESSAGE, "повторный ввод пропал: пересоздание не одно")
    sending = labels(driver.do("click", label="Send"), STATUS_SENDING)
    need(len(sending) == 1, f"сразу после Send кнопок «HH:MM Sending…»: {len(sending)}, ожидалась 1")
    problem = chat_problem(server.wait_report(run))
    need(problem is None, f"отчёт страницы сразу после Send: {problem}")
    time.sleep(SEND_STATUS_MS / 1000 + 0.3)
    done = driver.observe()
    need(not labels(done, STATUS_SENDING), f"через {SEND_STATUS_MS} мс статус всё ещё «Sending…»")
    info = driver.do("click", label=labels(done, STATUS_SENT)[-1])
    need("Message info" in info["text"], "клик по «HH:MM Sent» не открыл «Message info»")
    problem = chat_problem(server.wait_report(run))
    need(problem is None, f"отчёт страницы после «Message info»: {problem}")
    return (
        f"ввод через {typed_ms} мс после клика — текст есть; через {REMOUNT_MS} мс поле новое, пустое, без Send; "
        f"повторный ввод остался; «{sending[0]}» → «Sent»; прошлых «HH:MM Sent» в снимке {len(past)}; "
        "клик по статусу — «Message info»"
    )


def check_form(driver: Driver, server: FixtureServer, run: str) -> str:
    driver.open(server.url(with_params(TASKS["form"].page, run=run)))
    driver.do("fill", label="Name", text=FORM_EXPECTED["name"])
    driver.do("fill", label="Email", text=FORM_EXPECTED["email"])
    driver.do("select", label=f"Country → {FORM_EXPECTED['country']}")
    driver.do("click", label="I agree to the terms")
    driver.do("click", label="Submit")
    problem = form_problem(server.wait_report(run))
    need(problem is None, f"отчёт страницы: {problem}")
    return "отправлено, все 4 поля совпали"


# скриптовые проверки страниц (`--fixtures-only`); не путать со сценариями задач (`--mode scenario`)
FIXTURE_CHECKS: dict[str, Callable[[Driver, FixtureServer, str], str]] = {
    "search": check_search,
    "search-spinner": check_spinner,
    "boot": check_boot,
    "search-remount": check_remount,
    "form": check_form,
}


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
                    tab = chrome.new_tab()
                    driver = Driver(tab)
                    run = f"fixtures-{name}-{uuid.uuid4().hex[:6]}"
                    try:
                        detail = FIXTURE_CHECKS[name](driver, server, run)
                        print(f"ok    {name}: {detail}", flush=True)
                    except Exception as exc:  # любой сбой проверки печатаем и идём дальше
                        failures += 1
                        print(f"FAIL  {name}: {type(exc).__name__}: {exc}", flush=True)
                    finally:
                        tab.close()
                    if any(how != "—" for _, how in driver.settles):
                        steps = "; ".join(f"{what} → {how}" for what, how in driver.settles)
                        print(f"      settle: {steps}", flush=True)
                    elif driver.settles:
                        print("      settle: у ядра нет Tab.last_settle (до §4.1 плана надёжности)", flush=True)
        finally:
            chrome.close()
    return 0 if failures == 0 else 1


# --- вход ----------------------------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Стенд надёжности browser-hands: локальные страницы, проверка по DOM.")
    parser.add_argument("--runs", type=int, default=5, metavar="N", help="прогонов на задачу (по умолчанию 5)")
    parser.add_argument(
        "--mode", choices=MODES, default="goal", help="goal — цель задачи (по умолчанию), scenario — её сценарий steps"
    )
    parser.add_argument(
        "--tasks",
        default=",".join(DEFAULT_TASKS),
        help=f"через запятую из: {', '.join(TASKS)}; all — все (wiki — внешняя сеть, по умолчанию не входит)",
    )
    parser.add_argument("--label", default="", help="метка замера в JSONL (before, after, …)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "traces", help="куда писать eval-<ts>.jsonl")
    parser.add_argument("--timeout", type=float, default=60.0, metavar="S", help="дедлайн прогона (60)")
    parser.add_argument("--max-steps", type=int, default=12, metavar="N", help="шагов на прогон (12)")
    parser.add_argument("--delay", type=int, metavar="MS", help="задержка поиска на app.html вместо заданной")
    parser.add_argument("--max-cost", type=float, metavar="USD", help="не начинать новый прогон после этой суммы")
    parser.add_argument("--fixtures-only", action="store_true", help="без моделей: скриптовые действия (бесплатно)")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs: ожидается ≥ 1")
    names = [name.strip() for name in args.tasks.split(",") if name.strip()]
    if names == ["all"]:
        names = list(TASKS)
    unknown = [name for name in names if name not in TASKS]
    if not names or unknown:
        parser.error(f"--tasks: неизвестные {', '.join(unknown) or '(пусто)'}; есть {', '.join(TASKS)}")
    args.tasks = list(dict.fromkeys(names))
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
                evaluation = run_all(service, server, tasks, args, trace, head=head)
                summary = summarize(evaluation, tasks, label=args.label, head=head, mode=args.mode)
                trace.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
        finally:
            service.close()
    print(format_summary(summary))
    print(f"trace: {out}")
    complete = not evaluation.stopped and summary["runs"] == args.runs * len(tasks)
    return 0 if complete and summary["verified"] == summary["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
