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

from browser_hands.config import RunConfig, Settings
from browser_hands.server import (
    BROWSE_DESCRIPTION,
    INSTRUCTIONS,
    BrowseService,
    UnsupportedUrl,
    build_server,
    create_server,
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
    assert schema["required"] == ["url", "goal"]
    assert schema["properties"]["max_steps"]["default"] == 7
    assert schema["properties"]["max_steps"]["maximum"] == 50
    assert schema["properties"]["timeout_seconds"]["default"] == 90.0
    assert schema["properties"]["timeout_seconds"]["maximum"] == 300
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
    )

    agent = core.agents[0]
    assert agent["url"] == "https://example.com/path"
    assert agent["goal"] == "найти"
    assert agent["run"] == RunConfig(max_steps=3, timeout_s=5.0, keep_open=True)
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
    assert "tab: оставлена открытой" in text
    assert "screenshot: нет" in text
    verbose = format_result(result, verbose=True)
    assert "1. CLICK Target 1  [0.7 с, conf 0.90, изменилась: да, https://example.test/1]" in verbose
