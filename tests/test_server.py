import base64
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import anyio.to_thread
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent

from browser_hands import server as server_module
from browser_hands.config import RunConfig, Settings
from browser_hands.scenario import MAX_DO, MAX_STEPS, MAX_TEXT, ScenarioStep
from browser_hands.server import (
    BROWSE_DESCRIPTION,
    INSTRUCTIONS,
    STEPS_DESCRIPTION,
    BrowseService,
    UnsupportedUrl,
    build_server,
    create_server,
    default_agent_factory,
    failed_result,
    format_result,
    normalize_url,
)
from tests.fakes import JPEG, FakeCore, make_result

ROOT = Path(__file__).resolve().parent.parent


def keyed_settings() -> Settings:
    s = Settings()
    s.models.jev_api_key = s.models.text_api_key = "test-key"
    return s


async def _call(server, **arguments):
    async with create_connected_server_and_client_session(server) as client:
        return await client.call_tool("browse", arguments)


def call_browse(server, **arguments) -> CallToolResult:
    return anyio.run(lambda: _call(server, **arguments))


def text_of(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def image_of(result: CallToolResult) -> ImageContent:
    block = result.content[1]
    assert isinstance(block, ImageContent)
    return block


class ChromeUnavailable(Exception):
    """Как в ядре: исключение из browser_hands.* показывается текстом, без имени класса."""


ChromeUnavailable.__module__ = "browser_hands.chrome"


def test_list_tools_gives_one_short_browse_tool():
    settings = keyed_settings()
    settings.run.max_steps = 7
    server = create_server(settings, **FakeCore().factories())

    async def main():
        async with create_connected_server_and_client_session(server) as client:
            return await client.list_tools()

    tools = anyio.run(main).tools

    assert [t.name for t in tools] == ["browse"]
    assert tools[0].description == BROWSE_DESCRIPTION
    assert len(tools[0].description) <= 200
    assert "скриншот" in tools[0].description
    schema = tools[0].inputSchema
    assert schema["required"] == ["url"]  # goal необязателен: вместо него может быть steps
    assert schema["properties"]["goal"]["default"] == ""
    assert schema["properties"]["max_steps"]["default"] == 7
    assert schema["properties"]["max_steps"]["maximum"] == 50
    assert schema["properties"]["timeout_seconds"]["default"] == 90.0
    assert schema["properties"]["timeout_seconds"]["maximum"] == 300
    new_tab = schema["properties"]["new_tab"]
    assert (new_tab["type"], new_tab["default"]) == ("boolean", False)
    assert len(new_tab["description"]) <= 80
    assert "new_tab" not in schema["required"]
    assert server.instructions == INSTRUCTIONS
    assert len(INSTRUCTIONS) <= 120


def test_browse_returns_text_and_jpeg():
    core = FakeCore()
    result = call_browse(create_server(keyed_settings(), **core.factories()), url="https://example.test", goal="g")

    assert result.isError is False
    assert [c.type for c in result.content] == ["text", "image"]
    text = text_of(result)
    assert text.startswith("status: done\n")
    assert "steps: 2" in text
    assert '2. TYPE_TEXT Target 2 — "text 2"' in text
    assert "cost: $0.0042 (model calls: 2)" in text
    assert "elapsed 1.7s (model 1.2 / text 0.3 / browser 0.1 / wait 0.1)" in text
    assert "\nвкладка: новая\n" in text
    assert core.agents[0]["run"].new_tab is False  # по умолчанию — искать открытую вкладку сайта
    image = image_of(result)
    assert image.mimeType == "image/jpeg"
    assert base64.b64decode(image.data) == JPEG


def test_agent_exception_gives_failed_text_without_image():
    core = FakeCore(error=RuntimeError("boom"))
    result = call_browse(create_server(keyed_settings(), **core.factories()), url="https://example.test", goal="g")

    assert result.isError is False  # Claude Code видит ответ, а не исключение
    assert [c.type for c in result.content] == ["text"]
    text = text_of(result)
    assert text.startswith("status: failed\nerror: RuntimeError: boom\n")
    assert "screenshot: нет" in text


def test_second_browse_reuses_chrome_and_clients():
    core = FakeCore()
    service = BrowseService(keyed_settings(), **core.factories())

    assert service.browse("https://a.test", "g").status == "done"
    assert service.browse("https://b.test", "g").status == "done"

    assert len(core.chromes) == 1  # created == 1
    assert core.chromes[0].connect_calls == 2
    assert len(core.clients) == 1
    assert core.clients[0].warmed.wait(1)
    time.sleep(0.05)
    assert core.clients[0].warmup_calls == 1  # прогрев один раз на процесс
    assert core.agents[0]["chrome"] is core.agents[1]["chrome"]


def test_dead_chrome_is_reconnected_not_recreated():
    core = FakeCore()
    service = BrowseService(keyed_settings(), **core.factories())
    service.browse("https://a.test", "g")
    chrome = core.chromes[0]
    chrome.is_alive = False  # ws умер между вызовами

    assert service.browse("https://a.test", "g").status == "done"

    assert chrome.connect_calls == 2
    assert chrome.alive() is True
    assert len(core.chromes) == 1


def test_chrome_unavailable_is_failed_text_and_next_call_retries():
    core = FakeCore()
    service = BrowseService(keyed_settings(), **core.factories())
    service.browse("https://a.test", "g")
    core.chromes[0].connect_error = ChromeUnavailable("Chrome не запущен или отладка выключена")

    failed = service.browse("https://a.test", "g")
    assert failed.status == "failed"
    assert failed.error == "Chrome не запущен или отладка выключена"
    assert failed.screenshot_jpeg is None
    assert len(core.agents) == 1  # агент не создавался

    core.chromes[0].connect_error = None
    assert service.browse("https://a.test", "g").status == "done"
    assert len(core.chromes) == 1


def test_missing_key_is_failed_text_before_chrome():
    core = FakeCore()
    result = call_browse(create_server(Settings(), **core.factories()), url="https://a.test", goal="g")

    text = text_of(result)
    assert "status: failed" in text
    assert "нет ключа: задайте OPENROUTER_API_KEY" in text
    assert core.chromes == [] and core.clients == []


def test_core_not_ready_is_named_before_key_check(monkeypatch):
    monkeypatch.setitem(sys.modules, "browser_hands.chrome", None)  # как будто ядро не влито
    service = BrowseService(Settings())

    result = service.browse("https://a.test", "g")

    assert result.status == "failed"
    assert result.error == "модуль browser_hands.chrome ещё не готов"


def test_arguments_reach_agent():
    settings = keyed_settings()
    settings.browser.screenshot_quality = 40
    settings.browser.screenshot_scale = 0.5
    core = FakeCore()
    call_browse(
        create_server(settings, **core.factories()),
        url=" example.com/path ",
        goal="найти",
        max_steps=3,
        timeout_seconds=5,
        keep_open=True,
        new_tab=True,
    )

    agent = core.agents[0]
    assert agent["url"] == "https://example.com/path"
    assert agent["goal"] == "найти"
    assert agent["run"] == RunConfig(max_steps=3, timeout_s=5.0, keep_open=True, new_tab=True)
    assert agent["run"].new_tab is True
    assert (agent["screenshot_quality"], agent["screenshot_scale"]) == (40, 0.5)


@pytest.mark.parametrize(
    "arguments",
    [{"max_steps": 0}, {"max_steps": 51}, {"timeout_seconds": 0}, {"timeout_seconds": 301}],
)
def test_invalid_arguments_are_tool_errors(arguments):
    core = FakeCore()
    result = call_browse(
        create_server(keyed_settings(), **core.factories()), url="https://a.test", goal="g", **arguments
    )

    assert result.isError is True
    assert core.agents == []
    text = text_of(result)
    assert text.startswith(f"неверные аргументы browse — {next(iter(arguments))}: ")
    assert "pydantic" not in text and "\n" not in text  # одна строка без ссылок на документацию


def test_limits_at_ceiling_reach_agent_and_service_rejects_above():
    core = FakeCore()
    call_browse(
        create_server(keyed_settings(), **core.factories()),
        url="https://a.test",
        goal="g",
        max_steps=50,
        timeout_seconds=300,
    )
    assert (core.agents[0]["run"].max_steps, core.agents[0]["run"].timeout_s) == (50, 300.0)

    service = BrowseService(keyed_settings(), **core.factories())
    over = service.browse("https://a.test", "g", max_steps=51)
    assert over.status == "failed" and "от 1 до 50" in over.error
    assert len(core.agents) == 1


def test_lock_serializes_parallel_browse():
    gate = threading.Event()
    core = FakeCore(gate=gate)
    service = BrowseService(keyed_settings(), **core.factories())
    results = []
    first = threading.Thread(target=lambda: results.append(service.browse("https://1.test", "g")))
    second = threading.Thread(target=lambda: results.append(service.browse("https://2.test", "g")))

    first.start()
    while not core.started:
        time.sleep(0.005)
    assert core.started[0].wait(1)
    second.start()
    time.sleep(0.2)
    assert len(core.agents) == 1  # второй ждёт лок, агента ещё нет

    gate.set()
    first.join(2)
    second.join(2)
    assert [r.status for r in results] == ["done", "done"]
    assert [a["url"] for a in core.agents] == ["https://1.test", "https://2.test"]


def test_event_loop_stays_responsive_during_browse():
    gate = threading.Event()
    core = FakeCore(gate=gate)
    server = create_server(keyed_settings(), **core.factories())

    async def main() -> tuple[ListToolsResult, list[CallToolResult]]:
        results: list[CallToolResult] = []
        listed: list[ListToolsResult] = []
        async with create_connected_server_and_client_session(server) as client:
            async with anyio.create_task_group() as tg:

                async def browse():
                    results.append(await client.call_tool("browse", {"url": "https://a.test", "goal": "g"}))

                tg.start_soon(browse)
                while not core.started:
                    await anyio.sleep(0.005)
                with anyio.fail_after(2):
                    listed.append(await client.list_tools())  # browse висит в потоке, сервер отвечает
                assert len(results) == 0
                gate.set()
        return listed[0], results

    tools, results = anyio.run(main)
    assert [t.name for t in tools.tools] == ["browse"]
    assert text_of(results[0]).startswith("status: done")


def test_close_closes_resources_once_and_rejects_new_browse():
    core = FakeCore()
    service = BrowseService(keyed_settings(), **core.factories())
    service.browse("https://a.test", "g")

    service.close()
    service.close()

    assert core.chromes[0].close_calls == 1
    assert core.clients[0].close_calls == 1
    after = service.browse("https://a.test", "g")
    assert after.status == "failed" and after.error == "сервер закрывается"


def wait_for(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "условие не выполнилось"
        time.sleep(0.005)


def test_cancelled_waiting_browse_never_creates_agent():
    gate = threading.Event()
    core = FakeCore(gate=gate)
    service = BrowseService(keyed_settings(), **core.factories())
    cancel = threading.Event()
    results = {}
    first = threading.Thread(target=lambda: results.update(first=service.browse("https://1.test", "g")))
    second = threading.Thread(
        target=lambda: results.update(second=service.browse("https://2.test", "g", cancel=cancel))
    )

    first.start()
    wait_for(lambda: core.started and core.started[0].is_set())
    second.start()
    wait_for(lambda: len(service._active) == 2)  # второй ждёт лок
    cancel.set()  # Esc на вызове в очереди
    gate.set()
    first.join(2)
    second.join(2)

    assert results["first"].status == "done"
    assert (results["second"].status, results["second"].error) == ("failed", "cancelled")
    assert [a["url"] for a in core.agents] == ["https://1.test"]  # второй агент не создавался


def test_tool_cancel_sets_event_for_running_and_queued_calls():
    gate = threading.Event()
    core = FakeCore(gate=gate)
    service = BrowseService(keyed_settings(), **core.factories())
    finished = []
    browse = service.browse

    def spy(*args, **kwargs):
        result = browse(*args, **kwargs)
        finished.append(result)
        return result

    service.browse = spy  # брошенные потоки доработают после отмены — ловим их результат
    server = build_server(service)

    async def main():
        running, queued = anyio.CancelScope(), anyio.CancelScope()

        async def call(scope, url):
            with scope:
                await server.call_tool("browse", {"url": url, "goal": "g"})

        async with anyio.create_task_group() as tg:
            tg.start_soon(call, running, "https://1.test")
            while not core.started:
                await anyio.sleep(0.005)
            tg.start_soon(call, queued, "https://2.test")
            while len(service._active) < 2:
                await anyio.sleep(0.005)
            queued.cancel()  # отмена ждущего в очереди
            running.cancel()  # отмена идущего: агент встанет между шагами
        with anyio.fail_after(2):
            while len(finished) < 2:
                await anyio.sleep(0.005)

    anyio.run(main)

    assert core.agents[0]["cancel"].is_set()
    assert len(core.agents) == 1  # из очереди прогон не начался
    assert [(r.status, r.error) for r in finished] == [("failed", "cancelled")] * 2
    assert not gate.is_set()  # остановила отмена, а не конец прогона


def test_abort_cancels_and_closes_chrome_without_waiting_for_lock():
    gate = threading.Event()
    core = FakeCore(gate=gate)
    service = BrowseService(keyed_settings(), **core.factories())
    results = []
    running = threading.Thread(target=lambda: results.append(service.browse("https://1.test", "g")))
    running.start()
    wait_for(lambda: core.started and core.started[0].is_set())

    started = time.monotonic()
    service.abort()

    assert time.monotonic() - started < 0.5  # лок держит идущий прогон — abort его не ждёт
    assert core.chromes[0].close_calls == 1 and core.clients[0].close_calls == 1
    assert core.agents[0]["cancel"].is_set()
    running.join(2)
    assert (results[0].status, results[0].error) == ("failed", "cancelled")
    after = service.browse("https://a.test", "g")
    assert after.status == "failed" and after.error == "сервер закрывается"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com"),
        (" https://a.test/x ", "https://a.test/x"),
        ("HTTPS://A.test", "HTTPS://A.test"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("localhost:8000", "https://localhost:8000"),
        ("example.com:8080/path?q=1", "https://example.com:8080/path?q=1"),
        ("about:blank", "about:blank"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/passwd",
        "chrome://settings",
        "chrome-extension://abcdef/popup.html",
        "data:text/html,<b>x</b>",
        "javascript:alert(1)",
        "view-source:https://a.test",
        "about:settings",
        "ftp://a.test/file",
        "   ",
    ],
)
def test_other_schemes_fail_without_opening_tab(raw):
    with pytest.raises(UnsupportedUrl):
        normalize_url(raw)
    core = FakeCore()
    service = BrowseService(keyed_settings(), **core.factories())

    result = service.browse(raw, "g")

    assert result.status == "failed"
    assert result.error == "пустой url" or "только http://, https:// и about:blank" in result.error
    assert core.chromes == [] and core.agents == []  # ни Chrome, ни вкладки


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_info_log_has_only_host(level):
    package = logging.getLogger("browser_hands")
    handler, previous = Records(), package.level
    package.addHandler(handler)
    package.setLevel(level)
    try:
        url = "https://mail.example.test/inbox/42?user=ivan@example.test"
        BrowseService(keyed_settings(), **FakeCore().factories()).browse(url, "прочитать письмо Ивана")
    finally:
        package.removeHandler(handler)
        package.setLevel(previous)

    info = [r.getMessage() for r in handler.records if r.levelno >= logging.INFO]
    assert any("mail.example.test" in m for m in info)
    assert not any("ivan" in m or "/inbox" in m or "Ивана" in m for m in info)
    debug = [r.getMessage() for r in handler.records if r.levelno == logging.DEBUG]
    assert (url in "\n".join(debug)) is (level == logging.DEBUG)


def test_http_loggers_stay_quiet_with_debug_log():
    code = """
import logging
from browser_hands.config import Settings
from browser_hands.server import create_server
create_server(Settings())
logging.getLogger("hpack.hpack").debug("authorization: Bearer sk-test-secret")
names = ("hpack", "hpack.hpack", "h2", "httpcore", "httpx")
print(logging.getLogger().level, *(logging.getLogger(n).getEffectiveLevel() for n in names))
"""
    env = {**os.environ, "BROWSER_HANDS_LOG": "DEBUG"}
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True, timeout=30, check=True
    )

    root, *levels = map(int, done.stdout.split())
    assert root == logging.DEBUG  # FastMCP на DEBUG поставил DEBUG корневому логгеру
    assert all(level >= logging.WARNING for level in levels)
    assert "sk-test-secret" not in done.stderr


def test_format_result_marks_missing_parts():
    result = make_result("timeout", steps=1, screenshot=None, cost=None, error="дедлайн", tab_kept=True)
    text = format_result(result)

    assert text.splitlines()[:2] == ["status: timeout", "error: дедлайн"]
    assert "cost: n/a (model calls: 1)" in text
    assert "вкладка: новая, оставлена открытой" in text
    assert "tab: оставлена открытой" not in text  # старая строка
    assert "screenshot: нет" in text
    verbose = format_result(result, verbose=True)
    assert "1. CLICK Target 1  [0.7 с, conf 0.90, изменилась: да, https://example.test/1]" in verbose


@pytest.mark.parametrize(
    ("tab", "tab_kept", "url", "line"),
    [
        ("user", False, "https://web.whatsapp.com/", "вкладка: твоя (web.whatsapp.com)"),
        ("user", True, "https://Web.WhatsApp.com/x?chat=42", "вкладка: твоя (web.whatsapp.com)"),  # keep_open — мимо
        ("new", False, "https://example.test/done", "вкладка: новая"),
        ("new", True, "https://example.test/done", "вкладка: новая, оставлена открытой"),
        (None, False, "https://example.test/done", None),
    ],
)
def test_format_result_tab_line(tab, tab_kept, url, line):
    text = format_result(make_result(tab=tab, tab_kept=tab_kept, url=url))

    tab_lines = [s for s in text.splitlines() if s.startswith(("вкладка:", "tab:"))]
    assert tab_lines == ([] if line is None else [line])
    if line is not None:  # сразу за url/title — где работал агент
        assert text.splitlines()[text.splitlines().index(line) - 1] == "title: Example"


def test_failed_result_has_no_tab():
    result = failed_result("https://a.test", "нет ключа", time.monotonic())

    assert result.tab is None and result.tab_kept is False
    assert "вкладка:" not in format_result(result)


def test_user_tab_line_reaches_claude():
    core = FakeCore(result=make_result(tab="user", url="https://web.whatsapp.com/"))

    text = text_of(call_browse(create_server(keyed_settings(), **core.factories()), url="web.whatsapp.com", goal="g"))

    assert "\nвкладка: твоя (web.whatsapp.com)\n" in text
    assert core.agents[0]["run"].new_tab is False


def test_new_tab_is_per_call_and_does_not_stick():
    core = FakeCore()
    service = BrowseService(keyed_settings(), **core.factories())

    service.browse("https://a.test", "g", new_tab=True)
    service.browse("https://a.test", "g")  # None — из настроек

    assert [a["run"].new_tab for a in core.agents] == [True, False]
    assert service.settings.run.new_tab is False


# --- сценарий (steps) ------------------------------------------------------------------------------------------------

CHAT_STEPS = [
    {"do": "Type the chat name into the chat search box", "text": "Рабочий"},
    {"do": "Open the chat named «Рабочий»"},
    {"do": "Type the message into the message box", "text": "это я через агента, проверка 👋"},
    {"do": "Send the message"},
]


def list_browse_schema() -> dict:
    server = create_server(keyed_settings(), **FakeCore().factories())

    async def main():
        async with create_connected_server_and_client_session(server) as client:
            return await client.list_tools()

    (tool,) = anyio.run(main).tools
    return tool.inputSchema


def test_list_tools_describes_steps_with_scenario_limits():
    schema = list_browse_schema()

    steps = schema["properties"]["steps"]
    assert steps["description"] == STEPS_DESCRIPTION and len(STEPS_DESCRIPTION) <= 200
    assert "по-английски" in STEPS_DESCRIPTION and "дословно" in STEPS_DESCRIPTION
    assert "многошаговых" in STEPS_DESCRIPTION  # когда давать steps
    assert steps["default"] is None
    (array,) = [branch for branch in steps["anyOf"] if branch.get("type") == "array"]
    assert (array["minItems"], array["maxItems"]) == (1, MAX_STEPS) == (1, 20)
    step = schema["$defs"][array["items"]["$ref"].rsplit("/", 1)[1]]
    assert step["additionalProperties"] is False
    assert step["required"] == ["do"]
    assert (step["properties"]["do"]["minLength"], step["properties"]["do"]["maxLength"]) == (1, MAX_DO) == (1, 300)
    (text,) = [branch for branch in step["properties"]["text"]["anyOf"] if branch.get("type") == "string"]
    assert (text["minLength"], text["maxLength"]) == (1, MAX_TEXT) == (1, 2000)
    assert "по-английски" in step["properties"]["do"]["description"]
    assert "дословно" in step["properties"]["text"]["description"]
    assert "description" not in step  # docstring модели не раздувает tools/list
    assert "steps" not in schema["required"]


def test_steps_reach_agent_without_goal():
    core = FakeCore(result=make_result(scenario=(4, 4)))
    steps = [dict(step) for step in CHAT_STEPS]
    steps[1]["do"] = "  Open the chat named «Рабочий»  "  # do — без пробелов по краям, text — дословно

    result = call_browse(create_server(keyed_settings(), **core.factories()), url="https://a.test", steps=steps)

    assert result.isError is False
    agent = core.agents[0]
    assert agent["goal"] == ""
    assert agent["steps"] == [
        ScenarioStep("Type the chat name into the chat search box", "Рабочий"),
        ScenarioStep("Open the chat named «Рабочий»"),
        ScenarioStep("Type the message into the message box", "это я через агента, проверка 👋"),
        ScenarioStep("Send the message"),
    ]
    assert "\nсценарий: 4 из 4 выполнено\n" in text_of(result)


def test_goal_and_steps_together_reach_agent():
    core = FakeCore()

    call_browse(
        create_server(keyed_settings(), **core.factories()), url="https://a.test", goal="g", steps=CHAT_STEPS[:1]
    )

    assert core.agents[0]["goal"] == "g"
    assert core.agents[0]["steps"] == [ScenarioStep("Type the chat name into the chat search box", "Рабочий")]


def test_goal_mode_passes_no_steps_to_agent():
    core = FakeCore()

    call_browse(create_server(keyed_settings(), **core.factories()), url="https://a.test", goal="g")

    assert core.agents[0]["steps"] is None


@pytest.mark.parametrize("goal", [None, "", "   "])
def test_without_goal_and_steps_is_failed_before_chrome(goal):
    core = FakeCore()
    arguments = {} if goal is None else {"goal": goal}

    result = call_browse(create_server(keyed_settings(), **core.factories()), url="a.test", **arguments)

    assert result.isError is False
    assert text_of(result).startswith("status: failed\nerror: нужен goal или steps\nurl: https://a.test\n")
    assert core.chromes == [] and core.agents == []


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"do": "x"}] * (MAX_STEPS + 1), "steps: List should have at most 20 items"),
        ([], "steps: List should have at least 1 item"),
        ("Open the chat", "steps: Input should be a valid list"),
        (
            [{"do": "Open"}, {"do": "Send", "url": "x"}],
            "step 2 url: Extra inputs are not permitted (allowed: do, text)",
        ),
        ([{"do": ""}], "step 1 do: String should have at least 1 character"),
        ([{"do": "Open"}, {"do": " \n "}], "step 2 do: String should have at least 1 character"),
        ([{"text": "hi"}], "step 1 do: Field required"),
        ([{"do": "x" * (MAX_DO + 1)}], "step 1 do: String should have at most 300 characters"),
        ([{"do": "Type", "text": ""}], "step 1 text: String should have at least 1 character"),
        ([{"do": "Type", "text": "y" * (MAX_TEXT + 1)}], "step 1 text: String should have at most 2000 characters"),
        (["Open"], "step 1: Input should be a valid dictionary or instance of StepIn"),
    ],
)
def test_invalid_steps_are_short_tool_errors(steps, message):
    core = FakeCore()

    result = call_browse(create_server(keyed_settings(), **core.factories()), url="https://a.test", steps=steps)

    assert result.isError is True
    text = text_of(result)
    assert text.startswith("неверные аргументы browse — ") and message in text
    assert "pydantic" not in text and "input_value" not in text and "\n" not in text
    assert core.agents == [] and core.chromes == []


def test_steps_at_limits_reach_agent():
    core = FakeCore()
    steps = [{"do": "x" * MAX_DO, "text": " " + "y" * (MAX_TEXT - 2) + " "}] * MAX_STEPS

    result = call_browse(
        create_server(keyed_settings(), **core.factories()), url="https://a.test", steps=steps, max_steps=MAX_STEPS
    )

    assert result.isError is False
    assert len(core.agents[0]["steps"]) == MAX_STEPS
    assert core.agents[0]["steps"][0].text == " " + "y" * (MAX_TEXT - 2) + " "  # text не обрезается


def test_steps_above_max_steps_are_failed_before_chrome():
    core = FakeCore()

    result = call_browse(
        create_server(keyed_settings(), **core.factories()), url="https://a.test", steps=CHAT_STEPS, max_steps=3
    )

    assert text_of(result).startswith("status: failed\nerror: steps: шагов сценария 4, а max_steps=3\n")
    assert core.chromes == [] and core.agents == []

    settings = keyed_settings()
    settings.run.max_steps = 3  # без max_steps в вызове — лимит из настроек
    service = BrowseService(settings, **core.factories())
    four = [ScenarioStep(step["do"], step.get("text")) for step in CHAT_STEPS]
    assert service.browse("https://a.test", steps=four).error == "steps: шагов сценария 4, а max_steps=3"
    assert service.browse("https://a.test", steps=four, max_steps=4).status == "done"
    assert len(core.agents) == 1


def test_service_rechecks_steps_of_direct_callers():
    core = FakeCore()
    service = BrowseService(keyed_settings(), **core.factories())

    bad = service.browse("a.test", steps=[ScenarioStep("Open"), ScenarioStep("  ")])
    empty = service.browse("a.test", "g", steps=[])

    assert (bad.status, bad.error, bad.url) == ("failed", "step 2: do is empty", "https://a.test")
    assert (empty.status, empty.error) == ("failed", "steps: empty list")
    assert core.chromes == [] and core.agents == []
    service.browse("a.test", steps=[ScenarioStep("  Open  ", " hi ")])
    assert core.agents[0]["steps"] == [ScenarioStep("Open", " hi ")]


def test_blocked_scenario_names_the_step_it_stopped_on():
    core = FakeCore(result=make_result("blocked", steps=3, scenario=(2, 4), error="Model chose BLOCKED"))

    text = text_of(call_browse(create_server(keyed_settings(), **core.factories()), url="a.test", steps=CHAT_STEPS))

    lines = text.splitlines()
    assert lines[:4] == [
        "status: blocked",
        "error: Model chose BLOCKED",
        "сценарий: 2 из 4 выполнено",
        "остановился на шаге 3 из 4: Type the message into the message box",
    ]
    assert "1. CLICK Target 1 [шаг 1]" in lines
    assert '2. TYPE_TEXT Target 2 — "text 2" [шаг 2]' in lines
    assert "3. CLICK Target 3 [шаг 3]" in lines
    assert "steps: 3" in lines  # число действий — как в режиме цели


def test_format_result_scenario_lines():
    steps = [ScenarioStep(f"Step {i}") for i in range(1, 5)]
    done = format_result(make_result(scenario=(4, 4)), steps=steps)
    assert "сценарий: 4 из 4 выполнено" in done and "остановился" not in done

    timeout = format_result(make_result("timeout", steps=1, scenario=(0, 4)), steps=steps)
    assert "\nостановился на шаге 1 из 4: Step 1\n" in timeout

    unknown = format_result(make_result("step_limit", steps=1, scenario=(1, 4)))  # сценарий вызова не передан
    assert "\nостановился на шаге 2 из 4\n" in unknown

    verbose = format_result(make_result("blocked", steps=1, scenario=(0, 4)), verbose=True, steps=steps)
    assert "1. CLICK Target 1 [шаг 1]  [0.7 с, conf 0.90, изменилась: да, https://example.test/1]" in verbose


GOAL_TEXT = (
    "status: done\nurl: https://example.test/done\ntitle: Example\nвкладка: новая\nsteps: 2\n1. CLICK Target 1\n"
    '2. TYPE_TEXT Target 2 — "text 2"\ncost: $0.0042 (model calls: 2)\n'
    "elapsed 1.7s (model 1.2 / text 0.3 / browser 0.1 / wait 0.1)"
)
GOAL_VERBOSE = (
    "status: done\nurl: https://example.test/done\ntitle: Example\nвкладка: новая\nsteps: 2\n"
    "1. CLICK Target 1  [0.7 с, conf 0.90, изменилась: да, https://example.test/1]\n"
    '2. TYPE_TEXT Target 2 — "text 2"  [1.0 с, conf 0.90, изменилась: да, https://example.test/2]\n'
    "cost: $0.0042 (model calls: 2)\nelapsed 1.7s (model 1.2 / text 0.3 / browser 0.1 / wait 0.1)"
)


def test_goal_mode_text_is_unchanged():
    """Режим цели — вывод байт в байт как до сценариев (снято с format_result на dc1da4e)."""
    result = make_result()
    assert format_result(result) == GOAL_TEXT
    assert format_result(result, verbose=True) == GOAL_VERBOSE
    assert format_result(result, steps=[ScenarioStep("x")]) == GOAL_TEXT  # сценарий без scenario_total не влияет


def test_default_agent_factory_passes_steps_only_to_a_scenario(monkeypatch):
    calls: list[dict] = []

    class Agent:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(server_module, "import_core", lambda name: type("Core", (), {"Agent": Agent}))
    common = {"screenshot_quality": 60, "screenshot_scale": 1.0, "cancel": threading.Event()}
    steps = [ScenarioStep("Send the message")]

    default_agent_factory(None, None, "https://a.test", "g", RunConfig(), **common)  # type: ignore[arg-type]
    default_agent_factory(None, None, "https://a.test", "", RunConfig(), steps=steps, **common)  # type: ignore[arg-type]

    assert "steps" not in calls[0]  # ядро без сценариев (до слияния) зовётся как раньше
    assert calls[1]["steps"] == steps


def test_info_log_has_no_step_texts():
    package = logging.getLogger("browser_hands")
    handler, previous = Records(), package.level
    package.addHandler(handler)
    package.setLevel(logging.INFO)
    try:
        steps = [ScenarioStep("Type the secret", "секретный текст"), ScenarioStep("Send")]
        BrowseService(keyed_settings(), **FakeCore().factories()).browse("https://a.test/x", steps=steps)
    finally:
        package.removeHandler(handler)
        package.setLevel(previous)

    info = "\n".join(r.getMessage() for r in handler.records)
    assert "сценарий из 2" in info and "a.test" in info
    assert "секретный" not in info and "Type the secret" not in info
