"""Режимы attach/launch/ws без настоящего Chrome: временные каталоги, фейковый Popen и фейковый CDP-сервер."""

import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from browser_hands import browser as browser_module
from browser_hands import chrome as chrome_module
from browser_hands.cdp import CDPError, ChromeDisconnected
from browser_hands.chrome import (
    AMBIGUOUS,
    BUSY,
    Chrome,
    ChromeUnavailable,
    TabTaken,
    UserTabUnavailable,
    launch_chrome,
    read_devtools_active_port,
    resolve_ws_url,
    same_page,
    site_label,
    web_origin,
)
from browser_hands.config import BrowserConfig
from tests.fake_cdp import FakeCDPServer, error, reply


def write_port_file(data_dir: Path, port: int, path: str = "/devtools/browser/fake") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "DevToolsActivePort").write_text(f"{port}\n{path}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def server():
    with FakeCDPServer() as s:
        yield s


def attach_config(tmp_path: Path, **kw) -> BrowserConfig:
    return BrowserConfig(mode="attach", chrome_data_dir=tmp_path / "Chrome", connect_timeout_s=2.0, **kw)


def test_devtools_active_port_becomes_ws_url(tmp_path):
    write_port_file(tmp_path, 9222, "/devtools/browser/abc-123")
    assert read_devtools_active_port(tmp_path) == "ws://127.0.0.1:9222/devtools/browser/abc-123"


@pytest.mark.parametrize("content", [None, "", "9222", "port\n/devtools/browser/x", "9222\n/json/version"])
def test_missing_or_broken_port_file_is_chrome_unavailable(tmp_path, content):
    if content is not None:
        (tmp_path / "DevToolsActivePort").write_text(content)
    with pytest.raises(ChromeUnavailable, match="chrome://inspect/#remote-debugging"):
        read_devtools_active_port(tmp_path)


def test_ws_url_has_priority_and_mode_selects_profile(tmp_path):
    write_port_file(tmp_path / "user", 9222)
    write_port_file(tmp_path / "own", 9333)
    cfg = BrowserConfig(mode="attach", chrome_data_dir=tmp_path / "user", launch_data_dir=tmp_path / "own")
    assert resolve_ws_url(cfg).startswith("ws://127.0.0.1:9222/")
    cfg.mode = "launch"
    assert resolve_ws_url(cfg).startswith("ws://127.0.0.1:9333/")
    cfg.ws_url = "ws://127.0.0.1:1/devtools/browser/given"
    assert resolve_ws_url(cfg) == "ws://127.0.0.1:1/devtools/browser/given"


def test_launch_removes_stale_port_file_and_waits_for_the_new_one(tmp_path, monkeypatch):
    data_dir = tmp_path / "profile"
    write_port_file(data_dir, 1111, "/devtools/browser/stale")
    seen = {}

    def fake_popen(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        seen["stale_left"] = (data_dir / "DevToolsActivePort").exists()
        write_port_file(data_dir, 4444, "/devtools/browser/new")
        return FakeProc()

    monkeypatch.setattr(chrome_module.subprocess, "Popen", fake_popen)
    cfg = BrowserConfig(mode="launch", launch_data_dir=data_dir, headless=True, connect_timeout_s=2.0)
    launch_chrome(cfg)
    assert not seen["stale_left"]
    assert resolve_ws_url(cfg) == "ws://127.0.0.1:4444/devtools/browser/new"
    args = seen["args"]
    assert f"--user-data-dir={data_dir}" in args and "--remote-debugging-port=0" in args
    assert "--headless=new" in args and args[-1] == "about:blank"
    assert seen["kwargs"]["stdout"] is subprocess.DEVNULL  # stdout занят MCP stdio


def test_launched_chrome_gets_no_api_keys_in_env(tmp_path, monkeypatch):
    for name in ("OPENROUTER_API_KEY", "BROWSER_HANDS_JEV_API_KEY", "BROWSER_HANDS_TEXT_API_KEY", "openai_api_key"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("BROWSER_HANDS_MODE", "launch")
    seen = {}

    def fake_popen(args, **kwargs):
        seen.update(kwargs)
        write_port_file(tmp_path, 4444)
        return FakeProc()

    monkeypatch.setattr(chrome_module.subprocess, "Popen", fake_popen)
    launch_chrome(BrowserConfig(mode="launch", launch_data_dir=tmp_path, connect_timeout_s=2.0))
    env = seen["env"]
    assert "secret" not in env.values()
    assert env["BROWSER_HANDS_MODE"] == "launch" and env["PATH"] == os.environ["PATH"]
    assert os.environ["OPENROUTER_API_KEY"] == "secret"  # своё окружение не тронуто


def test_launch_refuses_a_profile_held_by_a_live_chrome(tmp_path, monkeypatch):
    write_port_file(tmp_path, 5555)
    (tmp_path / "SingletonLock").symlink_to(f"host.local-{os.getpid()}")  # живой процесс
    popen = []
    monkeypatch.setattr(chrome_module.subprocess, "Popen", lambda *a, **k: popen.append(a))
    cfg = BrowserConfig(mode="launch", launch_data_dir=tmp_path, connect_timeout_s=2.0)
    with pytest.raises(ChromeUnavailable, match=f"pid {os.getpid()}"):
        launch_chrome(cfg)
    assert popen == []
    assert (tmp_path / "DevToolsActivePort").exists()  # порт чужого Chrome не стёрт


def test_stale_singleton_lock_does_not_block_launch(tmp_path, monkeypatch):
    dead = subprocess.Popen(["true"])
    dead.wait()
    (tmp_path / "SingletonLock").symlink_to(f"host.local-{dead.pid}")

    def fake_popen(args, **kwargs):
        write_port_file(tmp_path, 4444)
        return FakeProc()

    monkeypatch.setattr(chrome_module.subprocess, "Popen", fake_popen)
    launch_chrome(BrowserConfig(mode="launch", launch_data_dir=tmp_path, connect_timeout_s=2.0))
    assert read_devtools_active_port(tmp_path).startswith("ws://127.0.0.1:4444/")


def test_launch_reports_early_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome_module.subprocess, "Popen", lambda *a, **k: FakeProc(returncode=0))
    cfg = BrowserConfig(mode="launch", launch_data_dir=tmp_path, connect_timeout_s=2.0)
    with pytest.raises(ChromeUnavailable, match="завершился"):
        launch_chrome(cfg)


def test_attach_close_closes_only_own_tabs_and_leaves_browser_alone(tmp_path, server):
    cfg = attach_config(tmp_path)
    write_port_file(cfg.chrome_data_dir, server.port)
    launched = []
    chrome = Chrome(cfg, launcher=lambda c: launched.append(c))
    chrome.connect()
    assert chrome.alive()
    tab = chrome.new_tab()
    assert tab.target_id in chrome.owned_targets
    create = next(f for f in server.frames if f["method"] == "Target.createTarget")
    assert create["params"] == {"url": "about:blank", "background": True}
    attach = next(f for f in server.frames if f["method"] == "Target.attachToTarget")
    assert attach["params"] == {"targetId": tab.target_id, "flatten": True}
    emulation = [f for f in server.frames if f["method"].startswith("Emulation.")]
    assert [f["sessionId"] for f in emulation] == [tab.session_id, tab.session_id]
    assert emulation[0]["params"]["width"] == 1120 and emulation[0]["params"]["height"] == 780

    chrome.close()
    # Только своя вкладка (id из своего createTarget); вкладки пользователя и сам браузер не трогаем.
    closes = [f["params"]["targetId"] for f in server.frames if f["method"] == "Target.closeTarget"]
    assert closes == [tab.target_id]
    assert "Browser.close" not in server.methods()
    assert chrome.owned_targets == set()
    assert launched == []
    assert not chrome.alive()


def test_connect_is_idempotent_and_reconnects_when_dead(tmp_path, server):
    cfg = attach_config(tmp_path)
    write_port_file(cfg.chrome_data_dir, server.port)
    created = []

    def factory(url, **kw):
        created.append(url)
        return chrome_module.CDPClient(url, **kw)

    chrome = Chrome(cfg, client_factory=factory)
    chrome.connect()
    chrome.connect()
    assert len(created) == 1

    chrome.client.close()  # соединение умерло
    assert not chrome.alive()
    chrome.connect()
    assert len(created) == 2 and chrome.alive()
    chrome.close()


def test_reconnect_rereads_port_file_after_chrome_restart(tmp_path):
    cfg = attach_config(tmp_path)
    with FakeCDPServer() as first:
        write_port_file(cfg.chrome_data_dir, first.port)
        chrome = Chrome(cfg)
        chrome.connect()
    with FakeCDPServer() as second:
        write_port_file(cfg.chrome_data_dir, second.port)
        chrome.connect()
        assert chrome.alive() and second.connections == 1
        chrome.close()


def test_attach_refused_is_chrome_unavailable_without_launch(tmp_path):
    cfg = attach_config(tmp_path)
    write_port_file(cfg.chrome_data_dir, free_port())
    launched = []
    chrome = Chrome(cfg, launcher=lambda c: launched.append(c))
    with pytest.raises(ChromeUnavailable, match="не запущен или отладка выключена"):
        chrome.connect()
    assert launched == []


def test_attach_without_port_file_is_chrome_unavailable(tmp_path):
    with pytest.raises(ChromeUnavailable, match="BROWSER_HANDS_MODE=launch"):
        Chrome(attach_config(tmp_path)).connect()


def launch_chrome_on(server, tmp_path, proc, **kw) -> Chrome:
    cfg = BrowserConfig(mode="launch", launch_data_dir=tmp_path / "own", connect_timeout_s=2.0, **kw)

    def launcher(c):
        write_port_file(c.launch_data_dir, server.port)
        return proc

    return Chrome(cfg, launcher=launcher)


def test_launch_close_stops_process_without_closing_tabs_one_by_one(tmp_path, server):
    proc = FakeProc()
    chrome = launch_chrome_on(server, tmp_path, proc)
    chrome.connect()
    chrome.new_tab()
    chrome.close()
    assert "Target.closeTarget" not in server.methods()  # вкладки умирают вместе с процессом
    assert proc.terminated and chrome.owned_targets == set()


def test_launch_close_with_a_busy_worker_takes_about_a_second_at_most(tmp_path, server):
    proc = FakeProc()
    chrome = launch_chrome_on(server, tmp_path, proc, call_timeout_s=30.0)
    chrome.connect()
    tab = chrome.new_tab()
    server.on["Runtime.evaluate"] = lambda f, ws: None  # рабочий поток ждёт ответа до call_timeout_s
    outcome = []

    def worker():
        try:
            tab.evaluate("1")
        except Exception as exc:
            outcome.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while "Runtime.evaluate" not in server.methods():
        time.sleep(0.01)
    started = time.monotonic()
    chrome.close()
    assert time.monotonic() - started < 1.0
    assert proc.terminated
    thread.join(timeout=2)
    assert not thread.is_alive() and isinstance(outcome[0], ChromeDisconnected)


def test_orphan_tab_from_dead_connection_is_closed_on_reconnect(tmp_path, server):
    cfg = attach_config(tmp_path)
    write_port_file(cfg.chrome_data_dir, server.port)
    chrome = Chrome(cfg)
    chrome.connect()
    tab = chrome.new_tab()
    chrome.client.close()  # обрыв: вкладку не закрыть
    tab.close()
    assert tab.target_id in chrome.owned_targets
    chrome.connect()
    closes = [f["params"]["targetId"] for f in server.frames if f["method"] == "Target.closeTarget"]
    assert closes == [tab.target_id]
    assert chrome.owned_targets == set()
    chrome.close()


def test_orphan_is_closed_by_connect_while_the_connection_is_alive(tmp_path, server, monkeypatch):
    monkeypatch.setattr(browser_module, "SCREENSHOT_TIMEOUT_S", 0.2)
    cfg = attach_config(tmp_path)
    write_port_file(cfg.chrome_data_dir, server.port)
    chrome = Chrome(cfg)
    chrome.connect()
    tab = chrome.new_tab()
    server.on["Target.closeTarget"] = lambda f, ws: None  # Chrome не ответил: вкладка могла остаться
    tab.close()
    assert tab.target_id in chrome.owned_targets
    del server.on["Target.closeTarget"]
    chrome.connect()  # соединение живо — всё равно закрываем свою сироту
    closes = [f["params"]["targetId"] for f in server.frames if f["method"] == "Target.closeTarget"]
    assert closes == [tab.target_id, tab.target_id]
    assert chrome.owned_targets == set()
    chrome.close()


def test_connect_does_not_close_a_tab_still_in_use(tmp_path, server):
    cfg = attach_config(tmp_path)
    write_port_file(cfg.chrome_data_dir, server.port)
    chrome = Chrome(cfg)
    chrome.connect()
    tab = chrome.new_tab()
    chrome.connect()
    assert "Target.closeTarget" not in server.methods()
    assert tab.target_id in chrome.owned_targets
    chrome.close()


def test_attach_close_gives_up_on_a_silent_tab_in_about_a_second(tmp_path, server):
    cfg = attach_config(tmp_path)
    write_port_file(cfg.chrome_data_dir, server.port)
    chrome = Chrome(cfg)
    chrome.connect()
    tab = chrome.new_tab()
    server.on["Target.closeTarget"] = lambda f, ws: None
    started = time.monotonic()
    chrome.close()
    assert time.monotonic() - started < 2.0
    assert not chrome.alive()
    assert tab.target_id in chrome.owned_targets  # не закрыта: следующий connect() попробует снова


def test_closed_and_released_tabs_are_forgotten(tmp_path, server):
    cfg = attach_config(tmp_path)
    write_port_file(cfg.chrome_data_dir, server.port)
    chrome = Chrome(cfg)
    chrome.connect()
    closed, kept = chrome.new_tab(), chrome.new_tab()
    closed.close()
    kept.release()
    assert chrome.owned_targets == set()
    assert "Target.detachFromTarget" in server.methods()
    assert [f["params"]["targetId"] for f in server.frames if f["method"] == "Target.closeTarget"] == [closed.target_id]
    chrome.close()


# --- вкладка пользователя (attach) -----------------------------------------------------------------------------


def user_target(target_id: str, url: str, **kw) -> dict:
    """TargetInfo как у Chrome 154 (поля проверены живьём): вкладка без клиентов — `attached: False`."""
    return {"targetId": target_id, "type": "page", "title": "", "url": url, "attached": False, **kw}


def attached_chrome(tmp_path, server, **kw) -> Chrome:
    cfg = attach_config(tmp_path, **kw)
    write_port_file(cfg.chrome_data_dir, server.port)
    chrome = Chrome(cfg, launcher=lambda c: pytest.fail("attach не запускает Chrome"))
    chrome.connect()
    return chrome


def measure_as(server, value):
    """Runtime.evaluate замера окна вкладки пользователя: [innerWidth, innerHeight, devicePixelRatio]; метки — фейк."""
    return lambda f, ws: server.page_js(f, ws) or reply(ws, f, {"result": {"type": "object", "value": value}})


def info_lines(caplog, action):
    """INFO-строки browser_hands.chrome за время `action()`; пакетный логгер может не передавать записи корню."""
    logger = logging.getLogger("browser_hands.chrome")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="browser_hands.chrome"):
            caplog.clear()
            action()
    finally:
        logger.removeHandler(caplog.handler)
    lines = [r.getMessage() for r in caplog.records if r.name == "browser_hands.chrome" and r.levelno == logging.INFO]
    return list(dict.fromkeys(lines))  # запись может прийти дважды: handler на логгере и корень


def test_find_user_tab_matches_host_only_pages_and_skips_own_and_attached(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    own = chrome.new_tab()
    server.targets = [
        user_target("iframe", "https://web.whatsapp.com/frame", type="iframe"),
        user_target(own.target_id, "https://web.whatsapp.com/"),
        user_target("busy", "https://web.whatsapp.com/", attached=True),
        user_target("prerender", "https://web.whatsapp.com/", subtype="prerender"),
        user_target("other", "https://example.test/"),
        user_target("U1", "https://WEB.whatsapp.com/x"),
        user_target("U2", "https://web.whatsapp.com/y"),
    ]
    assert chrome.find_user_tab("https://web.whatsapp.com") == "U1"
    lookup = server.sent("Target.getTargets")[-1]
    assert "sessionId" not in lookup and lookup["params"] == {}
    chrome.close()


def test_find_user_tab_skips_service_pages_and_needs_a_web_url(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.targets = [
        user_target("newtab", "chrome://newtab/"),
        user_target("blank", "about:blank"),
        user_target("data", "data:text/html,<p>x</p>"),
        user_target("devtools", "devtools://devtools/bundled/inspector.html"),
    ]
    calls = server.methods().count("Target.getTargets")
    for url in ("about:blank", "chrome://newtab/", "data:text/html,x", "web.whatsapp.com", "file:///tmp/x.html"):
        assert chrome.find_user_tab(url) is None
    assert server.methods().count("Target.getTargets") == calls  # не веб-адрес: даже не спрашиваем Chrome
    chrome.close()


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Example.com/x", ("example.com", 443)),
        ("http://example.com", ("example.com", 80)),
        ("https://example.com:443/", ("example.com", 443)),
        ("https://example.com:8443/", ("example.com", 8443)),
        ("http://localhost:5173/", ("localhost", 5173)),
        ("http://localhost:99999/", None),  # ValueError от .port
        ("http://localhost:abc/", None),
        ("about:blank", None),
        ("chrome://newtab/", None),
    ],
)
def test_web_origin_is_host_with_the_effective_port(url, origin):
    assert web_origin(url) == origin


def test_site_label_and_same_page_use_the_effective_port():
    assert site_label("https://web.whatsapp.com/?chat=secret") == "web.whatsapp.com"
    assert site_label("http://localhost:5173/app") == "localhost:5173"
    assert site_label("https://example.com:443/") == "example.com"
    assert same_page("https://example.com:443/x#a", "https://EXAMPLE.com/x")
    assert not same_page("https://example.com:8443/x", "https://example.com/x")
    assert not same_page("http://example.com/x", "https://example.com/x")
    assert not same_page("http://x:99999/", "http://x:99999/")


def test_find_user_tab_compares_host_with_the_effective_port(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.targets = [
        user_target("APP3000", "http://localhost:3000/"),
        user_target("PROD8443", "https://example.com:8443/"),
        user_target("PLAIN_HTTP", "http://example.com/"),
        user_target("BAD_PORT", "http://example.com:99999/"),  # ValueError от .port: не подходит, без исключения
    ]
    assert chrome.find_user_tab("http://localhost:5173/") is None
    assert chrome.find_user_tab("https://example.com/") is None
    server.targets += [user_target("DEV", "http://localhost:5173/"), user_target("SITE", "https://example.com/")]
    assert chrome.find_user_tab("http://localhost:5173") == "DEV"
    assert chrome.find_user_tab("https://example.com:443/") == "SITE"
    calls = server.methods().count("Target.getTargets")
    assert chrome.find_user_tab("http://example.com:99999/") is None  # неверный порт в url: Chrome не спрашиваем
    assert server.methods().count("Target.getTargets") == calls
    chrome.close()


def test_user_tab_only_for_the_site_root_or_the_same_page(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.targets = [
        user_target("A", "https://en.wikipedia.org/wiki/Main_Page"),
        user_target("B", "https://en.wikipedia.org/wiki/Kurt_G%C3%B6del#Life"),
    ]
    assert chrome.find_user_tab("https://en.wikipedia.org/wiki/Kurt_G%C3%B6del") == "B"  # та же, без #fragment
    assert chrome.find_user_tab("https://en.wikipedia.org/wiki/Other") is None  # сайт открыт, страница другая
    assert chrome.find_user_tab("https://en.wikipedia.org/wiki/Main_Page?action=edit") is None  # другой query
    assert chrome.find_user_tab("https://en.wikipedia.org/") == "A"  # только сайт: любая вкладка, первая по порядку
    assert chrome.find_user_tab("https://en.wikipedia.org") == "A"
    assert chrome.find_user_tab("https://en.wikipedia.org/?search=x") is None  # с query — уже не корень
    server.targets.append(user_target("ROOT", "https://en.wikipedia.org/#top"))
    assert chrome.find_user_tab("https://en.wikipedia.org/") == "ROOT"  # корень: точное совпадение первым
    server.targets = [user_target("C", "https://web.whatsapp.com/")]
    assert chrome.find_user_tab("https://web.whatsapp.com") == "C"  # пустой путь = /
    chrome.close()


def test_find_user_tab_logs_one_host_only_reason(tmp_path, server, caplog):
    chrome = attached_chrome(tmp_path, server)
    server.targets = [user_target("A", "https://en.wikipedia.org/wiki/Main_Page")]
    cases = [
        ("https://en.wikipedia.org/wiki/Secret_Page?q=1", None, "открыт на другой странице"),
        ("https://mail.example/secret", None, "нет"),
        ("https://en.wikipedia.org/", "A", "задан только сайт"),
        ("https://en.wikipedia.org/wiki/Main_Page", "A", "та же страница"),
    ]
    for url, target, reason in cases:
        found = []
        lines = info_lines(caplog, lambda url=url: found.append(chrome.find_user_tab(url)))
        assert found == [target] and len(lines) == 1 and reason in lines[0], lines
        assert all(part not in lines[0] for part in ("/wiki", "Secret", "secret", "q=1")), lines
    chrome.close()


def test_all_matching_tabs_busy_is_an_error_not_an_own_tab(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    no_field = {k: v for k, v in user_target("unknown", "https://web.whatsapp.com/").items() if k != "attached"}
    server.targets = [user_target("busy", "https://web.whatsapp.com/?chat=secret", attached=True), no_field]
    with pytest.raises(UserTabUnavailable) as busy:
        chrome.find_user_tab("https://web.whatsapp.com/")
    assert str(busy.value) == BUSY.format(site="web.whatsapp.com")
    assert "занята другим клиентом" in str(busy.value) and "new_tab=true" in str(busy.value)
    assert "secret" not in str(busy.value)
    server.targets.append(user_target("free", "https://web.whatsapp.com/"))
    assert chrome.find_user_tab("https://web.whatsapp.com/") == "free"  # свободная рядом — она
    with pytest.raises(UserTabUnavailable, match="занята"):
        chrome.find_user_tab("https://web.whatsapp.com/", skip={"free"})  # чужая метка __bhOwner — тоже занята
    server.targets = [user_target("busy", "https://en.wikipedia.org/wiki/A", attached=True)]
    assert chrome.find_user_tab("https://en.wikipedia.org/wiki/B") is None  # занята не та страница: своя вкладка
    assert "Target.attachToTarget" not in server.methods()
    chrome.close()


def test_tabs_in_several_profiles_need_exactly_one_matching_url(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    main = [user_target("M1", "https://mail.example/inbox", browserContextId="CTX-MAIN")]
    incognito = [user_target("I1", "https://mail.example/inbox", browserContextId="CTX-INCOGNITO")]
    server.targets = main + incognito
    with pytest.raises(UserTabUnavailable) as ambiguous:
        chrome.find_user_tab("https://mail.example/")
    assert str(ambiguous.value) == AMBIGUOUS.format(site="mail.example")
    assert "нескольких профилях/окнах инкогнито" in str(ambiguous.value) and "new_tab=true" in str(ambiguous.value)
    server.targets = main + incognito + [user_target("I2", "https://mail.example/#x", browserContextId="CTX-INCOGNITO")]
    assert chrome.find_user_tab("https://mail.example/") == "I2"  # точное совпадение одно — его
    server.targets.append(user_target("M2", "https://mail.example/", browserContextId="CTX-MAIN"))
    with pytest.raises(UserTabUnavailable, match="профилях"):
        chrome.find_user_tab("https://mail.example/")  # два точных в разных профилях
    with pytest.raises(UserTabUnavailable, match="профилях"):
        chrome.find_user_tab("https://mail.example/inbox")
    server.targets = main + [user_target("M3", "https://mail.example/x", browserContextId="CTX-MAIN")]
    server.targets.append(user_target("I3", "https://mail.example/", browserContextId="CTX-INCOGNITO", attached=True))
    assert chrome.find_user_tab("https://mail.example/") == "M1"  # свободные — из одного профиля: первая
    chrome.close()


def test_find_user_tab_is_none_in_launch_and_ws_modes(tmp_path, server):
    server.targets = [user_target("U1", "https://web.whatsapp.com/")]
    launched = launch_chrome_on(server, tmp_path, FakeProc())
    ws = Chrome(BrowserConfig(mode="attach", ws_url=server.url, connect_timeout_s=2.0))
    for chrome in (launched, ws):
        chrome.connect()
        calls = server.methods().count("Target.getTargets")  # один — из alive()/connect()
        assert chrome.find_user_tab("https://web.whatsapp.com/") is None
        assert server.methods().count("Target.getTargets") == calls
        with pytest.raises(ValueError, match="attach без ws_url"):
            chrome.attach_tab("U1")
        chrome.close()
    assert "Target.attachToTarget" not in server.methods()


def test_attach_tab_sends_no_create_no_metrics_and_close_never_closes_it(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.targets = [user_target("U1", "https://web.whatsapp.com/")]
    server.on["Runtime.evaluate"] = measure_as(server, [1728, 1000, 2])
    tab = chrome.attach_tab(chrome.find_user_tab("https://web.whatsapp.com/") or "")
    assert not tab.owned and tab.target_id == "U1" and tab.session_id == "S-U1"
    assert tab.viewport == (1728, 1000) and tab.dpr == 2.0
    assert [f["params"] for f in server.sent("Target.attachToTarget")] == [{"targetId": "U1", "flatten": True}]
    focus = server.sent("Emulation.setFocusEmulationEnabled")
    assert [(f["params"], f["sessionId"]) for f in focus] == [({"enabled": True}, "S-U1")]
    for method in ("Target.createTarget", "Emulation.setDeviceMetricsOverride", "Page.navigate", "Page.reload"):
        assert method not in server.methods()
    assert "U1" not in chrome.owned_targets

    chrome.close()
    focus = server.sent("Emulation.setFocusEmulationEnabled")
    assert [(f["params"], f["sessionId"]) for f in focus][-1] == ({"enabled": False}, "S-U1")
    assert [f["params"] for f in server.sent("Target.detachFromTarget")] == [{"sessionId": "S-U1"}]
    assert "Target.closeTarget" not in server.methods() and "Browser.close" not in server.methods()
    assert chrome.owned_targets == set() and tab.closed


def test_borrowed_and_own_tabs_close_separately(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.targets = [user_target("U1", "https://web.whatsapp.com/")]
    own = chrome.new_tab()
    borrowed = chrome.attach_tab("U1")
    borrowed.close()  # агент по ошибке зовёт close(): для вкладки пользователя это release()
    chrome.connect()  # живое соединение: уборка сирот
    chrome.close()
    assert [f["params"]["targetId"] for f in server.sent("Target.closeTarget")] == [own.target_id]
    assert [f["params"] for f in server.sent("Target.detachFromTarget")] == [{"sessionId": "S-U1"}]


def test_attach_tab_refuses_an_own_tab(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    own = chrome.new_tab()
    with pytest.raises(ValueError, match="своя вкладка"):
        chrome.attach_tab(own.target_id)
    chrome.close()


def test_reconnect_does_not_close_a_borrowed_tab(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.targets = [user_target("U1", "https://web.whatsapp.com/")]
    tab = chrome.attach_tab("U1")
    chrome.client.close()  # обрыв ws посреди прогона
    chrome.connect()
    assert chrome._borrowed == {} and chrome.owned_targets == set()
    tab.close()  # агент доделывает _finish по мёртвому соединению — без исключений
    chrome.close()
    assert "Target.closeTarget" not in server.methods()


def test_attach_tab_failure_detaches_and_raises(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.targets = [user_target("U1", "https://web.whatsapp.com/")]
    server.on["Emulation.setFocusEmulationEnabled"] = lambda f, ws: error(ws, f, "Not allowed")
    with pytest.raises(CDPError, match="Not allowed"):
        chrome.attach_tab("U1")
    assert [f["params"] for f in server.sent("Target.detachFromTarget")] == [{"sessionId": "S-U1"}]
    assert "Target.closeTarget" not in server.methods()
    assert chrome._borrowed == {}
    chrome.close()
    assert "Target.closeTarget" not in server.methods()


def test_attach_tab_survives_a_page_that_is_still_loading(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.on["Runtime.evaluate"] = lambda f, ws: reply(
        ws, f, {"result": {"type": "undefined"}, "exceptionDetails": {"text": "Execution context was destroyed"}}
    )
    tab = chrome.attach_tab("U1")  # пользователь только что нажал F5: замер не удался — не повод падать
    assert tab.viewport == (1120, 780) and tab.dpr == 1.0
    chrome.close()


def test_close_with_a_busy_worker_detaches_borrowed_in_about_a_second(tmp_path, server):
    chrome = attached_chrome(tmp_path, server, call_timeout_s=30.0)
    tab = chrome.attach_tab("U1")
    server.on["Runtime.evaluate"] = lambda f, ws: None  # рабочий поток ждёт ответа до call_timeout_s
    outcome = []

    def worker():
        try:
            tab.evaluate("1")
        except Exception as exc:
            outcome.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while server.methods().count("Runtime.evaluate") < 3:  # метка и замер при attach + вызов рабочего потока
        time.sleep(0.01)
    started = time.monotonic()
    chrome.close()
    assert time.monotonic() - started < 2.0  # лок занят: release ждёт ≤1 с на оба вызова, затем ws закрыт
    thread.join(timeout=2)
    assert not thread.is_alive() and isinstance(outcome[0], ChromeDisconnected)
    assert "Target.closeTarget" not in server.methods() and tab.closed


# --- мьютекс вкладки пользователя: window.__bhOwner ------------------------------------------------------------


def test_attach_tab_claims_the_tab_right_after_attaching_and_release_removes_only_own_marks(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    tab = chrome.attach_tab("U1")
    methods = server.methods()
    claim = server.frames[methods.index("Target.attachToTarget") + 1]
    assert claim["method"] == "Runtime.evaluate" and claim["sessionId"] == "S-U1"
    assert claim["params"] == {"expression": f'window.__bhOwner ??= "{chrome.owner_id}"', "returnByValue": True}
    assert methods.index("Runtime.evaluate") < methods.index("Emulation.setFocusEmulationEnabled")
    assert server.windows["U1"] == {"__bhOwner": chrome.owner_id}
    server.windows["U1"]["__jevFast"] = "snapshot cache"
    tab.release()
    assert server.windows["U1"] == {}  # свои метка и кэш сняты
    cleanup = [f for f in server.sent("Runtime.evaluate") if f["params"]["expression"].startswith("delete")]
    assert len(cleanup) == 1  # одним вызовом
    chrome.close()


def test_foreign_owner_mark_makes_the_tab_taken_and_is_left_alone(tmp_path, server):
    chrome = attached_chrome(tmp_path, server)
    server.windows["U1"] = {"__bhOwner": "other-server", "__jevFast": "their cache"}
    with pytest.raises(TabTaken):
        chrome.attach_tab("U1")
    assert server.windows["U1"] == {"__bhOwner": "other-server", "__jevFast": "their cache"}
    methods = server.methods()
    after = methods[methods.index("Target.attachToTarget") :]
    assert after == ["Target.attachToTarget", "Runtime.evaluate", "Target.detachFromTarget"]  # ни эмуляции, ни уборки
    assert chrome._borrowed == {}
    server.windows["U2"] = {"__bhOwner": chrome.owner_id}  # своя метка, оставшаяся от прошлого прогона
    chrome.attach_tab("U2")
    chrome.close()
    assert "Target.closeTarget" not in server.methods() and server.windows["U2"] == {}


def test_two_servers_racing_for_one_tab_only_one_gets_it(tmp_path, server):
    url = "https://web.whatsapp.com/"
    server.targets = [user_target("U1", url)]  # оба сервера успели увидеть attached=false
    first, second = attached_chrome(tmp_path, server), attached_chrome(tmp_path, server)
    assert first.owner_id != second.owner_id
    tab = first.attach_tab(first.find_user_tab(url) or "")
    with pytest.raises(TabTaken):
        second.attach_tab(second.find_user_tab(url) or "")
    assert server.windows["U1"]["__bhOwner"] == first.owner_id
    tab.release()
    assert "__bhOwner" not in server.windows["U1"]
    second.attach_tab("U1")  # первый отпустил — вкладка свободна
    assert server.windows["U1"]["__bhOwner"] == second.owner_id
    first.close()
    second.close()
