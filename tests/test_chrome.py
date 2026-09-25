"""Режимы attach/launch/ws без настоящего Chrome: временные каталоги, фейковый Popen и фейковый CDP-сервер."""

import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from browser_hands import browser as browser_module
from browser_hands import chrome as chrome_module
from browser_hands.cdp import ChromeDisconnected
from browser_hands.chrome import Chrome, ChromeUnavailable, launch_chrome, read_devtools_active_port, resolve_ws_url
from browser_hands.config import BrowserConfig
from tests.fake_cdp import FakeCDPServer


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
