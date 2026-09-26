"""Tab на Mock-клиенте: атомарный снимок, свежесть до ввода, без повтора мутаций, финальный кадр."""

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from browser_hands import browser
from browser_hands.browser import SETTLE, NavigationFailed, StalePage, Tab, fingerprint, url_host
from browser_hands.cdp import CDPError, CDPTimeout, ChromeDisconnected, TabGone


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


SETTLED = {"result": {"type": "object", "value": {"reason": "quiet", "ms": 230, "mutations": 4}}}


def settle_params(expression):
    """Параметры успокоения из выражения `SETTLE + json + ")"` — как их увидит страница."""
    assert expression.startswith(SETTLE) and expression.endswith(")")
    return json.loads(expression[len(SETTLE) : -1])


def test_settle_expression_watches_significant_mutations_with_constants_in_params():
    assert "MutationObserver" in SETTLE and "attributeFilter" in SETTLE and "attributeOldValue" in SETTLE
    assert "requestAnimationFrame" in SETTLE and "disconnect()" in SETTLE
    assert "script,style,template,noscript" in SETTLE
    client = Mock()
    client.call.return_value = SETTLED
    tab = make_tab(client)
    tab.settle(page()["actions"][0])
    params = settle_params(client.call.call_args.args[1]["expression"])
    assert params == {
        "action": {"kind": "fill", "node": 10},  # метка и значение поля в страницу не уходят
        "floor": 50,
        "quiet": 200,
        "ceiling": 1500,
        "attributes": list(browser.SETTLE_ATTRIBUTES),
    }
    assert "style" not in params["attributes"]  # JS-анимации пишут style каждый кадр
    assert {"class", "hidden", "aria-expanded", "aria-busy", "disabled"} <= set(params["attributes"])
    assert client.call.call_args.args[1]["awaitPromise"] is True


def test_settle_wait_after_input_is_read_only_and_counted_as_wait(monkeypatch):
    p = page()
    ticks = iter([0.0, 0.3, 0.3, 0.31])  # успокоение 300 мс, снимок 10 мс
    monkeypatch.setattr("browser_hands.browser.time.perf_counter", lambda: next(ticks))
    client = Mock()
    client.call.side_effect = [SETTLED, {"result": {"value": p}}]
    tab = make_tab(client)
    tab.after_input = p["actions"][0]
    tab.observe()
    settle = client.call.call_args_list[0]
    assert settle.args[0] == "Runtime.evaluate" and settle.args[1]["awaitPromise"] is True
    assert settle_params(settle.args[1]["expression"])["action"] == {"kind": "fill", "node": 10}
    assert client.call.call_args_list[1].args[1]["expression"] == browser.READ_STATE
    assert tab.after_input is None
    assert tab.last_settle == {"reason": "quiet", "ms": 230, "mutations": 4}
    timing = tab.take_timing()
    assert timing.wait_ms == 300 and timing.browser_ms == 10  # успокоение — ожидание, не работа CDP
    assert timing.model_ms == 0 and timing.text_ms == 0
    assert tab.take_timing().wait_ms == 0  # счётчики обнулились


def test_wait_action_settles_like_any_other_action():
    p = page()
    client = Mock()
    tab = make_tab(client)
    tab.fresh = Mock(return_value=True)
    tab._sleep = Mock()
    tab.act(p["actions"][3], p)
    tab._sleep.assert_called_once_with(0.1)  # 100 мс, как раньше, и затем успокоение
    assert tab.after_input == p["actions"][3]
    client.call.side_effect = [SETTLED, {"result": {"value": p}}]
    tab.observe()
    assert settle_params(client.call.call_args_list[0].args[1]["expression"])["action"] == {"kind": "wait"}


@pytest.mark.parametrize(
    "interrupted",
    [
        CDPError("Runtime.evaluate", "Execution context was destroyed."),
        {"exceptionDetails": {"text": "Execution context was destroyed"}},
        {"result": {"type": "undefined"}},
    ],
    ids=["cdp-error", "exception", "no-value"],
)
def test_observe_snapshots_the_page_when_settle_is_interrupted(interrupted):
    p = page()
    client = Mock()
    client.call.side_effect = [interrupted, {"result": {"value": p}}]
    tab = make_tab(client)
    tab.last_settle = {"reason": "quiet", "ms": 1, "mutations": 0}  # от прошлого действия
    tab.after_input = p["actions"][2]
    assert tab.observe()["actions"] == p["actions"]
    assert tab.last_settle is None
    assert client.call.call_count == 2


def test_settle_ceiling_stops_short_of_the_run_deadline(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("browser_hands.browser.time.monotonic", lambda: clock["now"])
    client = Mock(call_timeout=30.0)
    client.call.return_value = SETTLED
    tab = make_tab(client)
    tab.deadline = 101.2  # 1,2 с до дедлайна: потолок 0,7 с, на ответ остаётся 0,5 с
    tab.settle({"kind": "click", "node": 20})
    assert settle_params(client.call.call_args.args[1]["expression"])["ceiling"] == 700
    assert client.call.call_args.kwargs["timeout"] == pytest.approx(1.2)  # таймаут CDP — остаток дедлайна, как у всех

    tab.deadline = 130.0
    tab.settle({"kind": "click", "node": 20})
    assert settle_params(client.call.call_args.args[1]["expression"])["ceiling"] == 1500

    client.call.reset_mock()
    tab.deadline = 100.4  # меньше 0,5 с: не ждём вовсе
    assert tab.settle({"kind": "click", "node": 20}) is None
    client.call.assert_not_called()
    assert tab.last_settle is None


def test_settle_is_skipped_after_cancel():
    client = Mock()
    tab = make_tab(client)
    tab.cancel = threading.Event()
    tab.cancel.set()
    assert tab.settle({"kind": "wait"}) is None
    client.call.assert_not_called()


def test_settle_reason_and_time_are_logged_at_debug(caplog):
    client = Mock()
    client.call.return_value = SETTLED
    logger = logging.getLogger("browser_hands.browser")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="browser_hands.browser"):
            make_tab(client).settle({"kind": "click", "node": 20})
    finally:
        logger.removeHandler(caplog.handler)
    assert "settle quiet 230 мс, мутаций 4" in [r.getMessage() for r in caplog.records]


def test_navigate_waits_for_ready_state_then_settles_and_reports_errors():
    client = Mock()
    client.call.side_effect = [
        {"frameId": "F"},
        {"result": {"value": "loading"}},
        {"result": {"value": "complete"}},
        SETTLED,
    ]
    tab = make_tab(client)
    tab.navigate("https://example.test/")
    assert methods_of(client) == ["Page.navigate", "Runtime.evaluate", "Runtime.evaluate", "Runtime.evaluate"]
    settle = client.call.call_args_list[3].args[1]
    assert settle["awaitPromise"] is True and settle_params(settle["expression"])["action"] == {"kind": "load"}
    assert tab.last_settle is not None
    assert tab.take_timing().browser_ms == 0  # загрузка и успокоение — это wait_ms

    client.call.side_effect = [{"frameId": "F", "errorText": "net::ERR_NAME_NOT_RESOLVED"}]
    with pytest.raises(NavigationFailed, match="nope.invalid failed: net::ERR_NAME_NOT_RESOLVED") as info:
        tab.navigate("https://user:pw@nope.invalid/private?token=1")
    assert "token" not in str(info.value) and "pw" not in str(info.value)  # в логи INFO попадает только хост


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
    ticks = [0.0, 1.3, 1.3, 1.31]  # первая попытка снимка «висит» 1,3 с на навигации

    def clock():
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    monkeypatch.setattr("browser_hands.browser.time.perf_counter", clock)
    client = Mock()
    client.call.side_effect = [{"result": {"value": None}}, {"result": {"value": p}}]
    tab = make_tab(client)
    tab._sleep = lambda _s: None
    tab.observe()
    timing = tab.take_timing()
    assert timing.wait_ms == 1300 and timing.browser_ms == 10


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


# --- SETTLE в node: виртуальное время, фейковые DOM и MutationObserver (tests/settle_harness.js) ------------------

HARNESS = Path(__file__).with_name("settle_harness.js")


def settle_in_node(scenario, action=None, deadline_s=None):
    """Выражение, которое шлёт `Tab.settle`, исполненное в node на сценарии страницы из `settle_harness.js`."""
    client = Mock(call_timeout=30.0)
    client.call.return_value = SETTLED
    tab = make_tab(client)
    if deadline_s is not None:
        tab.deadline = time.monotonic() + deadline_s
    tab.settle(action or {"kind": "click", "node": 20})
    payload = json.dumps({"expression": client.call.call_args.args[1]["expression"], "scenario": scenario})
    done = subprocess.run(["node", str(HARNESS)], input=payload, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["failure"] is None and out["result"] is not None, out
    assert out["disconnected"]  # на выходе observer отключён
    return out


node_only = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


@node_only
def test_settle_in_node_quiet_page_is_ready_after_quiet_period_and_cleans_up():
    out = settle_in_node("quiet")
    assert out["result"]["reason"] == "quiet" and 200 <= out["result"]["ms"] <= 216
    assert out["result"]["mutations"] == 0
    assert out["pendingTimers"] == 0 and out["pendingFrames"] == 0  # потолок и запасной таймер сняты, кадры не идут
    assert out["observed"] and out["options"]["subtree"] and "style" not in out["options"]["attributeFilter"]


@node_only
def test_settle_in_node_endless_animation_and_ticker_stop_at_the_ceiling_not_later():
    # style каждый кадр (не значим) + счётчик каждые 100 мс (значим): тишины 200 мс не бывает
    out = settle_in_node("animation")
    assert out["result"]["reason"] == "ceiling"
    assert 1500 <= out["result"]["ms"] <= 1500 + 16  # потолок и не больше одного кадра сверху
    assert 13 <= out["result"]["mutations"] <= 15  # только счётчик


@node_only
@pytest.mark.parametrize("scenario", ["style-only", "same-class", "unwatched-attribute", "script-noise"])
def test_settle_in_node_ignores_insignificant_mutations(scenario):
    out = settle_in_node(scenario)
    assert out["result"]["reason"] == "quiet" and 200 <= out["result"]["ms"] <= 216
    assert out["result"]["mutations"] == 0


@node_only
def test_settle_in_node_waits_for_quiet_after_the_last_change():
    out = settle_in_node("late-change")  # class меняется на 120 мс
    assert out["result"]["reason"] == "quiet" and 320 <= out["result"]["ms"] <= 336
    assert out["result"]["mutations"] == 1


@node_only
def test_settle_in_node_combobox_with_visible_options_is_ready_at_once():
    fill = {"kind": "fill", "node": 10}
    out = settle_in_node("combobox", fill)
    assert out["result"]["reason"] == "options" and out["result"]["ms"] <= 32  # второй кадр
    out = settle_in_node("combobox-late", fill)
    assert out["result"]["reason"] == "options" and 100 <= out["result"]["ms"] <= 116
    assert settle_in_node("combobox", {"kind": "click", "node": 10})["result"]["reason"] == "quiet"  # не ввод


@node_only
def test_settle_in_node_background_tab_without_frames_uses_timers():
    out = settle_in_node("background")
    assert out["result"]["reason"] == "frames" and out["result"]["ms"] == 250  # quiet + floor по таймеру
    out = settle_in_node("background-busy")
    assert out["result"]["reason"] == "ceiling" and out["result"]["ms"] == 1500


@node_only
def test_settle_in_node_ceiling_follows_the_deadline_and_no_body_is_not_an_error():
    out = settle_in_node("animation", deadline_s=0.8)  # потолок 0,3 с
    assert out["result"]["reason"] == "ceiling" and 290 <= out["result"]["ms"] <= 316
    out = settle_in_node("no-body")
    assert out["result"]["reason"] == "quiet" and not out["observed"]


# --- SETTLE в настоящем headless Chrome (по запросу: BROWSER_HANDS_CHROME_TESTS=1) --------------------------------

CHROME_TESTS = os.environ.get("BROWSER_HANDS_CHROME_TESTS") == "1"
MOVE = "let i=0; const f=()=>{box.style.transform=`translateX(${i++%100}px)`; requestAnimationFrame(f)}; f();"
ANIMATION_PAGE = f"""<!doctype html><body><div id=box>box</div><span id=n>0</span><script>{MOVE}
setInterval(()=>{{n.textContent=String(+n.textContent+1)}}, 100);
</script></body>"""
STYLE_ONLY_PAGE = f"<!doctype html><body><div id=box>box</div><script>{MOVE}</script></body>"


@pytest.mark.skipif(not CHROME_TESTS, reason="настоящий Chrome — по BROWSER_HANDS_CHROME_TESTS=1")
def test_settle_in_headless_chrome_endless_animation_is_capped(tmp_path):
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
        started = time.perf_counter()
        tab.navigate("data:text/html," + quote(ANIMATION_PAGE))
        loaded_s = time.perf_counter() - started
        assert tab.last_settle is not None and tab.last_settle["reason"] == "ceiling"
        started = time.perf_counter()
        result = tab.settle({"kind": "click", "node": 1})
        took_s = time.perf_counter() - started
        assert result is not None and result["reason"] == "ceiling"
        assert 1500 <= result["ms"] <= 1500 + 50 and took_s < 1.5 + 0.3, (result, took_s)
        assert result["mutations"] >= 10  # счётчик считается, style — нет (иначе было бы ~90)
        assert result["mutations"] <= 20 and loaded_s < 15

        tab.navigate("data:text/html," + quote(STYLE_ONLY_PAGE))
        result = tab.settle({"kind": "click", "node": 1})
        assert result is not None and result["reason"] == "quiet" and result["ms"] < 400, result
        assert tab.take_timing().browser_ms < 500  # почти всё — wait_ms
    finally:
        chrome.close()
