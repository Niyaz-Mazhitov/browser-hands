"""Tab на Mock-клиенте: атомарный снимок, свежесть до ввода, без повтора мутаций, финальный кадр. Ожидания по событиям
(docs/plan-waits.md §5) — на Mock, на FakeCDPServer с событиями Network, в node (settle_harness.js) и в Chrome."""

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from browser_hands import browser
from browser_hands.browser import CHANGE, LOAD, READY, NavigationFailed, StalePage, Tab, fingerprint, url_host
from browser_hands.cdp import CDPClient, CDPError, CDPTimeout, ChromeDisconnected, TabGone
from tests.fake_cdp import FakeCDPServer, event, reply


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def make_tab(client=None, **kw):
    """Вкладка на Mock-клиенте; учёт сети — только если тест просит (`network=True`): у Mock нет событий."""
    kw.setdefault("network", False)
    return Tab(client or Mock(), "S1", "T1", **kw)


def test_observation_is_one_atomic_browser_read():
    p = page()
    client = Mock()
    client.call.return_value = {"result": {"value": p}}
    actual = make_tab(client).observe(screenshot=False)
    assert actual["actions"] == p["actions"]
    assert client.call.call_count == 1
    assert client.call.call_args.args[0] == "Runtime.evaluate"
    assert client.call.call_args.kwargs["session_id"] == "S1"


def test_executor_rejects_a_stale_page_before_browser_input():
    tab = make_tab()
    tab.fresh = Mock(return_value=False)
    tab._act = Mock()
    with pytest.raises(StalePage):
        tab.act(page()["actions"][0], page(), "book")
    tab._act.assert_not_called()
    tab.client.call.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(response):
    # Навигация может уничтожить результат evaluate уже после того, как change сработал.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    client = Mock()
    client.call.return_value = response
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        make_tab(client)._act({"id": "e1", "kind": "select", "node": 1, "value": "Design"}, None)
    assert client.call.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


def test_click_resolves_current_geometry_then_presses_there():
    client = Mock()
    client.call.side_effect = [{"result": {"value": {"x": 40, "y": 60}}}, {}, {}]
    make_tab(client)._act(page()["actions"][2], None)
    methods = [c.args[0] for c in client.call.call_args_list]
    assert methods == ["Runtime.evaluate", "Input.dispatchMouseEvent", "Input.dispatchMouseEvent"]
    assert client.call.call_args_list[1].args[1] == {
        "type": "mousePressed",
        "x": 40,
        "y": 60,
        "button": "left",
        "clickCount": 1,
    }


def test_covered_target_is_stale_and_nothing_is_pressed():
    client = Mock()
    client.call.return_value = {"result": {"value": None}}
    with pytest.raises(StalePage, match="covered"):
        make_tab(client)._act(page()["actions"][2], None)
    assert client.call.call_count == 1


def test_fill_selects_all_then_inserts_generated_text():
    client = Mock()
    client.call.side_effect = [{"result": {"value": {"x": 5, "y": 5}}}, {}, {}, {"result": {"value": True}}, {}, {}, {}]
    make_tab(client)._act(page()["actions"][0], "book")
    methods = [c.args[0] for c in client.call.call_args_list]
    assert methods == [
        "Runtime.evaluate",  # геометрия цели
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
        "Runtime.evaluate",  # фокус на цели — одним evaluate
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
        "Input.insertText",
    ]
    focus = client.call.call_args_list[3].args[1]["expression"]
    assert "document.activeElement" in focus and "password" in focus and focus.endswith("(10)")
    assert client.call.call_args_list[4].args[1]["commands"] == ["selectAll"]
    assert client.call.call_args_list[-1].args[1] == {"text": "book"}


@pytest.mark.parametrize(
    "focus",
    [
        {"result": {"value": False}},  # фокус ушёл на другой элемент, в password/file/hidden или цель пропала
        {"result": {}},
        {"exceptionDetails": {"text": "Execution context destroyed"}},
    ],
)
def test_fill_types_nothing_when_focus_is_not_on_the_target(focus):
    client = Mock()
    client.call.side_effect = [{"result": {"value": {"x": 5, "y": 5}}}, {}, {}, focus]
    with pytest.raises(StalePage, match="nothing typed"):
        make_tab(client)._act(page()["actions"][0], "secret")
    methods = [c.args[0] for c in client.call.call_args_list]
    assert "Input.dispatchKeyEvent" not in methods and "Input.insertText" not in methods
    assert client.call.call_count == 4


def test_fill_without_text_fails_before_the_click():
    client = Mock()
    with pytest.raises(ValueError, match="generated text"):
        make_tab(client)._act(page()["actions"][0], None)
    client.call.assert_not_called()


READY_DONE = {"result": {"type": "object", "value": {"reason": "quiet", "ms": 32, "mutations": 4, "frames": 2}}}
CHANGED = {"result": {"type": "object", "value": {"reason": "mutation", "ms": 40}}}


def params_of(expression, prefix=READY):
    """Параметры ожидания из выражения `READY|CHANGE|LOAD + json + ")"` — как их увидит страница."""
    assert expression.startswith(prefix) and expression.endswith(")")
    return json.loads(expression[len(prefix) : -1])


def ready_calls(client):
    return [c for c in client.call.call_args_list if c.args[1].get("expression", "").startswith(READY)]


def frozen_clock(monkeypatch, now=100.0):
    monkeypatch.setattr("browser_hands.browser.time.monotonic", lambda: now)


def test_ready_expression_watches_significant_mutations_with_numbers_in_params(monkeypatch):
    frozen_clock(monkeypatch)
    assert "MutationObserver" in READY and "attributeFilter" in READY and "attributeOldValue" in READY
    assert "requestAnimationFrame" in READY and "disconnect()" in READY and "MessageChannel" in READY
    assert "script,style,template,noscript" in READY
    assert "readyState" in READY and "fonts" in READY and "getAnimations" in READY and "aria-busy" in READY
    client = Mock()
    client.call.return_value = READY_DONE
    tab = make_tab(client)
    tab.await_ready(page()["actions"][0])
    call = client.call.call_args
    params = params_of(call.args[1]["expression"])
    assert params == {
        "action": {"kind": "fill", "node": 10},  # метка и значение поля в страницу не уходят
        "fuse_ms": 1500,  # Thresholds.wait_fuse_s — единственное время
        "frames": 2,
        "attributes": list(browser.MUTATION_ATTRIBUTES),
    }
    assert "style" not in params["attributes"]  # JS-анимации пишут style каждый кадр
    assert {"class", "hidden", "aria-expanded", "aria-busy", "disabled"} <= set(params["attributes"])
    assert call.args[1]["awaitPromise"] is True and call.kwargs["session_id"] == "S1"
    assert not [name for name in vars(browser) if name.endswith("_MS")]  # чисел в мс в модуле нет


def test_wait_after_input_is_read_only_and_counted_as_wait(monkeypatch):
    p = page()
    ticks = iter([0.0, 0.3, 0.3, 0.31])  # ожидание 300 мс, снимок 10 мс
    monkeypatch.setattr("browser_hands.browser.time.perf_counter", lambda: next(ticks))
    client = Mock()
    client.call.side_effect = [READY_DONE, {"result": {"value": p}}]
    tab = make_tab(client)
    tab.after_input = p["actions"][0]
    tab.observe()
    ready = client.call.call_args_list[0]
    assert ready.args[0] == "Runtime.evaluate" and ready.args[1]["awaitPromise"] is True
    assert params_of(ready.args[1]["expression"])["action"] == {"kind": "fill", "node": 10}
    assert client.call.call_args_list[1].args[1]["expression"] == browser.READ_STATE
    assert tab.after_input is None
    assert tab.last_settle is not None
    assert {k: tab.last_settle[k] for k in ("reason", "wait_reason", "mutations", "frames", "passes")} == {
        "reason": "quiet",
        "wait_reason": "quiet",
        "mutations": 4,
        "frames": 2,
        "passes": 1,
    }
    assert tab.last_settle["pending_requests"] == 0  # без учёта сети
    timing = tab.take_timing()
    assert timing.wait_ms == 300 and timing.browser_ms == 10  # ожидание — не работа CDP
    assert timing.model_ms == 0 and timing.text_ms == 0
    assert tab.take_timing().wait_ms == 0  # счётчики обнулились


def test_wait_action_waits_for_the_next_change_without_a_pause(monkeypatch):
    frozen_clock(monkeypatch)
    p = page()
    client = Mock()
    tab = make_tab(client)
    tab.fresh = Mock(return_value=True)
    tab._sleep = Mock()
    tab.act(p["actions"][3], p)
    tab._sleep.assert_not_called()  # WAIT — не пауза по часам
    client.call.assert_not_called()
    assert tab.after_input == p["actions"][3]
    client.call_until.return_value = CHANGED
    client.call.side_effect = [READY_DONE, {"result": {"value": p}}]
    tab.observe()
    change = client.call_until.call_args
    assert params_of(change.args[1]["expression"], CHANGE) == {
        "fuse_ms": 1500,
        "attributes": list(browser.MUTATION_ATTRIBUTES),
    }
    assert change.args[1]["awaitPromise"] is True
    assert params_of(client.call.call_args_list[0].args[1]["expression"])["action"] == {"kind": "wait"}
    assert client.call.call_args_list[1].args[1]["expression"] == browser.READ_STATE
    assert tab.last_settle is not None
    assert (tab.last_settle["wait_reason"], tab.last_settle["change"], tab.last_settle["ready"]) == (
        "change",
        "mutation",
        "quiet",
    )


@pytest.mark.parametrize(
    "interrupted",
    [
        CDPError("Runtime.evaluate", "Execution context was destroyed."),
        {"exceptionDetails": {"text": "Execution context was destroyed"}},
    ],
    ids=["cdp-error", "exception"],
)
def test_ready_interrupted_by_navigation_runs_again_in_the_new_document(interrupted):
    p = page()
    client = Mock()
    client.call.side_effect = [interrupted, READY_DONE, {"result": {"value": p}}]
    tab = make_tab(client)
    tab.last_settle = {"reason": "quiet", "ms": 1, "mutations": 0}  # от прошлого действия
    tab.after_input = p["actions"][2]
    assert tab.observe()["actions"] == p["actions"]
    assert tab.last_settle is not None and tab.last_settle["reason"] == "quiet" and tab.last_settle["passes"] == 2
    assert len(ready_calls(client)) == 2 and client.call.call_count == 3


def test_ready_without_a_value_ends_the_wait_and_the_page_is_snapshotted():
    p = page()
    client = Mock()
    client.call.side_effect = [{"result": {"type": "undefined"}}, {"result": {"value": p}}]
    tab = make_tab(client)
    tab.last_settle = {"reason": "quiet", "ms": 1, "mutations": 0}
    tab.after_input = p["actions"][2]
    assert tab.observe()["actions"] == p["actions"]
    assert tab.last_settle is None
    assert client.call.call_count == 2


def test_document_that_keeps_changing_ends_the_wait_after_stale_retries():
    client = Mock()
    client.call.return_value = {"exceptionDetails": {"text": "Execution context was destroyed"}}
    tab = make_tab(client)
    assert tab.await_ready({"kind": "click", "node": 20}) == {} and tab.last_settle is None
    assert client.call.call_count == browser.STALE_RETRIES


def test_fuse_stops_short_of_the_run_deadline(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("browser_hands.browser.time.monotonic", lambda: clock["now"])
    client = Mock(call_timeout=30.0)
    client.call.return_value = READY_DONE
    tab = make_tab(client)
    tab.deadline = 101.2  # 1,2 с до дедлайна: предохранитель 0,7 с, на ответ остаётся 0,5 с
    tab.await_ready({"kind": "click", "node": 20})
    assert params_of(client.call.call_args.args[1]["expression"])["fuse_ms"] == 700
    assert client.call.call_args.kwargs["timeout"] == pytest.approx(1.2)  # таймаут CDP — остаток дедлайна

    tab.deadline = 130.0
    tab.await_ready({"kind": "click", "node": 20})
    assert params_of(client.call.call_args.args[1]["expression"])["fuse_ms"] == 1500
    tab.fuse_s = 0.8  # агент задал свой Thresholds.wait_fuse_s
    tab.await_ready({"kind": "click", "node": 20})
    assert params_of(client.call.call_args.args[1]["expression"])["fuse_ms"] == 800

    client.call.reset_mock()
    tab.deadline = 100.4  # меньше 0,5 с: не ждём вовсе
    assert tab.await_ready({"kind": "click", "node": 20}) == {}
    assert tab.await_change() == {}
    client.call.assert_not_called()
    client.call_until.assert_not_called()
    assert tab.last_settle is None


def test_waits_are_skipped_after_cancel():
    client = Mock()
    tab = make_tab(client)
    tab.cancel = threading.Event()
    tab.cancel.set()
    assert tab.await_ready({"kind": "click", "node": 20}) == {}
    assert tab.await_change() == {}
    assert tab.settle({"kind": "retry"}) is None  # совместимость agent.py: None — не ждали
    client.call.assert_not_called()
    client.call_until.assert_not_called()


def test_wait_reason_and_time_are_logged_at_debug(caplog):
    client = Mock()
    client.call.return_value = READY_DONE
    logger = logging.getLogger("browser_hands.browser")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="browser_hands.browser"):
            make_tab(client).await_ready({"kind": "click", "node": 20})
    finally:
        logger.removeHandler(caplog.handler)
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        line.startswith("await_ready quiet ") and line.endswith("мутаций 4, кадров 2, проходов 1, запросов в полёте 0")
        for line in lines
    ), lines


LOADED = {"result": {"type": "boolean", "value": True}}


def test_navigate_waits_for_the_load_event_then_ready_and_reports_errors(monkeypatch):
    frozen_clock(monkeypatch)
    client = Mock()
    client.call.side_effect = [{"frameId": "F"}, LOADED, READY_DONE]
    tab = make_tab(client)
    tab.navigate("https://example.test/")
    assert methods_of(client) == ["Page.navigate", "Runtime.evaluate", "Runtime.evaluate"]
    load, ready = (c.args[1] for c in client.call.call_args_list[1:])
    assert load["awaitPromise"] is True and params_of(load["expression"], LOAD) == {"fuse_ms": 15000}
    assert ready["awaitPromise"] is True and params_of(ready["expression"])["action"] == {"kind": "load"}
    assert tab.last_settle is not None
    assert tab.take_timing().browser_ms == 0  # загрузка и ожидание — это wait_ms

    client.call.side_effect = [{"frameId": "F", "errorText": "net::ERR_NAME_NOT_RESOLVED"}]
    with pytest.raises(NavigationFailed, match="nope.invalid failed: net::ERR_NAME_NOT_RESOLVED") as info:
        tab.navigate("https://user:pw@nope.invalid/private?token=1")
    assert "token" not in str(info.value) and "pw" not in str(info.value)  # в логи INFO попадает только хост


def test_navigate_follows_a_redirect_and_skips_ready_when_the_page_never_loads(monkeypatch):
    frozen_clock(monkeypatch)
    client = Mock()
    redirected = {"exceptionDetails": {"text": "Execution context was destroyed"}}
    client.call.side_effect = [{"frameId": "F"}, redirected, LOADED, READY_DONE]
    tab = make_tab(client)
    tab.navigate("https://example.test/")
    assert [params_of(c.args[1]["expression"], LOAD) for c in client.call.call_args_list[1:3]] == [
        {"fuse_ms": 15000},
        {"fuse_ms": 15000},
    ]
    assert len(ready_calls(client)) == 1

    client.call.reset_mock()
    client.call.side_effect = [{"frameId": "F"}, {"result": {"type": "boolean", "value": False}}]
    tab.navigate("https://example.test/slow", timeout=2.0)  # предохранитель навигации вышел: дальше без ожидания
    assert methods_of(client) == ["Page.navigate", "Runtime.evaluate"] and ready_calls(client) == []


def test_location_reads_target_info_at_browser_level_and_tolerates_errors():
    client = Mock()
    client.call.return_value = {"targetInfo": {"targetId": "T1", "url": "https://example.test/x", "title": "X"}}
    tab = make_tab(client)
    assert tab.location() == ("https://example.test/x", "X")
    assert client.call.call_args.args == ("Target.getTargetInfo", {"targetId": "T1"})
    assert client.call.call_args.kwargs.get("session_id") is None  # не JS страницы: отвечает и во время загрузки
    assert client.call.call_args.kwargs["timeout"] <= browser.LOCATION_TIMEOUT_S
    client.call.return_value = {"targetInfo": {"url": "", "title": "X"}}
    assert tab.location() is None
    client.call.return_value = {"targetInfo": {"url": "https://example.test/x"}}
    assert tab.location() == ("https://example.test/x", "")
    for failure in (CDPError("Target.getTargetInfo", "No target with given id found"), CDPTimeout("slow")):
        client.call.side_effect = failure
        assert tab.location() is None


def test_url_host_drops_path_query_and_credentials():
    assert url_host("https://user:pw@en.wikipedia.org:443/wiki/X?q=1#f") == "en.wikipedia.org"
    assert url_host("about:blank") == "about"


def test_screenshot_returns_jpeg_bytes_or_none_when_tab_is_gone():
    client = Mock()
    client.call.return_value = {"data": base64.b64encode(b"\xff\xd8jpeg").decode()}
    tab = make_tab(client, screenshot_quality=60)
    assert tab.screenshot() == b"\xff\xd8jpeg"
    assert client.call.call_args.args[1] == {"format": "jpeg", "quality": 60}

    client.call.side_effect = TabGone("gone")
    assert tab.screenshot() is None
    client.call.side_effect = ChromeDisconnected("closed")
    assert tab.screenshot() is None


def test_scaled_screenshot_clips_the_visible_viewport():
    client = Mock()
    client.call.side_effect = [
        {"cssVisualViewport": {"pageX": 0, "pageY": 300, "clientWidth": 1120, "clientHeight": 780}},
        {"data": base64.b64encode(b"x").decode()},
    ]
    make_tab(client).screenshot(scale=0.5)
    clip = client.call.call_args_list[1].args[1]["clip"]
    assert clip == {"x": 0, "y": 300, "width": 1120, "height": 780, "scale": 0.5}


def test_close_is_idempotent_and_swallows_disconnect():
    client = Mock()
    client.call.side_effect = ChromeDisconnected("closed")
    released = []
    tab = make_tab(client, on_release=released.append)
    tab.close()
    tab.close()
    assert client.call.call_count == 1
    assert released == []  # не закрыта: Chrome закроет сироту при следующем connect()
    assert tab.screenshot() is None


def test_close_releases_target_after_close_target():
    client = Mock()
    client.call.return_value = {}
    released = []
    tab = make_tab(client, on_release=released.append)
    tab.close()
    assert client.call.call_args.args == ("Target.closeTarget", {"targetId": "T1"})
    assert "session_id" not in client.call.call_args.kwargs
    assert released == ["T1"]


def test_snapshot_interrupted_by_navigation_counts_as_wait(monkeypatch):
    p = page()
    ticks = [0.0, 1.3, 1.3, 1.3, 1.3, 1.31]  # первая попытка снимка «висит» 1,3 с на навигации

    def clock():
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    monkeypatch.setattr("browser_hands.browser.time.perf_counter", clock)
    client = Mock()
    client.call.side_effect = [{"result": {"value": None}}, READY_DONE, {"result": {"value": p}}]
    tab = make_tab(client)
    tab.observe()
    timing = tab.take_timing()
    assert timing.wait_ms == 1300 and timing.browser_ms == 10
    # повтор снимка — после готовности нового документа (READY), не по часам; итог действия он не подменяет
    assert params_of(client.call.call_args_list[1].args[1]["expression"])["action"] == {"kind": "retry"}
    assert tab.last_settle is None


# --- вкладка пользователя (owned=False) -------------------------------------------------------------------------


def methods_of(client):
    return [c.args[0] for c in client.call.call_args_list]


def test_borrowed_tab_setup_never_overrides_device_metrics():
    client = Mock()
    client.call.return_value = {}
    make_tab(client, owned=False).setup()
    assert methods_of(client) == ["Emulation.setFocusEmulationEnabled"]
    assert client.call.call_args.args[1] == {"enabled": True}

    own = Mock()
    own.call.return_value = {}
    make_tab(own).setup()
    assert methods_of(own) == ["Emulation.setDeviceMetricsOverride", "Emulation.setFocusEmulationEnabled"]


def test_network_is_enabled_only_in_own_tab_until_the_whatsapp_measurement():
    own, user = Mock(seq=7), Mock(seq=7)
    own.call.return_value = user.call.return_value = {}
    tab = Tab(own, "S1", "T1")
    assert tab.network and not Tab(user, "S2", "T2", owned=False).network  # USER_TAB_NETWORK = False (§0.2)
    tab.setup()
    Tab(user, "S2", "T2", owned=False).setup()
    assert methods_of(own)[-1] == "Network.enable"
    assert own.call.call_args.args[1] == {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0, "maxPostDataSize": 0}
    assert tab._epoch == 7  # запросы до включения сети — не от действий
    assert "Network.enable" not in methods_of(user)
    assert Tab(user, "S3", "T3", owned=False, network=True).network  # флаг в коде: после замера §8


def test_borrowed_tab_close_only_detaches_and_turns_focus_emulation_off():
    client = Mock()
    client.call.return_value = {}
    released = []
    tab = make_tab(client, owned=False, on_release=released.append)
    tab.setup()
    client.call.reset_mock()
    tab.close()
    tab.close()
    tab.release()
    assert methods_of(client) == ["Runtime.evaluate", "Emulation.setFocusEmulationEnabled", "Target.detachFromTarget"]
    cleanup, focus, detach = client.call.call_args_list
    assert cleanup.args[1] == {"expression": "delete window.__jevFast"} and cleanup.kwargs["session_id"] == "S1"
    assert focus.args[1] == {"enabled": False} and focus.kwargs["session_id"] == "S1"
    assert detach.args[1] == {"sessionId": "S1"} and detach.kwargs["session_id"] is None
    assert released == ["T1"] and tab.closed
    assert tab.screenshot() is None


def test_release_turns_the_network_off_before_detaching():
    client = Mock()
    client.call.return_value = {}
    tab = make_tab(client, owned=False, network=True)
    tab.setup()
    client.call.reset_mock()
    tab.release()
    assert methods_of(client) == [
        "Runtime.evaluate",
        "Emulation.setFocusEmulationEnabled",
        "Network.disable",
        "Target.detachFromTarget",
    ]
    client.forget_network.assert_called_once_with("S1")

    own = Mock()
    own.call.return_value = {}
    kept = make_tab(own, network=True)
    kept.setup()
    own.call.reset_mock()
    kept.release()  # keep_open: своя вкладка остаётся без наших доменов
    assert methods_of(own) == ["Emulation.setFocusEmulationEnabled", "Network.disable", "Target.detachFromTarget"]


def test_release_without_focus_emulation_only_detaches():
    client = Mock()
    client.call.return_value = {}
    make_tab(client, owned=False).release()
    assert methods_of(client) == ["Runtime.evaluate", "Target.detachFromTarget"]


@pytest.mark.parametrize("failure", [TabGone("closed by the user"), ChromeDisconnected("ws closed")])
def test_release_swallows_tab_gone(failure):
    client = Mock()
    client.call.return_value = {}
    released = []
    tab = make_tab(client, owned=False, on_release=released.append)
    tab.setup()
    client.call.side_effect = failure
    tab.release()  # вкладку закрыл пользователь или оборвалось соединение: наружу ничего
    assert methods_of(client)[-2:] == ["Emulation.setFocusEmulationEnabled", "Target.detachFromTarget"]
    assert "Target.closeTarget" not in methods_of(client)
    assert released == ["T1"]


def test_release_shares_one_timeout_between_both_calls(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("browser_hands.browser.time.monotonic", lambda: clock["now"])
    client = Mock()
    timeouts = []

    def call(method, params=None, *, session_id=None, timeout=None):
        timeouts.append(timeout)
        clock["now"] += 0.3  # Chrome отвечает медленно
        return {}

    client.call.side_effect = call
    tab = make_tab(client, owned=False)
    tab._focus_emulated = True
    tab.release(timeout=1.0)
    # уборка кэша — не больше трети бюджета; дальше — остаток общего дедлайна
    assert timeouts == [pytest.approx(1 / 3), pytest.approx(0.7), pytest.approx(0.4)]


def claimed_tab(client, answer):
    """Вкладка пользователя после claim("me"): `answer` — ответ Runtime.evaluate на метку."""
    client.call.return_value = answer
    tab = make_tab(client, owned=False)
    claimed = tab.claim("me")
    client.call.reset_mock()
    client.call.return_value = {}
    return tab, claimed


def test_release_removes_own_mark_with_the_cache_in_one_call_within_the_same_budget(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("browser_hands.browser.time.monotonic", lambda: clock["now"])
    client = Mock()
    tab, claimed = claimed_tab(client, {"result": {"value": "me"}})
    assert claimed is True
    timeouts = []

    def call(method, params=None, *, session_id=None, timeout=None):
        timeouts.append(timeout)
        clock["now"] += 0.3
        return {}

    client.call.side_effect = call
    tab._focus_emulated = True
    tab.release(timeout=1.0)
    assert methods_of(client) == ["Runtime.evaluate", "Emulation.setFocusEmulationEnabled", "Target.detachFromTarget"]
    cleanup = client.call.call_args_list[0].args[1]["expression"]
    assert cleanup == 'delete window.__jevFast; if (window.__bhOwner === "me") delete window.__bhOwner'
    assert timeouts == [pytest.approx(1 / 3), pytest.approx(0.7), pytest.approx(0.4)]  # бюджет прежний


def test_foreign_mark_release_does_not_touch_the_page():
    client = Mock()
    tab, claimed = claimed_tab(client, {"result": {"value": "other-server"}})
    assert claimed is False
    tab.release()
    assert methods_of(client) == ["Target.detachFromTarget"]


@pytest.mark.parametrize(
    "answer",
    [
        {"result": {"type": "undefined"}, "exceptionDetails": {"text": "Execution context was destroyed"}},
        CDPError("Runtime.evaluate", "Cannot find default execution context"),
        {"result": {}},
    ],
    ids=["context-destroyed", "no-context", "no-value"],
)
def test_claim_on_a_document_being_replaced_goes_on_and_cleans_up_conditionally(answer):
    client = Mock()
    client.call.side_effect = answer if isinstance(answer, Exception) else None
    tab, claimed = claimed_tab(client, None if isinstance(answer, Exception) else answer)
    client.call.side_effect = None
    assert claimed is True  # навигация стирает и чужую метку: от второго клиента защищает attached
    tab.release()
    assert '=== "me") delete window.__bhOwner' in client.call.call_args_list[0].args[1]["expression"]


def test_claim_timeout_is_raised():
    client = Mock()
    client.call.side_effect = CDPTimeout("Runtime.evaluate")
    with pytest.raises(CDPTimeout):
        make_tab(client, owned=False).claim("me")
    assert client.call.call_args.kwargs["timeout"] == 3.0


@pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")
def test_owner_mark_expressions_in_real_js():
    claims, cleanups = {}, {}
    for owner in ("server-a", "server-b"):  # выражения, которые шлёт Tab каждого из двух серверов
        client = Mock()
        client.call.return_value = {"result": {"value": owner}}
        tab = make_tab(client, owned=False)
        tab.claim(owner)
        tab.release()
        claims[owner], cleanups[owner] = (c.args[1]["expression"] for c in client.call.call_args_list[:2])
    script = f"""
      globalThis.window = {{__jevFast: 1}};
      const out = [eval({json.dumps(claims["server-a"])}), eval({json.dumps(claims["server-b"])})];
      eval({json.dumps(cleanups["server-b"])}); out.push(window.__bhOwner ?? null, "__jevFast" in window);
      eval({json.dumps(cleanups["server-a"])}); out.push(window.__bhOwner ?? null);
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=10)
    # A ставит метку, B получает метку A; уборка B не снимает метку A; уборка A — снимает
    assert json.loads(result.stdout) == ["server-a", "server-a", "server-a", False, None]


def test_borrowed_tab_refuses_navigation():
    client = Mock()
    with pytest.raises(RuntimeError, match="вкладке пользователя"):
        make_tab(client, owned=False).navigate("https://web.whatsapp.com/")
    client.call.assert_not_called()


def borrowed_screenshot_client(inner, css_width, css_height, page_y=0):
    client = Mock()
    client.call.side_effect = [
        {"result": {"value": inner}},  # measure: innerWidth, innerHeight, devicePixelRatio
        {"cssVisualViewport": {"pageX": 0, "pageY": page_y, "clientWidth": css_width, "clientHeight": css_height}},
        {"data": base64.b64encode(b"\xff\xd8jpeg").decode()},
    ]
    return client


def test_borrowed_screenshot_scales_to_target_width():
    client = borrowed_screenshot_client([1728, 1000, 2], 1728, 1000, page_y=400)
    tab = make_tab(client, owned=False)
    assert tab.screenshot() == b"\xff\xd8jpeg"
    assert methods_of(client) == ["Runtime.evaluate", "Page.getLayoutMetrics", "Page.captureScreenshot"]
    clip = client.call.call_args_list[2].args[1]["clip"]
    # Живой замер (headless, DPR 2): картинка = clip.width × scale × DPR = 1728 × 1120/3456 × 2 = 1120 px.
    assert clip == {"x": 0, "y": 400, "width": 1728, "height": 1000, "scale": pytest.approx(1120 / 3456)}
    assert tab.viewport == (1728, 1000) and tab.dpr == 2.0
    assert "Emulation.setDeviceMetricsOverride" not in methods_of(client)


def test_borrowed_screenshot_of_a_narrow_window_is_not_upscaled():
    client = borrowed_screenshot_client([1000, 700, 1], 1000, 700)
    make_tab(client, owned=False).screenshot()
    assert "clip" not in client.call.call_args_list[2].args[1]  # 1000 px ≤ 1120: снимок как есть


def test_borrowed_screenshot_respects_a_smaller_configured_scale():
    client = borrowed_screenshot_client([1000, 700, 1], 1000, 700)
    make_tab(client, owned=False, screenshot_scale=0.5).screenshot()
    assert client.call.call_args_list[2].args[1]["clip"]["scale"] == 0.5


def test_measure_keeps_previous_values_when_the_page_is_busy():
    client = Mock()
    client.call.return_value = {"exceptionDetails": {"text": "Execution context destroyed"}}
    tab = make_tab(client, owned=False)
    assert tab.measure() is False
    assert tab.viewport == (1120, 780) and tab.dpr == 1.0
    client.call.return_value = {"result": {"value": [1728, 1000, 2]}}
    assert tab.measure() is True
    assert tab.viewport == (1728, 1000) and tab.dpr == 2.0


def test_borrowed_snapshot_tracks_the_window_size_for_scrolling():
    p = page()
    p.update(w=1440, h=600)
    client = Mock()
    client.call.side_effect = [{"result": {"value": p}}, {}]
    tab = make_tab(client, owned=False)
    tab.observe()
    assert tab.viewport == (1440, 600)
    tab._act({"id": "scroll_down", "kind": "scroll", "delta": 560}, None)
    wheel = client.call.call_args_list[1].args[1]
    assert (wheel["x"], wheel["y"]) == (round(1440 * 550 / 1120), round(600 * 650 / 780))  # внутри окна


# --- контракт ожиданий и значения полей (docs/plan-waits.md §4.2) -------------------------------------------------


def test_await_ready_returns_wait_reason_and_pending_requests_and_empty_when_it_did_not_wait():
    client = Mock()
    client.call.return_value = READY_DONE
    tab = make_tab(client)
    result = tab.await_ready({"kind": "click", "node": 20})
    assert (result["wait_reason"], result["pending_requests"], result["reason"]) == ("quiet", 0, "quiet")
    assert params_of(client.call.call_args.args[1]["expression"])["action"] == {"kind": "click", "node": 20}
    assert tab.last_settle == result
    client.call.return_value = {"result": {"type": "undefined"}}
    assert tab.await_ready() == {} and tab.last_settle is None  # страница не дала итога
    tab.cancel = threading.Event()
    tab.cancel.set()
    calls = client.call.call_count
    assert tab.await_ready() == {} and client.call.call_count == calls  # отмена — не ждём


def test_await_change_waits_for_a_change_then_readiness_and_not_for_nothing(monkeypatch):
    frozen_clock(monkeypatch)
    client = Mock()
    client.call.return_value = READY_DONE
    client.call_until.return_value = CHANGED
    tab = make_tab(client)
    result = tab.await_change()
    assert (result["wait_reason"], result["change"], result["ready"], result["pending_requests"]) == (
        "change",
        "mutation",
        "quiet",
        0,
    )
    assert params_of(client.call.call_args.args[1]["expression"])["action"] == {"kind": "wait"}

    client.call.reset_mock()
    client.call_until.return_value = {"result": {"type": "object", "value": {"reason": "fuse", "ms": 1500}}}
    result = tab.await_change()
    assert (result["wait_reason"], result["change"]) == ("fuse", None)
    client.call.assert_not_called()  # изменений не было — готовность не ждём ещё раз

    client.call_until.return_value = {"exceptionDetails": {"text": "Execution context was destroyed"}}
    client.call.return_value = READY_DONE
    assert tab.await_change()["change"] == "navigation"  # смена документа — тоже изменение


def test_field_values_reads_all_fields_in_one_evaluate_on_fake_cdp():
    with FakeCDPServer() as server:
        values = {"result": {"type": "object", "value": ["book", None]}}
        server.on["Runtime.evaluate"] = lambda f, ws: reply(ws, f, values)
        client = CDPClient(server.url, call_timeout=2.0, connect_timeout=2.0)
        try:
            tab = Tab(client, "S1", "T1")
            specs = [{"node": 10, "label": "Search", "value": "секрет"}, {"node": "x", "label": 5}]
            assert tab.field_values(specs) == ["book", None]
            assert tab.field_values([]) == []  # пусто — без вызова
        finally:
            client.close()
    (frame,) = server.frames
    assert frame["method"] == "Runtime.evaluate" and frame["sessionId"] == "S1"
    assert frame["params"]["returnByValue"] is True and "awaitPromise" not in frame["params"]
    expression = frame["params"]["expression"]
    assert expression.startswith(browser.FIELD_VALUES) and expression.endswith(")")
    # в страницу уходят только узел и подпись; кривые — как null; значения полей и тексты шагов — нет
    assert json.loads(expression[len(browser.FIELD_VALUES) : -1]) == [
        {"node": 10, "label": "Search"},
        {"node": None, "label": None},
    ]
    assert "секрет" not in expression
    assert tab.take_timing().wait_ms == 0  # чтение — работа CDP, не ожидание


@pytest.mark.parametrize(
    "response",
    [
        {"exceptionDetails": {"text": "Execution context was destroyed"}},
        {"result": {"type": "object", "value": ["one"]}},  # не по числу полей
        {"result": {"type": "object", "value": None}},
    ],
    ids=["exception", "short", "null"],
)
def test_field_values_on_a_changing_document_is_stale(response):
    client = Mock()
    client.call.return_value = response
    with pytest.raises(StalePage):
        make_tab(client).field_values([{"node": 10, "label": "Search"}, {"node": 20, "label": "Go"}])


def test_field_values_expression_shares_label_and_role_code_with_the_snapshot():
    for helper in (
        "const safe = e =>",
        "const visible = e =>",
        "const name = (e,seen=new Set()) =>",
        "const role = e =>",
    ):
        assert helper in browser.FIELD_VALUES and helper in browser.READ_STATE


FAKE_DOM = """
class El {
  constructor(tagName, attrs, props) {
    Object.assign(this, {tagName, attrs, isConnected: true, childNodes: [], readOnly: false}, props);
  }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  closest() { return null; }
  matches() { return false; }
  checkVisibility() { return !this.hiddenForTest; }
}
const input = (label, value, props={}) => new El('INPUT', {'aria-label': label}, {type: 'text', value, ...props});
const editor = (label, text, props={}) =>
  new El('DIV', {'aria-label': label, role: 'textbox'}, {isContentEditable: true, innerText: text, ...props});
const search = input('Search', 'book');
const oldMessage = editor('Type a message', 'привет', {isConnected: false});  // пересоздан: узел вне документа
const hiddenMessage = editor('Type a message', 'чужое', {hiddenForTest: true});
const newMessage = editor('Type a message', '  привет 👋 ');
const password = input('Password', 'secret', {type: 'password'});
const readonly = input('Code', '1234', {readOnly: true});
const nameless = new El('INPUT', {role: 'combobox'}, {type: 'text', value: 'Gödel'});  // пересоздан с другой ролью
const elements = [search, hiddenMessage, newMessage, password, readonly, nameless];
globalThis.document = {querySelectorAll: () => elements, getElementById: () => null};
const specs = [
  {node: 1, label: 'Search'}, {node: 2, label: 'Type a message'}, {node: 3, label: 'Password'},
  {node: null, label: 'Code'}, {node: 99, label: 'Nothing'}, {node: null, label: null},
  {node: 1, label: 'Renamed'},  // подпись сменилась, узел тот же
  {node: 98, label: ''},  // поле без имени: любое видимое поле без имени, роль не сравнивается
];
globalThis.window = {__jevFast: {nodes: new Map([[1, search], [2, oldMessage], [3, password]])}};
const withCache = eval(EXPRESSION);
globalThis.window = {};  // документ сменился: кэша снимка нет — только по подписи
const withoutCache = eval(EXPRESSION);
console.log(JSON.stringify({withCache, withoutCache}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")
def test_field_values_expression_in_node_by_node_then_by_label():
    expression = browser.FIELD_VALUES + "specs)"  # исполняем с переменной specs скрипта
    script = FAKE_DOM.replace("EXPRESSION", json.dumps(expression))
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    # узел в документе — его значение; узел пересоздан — первое видимое редактируемое поле с той же подписью;
    # password не читается ни по узлу, ни по подписи; readonly — не поле ввода; нет ни узла, ни подписи — null
    assert out["withCache"] == ["book", "привет 👋", None, None, None, None, "book", "Gödel"]
    assert out["withoutCache"] == ["book", "привет 👋", None, None, None, None, None, "Gödel"]  # только подпись


# --- ожидания на FakeCDPServer с событиями Network (docs/plan-waits.md §5.2–5.4) ----------------------------------


def ready_value(reason="quiet", mutations=0):
    return {"result": {"type": "object", "value": {"reason": reason, "ms": 32, "mutations": mutations, "frames": 2}}}


def net(ws, method, rid, **params):
    event(ws, method, {"requestId": rid, **params}, "S1")


@pytest.fixture
def fake_tab():
    """Своя вкладка (учёт сети включён) на настоящем CDPClient к FakeCDPServer; сервер — `tab.server`."""
    with FakeCDPServer() as server:
        client = CDPClient(server.url, call_timeout=5.0, connect_timeout=2.0)
        tab = Tab(client, "S1", "T1")
        tab.server = server  # type: ignore[attr-defined]
        try:
            yield tab
        finally:
            client.close()


def evaluations(server, prefix=READY):
    return [f for f in server.sent("Runtime.evaluate") if f["params"]["expression"].startswith(prefix)]


def test_await_ready_waits_for_a_request_started_by_the_action_then_looks_again(fake_tab):
    server, passes = fake_tab.server, []

    def evaluate(frame, ws):
        passes.append(frame)
        if len(passes) == 1:
            net(ws, "Network.requestWillBeSent", "api", type="Fetch")
            reply(ws, frame, ready_value())
            time.sleep(0.15)  # ответ сервера приходит, пока клиент в насосе событий
            net(ws, "Network.loadingFinished", "api")
        else:
            reply(ws, frame, ready_value(mutations=3))  # ответ поменял DOM

    server.on["Runtime.evaluate"] = evaluate
    fake_tab._epoch = fake_tab.client.seq  # как перед командой действия
    result = fake_tab.await_ready({"kind": "click", "node": 20})
    assert (result["wait_reason"], result["pending_requests"], result["passes"]) == ("quiet", 0, 2)
    assert result["mutations"] == 3 and result["ms"] >= 150
    assert len(evaluations(server)) == 2
    assert fake_tab.take_timing().browser_ms == 0  # проходы и насос — ожидание


def test_background_websocket_and_event_stream_requests_do_not_hold_the_wait(fake_tab):
    server = fake_tab.server
    server.on["Target.getTargets"] = lambda f, ws: (
        net(ws, "Network.requestWillBeSent", "old", type="XHR"),
        reply(ws, f),
    )
    fake_tab.client.call("Target.getTargets")  # запрос начат до действия — фон
    fake_tab._epoch = fake_tab.client.seq

    def evaluate(frame, ws):
        net(ws, "Network.requestWillBeSent", "ws", type="WebSocket")
        net(ws, "Network.requestWillBeSent", "sse", type="Fetch")
        net(ws, "Network.responseReceived", "sse", type="Fetch", response={"mimeType": "text/event-stream"})
        net(ws, "Network.webSocketFrameReceived", "ws", response={"payloadData": "x"})
        reply(ws, frame, ready_value())

    server.on["Runtime.evaluate"] = evaluate
    result = fake_tab.await_ready({"kind": "click", "node": 20})
    assert (result["wait_reason"], result["pending_requests"], result["passes"]) == ("quiet", 0, 1)


def test_long_poll_hits_the_fuse_once_then_is_background(fake_tab):
    server = fake_tab.server
    fake_tab.fuse_s = 0.3

    def evaluate(frame, ws):
        if len(evaluations(server)) == 1:
            net(ws, "Network.requestWillBeSent", "poll", type="XHR")  # не кончится
        reply(ws, frame, ready_value())

    server.on["Runtime.evaluate"] = evaluate
    fake_tab._epoch = fake_tab.client.seq
    started = time.monotonic()
    result = fake_tab.await_ready({"kind": "click", "node": 20})
    assert (result["wait_reason"], result["pending_requests"]) == ("fuse", 1)
    assert 0.3 <= time.monotonic() - started < 1.0
    assert fake_tab.client.network["S1"].background == {"poll"}
    again = fake_tab.await_ready({"kind": "retry"})  # тот же запрос ещё в полёте — уже фон
    assert (again["wait_reason"], again["pending_requests"], again["passes"]) == ("quiet", 0, 1)


def test_cancel_during_the_network_wait_ends_it_empty(fake_tab):
    server = fake_tab.server
    fake_tab.cancel = threading.Event()

    def evaluate(frame, ws):
        net(ws, "Network.requestWillBeSent", "api", type="Fetch")
        reply(ws, frame, ready_value())
        time.sleep(0.05)
        fake_tab.cancel.set()  # Esc в клиенте MCP
        net(ws, "Network.dataReceived", "api", dataLength=1)  # насос видит событие и проверяет отмену

    server.on["Runtime.evaluate"] = evaluate
    fake_tab._epoch = fake_tab.client.seq
    started = time.monotonic()
    assert fake_tab.await_ready({"kind": "click", "node": 20}) == {}
    assert time.monotonic() - started < 1.0 and len(evaluations(server)) == 1


def test_await_change_is_woken_by_a_finished_request_then_waits_for_readiness(fake_tab):
    server = fake_tab.server
    server.on["Target.getTargets"] = lambda f, ws: (net(ws, "Network.requestWillBeSent", "r", type="XHR"), reply(ws, f))
    fake_tab.client.call("Target.getTargets")

    def evaluate(frame, ws):
        if frame["params"]["expression"].startswith(CHANGE):
            net(ws, "Network.loadingFinished", "r")  # страница ещё не поменялась, но ответ пришёл
            return  # промис CHANGE висит до своего предохранителя; его ответ клиент пропустит
        reply(ws, frame, ready_value(mutations=2))

    server.on["Runtime.evaluate"] = evaluate
    started = time.monotonic()
    result = fake_tab.await_change()
    assert (result["wait_reason"], result["change"], result["ready"]) == ("change", "network", "quiet")
    assert time.monotonic() - started < 1.0  # не ждали предохранитель CHANGE (1,5 с)
    assert [f["params"]["expression"][:20] for f in server.sent("Runtime.evaluate")] == [CHANGE[:20], READY[:20]]


def test_navigate_on_fake_cdp_waits_for_the_document_then_its_requests(fake_tab):
    server = fake_tab.server

    def navigate(frame, ws):
        net(ws, "Network.requestWillBeSent", "doc", type="Document")
        net(ws, "Network.requestWillBeSent", "app", type="Script")
        reply(ws, frame, {"frameId": "F", "loaderId": "L"})

    def evaluate(frame, ws):
        expression = frame["params"]["expression"]
        if expression.startswith(LOAD):
            net(ws, "Network.loadingFinished", "doc")
            reply(ws, frame, LOADED)
            time.sleep(0.05)
            net(ws, "Network.loadingFinished", "app")
        else:
            reply(ws, frame, ready_value())

    server.on["Page.navigate"] = navigate
    server.on["Runtime.evaluate"] = evaluate
    fake_tab.navigate("https://example.test/")
    methods = [(f["method"], f["params"].get("expression", "")[:12]) for f in server.frames]
    assert methods == [("Page.navigate", ""), ("Runtime.evaluate", LOAD[:12]), ("Runtime.evaluate", READY[:12])]
    assert fake_tab.last_settle is not None and fake_tab.last_settle["pending_requests"] == 0
    assert fake_tab.last_settle["passes"] == 1  # скрипт догрузился, пока шёл проход READY


def test_click_epoch_is_taken_right_before_the_input(fake_tab):
    server = fake_tab.server
    p = page()
    p.update(marker="m", page_key=["k"], guards={"20": "g"})

    def evaluate(frame, ws):
        expression = frame["params"]["expression"]
        if "c.pageKey()" in expression:
            net(ws, "Network.requestWillBeSent", "before", type="XHR")  # фон страницы до клика
            reply(ws, frame, {"result": {"value": [["k"], "g"]}})
        else:
            reply(ws, frame, {"result": {"value": {"x": 5, "y": 5}}})

    def mouse(frame, ws):
        if frame["params"]["type"] == "mousePressed":
            net(ws, "Network.requestWillBeSent", "click", type="Fetch")
        reply(ws, frame)

    server.on["Runtime.evaluate"] = evaluate
    server.on["Input.dispatchMouseEvent"] = mouse
    fake_tab.act(p["actions"][2], p)
    assert fake_tab.client.pending_since("S1", fake_tab._epoch) == 1  # только запрос от клика
    assert fake_tab.after_input == p["actions"][2]


def clicked_with_a_slow_request(fake_tab):
    """Клик по «Go» (узел 20) на FakeCDPServer: до клика страница начала свой запрос `boot`, клик — запрос `search`,
    ответ на который не приходит (медленный сервер). Итог — ожидание после клика (`await_ready`)."""
    server = fake_tab.server
    p = page()
    p.update(marker="m", page_key=["k"], guards={"20": "g"})
    server.on["Target.getTargets"] = lambda f, ws: (
        net(ws, "Network.requestWillBeSent", "boot", type="XHR"),
        reply(ws, f),
    )
    fake_tab.client.call("Target.getTargets")

    def evaluate(frame, ws):
        expression = frame["params"]["expression"]
        if "c.pageKey()" in expression:
            reply(ws, frame, {"result": {"value": [["k"], "g"]}})
        elif expression.startswith(READY):
            reply(ws, frame, ready_value())
        else:
            reply(ws, frame, {"result": {"value": {"x": 5, "y": 5}}})

    def mouse(frame, ws):
        if frame["params"]["type"] == "mousePressed":
            net(ws, "Network.requestWillBeSent", "search", type="Fetch")
        reply(ws, frame)

    server.on["Runtime.evaluate"] = evaluate
    server.on["Input.dispatchMouseEvent"] = mouse
    fake_tab.act(p["actions"][2], p)
    return fake_tab.await_ready(p["actions"][2])


def test_in_flight_counts_requests_of_the_action_even_after_the_fuse(fake_tab):
    fake_tab.fuse_s = 0.3
    assert fake_tab.action_epoch is None and fake_tab.in_flight(fake_tab.action_epoch) == 0  # действий ещё не было
    result = clicked_with_a_slow_request(fake_tab)
    assert (result["wait_reason"], result["pending_requests"]) == ("fuse", 1)
    assert fake_tab.client.pending_since("S1", fake_tab._epoch) == 0  # фон: ожиданий больше не держит
    assert fake_tab.action_epoch == fake_tab._epoch
    assert fake_tab.in_flight(fake_tab.action_epoch) == 1  # но ответа нет — страница загружается (`boot` — не от клика)
    assert fake_tab.in_flight(0) == 2 and fake_tab.in_flight(None) == 0
    fake_tab.server.on["Target.getTargets"] = lambda f, ws: (net(ws, "Network.loadingFinished", "search"), reply(ws, f))
    fake_tab.client.call("Target.getTargets")
    assert fake_tab.in_flight(fake_tab.action_epoch) == 0


def test_wait_after_the_fuse_is_woken_when_the_backgrounded_request_of_the_action_finishes(fake_tab):
    fake_tab.fuse_s = 0.3
    assert clicked_with_a_slow_request(fake_tab)["wait_reason"] == "fuse"
    server = fake_tab.server
    fake_tab.fuse_s = 5.0  # WAIT Jev: изменения нет до ответа сервера — будит ответ, а не предохранитель

    def evaluate(frame, ws):
        if frame["params"]["expression"].startswith(CHANGE):
            time.sleep(0.1)
            net(ws, "Network.loadingFinished", "search")  # фоновый запрос действия завершился
            return
        reply(ws, frame, ready_value(mutations=4))  # результаты поиска легли в DOM

    server.on["Runtime.evaluate"] = evaluate
    started = time.monotonic()
    result = fake_tab.await_change()
    assert (result["wait_reason"], result["change"], result["ready"]) == ("change", "network", "quiet")
    assert time.monotonic() - started < 2.0
    assert fake_tab.in_flight(fake_tab.action_epoch) == 0


def test_navigation_is_not_an_action_epoch(fake_tab):
    server = fake_tab.server

    def navigate(frame, ws):
        net(ws, "Network.requestWillBeSent", "poll", type="XHR")  # long-poll страницы с загрузки
        reply(ws, frame, {"frameId": "F", "loaderId": "L"})

    server.on["Page.navigate"] = navigate
    server.on["Runtime.evaluate"] = lambda f, ws: reply(
        ws, f, LOADED if f["params"]["expression"].startswith(LOAD) else ready_value()
    )
    fake_tab.fuse_s = 0.2
    fake_tab.navigate("https://example.test/")
    assert fake_tab.action_epoch is None and fake_tab.in_flight(fake_tab.action_epoch) == 0
    assert fake_tab.in_flight(0) == 1


def test_user_tab_without_network_never_reports_requests_in_flight():
    tab = make_tab(Mock(), owned=False, network=None)
    assert tab.network is False and tab.in_flight(0) == 0


def test_user_tab_on_fake_cdp_enables_no_network_and_waits_on_dom_signals_only(fake_tab):
    server = fake_tab.server
    tab = Tab(fake_tab.client, "S1", "T1", owned=False)
    tab.setup()
    assert "Network.enable" not in server.methods()

    def evaluate(frame, ws):
        net(ws, "Network.requestWillBeSent", "stray", type="Fetch")  # чужие события не держат ожидание
        reply(ws, frame, ready_value())

    server.on["Runtime.evaluate"] = evaluate
    result = tab.await_ready({"kind": "click", "node": 20})
    assert (result["wait_reason"], result["pending_requests"], result["passes"]) == ("quiet", 0, 1)
    tab.release()
    assert "Network.disable" not in server.methods()


# --- READY и CHANGE в node: виртуальное время, фейковые DOM, анимации, шрифты (tests/settle_harness.js) ------------

HARNESS = Path(__file__).with_name("settle_harness.js")


def in_node(expression, scenario):
    payload = json.dumps({"expression": expression, "scenario": scenario})
    done = subprocess.run(["node", str(HARNESS)], input=payload, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["failure"] is None and out["result"] is not None, out
    assert out["disconnected"]  # на выходе observer отключён
    assert out["pendingTasks"] == 0 and out["listeners"] == 0  # ходы MessageChannel и слушатели сняты
    return out


def ready_in_node(scenario, action=None, deadline_s=None):
    """Выражение, которое шлёт `Tab.await_ready`, исполненное в node на сценарии страницы из `settle_harness.js`."""
    client = Mock(call_timeout=30.0)
    client.call.return_value = READY_DONE
    tab = make_tab(client)
    if deadline_s is not None:
        tab.deadline = time.monotonic() + deadline_s
    tab.await_ready(action or {"kind": "click", "node": 20})
    return in_node(client.call.call_args.args[1]["expression"], scenario)


def change_in_node(scenario):
    client = Mock(call_timeout=30.0)
    client.call_until.return_value = {"result": {"type": "object", "value": {"reason": "fuse", "ms": 1500}}}
    make_tab(client).await_change()
    return in_node(client.call_until.call_args.args[1]["expression"], scenario)


node_only = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


@node_only
def test_ready_in_node_static_page_is_quiet_after_two_frames_and_cleans_up():
    out = ready_in_node("quiet")
    assert out["result"] == {"reason": "quiet", "ms": 32, "mutations": 0, "frames": 2}  # было ≈228 мс
    assert out["pendingTimers"] == 0 and out["pendingFrames"] == 0  # предохранитель снят, кадры не идут
    assert out["observed"] and out["options"]["subtree"] and "style" not in out["options"]["attributeFilter"]


@node_only
def test_ready_in_node_mutation_on_the_fifth_frame_waits_two_quiet_frames_after_it():
    out = ready_in_node("busy-5-frames")  # мутации в кадрах 16…80 мс
    assert out["result"]["reason"] == "quiet" and out["result"]["ms"] == 112  # кадры 96 и 112 тихие
    assert out["result"]["mutations"] == 5 and out["result"]["frames"] == 7
    out = ready_in_node("late-change")  # мутация на 80 мс, когда два тихих кадра уже были: готово раньше неё
    assert out["result"]["reason"] == "quiet" and out["result"]["ms"] == 32 and out["result"]["mutations"] == 0


@node_only
@pytest.mark.parametrize(
    "scenario",
    ["style-only", "same-class", "unwatched-attribute", "script-noise", "infinite-animation", "aria-busy-hidden"],
)
def test_ready_in_node_ignores_insignificant_mutations_spinners_and_hidden_busy(scenario):
    out = ready_in_node(scenario)
    assert out["result"]["reason"] == "quiet" and out["result"]["ms"] == 32
    assert out["result"]["mutations"] == 0


@node_only
def test_ready_in_node_ticker_with_quiet_gaps_is_ready_between_ticks():
    # style каждый кадр (не значим) + счётчик каждые 100 мс: между тиками есть два тихих кадра — готово
    out = ready_in_node("animation")
    assert out["result"]["reason"] == "quiet" and out["result"]["ms"] == 32


@node_only
@pytest.mark.parametrize(
    ("scenario", "ms"),
    [
        ("finite-animation", 304),  # анимация 300 мс: первый кадр после её finished
        ("aria-busy", 240),  # aria-busy снят на 200 мс: мутация + два тихих кадра
        ("fonts", 112),  # шрифты загрузились на 100 мс
        ("loading-doc", 160),  # readyState complete на 150 мс
    ],
)
def test_ready_in_node_waits_for_animations_busy_fonts_and_the_document(scenario, ms):
    out = ready_in_node(scenario)
    assert out["result"]["reason"] == "quiet" and out["result"]["ms"] == ms, out["result"]


@node_only
def test_ready_in_node_combobox_with_visible_options_is_ready_at_once():
    fill = {"kind": "fill", "node": 10}
    out = ready_in_node("combobox", fill)
    assert out["result"]["reason"] == "options" and out["result"]["ms"] == 32  # второй кадр
    out = ready_in_node("combobox-late", fill)
    assert out["result"]["reason"] == "options" and out["result"]["ms"] == 112  # первый кадр, где подсказки видны
    assert ready_in_node("combobox", {"kind": "click", "node": 10})["result"]["reason"] == "quiet"  # не ввод


@node_only
def test_ready_in_node_hidden_tab_without_frames_counts_message_channel_turns():
    for scenario in ("background", "background-busy"):  # тикер скрытой вкладки троттлится таймерами — не ждём его
        out = ready_in_node(scenario)
        assert out["result"] == {"reason": "frames", "ms": 0, "mutations": 0, "frames": 0}, out
        assert out["taskRuns"] == 2  # два хода MessageChannel без мутаций
    out = ready_in_node("background-loading")  # документ грузится: ход ждёт readystatechange, а не крутится
    assert out["result"]["reason"] == "frames" and out["result"]["ms"] == 150 and out["taskRuns"] == 3


@node_only
def test_ready_in_node_fuse_follows_the_deadline_and_no_body_is_not_an_error():
    out = ready_in_node("ticker")  # текст меняется каждый кадр
    assert out["result"]["reason"] == "fuse" and out["result"]["ms"] == 1500
    out = ready_in_node("ticker", deadline_s=0.8)  # предохранитель 0,3 с
    assert out["result"]["reason"] == "fuse" and 290 <= out["result"]["ms"] <= 300
    out = ready_in_node("no-body")
    assert out["result"]["reason"] == "quiet" and not out["observed"]


@node_only
@pytest.mark.parametrize(
    ("scenario", "reason", "ms"),
    [
        ("late-change", "mutation", 80),
        ("animation-end", "animation", 50),
        ("style-only", "fuse", 1500),  # style не значим: изменения нет
        ("quiet", "fuse", 1500),
    ],
)
def test_change_in_node_wakes_on_the_first_significant_change(scenario, reason, ms):
    out = change_in_node(scenario)
    assert out["result"] == {"reason": reason, "ms": ms}
    assert out["pendingTimers"] == 0  # предохранитель снят


# --- ожидания в настоящем headless Chrome (по запросу: BROWSER_HANDS_CHROME_TESTS=1) --------------------------------

CHROME_TESTS = os.environ.get("BROWSER_HANDS_CHROME_TESTS") == "1"
MOVE = "let i=0; const f=()=>{box.style.transform=`translateX(${i++%100}px)`; requestAnimationFrame(f)}; f();"
ANIMATION_PAGE = f"""<!doctype html><body><div id=box>box</div><span id=n>0</span><script>{MOVE}
setInterval(()=>{{n.textContent=String(+n.textContent+1)}}, 100);
</script></body>"""
STYLE_ONLY_PAGE = f"<!doctype html><body><div id=box>box</div><script>{MOVE}</script></body>"
EVERY_FRAME_PAGE = """<!doctype html><body><span id=n>0</span><script>
const f=()=>{n.textContent=String(+n.textContent+1); requestAnimationFrame(f)}; f();
</script></body>"""
chrome_only = pytest.mark.skipif(not CHROME_TESTS, reason="настоящий Chrome — по BROWSER_HANDS_CHROME_TESTS=1")


@contextmanager
def headless_chrome(tmp_path):
    from browser_hands.chrome import Chrome
    from browser_hands.config import BrowserConfig

    config = BrowserConfig(mode="launch", headless=True, launch_data_dir=tmp_path / "profile", connect_timeout_s=30.0)
    if not config.chrome_binary.exists():
        pytest.skip("нет Chrome")
    chrome = Chrome(config)
    chrome.connect()
    try:
        yield chrome
    finally:
        chrome.close()


@chrome_only
def test_ready_in_headless_chrome_frames_not_milliseconds(tmp_path):
    from urllib.parse import quote

    with headless_chrome(tmp_path) as chrome:
        tab = chrome.new_tab()  # setup: Network.enable с нулевыми буферами — без ошибки
        assert tab._network_enabled
        started = time.perf_counter()
        tab.navigate("data:text/html," + quote(ANIMATION_PAGE))
        assert time.perf_counter() - started < 15
        assert tab.last_settle is not None and tab.last_settle["reason"] == "quiet"
        result = tab.await_ready({"kind": "click", "node": 1})  # тикер 100 мс: между тиками два тихих кадра
        assert result["reason"] == "quiet" and result["ms"] < 150 and result["mutations"] <= 2, result

        tab.navigate("data:text/html," + quote(STYLE_ONLY_PAGE))
        result = tab.await_ready({"kind": "click", "node": 1})
        assert result["reason"] == "quiet" and result["ms"] < 100 and result["mutations"] == 0, result

        tab.navigate("data:text/html," + quote(EVERY_FRAME_PAGE))
        result = tab.await_ready({"kind": "click", "node": 1})  # значимая мутация каждый кадр — предохранитель
        assert result["reason"] == "fuse" and 1500 <= result["ms"] <= 1500 + 300, result
        assert tab.take_timing().browser_ms < 500  # почти всё — wait_ms


NETWORK_PAGE = """<!doctype html><body>
<button onclick="fetch('/slow').then(r=>r.text()).then(t=>{out.textContent=t})">Slow</button>
<button onclick="fetch('/poll').catch(()=>{})">Poll</button>
<div id=out></div></body>"""


@contextmanager
def network_server():
    """Страница с fetch к себе: /slow отвечает через 0,7 с, /poll — через 10 с (long-poll)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            pause, body = {"/slow": (0.7, b"done"), "/poll": (10.0, b"late")}.get(
                self.path, (0.0, NETWORK_PAGE.encode())
            )
            time.sleep(pause)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html" if not pause else "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # Chrome уже закрыт

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()
        httpd.server_close()


@chrome_only
def test_ready_in_headless_chrome_waits_for_fetch_and_long_poll_becomes_background(tmp_path):
    with headless_chrome(tmp_path) as chrome, network_server() as url:
        tab = chrome.new_tab()
        tab.navigate(url)
        state = tab.observe()
        slow = next(a for a in state["actions"] if a["label"] == "Slow")
        tab.act(slow, state)
        result = tab.await_ready(slow)
        assert result["wait_reason"] == "quiet" and result["pending_requests"] == 0, result
        assert result["ms"] >= 650 and result["passes"] >= 2, result  # ответ через 0,7 с — ждали его, не таймер
        state = tab.observe()
        assert "done" in state["text"]

        poll = next(a for a in state["actions"] if a["label"] == "Poll")
        tab.act(poll, state)
        result = tab.await_ready(poll)
        assert result["wait_reason"] == "fuse" and result["pending_requests"] == 1, result
        again = tab.await_ready({"kind": "retry"})  # long-poll ещё в полёте — теперь фон
        assert again["wait_reason"] == "quiet" and again["pending_requests"] == 0 and again["ms"] < 300, again


FIELDS_PAGE = """<!doctype html><body><input aria-label="Search">
<div id=msg contenteditable=true role=textbox aria-label="Type a message"></div>
<input type=password aria-label="Password" value="secret"></body>"""


@pytest.mark.skipif(not CHROME_TESTS, reason="настоящий Chrome — по BROWSER_HANDS_CHROME_TESTS=1")
def test_field_values_in_headless_chrome_follow_a_re_rendered_field(tmp_path):
    from urllib.parse import quote

    from browser_hands.chrome import Chrome
    from browser_hands.config import BrowserConfig

    config = BrowserConfig(mode="launch", headless=True, launch_data_dir=tmp_path / "profile", connect_timeout_s=30.0)
    if not config.chrome_binary.exists():
        pytest.skip("нет Chrome")
    chrome = Chrome(config)
    chrome.connect()
    try:
        tab = chrome.new_tab()
        tab.navigate("data:text/html," + quote(FIELDS_PAGE))
        for label, text in (("Search", "book"), ("Type a message", "привет")):
            state = tab.observe()
            field = next(a for a in state["actions"] if a["kind"] == "fill" and a["label"] == label)
            tab.act(field, state, text=text)
        state = tab.observe()
        fills = {a["label"]: a["node"] for a in state["actions"] if a["kind"] == "fill"}
        assert set(fills) == {"Search", "Type a message"}  # password в снимок не попадает
        specs = [{"node": fills[label], "label": label} for label in ("Search", "Type a message")]
        specs.append({"node": None, "label": "Password"})
        assert tab.field_values(specs) == ["book", "привет", None]
        # поле пересоздано (как у WhatsApp): новый узел с той же подписью, текст потерян — читается новое поле
        tab.evaluate("(() => { const old=document.getElementById('msg'); old.replaceWith(old.cloneNode(false)); })()")
        assert tab.field_values(specs) == ["book", "", None]
        tab.evaluate("document.getElementById('msg').textContent='снова'")
        assert tab.field_values(specs) == ["book", "снова", None]
    finally:
        chrome.close()
