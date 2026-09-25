"""Tab на Mock-клиенте: атомарный снимок, свежесть до ввода, без повтора мутаций, финальный кадр."""

import base64
from copy import deepcopy
from unittest.mock import Mock

import pytest

from browser_hands.browser import NavigationFailed, StalePage, Tab, fingerprint, url_host
from browser_hands.cdp import ChromeDisconnected, TabGone


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


def test_settle_wait_after_input_is_read_only_and_counted_as_wait():
    p = page()
    client = Mock()
    client.call.side_effect = [{"result": {"value": None}}, {"result": {"value": p}}]
    tab = make_tab(client)
    tab.after_input = p["actions"][0]
    tab.observe()
    settle = client.call.call_args_list[0]
    assert settle.args[1]["awaitPromise"] is True
    assert '"node": 10' in settle.args[1]["expression"]
    assert tab.after_input is None
    timing = tab.take_timing()
    assert timing.model_ms == 0 and timing.text_ms == 0
    assert tab.take_timing().wait_ms == 0  # счётчики обнулились


def test_navigate_waits_for_ready_state_and_reports_errors():
    client = Mock()
    client.call.side_effect = [{"frameId": "F"}, {"result": {"value": "loading"}}, {"result": {"value": "complete"}}]
    tab = make_tab(client)
    tab.navigate("https://example.test/")
    assert client.call.call_count == 3
    assert tab.take_timing().browser_ms == 0  # загрузка — это wait_ms

    client.call.side_effect = [{"frameId": "F", "errorText": "net::ERR_NAME_NOT_RESOLVED"}]
    with pytest.raises(NavigationFailed, match="nope.invalid failed: net::ERR_NAME_NOT_RESOLVED") as info:
        tab.navigate("https://user:pw@nope.invalid/private?token=1")
    assert "token" not in str(info.value) and "pw" not in str(info.value)  # в логи INFO попадает только хост


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
