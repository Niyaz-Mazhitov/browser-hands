"""CDPClient против локального websockets-сервера: без Chrome и без сети."""

import threading
import time

import pytest

from browser_hands.cdp import CDPClient, CDPError, CDPTimeout, ChromeDisconnected, TabGone
from tests.fake_cdp import FakeCDPServer, error, event, reply


@pytest.fixture
def server():
    with FakeCDPServer() as s:
        yield s


@pytest.fixture
def client(server):
    c = CDPClient(server.url, call_timeout=2.0, connect_timeout=2.0)
    yield c
    c.close()


def test_response_is_matched_by_id(server, client):
    server.on["Browser.getVersion"] = lambda f, ws: reply(ws, f, {"product": "Chrome/154"})
    assert client.call("Browser.getVersion") == {"product": "Chrome/154"}
    assert client.call("Target.getTargets") == {"targetInfos": []}
    assert [f["id"] for f in server.frames] == [1, 2]


def test_event_before_response_does_not_break_the_call(server, client):
    def handler(frame, ws):
        event(ws, "Page.frameNavigated", {"frame": {"id": "F"}}, session_id="S1")
        reply(ws, frame, {"ok": True})

    server.on["Page.navigate"] = handler
    assert client.call("Page.navigate", {"url": "about:blank"}, session_id="S1") == {"ok": True}
    assert client.events[-1]["method"] == "Page.frameNavigated"


def test_error_response_raises_cdp_error(server, client):
    server.on["Runtime.evaluate"] = lambda f, ws: error(ws, f, "Cannot find context with specified id")
    with pytest.raises(CDPError, match="Cannot find context") as info:
        client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    assert info.value.method == "Runtime.evaluate"
    assert isinstance(info.value, RuntimeError)


def test_session_id_goes_into_the_frame(server, client):
    client.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, session_id="S42")
    client.call("Target.getTargets")
    assert server.frames[0]["sessionId"] == "S42"
    assert server.frames[0]["params"] == {"enabled": True}
    assert "sessionId" not in server.frames[1]


def test_silent_chrome_raises_timeout_and_late_reply_is_skipped(server, client):
    late = []
    server.on["Runtime.evaluate"] = lambda f, ws: late.append(f)
    started = time.monotonic()
    with pytest.raises(CDPTimeout):
        client.call("Runtime.evaluate", {"expression": "1"}, timeout=0.2)
    assert time.monotonic() - started < 1.0

    # Запоздалый ответ на прошлый id не выдаётся за ответ следующего вызова.
    def answer_both(frame, ws):
        reply(ws, late[0], {"stale": True})
        reply(ws, frame, {"fresh": True})

    server.on["Target.getTargets"] = answer_both
    assert client.call("Target.getTargets") == {"fresh": True}


def test_closed_connection_raises_chrome_disconnected(server, client):
    server.on["Target.getTargets"] = lambda f, ws: ws.close()
    with pytest.raises(ChromeDisconnected):
        client.call("Target.getTargets")
    assert not client.connected
    with pytest.raises(ChromeDisconnected):
        client.call("Target.getTargets")


def test_detached_session_becomes_tab_gone(server, client):
    def handler(frame, ws):
        event(ws, "Target.detachedFromTarget", {"sessionId": frame["sessionId"], "targetId": "T1"})

    server.on["Runtime.evaluate"] = handler
    with pytest.raises(TabGone):
        client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    sent = len(server.frames)
    with pytest.raises(TabGone):
        client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    assert len(server.frames) == sent  # мёртвой сессии кадр уже не отправляется
    assert client.call("Target.getTargets") == {"targetInfos": []}  # соединение живо


def test_crashed_target_marks_its_session_dead(server, client):
    session = client.call("Target.attachToTarget", {"targetId": "T7", "flatten": True})["sessionId"]

    def crash(frame, ws):
        event(ws, "Target.targetCrashed", {"targetId": "T7", "status": "crashed", "errorCode": 1})
        reply(ws, frame, {"targetInfos": []})

    server.on["Target.getTargets"] = crash
    client.call("Target.getTargets")
    assert not client.session_alive(session)
    with pytest.raises(TabGone):
        client.call("Runtime.evaluate", {"expression": "1"}, session_id=session)


def test_unknown_session_error_is_tab_gone(server, client):
    server.on["Runtime.evaluate"] = lambda f, ws: error(ws, f, "Session with given id not found.")
    with pytest.raises(TabGone):
        client.call("Runtime.evaluate", {"expression": "1"}, session_id="S9")


def test_events_queue_is_bounded(server, client):
    def flood(frame, ws):
        for i in range(250):
            event(ws, "Network.dataReceived", {"i": i})
        reply(ws, frame)

    server.on["Target.getTargets"] = flood
    client.call("Target.getTargets")
    assert len(client.events) == 200
    assert client.events[-1]["params"]["i"] == 249


def test_close_is_idempotent(client):
    client.close()
    client.close()
    assert not client.connected


def test_close_from_another_thread_unblocks_a_waiting_call(server):
    client = CDPClient(server.url, call_timeout=30.0, connect_timeout=2.0)
    server.on["Runtime.evaluate"] = lambda f, ws: None  # Chrome «висит» на вызове
    outcome = []

    def worker():
        try:
            client.call("Runtime.evaluate", {"expression": "1"})
        except Exception as exc:
            outcome.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while "Runtime.evaluate" not in server.methods():
        time.sleep(0.01)
    started = time.monotonic()
    client.close()  # без лока: не ждёт call_timeout рабочего потока
    thread.join(timeout=2)
    assert time.monotonic() - started < 1.0
    assert not thread.is_alive() and isinstance(outcome[0], ChromeDisconnected)


def test_waiting_for_a_busy_client_counts_toward_the_call_timeout(server, client):
    held, release = threading.Event(), threading.Event()

    def hold():
        with client._lock:
            held.set()
            release.wait(5)

    threading.Thread(target=hold, daemon=True).start()
    held.wait(2)
    started = time.monotonic()
    with pytest.raises(CDPTimeout, match="занят"):
        client.call("Target.closeTarget", {"targetId": "T1"}, timeout=0.2)
    release.set()
    assert time.monotonic() - started < 1.0
    assert server.frames == []  # кадр не отправлен
