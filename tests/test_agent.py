"""Цикл Agent на Mock-вкладке и подменённых моделях: без Chrome, сети и платных вызовов."""

import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from browser_hands import agent as loop
from browser_hands.agent import Agent
from browser_hands.browser import StalePage, fingerprint
from browser_hands.cdp import TabGone
from browser_hands.config import ModelConfig, RunConfig
from browser_hands.model import Decision, ModelClients, TextHelper
from browser_hands.types import Timing

JPEG = b"\xff\xd8\xff\xe0jpeg"


def page(text="Search"):
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": text,
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


def changing_pages():
    n = 0
    while True:
        n += 1
        yield page(f"Search {n}")


def decision(action="e1", operation="TYPE_TEXT", cost=None):
    return Decision(
        choice=action,
        operation=operation,
        target="1",
        confidence=1.0,
        probabilities={action: 1.0},
        cost=cost,
        latency_ms=10,
    )


def make_tab():
    tab = Mock()
    tab.fresh.return_value = True
    tab.observe.side_effect = changing_pages()
    tab.take_timing.return_value = Timing(browser_ms=2, wait_ms=1)
    tab.screenshot.return_value = JPEG
    return tab


def make_agent(tab=None, cancel=None, **run):
    chrome = Mock()
    chrome.new_tab.return_value = tab or make_tab()
    clients = ModelClients(ModelConfig(jev_api_key="test", text_api_key="test"), http=Mock())
    run.setdefault("timeout_s", 30.0)
    return Agent(chrome, clients, "https://example.test/", "Find a book", RunConfig(**run), cancel=cancel)


def scripted(*decisions):
    """Подмена choose: отдаёт решения по очереди и считает вызовы."""
    queue = list(decisions)
    return Mock(side_effect=lambda *a, **k: queue.pop(0))


@pytest.fixture
def runner():
    a = make_agent()
    a._tab = Mock(
        fresh=Mock(return_value=True), observe=Mock(return_value=page()), take_timing=Mock(return_value=Timing())
    )
    a._page = page()
    a._decision = decision()
    return a


# --- перенос из источника (tests/test_agent.py:160-228, 315-320) ---------------------------------------------


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner._tab.fresh.return_value = False
    with pytest.raises(StalePage):
        runner._act()
    runner._tab.act.assert_not_called()
    assert runner._decision is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", TextHelper(model="test", latency_ms=10)))
    monkeypatch.setattr(loop, "field_text", helper)
    runner._tab.act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner._act()
    runner._decision = decision()
    runner._act()
    assert helper.call_count == 1
    assert runner._tab.act.call_count == 2  # первый вызов отклонён до любого ввода
    assert runner._pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", TextHelper(model="test", latency_ms=10)))
    monkeypatch.setattr(loop, "field_text", helper)
    runner._tab.act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner._act()
    runner._page["text"] = "Different page context"
    runner._decision = decision()
    runner._act()
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner._decision = decision("wait", "WAIT")
        runner._act()
    assert len(runner._history) == 5 and len(runner.steps) == 5
    assert all(s.page_changed is False for s in runner.steps)


def test_stale_observation_preserves_executed_action(runner):
    runner._decision = decision("e3", "CLICK")
    runner._tab.observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner._act()
    assert runner._history[-1]["action"] == "Go"
    assert runner.steps[-1].target == "Go" and runner.steps[-1].page_changed is None
    runner._tab.act.assert_called_once()


def test_navigation_during_prediction_reobserves_without_action(runner, monkeypatch):
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    runner._tab.fresh.side_effect = StalePage("Document navigating")
    runner._tick()
    assert runner._decision is None
    choose.assert_not_called()
    runner._tab.act.assert_not_called()
    runner._tab.observe.assert_called_once()


# --- новый Agent.run ---------------------------------------------------------------------------------------------


def test_run_to_done_sums_cost_timing_and_model_calls(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab)
    monkeypatch.setattr(
        loop,
        "choose",
        scripted(
            decision("e3", "CLICK", cost=0.001),
            decision("e1", "TYPE_TEXT", cost=0.001),
            decision("DONE", "DONE", cost=0.001),
        ),
    )
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", TextHelper("t", 5, cost=0.0005))))
    result = agent.run()
    assert result.status == "done" and result.error is None
    assert [s.operation for s in result.steps] == ["CLICK", "TYPE_TEXT"]
    assert [s.index for s in result.steps] == [1, 2]
    assert result.steps[1].text == "book"
    assert result.model_calls == 4
    assert result.cost == pytest.approx(0.0035)
    assert result.steps[0].cost == pytest.approx(0.001) and result.steps[1].cost == pytest.approx(0.0015)
    assert result.screenshot_jpeg == JPEG
    assert result.url == "https://example.test/" and result.title == "Search"
    assert sum((s.timing for s in result.steps), Timing()).browser_ms <= result.timing.browser_ms
    assert result.timing.browser_ms > 0
    tab.navigate.assert_called_once()
    tab.close.assert_called_once()
    assert not result.tab_kept


def test_cost_is_none_when_no_usage_cost_arrives(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(loop, "choose", scripted(decision("DONE", "DONE")))
    result = agent.run()
    assert result.status == "done" and result.cost is None and result.steps == []


def test_timeout_when_deadline_is_spent_before_the_first_action(monkeypatch):
    clock = {"now": 1000.0}
    fake_time = SimpleNamespace(monotonic=lambda: clock["now"], perf_counter=time.perf_counter)
    monkeypatch.setattr(loop, "time", fake_time)
    tab = make_tab()
    agent = make_agent(tab, timeout_s=10)

    def slow_navigation(*_a, **_k):
        clock["now"] += 11  # загрузка съела весь дедлайн

    tab.navigate.side_effect = slow_navigation
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "timeout" and "timed out" in result.error
    choose.assert_not_called()
    tab.close.assert_called_once()
    assert result.screenshot_jpeg == JPEG


def test_step_limit_after_max_steps_actions(monkeypatch):
    agent = make_agent(max_steps=2)
    choose = Mock(return_value=decision("e3", "CLICK"))
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "step_limit"
    assert len(result.steps) == 2 and result.model_calls == 3


def test_model_failure_is_failed_and_the_tab_is_closed(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=RuntimeError("Model connection failed; no action executed.")))
    result = agent.run()
    assert result.status == "failed" and "Model connection failed" in result.error
    assert result.model_calls == 1
    tab.act.assert_not_called()
    tab.close.assert_called_once()


def test_keep_open_releases_instead_of_closing(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab, keep_open=True)
    monkeypatch.setattr(loop, "choose", scripted(decision("DONE", "DONE")))
    result = agent.run()
    assert result.tab_kept
    tab.close.assert_not_called()
    tab.release.assert_called_once()


def test_dead_tab_is_failed_without_screenshot(monkeypatch):
    tab = make_tab()
    tab.act.side_effect = TabGone("crashed")
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", scripted(decision("e3", "CLICK")))
    result = agent.run()
    assert result.status == "failed" and "TabGone" in result.error
    assert result.screenshot_jpeg is None
    tab.screenshot.assert_not_called()


def test_three_unchanged_actions_are_blocked(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = None
    tab.observe.return_value = page()
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3", "CLICK")))
    result = agent.run()
    assert result.status == "blocked" and "No page change" in result.error
    assert len(result.steps) == 3


def test_model_blocked_and_stale_done_are_not_actions(monkeypatch):
    tab = make_tab()
    tab.fresh.side_effect = [True, False, True, True]  # predict, DONE устарел, predict, BLOCKED свеж
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", scripted(decision("DONE", "DONE"), decision("BLOCKED", "BLOCKED")))
    result = agent.run()
    assert result.status == "blocked" and "BLOCKED" in result.error
    assert result.steps == [] and result.model_calls == 2
    tab.act.assert_not_called()


def test_missing_jev_key_is_a_configuration_error_before_any_tab():
    agent = make_agent()
    agent.clients.config.jev_api_key = ""
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        agent.run()
    agent.chrome.new_tab.assert_not_called()


def test_empty_goal_is_rejected():
    with pytest.raises(ValueError, match="goal"):
        Agent(Mock(), Mock(), "https://example.test/", "  ", RunConfig())


# --- отмена ------------------------------------------------------------------------------------------------------


def cancelled_result(result):
    return result.status == "failed" and result.error == "cancelled"


def test_cancel_before_run_opens_no_tab_and_calls_no_model(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    agent = make_agent(cancel=cancel)
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert cancelled_result(result) and result.model_calls == 0
    agent.chrome.new_tab.assert_not_called()
    choose.assert_not_called()


def test_cancel_during_the_model_call_executes_nothing_and_closes_the_tab(monkeypatch):
    cancel = threading.Event()
    tab = make_tab()
    agent = make_agent(tab, cancel=cancel)

    def choose(*_a, **_k):
        cancel.set()  # отмена пришла, пока Jev думал
        return decision("e3", "CLICK")

    monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
    result = agent.run()
    assert cancelled_result(result) and result.model_calls == 1 and result.steps == []
    tab.act.assert_not_called()
    tab.screenshot.assert_not_called()  # финальный кадр отменённому не нужен
    tab.close.assert_called_once()


def test_cancel_during_text_generation_types_nothing(monkeypatch):
    cancel = threading.Event()
    tab = make_tab()
    agent = make_agent(tab, cancel=cancel)
    monkeypatch.setattr(loop, "choose", scripted(decision("e1", "TYPE_TEXT")))

    def field_text(*_a, **_k):
        cancel.set()
        return "book", TextHelper("t", 5)

    monkeypatch.setattr(loop, "field_text", Mock(side_effect=field_text))
    result = agent.run()
    assert cancelled_result(result) and result.model_calls == 2
    tab.act.assert_not_called()
    tab.close.assert_called_once()


def test_cancel_after_an_action_stops_before_the_next_model_call(monkeypatch):
    cancel = threading.Event()
    tab = make_tab()
    tab.act.side_effect = lambda *_a, **_k: cancel.set()
    agent = make_agent(tab, cancel=cancel)
    choose = Mock(return_value=decision("e3", "CLICK"))
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert cancelled_result(result)
    assert choose.call_count == 1 and tab.act.call_count == 1 and len(result.steps) == 1
    tab.close.assert_called_once()


def test_cancel_respects_keep_open(monkeypatch):
    cancel = threading.Event()
    tab = make_tab()
    agent = make_agent(tab, cancel=cancel, keep_open=True)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=lambda *a, **k: cancel.set() or decision("DONE", "DONE")))
    result = agent.run()
    assert cancelled_result(result) and result.tab_kept
    tab.release.assert_called_once()


# --- логи без персональных данных ------------------------------------------------------------------------------


def test_step_log_has_label_length_and_host_but_text_and_url_only_at_debug(monkeypatch, caplog):
    tab = make_tab()
    secret_url = "https://example.test/account?token=abc123"
    after = page("Signed in")
    after["url"] = secret_url
    after["fingerprint"] = fingerprint(after)
    tab.observe.side_effect = [page(), after, after]
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", scripted(decision("e1", "TYPE_TEXT"), decision("DONE", "DONE")))
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("jane@example.com", TextHelper("t", 5))))
    logger = logging.getLogger("browser_hands.agent")
    logger.addHandler(caplog.handler)  # пакетный логгер может не передавать записи корню (logging.py)
    try:
        with caplog.at_level(logging.DEBUG, logger="browser_hands.agent"):
            agent.run()
    finally:
        logger.removeHandler(caplog.handler)
    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    step = next(m for m in info if m.startswith("step 1"))
    assert "TYPE_TEXT 'Search' len=16 @ example.test" in step
    assert all("jane@example.com" not in m and "token" not in m for m in info)
    assert any("jane@example.com" in m and secret_url in m for m in debug)
