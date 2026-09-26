"""CDPClient против локального websockets-сервера: без Chrome и без сети."""

import threading
import time

import pytest

from browser_hands import cdp
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
            event(ws, "Page.lifecycleEvent", {"i": i}, session_id="S1")
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


# --- учёт сети и насос событий (docs/plan-waits.md §5.1) -----------------------------------------------------------


def request(ws, rid, kind="Fetch", session="S1"):
    event(ws, "Network.requestWillBeSent", {"requestId": rid, "type": kind, "request": {"url": "x"}}, session)


def finished(ws, rid, method="Network.loadingFinished", session="S1"):
    event(ws, method, {"requestId": rid}, session)


def test_network_events_are_counted_not_queued_and_seq_counts_every_message(server, client):
    def handler(frame, ws):
        request(ws, "A")
        event(ws, "Network.dataReceived", {"requestId": "A", "dataLength": 10}, "S1")
        event(ws, "Page.frameNavigated", {"frame": {"id": "F"}}, "S1")
        reply(ws, frame)

    server.on["Runtime.evaluate"] = handler
    epoch = client.seq
    client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    assert client.seq == epoch + 4  # три события и ответ
    assert [e["method"] for e in client.events] == ["Page.frameNavigated"]  # Network.* очередь не вытесняют
    assert client.pending_since("S1", epoch) == 1
    assert client.pending_since("S2", epoch) == 0  # у другой сессии сети нет


def test_only_requests_the_page_waits_for_are_pending(server, client):
    def handler(frame, ws):
        request(ws, "fetch")
        request(ws, "script", "Script")
        request(ws, "ws", "WebSocket")
        request(ws, "es", "EventSource")
        request(ws, "sse", "XHR")
        mime = {"requestId": "sse", "type": "XHR", "response": {"mimeType": "text/event-stream"}}
        event(ws, "Network.responseReceived", mime, "S1")
        request(ws, "img", "Image")
        finished(ws, "img", "Network.loadingFailed")
        request(ws, "css", "Stylesheet")
        finished(ws, "css", "Network.requestServedFromCache")
        request(ws, "fetch")  # редирект: тот же requestId — тот же запрос
        reply(ws, frame)

    server.on["Runtime.evaluate"] = handler
    epoch = client.seq
    client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    assert sorted(client.network["S1"].pending) == ["fetch", "script"]
    assert client.pending_since("S1", epoch) == 2
    assert client.finished_mark("S1") == 3  # event-stream, loadingFailed, requestServedFromCache
    assert client.finished_since("S1", 0, epoch) == 3 and client.finished_since("S1", 1, epoch) == 2


def test_requests_started_before_the_epoch_are_background(server, client):
    server.on["Target.getTargets"] = lambda f, ws: (request(ws, "old"), reply(ws, f))
    client.call("Target.getTargets")
    epoch = client.seq
    server.on["Runtime.evaluate"] = lambda f, ws: (request(ws, "new"), reply(ws, f))
    client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    assert client.pending_since("S1", epoch) == 1
    assert client.mark_background("S1") == 2  # предохранитель: всё, что в полёте, — фон
    assert client.pending_since("S1", 0) == 0
    server.on["Target.getTargets"] = lambda f, ws: (finished(ws, "old"), finished(ws, "new"), reply(ws, f))
    client.call("Target.getTargets")
    assert client.network["S1"].pending == {} and client.network["S1"].background == set()  # не копятся


def test_in_flight_counts_background_requests_too(server, client):
    server.on["Target.getTargets"] = lambda f, ws: (request(ws, "old"), reply(ws, f))
    client.call("Target.getTargets")
    epoch = client.seq
    server.on["Runtime.evaluate"] = lambda f, ws: (request(ws, "slow"), request(ws, "ws", "WebSocket"), reply(ws, f))
    client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    client.mark_background("S1")  # предохранитель вышел
    assert client.pending_since("S1", epoch) == 0  # ожиданий не держат
    assert client.in_flight_since("S1", epoch) == 1  # но ответа ещё нет; WebSocket — не в счёт
    assert client.in_flight_since("S1", 0) == 2 and client.in_flight_since("S2", 0) == 0
    server.on["Target.getTargets"] = lambda f, ws: (finished(ws, "slow"), reply(ws, f))
    client.call("Target.getTargets")
    assert client.in_flight_since("S1", epoch) == 0


def test_wait_events_pumps_until_the_predicate_holds(server, client):
    def handler(frame, ws):
        request(ws, "slow")
        reply(ws, frame)
        time.sleep(0.15)  # ответ сервера приходит позже, без команды от клиента
        event(ws, "Network.webSocketFrameReceived", {"requestId": "w", "response": {"payloadData": "x"}}, "S1")
        finished(ws, "slow")

    server.on["Runtime.evaluate"] = handler
    epoch = client.seq
    client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    started = time.monotonic()
    assert client.wait_events("S1", lambda: client.pending_since("S1", epoch) == 0, timeout=2.0) is True
    assert 0.1 <= time.monotonic() - started < 1.0
    assert client.finished_mark("S1") == 1  # WS-кадр — не завершение
    assert client.finished_since("S1", 0, epoch) == 1 and client.finished_since("S1", 0, client.seq) == 0
    assert client.wait_events("S1", lambda: True, timeout=0.0) is True  # предикат проверяется до чтения


def test_wait_events_times_out_and_skips_a_late_reply(server, client):
    late = []
    server.on["Runtime.evaluate"] = lambda f, ws: late.append((f, ws))
    with pytest.raises(CDPTimeout):
        client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1", timeout=0.1)
    frame, ws = late[0]
    reply(ws, frame, {"late": True})  # ответ на истёкший вызов приходит во время насоса
    started = time.monotonic()
    assert client.wait_events("S1", lambda: False, timeout=0.2) is False
    assert 0.2 <= time.monotonic() - started < 1.0
    assert client.call("Target.getTargets") == {"targetInfos": []}  # клиент в порядке


def test_wait_events_raises_tab_gone_when_the_session_detaches(server, client):
    def handler(frame, ws):
        reply(ws, frame)
        time.sleep(0.05)
        event(ws, "Target.detachedFromTarget", {"sessionId": "S1", "targetId": "T1"})

    server.on["Runtime.evaluate"] = lambda f, ws: (request(ws, "r"), handler(f, ws))
    client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    assert "S1" in client.network
    with pytest.raises(TabGone):
        client.wait_events("S1", lambda: False, timeout=2.0)
    assert "S1" not in client.network  # учёт отсоединённой сессии забыт


def test_wait_events_on_a_closed_connection_raises_chrome_disconnected(server, client):
    server.on["Runtime.evaluate"] = lambda f, ws: (reply(ws, f), ws.close())
    client.call("Runtime.evaluate", {"expression": "1"}, session_id="S1")
    with pytest.raises(ChromeDisconnected):
        client.wait_events("S1", lambda: False, timeout=2.0)


def test_pending_requests_are_bounded(server, client, monkeypatch):
    monkeypatch.setattr(cdp, "NETWORK_PENDING_MAX", 50)

    def flood(frame, ws):
        for i in range(120):
            request(ws, f"r{i}")
        reply(ws, frame)

    server.on["Target.getTargets"] = flood
    client.call("Target.getTargets")
    pending = client.network["S1"].pending
    assert len(pending) == 50 and "r119" in pending and "r0" not in pending  # забыты самые старые


def test_call_until_stops_on_an_event_and_its_reply_is_skipped_later(server, client):
    def handler(frame, ws):
        finished(ws, "r")  # запрос завершился раньше, чем промис страницы
        time.sleep(0.05)
        reply(ws, frame, {"result": {"value": "late"}})

    server.on["Target.getTargets"] = lambda f, ws: (request(ws, "r"), reply(ws, f))
    client.call("Target.getTargets")
    mark = client.finished_mark("S1")
    server.on["Runtime.evaluate"] = handler
    stop = lambda: client.finished_since("S1", mark, 0) > 0  # noqa: E731
    assert client.call_until("Runtime.evaluate", {"expression": "p"}, session_id="S1", stop=stop) is None
    server.on["Target.getTargets"] = lambda f, ws: reply(ws, f, {"fresh": True})
    assert client.call("Target.getTargets") == {"fresh": True}  # поздний ответ не выдан за этот


# --- смена документа: запросы старого документа (docs/core-notes.md, «После ревью ожиданий») ----------------------


def loaded(ws, rid, kind, loader, frame="T1"):
    params = {"requestId": rid, "type": kind, "frameId": frame, "loaderId": loader, "request": {"url": rid}}
    event(ws, "Network.requestWillBeSent", params, "S1")


def attached(server, client):
    """Сессия S1 вкладки T1, прицепленная через этот клиент (так делает Chrome.new_tab/attach_tab): главный фрейм
    вкладки — T1 (у вкладки Chrome id главного фрейма = targetId, зонд 26.09)."""
    server.on["Target.attachToTarget"] = lambda f, ws: reply(ws, f, {"sessionId": "S1"})
    client.call("Target.attachToTarget", {"targetId": "T1", "flatten": True})


def events(server, client, *send):
    """Отправить события (функции от ws) внутри одного вызова: клиент их прочтёт."""
    server.on["Target.getTargets"] = lambda f, ws: ([s(ws) for s in send], reply(ws, f))
    client.call("Target.getTargets")


def test_requests_of_a_replaced_document_are_dropped_when_the_new_one_commits(server, client):
    """Chrome не присылает loadingFinished/Failed запросам документа, который сменился (зонд ревью: fetch 8 с и уход
    со страницы — запрос «в полёте» навсегда): ожидания упирались в предохранитель, Jev всегда видел «ещё грузится»."""
    attached(server, client)
    epoch = client.seq
    events(
        server,
        client,
        lambda ws: loaded(ws, "api", "Fetch", "L1"),
        lambda ws: loaded(ws, "frame-api", "XHR", "LF", frame="F2"),  # iframe старого документа
        lambda ws: loaded(ws, "L2", "Document", "L2"),  # клик увёл на новую страницу
        lambda ws: loaded(ws, "late", "Fetch", "L1"),  # старый документ ещё жив до commit
    )
    assert client.in_flight_since("S1", epoch) == 4  # пока новый документ не пришёл, старый живёт — ждём всё
    events(server, client, lambda ws: finished(ws, "L2"))  # новый документ загружен (commit был)
    assert client.network["S1"].pending == {} and client.in_flight_since("S1", epoch) == 0
    events(server, client, lambda ws: loaded(ws, "after", "Fetch", "L1"))  # запрос сменившегося документа — не в счёт
    assert client.network["S1"].pending == {}
    events(server, client, lambda ws: loaded(ws, "img", "Image", "L2"), lambda ws: finished(ws, "api"))
    assert list(client.network["S1"].pending) == ["img"]  # запросы нового документа — как обычно


def test_subresource_of_the_new_document_commits_it_and_iframe_navigation_does_not(server, client):
    attached(server, client)
    events(
        server,
        client,
        lambda ws: loaded(ws, "api", "Fetch", "L1"),
        lambda ws: loaded(ws, "ad", "Document", "LA", frame="F2"),  # iframe сменил документ — не вкладка
    )
    assert sorted(client.network["S1"].pending) == ["ad", "api"]
    events(
        server,
        client,
        lambda ws: loaded(ws, "L2", "Document", "L2"),
        lambda ws: loaded(ws, "css", "Stylesheet", "L2"),
    )
    assert sorted(client.network["S1"].pending) == ["L2", "css"]  # подресурс нового документа: commit уже был


@pytest.mark.parametrize("ending", ["Network.loadingFailed"], ids=["download-or-204"])
def test_navigation_that_never_commits_keeps_the_old_document_requests(server, client, ending):
    """Ссылка на файл (Content-Disposition: attachment) и ответ 204: навигация кончается loadingFailed (зонд 26.09),
    старый документ жив — его запросы остаются в учёте и завершатся сами."""
    attached(server, client)
    events(server, client, lambda ws: loaded(ws, "api", "Fetch", "L1"), lambda ws: loaded(ws, "L2", "Document", "L2"))
    events(server, client, lambda ws: finished(ws, "L2", ending), lambda ws: loaded(ws, "more", "XHR", "L1"))
    assert sorted(client.network["S1"].pending) == ["api", "more"]
    events(server, client, lambda ws: finished(ws, "api"), lambda ws: finished(ws, "more"))
    assert client.network["S1"].pending == {}


def test_without_a_known_main_frame_nothing_is_dropped(server, client):
    events(server, client, lambda ws: loaded(ws, "api", "Fetch", "L1"), lambda ws: loaded(ws, "L2", "Document", "L2"))
    events(server, client, lambda ws: finished(ws, "L2"))
    assert list(client.network["S1"].pending) == ["api"]  # сессия не через этот клиент: главный фрейм неизвестен
