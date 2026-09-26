"""Цикл Agent на Mock-вкладке и подменённых моделях: без Chrome, сети и платных вызовов."""

import base64
import json
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from browser_hands import agent as loop
from browser_hands.agent import Agent
from browser_hands.browser import (
    FIELD_VALUES,
    FOCUSED,
    MARKER,
    MEASURE,
    READ_STATE,
    RESOLVE_TARGET,
    StalePage,
    fingerprint,
)
from browser_hands.cdp import CDPError, CDPTimeout, ChromeDisconnected, TabGone
from browser_hands.chrome import AMBIGUOUS, BUSY, Chrome, TabTaken
from browser_hands.config import BrowserConfig, ModelConfig, RunConfig, Settings, Thresholds
from browser_hands.model import Decision, InvalidTextValue, ModelClients, ModelTimeout, TextHelper
from browser_hands.scenario import ScenarioError, ScenarioStep
from browser_hands.types import Timing
from tests.fake_cdp import FakeCDPServer, reply
from tests.fake_http import FakeModelServer, answer, keepalive

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


def changing_pages(typed=None):
    """Каждый снимок — новая страница; `typed` — {узел: текст}: поле показывает то, что в него напечатали."""
    n = 0
    while True:
        n += 1
        state = page(f"Search {n}")
        if typed:
            for action in state["actions"]:
                if action.get("node") in typed:
                    action["value"] = typed[action["node"]]
            state["fingerprint"] = fingerprint(state)
        yield state


def decision(action="e1", operation="TYPE_TEXT", cost=None, step_done=None, confidence=1.0):
    return Decision(
        choice=action,
        operation=operation,
        target="1",
        confidence=confidence,
        probabilities={action: 1.0},
        cost=cost,
        latency_ms=10,
        step_done=step_done,
    )


def make_tab(owned=True):
    """Mock-вкладка: снимки меняются, напечатанный текст остаётся в поле (`tab.typed`) — как на обычной странице.

    `field_values` читает то же: по узлу, иначе по подписи поля (как `Tab.field_values`); `tab.remount()` — сайт
    пересоздал поля, напечатанное пропало."""
    tab = Mock()
    tab.owned = owned
    tab.fresh.return_value = True
    typed = {}
    by_label = {}

    def act(action, _page, text=None):
        if action["kind"] == "fill":
            typed[action["node"]] = text
            by_label[action["label"]] = text
        return {"executed": action["id"]}

    def field_values(specs):
        return [typed[s["node"]] if s["node"] in typed else by_label.get(s["label"]) for s in specs]

    def remount():
        typed.clear()
        by_label.clear()

    tab.act.side_effect = act
    tab.field_values.side_effect = field_values
    tab.remount = remount
    tab.await_ready.return_value = {}
    tab.await_change.return_value = {}
    tab.typed = typed
    tab.observe.side_effect = changing_pages(typed)
    tab.take_timing.return_value = Timing(browser_ms=2, wait_ms=1)
    tab.screenshot.return_value = JPEG
    tab.location.return_value = None  # Target.getTargetInfo не ответил
    return tab


def make_agent(tab=None, cancel=None, user_tab=None, steps=None, goal="Find a book", **run):
    """Своя вкладка `tab`; `user_tab` — открытая вкладка пользователя, которую найдёт `find_user_tab`; `steps` —
    сценарий (режим шагов)."""
    chrome = Mock()
    chrome.new_tab.return_value = tab or make_tab()
    chrome.find_user_tab.return_value = "T-user" if user_tab is not None else None
    chrome.attach_tab.return_value = user_tab
    clients = ModelClients(ModelConfig(jev_api_key="test", text_api_key="test"), http=Mock())
    run.setdefault("timeout_s", 30.0)
    return Agent(chrome, clients, "https://example.test/", goal, RunConfig(**run), steps=steps, cancel=cancel)


DONE = decision("DONE", "DONE")


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
    assert result.tab == "new"
    agent.chrome.find_user_tab.assert_called_once_with("https://example.test/", skip=set())


def test_run_hands_its_deadline_and_cancel_to_the_tab(monkeypatch):
    cancel = threading.Event()
    tab = make_tab()
    agent = make_agent(tab, cancel=cancel)
    monkeypatch.setattr(loop, "choose", scripted(DONE))
    agent.run()
    assert tab.cancel is cancel  # успокоение после действий не начинается после отмены
    assert tab.deadline is None  # _finish снимает дедлайн для финального кадра


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


BLOCKED = decision("BLOCKED", "BLOCKED")


def test_model_blocked_and_stale_done_are_not_actions(monkeypatch):
    tab = make_tab()
    # predict, DONE устарел; predict, BLOCKED устарел; predict, BLOCKED свеж → второй шанс; predict, BLOCKED свеж
    tab.fresh.side_effect = [True, False, True, False, True, True, True, True]
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", scripted(DONE, BLOCKED, BLOCKED, BLOCKED))
    result = agent.run()
    assert result.status == "blocked" and "BLOCKED" in result.error
    assert result.steps == [] and result.model_calls == 4
    tab.act.assert_not_called()
    tab.await_change.assert_called_once_with()  # устаревший BLOCKED шанс не тратит


# --- второй шанс перед BLOCKED ---------------------------------------------------------------------------------


def waiting_tab(wait_ms=300, owned=True):
    """Вкладка, у которой ожидание изменения (`await_change`) копит `wait_ms`, а `take_timing` отдаёт накопленное."""
    tab = make_tab(owned=owned)
    pending = {"wait": 0}

    def await_change():
        pending["wait"] += wait_ms
        return {"reason": "change", "ms": wait_ms, "mutations": 1}

    def take_timing():
        timing, pending["wait"] = Timing(browser_ms=2, wait_ms=pending["wait"]), 0
        return timing

    tab.await_change.side_effect = await_change
    tab.take_timing.side_effect = take_timing
    return tab


def test_second_chance_settles_reobserves_and_asks_again(monkeypatch, caplog):
    tab = waiting_tab()
    agent = make_agent(tab)
    seen = []

    def choose(_clients, state, _goal, history, **_k):
        seen.append((state["text"], [h["action"] for h in history]))
        return [BLOCKED, decision("e3", "CLICK"), DONE][len(seen) - 1]

    monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
    logger = logging.getLogger("browser_hands.agent")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="browser_hands.agent"):
            result = agent.run()
    finally:
        logger.removeHandler(caplog.handler)
    assert result.status == "done" and result.error is None
    assert len(result.steps) == 1 and result.steps[0].operation == "CLICK"
    assert result.model_calls == 3  # 2-й вызов Jev — второй шанс, шагом не считается
    tab.await_change.assert_called_once_with()
    assert seen[0][0] != seen[1][0]  # Jev спросили по новому снимку
    assert seen[1][1] == ["Wait for the page to update"]  # и он видит, что ждали
    assert agent._history[0]["kind"] == "wait" and agent._history[0]["page_changed"] is True
    assert result.steps[0].timing.wait_ms == 300 and result.timing.wait_ms == 300  # ожидание — wait_ms
    lines = {r.getMessage() for r in caplog.records if r.name == "browser_hands.agent" and "BLOCKED" in r.getMessage()}
    assert lines == {"BLOCKED: жду изменения страницы и спрашиваю ещё раз (example.test)"}


def test_blocked_twice_is_blocked_after_a_second_look(monkeypatch):
    tab = waiting_tab()
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", scripted(BLOCKED, BLOCKED))
    result = agent.run()
    assert result.status == "blocked" and "after a second look" in result.error
    assert result.model_calls == 2 and result.steps == []
    tab.await_change.assert_called_once()
    tab.act.assert_not_called()


def test_second_chance_is_not_repeated_while_the_page_does_not_change(monkeypatch):
    tab = waiting_tab()
    tab.observe.side_effect = None
    tab.observe.return_value = page()  # ничего не меняется: ни ожидание, ни WAIT
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", scripted(BLOCKED, decision("wait", "WAIT"), BLOCKED))
    result = agent.run()
    assert result.status == "blocked" and "after a second look" in result.error
    assert result.model_calls == 3 and [s.operation for s in result.steps] == ["WAIT"]
    # BLOCKED → шанс → WAIT (без изменений) → BLOCKED — без второго шанса; WAIT Jev — тоже ожидание изменения
    assert tab.await_change.call_count == 2


def test_second_chance_comes_back_after_an_action_that_changed_the_page(monkeypatch):
    tab = waiting_tab()
    agent = make_agent(tab)
    choose = scripted(BLOCKED, decision("e3", "CLICK"), BLOCKED, DONE)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.model_calls == 4 and len(result.steps) == 1
    assert tab.await_change.call_count == 2


def test_second_chance_honours_the_deadline(monkeypatch):
    tab = waiting_tab()
    clock = {"now": 1000.0}
    monkeypatch.setattr(loop, "time", SimpleNamespace(monotonic=lambda: clock["now"], perf_counter=time.perf_counter))

    def await_change():
        clock["now"] += 40  # страница менялась, пока не вышел дедлайн
        return {"reason": "fuse", "ms": 1500, "mutations": 9}

    tab.await_change.side_effect = await_change
    agent = make_agent(tab, timeout_s=30)
    choose = scripted(BLOCKED, DONE)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "timeout" and choose.call_count == 1 and result.model_calls == 1

    spent = make_tab()
    agent = make_agent(spent, timeout_s=30)

    def late_blocked(*_a, **_k):
        clock["now"] += 40  # Jev думал до дедлайна: ждать уже нечего
        return BLOCKED

    monkeypatch.setattr(loop, "choose", Mock(side_effect=late_blocked))
    result = agent.run()
    assert result.status == "timeout" and result.model_calls == 1
    spent.await_change.assert_not_called()


def test_second_chance_honours_cancel(monkeypatch):
    cancel = threading.Event()
    tab = waiting_tab()
    tab.await_change.side_effect = lambda: cancel.set()  # Esc, пока ждали изменения страницы
    agent = make_agent(tab, cancel=cancel)
    choose = scripted(BLOCKED, DONE)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert cancelled_result(result) and choose.call_count == 1 and result.model_calls == 1
    tab.close.assert_called_once()

    cancel = threading.Event()
    tab = waiting_tab()
    agent = make_agent(tab, cancel=cancel)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=lambda *a, **k: cancel.set() or BLOCKED))
    result = agent.run()
    assert cancelled_result(result) and result.model_calls == 1
    tab.await_change.assert_not_called()  # отмена пришла, пока Jev думал: не ждём


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


# --- вкладка пользователя ----------------------------------------------------------------------------------------


def never_closed(tab):
    """Вкладка пользователя: только release(), один раз; close() и navigate() не вызывались."""
    tab.close.assert_not_called()
    tab.navigate.assert_not_called()
    tab.release.assert_called_once_with()


def test_user_tab_is_used_without_navigation_and_only_released(monkeypatch):
    user = make_tab(owned=False)
    agent = make_agent(user_tab=user, keep_open=True)
    monkeypatch.setattr(loop, "choose", scripted(decision("e3", "CLICK"), decision("DONE", "DONE")))
    result = agent.run()
    assert result.status == "done" and result.tab == "user"
    assert result.tab_kept is False  # keep_open не про неё: она и так остаётся
    assert result.screenshot_jpeg == JPEG and len(result.steps) == 1
    agent.chrome.attach_tab.assert_called_once_with("T-user", screenshot_quality=60, screenshot_scale=1.0)
    agent.chrome.new_tab.assert_not_called()
    user.act.assert_called_once()
    never_closed(user)


def test_new_tab_flag_skips_the_search(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab, user_tab=make_tab(owned=False), new_tab=True)
    monkeypatch.setattr(loop, "choose", scripted(decision("DONE", "DONE")))
    result = agent.run()
    assert result.tab == "new"
    agent.chrome.find_user_tab.assert_not_called()
    agent.chrome.attach_tab.assert_not_called()
    agent.chrome.new_tab.assert_called_once()
    tab.navigate.assert_called_once()
    tab.close.assert_called_once()


def test_no_user_tab_falls_back_to_own(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab)
    monkeypatch.setattr(loop, "choose", scripted(decision("DONE", "DONE")))
    result = agent.run()
    assert result.tab == "new"
    agent.chrome.find_user_tab.assert_called_once()
    agent.chrome.attach_tab.assert_not_called()
    tab.navigate.assert_called_once()


def test_user_tab_closed_by_user_is_failed_and_not_closed(monkeypatch):
    user = make_tab(owned=False)
    user.act.side_effect = TabGone("detached")
    agent = make_agent(user_tab=user)
    monkeypatch.setattr(loop, "choose", scripted(decision("e3", "CLICK")))
    result = agent.run()
    assert result.status == "failed" and "TabGone" in result.error and result.tab == "user"
    assert result.screenshot_jpeg is None
    never_closed(user)


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("Model connection failed"), ChromeDisconnected("ws closed"), CDPTimeout("Runtime.evaluate")],
)
def test_errors_in_user_tab_only_release_it(monkeypatch, failure):
    user = make_tab(owned=False)
    agent = make_agent(user_tab=user)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=failure))
    result = agent.run()
    assert result.status == "failed" and result.tab == "user"
    never_closed(user)


def test_cancel_in_user_tab_releases_and_never_closes(monkeypatch):
    cancel = threading.Event()
    user = make_tab(owned=False)
    agent = make_agent(user_tab=user, cancel=cancel)

    def choose(*_a, **_k):
        cancel.set()  # Esc, пока Jev думал
        return decision("e3", "CLICK")

    monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
    result = agent.run()
    assert cancelled_result(result) and result.tab == "user" and result.steps == []
    user.act.assert_not_called()
    user.screenshot.assert_not_called()
    never_closed(user)


def test_attach_failure_is_failed_without_falling_back_to_own_tab(monkeypatch):
    agent = make_agent(user_tab=make_tab(owned=False))
    agent.chrome.attach_tab.side_effect = CDPError("Target.attachToTarget", "No target with given id found")
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "failed" and "attachToTarget" in result.error
    assert result.tab is None and result.screenshot_jpeg is None
    agent.chrome.new_tab.assert_not_called()  # без тихого перехода в свою вкладку
    choose.assert_not_called()


def test_failed_before_tab_has_tab_none(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    agent = make_agent(cancel=cancel, user_tab=make_tab(owned=False))
    monkeypatch.setattr(loop, "choose", Mock())
    result = agent.run()
    assert cancelled_result(result) and result.tab is None
    agent.chrome.find_user_tab.assert_not_called()
    agent.chrome.attach_tab.assert_not_called()

    lookup_fails = make_agent()
    lookup_fails.chrome.find_user_tab.side_effect = ChromeDisconnected("ws closed")
    result = lookup_fails.run()
    assert result.status == "failed" and result.tab is None


def fake_user_chrome(server, tmp_path):
    """Chrome в attach на фейковом сервере: открыта вкладка пользователя U1 (web.whatsapp.com), 1728×1000 @ DPR 2."""
    state = page()
    state.update(
        url="https://web.whatsapp.com/", w=1728, h=1000, marker="m1", page_key=["k"], guards={"10": "g", "20": "g"}
    )
    server.state = state  # что отдаёт снимок страницы; тест может менять

    def evaluate(frame, ws):
        if server.page_js(frame, ws):  # метка __bhOwner и уборка в release()
            return
        expression = frame["params"]["expression"]
        if expression == "document.readyState":  # своя вкладка после Page.navigate
            value = "complete"
        elif expression == MEASURE:
            value = [1728, 1000, 2]
        elif expression == READ_STATE:
            value = state
        elif expression == MARKER:
            value = state["marker"]
        elif expression.startswith(RESOLVE_TARGET):
            value = {"x": 100, "y": 200}
        elif "c.pageKey()" in expression:
            value = [["k"], "g"]
        elif expression.startswith(FIELD_VALUES):  # значения полей по узлу — из того же состояния страницы
            specs = json.loads(expression[len(FIELD_VALUES) : -1])
            fields = {a["node"]: a.get("value", "") for a in state["actions"] if a["kind"] == "fill"}
            value = [fields.get(spec["node"]) for spec in specs]
        else:
            value = None  # SETTLE
        reply(ws, frame, {"result": {"type": "object", "value": value}})

    server.targets = [
        {"targetId": "U1", "type": "page", "title": "WhatsApp", "url": "https://web.whatsapp.com/", "attached": False}
    ]
    server.on["Runtime.evaluate"] = evaluate
    server.on["Page.getLayoutMetrics"] = lambda f, ws: reply(
        ws, f, {"cssVisualViewport": {"pageX": 0, "pageY": 0, "clientWidth": 1728, "clientHeight": 1000}}
    )
    server.on["Page.captureScreenshot"] = lambda f, ws: reply(ws, f, {"data": base64.b64encode(JPEG).decode()})
    data_dir = tmp_path / "Chrome"
    data_dir.mkdir(parents=True)
    (data_dir / "DevToolsActivePort").write_text(f"{server.port}\n/devtools/browser/fake")
    chrome = Chrome(BrowserConfig(mode="attach", chrome_data_dir=data_dir, connect_timeout_s=2.0))
    chrome.connect()
    return chrome


def test_user_tab_end_to_end_on_fake_chrome_never_closes_navigates_or_resizes(monkeypatch, tmp_path):
    monkeypatch.setattr(loop, "choose", scripted(decision("e3", "CLICK"), decision("DONE", "DONE")))
    with FakeCDPServer() as server:
        chrome = fake_user_chrome(server, tmp_path)
        clients = ModelClients(ModelConfig(jev_api_key="test", text_api_key="test"), http=Mock())
        run = RunConfig(timeout_s=30.0, keep_open=True)
        result = Agent(chrome, clients, "https://web.whatsapp.com", "Open the chat", run).run()
        chrome.close()
    assert result.status == "done" and result.tab == "user" and not result.tab_kept
    assert result.screenshot_jpeg == JPEG and [s.target for s in result.steps] == ["Go"]
    methods = server.methods()
    forbidden = {
        "Target.createTarget",
        "Target.closeTarget",
        "Target.activateTarget",
        "Page.navigate",
        "Page.reload",
        "Emulation.setDeviceMetricsOverride",
    }
    assert forbidden.isdisjoint(methods)
    clicks = server.sent("Input.dispatchMouseEvent")
    assert {f["sessionId"] for f in clicks} == {"S-U1"} and len(clicks) == 2
    clip = server.sent("Page.captureScreenshot")[0]["params"]["clip"]
    assert clip["width"] == 1728 and clip["scale"] == pytest.approx(1120 / 3456)
    focus = [f["params"]["enabled"] for f in server.sent("Emulation.setFocusEmulationEnabled")]
    assert focus == [True, False]
    assert [f["params"] for f in server.sent("Target.detachFromTarget")] == [{"sessionId": "S-U1"}]
    assert methods.index("Emulation.setFocusEmulationEnabled", methods.index("Page.captureScreenshot")) < methods.index(
        "Target.detachFromTarget"
    )
    # после клика — одно ожидание готовности (await_ready агента), сразу за ним снимок: observe() второй раз не ждёт
    frames = server.frames
    release = max(i for i, f in enumerate(frames) if f["method"] == "Input.dispatchMouseEvent")
    snapshot = next(i for i in range(release, len(frames)) if frames[i]["params"].get("expression") == READ_STATE)
    waits = [f for f in frames[release:snapshot] if f["params"].get("awaitPromise")]
    assert len(waits) == 1


def run_on_fake_chrome(monkeypatch, tmp_path, url, prepare, *decisions):
    """Прогон Agent в attach на фейковом Chrome (`fake_user_chrome`); `prepare(server)` — вкладки и метки."""
    choose = scripted(*decisions)
    monkeypatch.setattr(loop, "choose", choose)
    with FakeCDPServer() as server:
        chrome = fake_user_chrome(server, tmp_path)
        prepare(server)
        clients = ModelClients(ModelConfig(jev_api_key="test", text_api_key="test"), http=Mock())
        result = Agent(chrome, clients, url, "Send hi", RunConfig(timeout_s=30.0)).run()
        chrome.close()
    return result, server, choose


def nothing_done(result, server, choose):
    """failed до вкладки: своей не открыли, никуда не перешли, Jev не звали."""
    assert result.status == "failed" and result.tab is None and result.screenshot_jpeg is None
    assert result.steps == [] and result.model_calls == 0
    choose.assert_not_called()
    for method in ("Target.createTarget", "Page.navigate", "Input.dispatchMouseEvent", "Target.closeTarget"):
        assert method not in server.methods()


def test_busy_user_tab_fails_without_own_tab_navigation_or_model(monkeypatch, tmp_path):
    def prepare(server):
        server.targets[0]["attached"] = True  # DevTools, расширение или другая сессия

    result, server, choose = run_on_fake_chrome(monkeypatch, tmp_path, "https://web.whatsapp.com", prepare)
    nothing_done(result, server, choose)
    assert result.error == BUSY.format(site="web.whatsapp.com")
    assert "Target.attachToTarget" not in server.methods()


def test_foreign_owner_mark_is_busy_and_the_next_free_tab_is_used(monkeypatch, tmp_path):
    def taken(server):
        server.windows["U1"] = {"__bhOwner": "other-server"}

    result, server, choose = run_on_fake_chrome(monkeypatch, tmp_path, "https://web.whatsapp.com", taken)
    nothing_done(result, server, choose)
    assert result.error == BUSY.format(site="web.whatsapp.com")
    assert server.windows["U1"] == {"__bhOwner": "other-server"}  # чужую метку не сняли

    def taken_and_free(server):
        taken(server)
        server.targets.append({**server.targets[0], "targetId": "U2"})

    result, server, _ = run_on_fake_chrome(
        monkeypatch, tmp_path / "2", "https://web.whatsapp.com", taken_and_free, decision("e3", "CLICK"), DONE
    )
    assert result.status == "done" and result.tab == "user"
    assert {f["sessionId"] for f in server.sent("Input.dispatchMouseEvent")} == {"S-U2"}
    assert [f["params"] for f in server.sent("Target.detachFromTarget")] == [
        {"sessionId": "S-U1"},
        {"sessionId": "S-U2"},
    ]
    assert server.windows["U1"] == {"__bhOwner": "other-server"} and server.windows["U2"] == {}


def test_tabs_in_two_profiles_fail_without_an_exact_match(monkeypatch, tmp_path):
    def two_profiles(server):
        server.targets = [
            {**server.targets[0], "targetId": "M1", "url": "https://web.whatsapp.com/x", "browserContextId": "MAIN"},
            {**server.targets[0], "targetId": "I1", "url": "https://web.whatsapp.com/y", "browserContextId": "INCOG"},
        ]

    result, server, choose = run_on_fake_chrome(monkeypatch, tmp_path, "https://web.whatsapp.com", two_profiles)
    nothing_done(result, server, choose)
    assert result.error == AMBIGUOUS.format(site="web.whatsapp.com")


def test_site_open_on_another_page_gets_own_tab_with_navigation(monkeypatch, tmp_path):
    url = "https://web.whatsapp.com/send?phone=123"
    result, server, _ = run_on_fake_chrome(monkeypatch, tmp_path, url, lambda server: None, DONE)
    assert result.status == "done" and result.tab == "new"
    assert [f["params"]["url"] for f in server.sent("Page.navigate")] == [url]
    assert [f["params"]["targetId"] for f in server.sent("Target.attachToTarget")] == ["T1"]  # U1 не тронута
    assert [f["params"]["targetId"] for f in server.sent("Target.closeTarget")] == ["T1"]


def test_tab_taken_between_lookup_and_attach_counts_as_busy(monkeypatch):
    user = make_tab(owned=False)
    agent = make_agent(user_tab=user)
    seen = []

    def find_user_tab(url, *, skip):
        seen.append(set(skip))
        return ["U1", "U2"][len(seen) - 1]

    agent.chrome.find_user_tab.side_effect = find_user_tab
    agent.chrome.attach_tab.side_effect = [TabTaken("U1"), user]
    monkeypatch.setattr(loop, "choose", scripted(DONE))
    result = agent.run()
    assert result.status == "done" and result.tab == "user" and seen == [set(), {"U1"}]
    assert [c.args[0] for c in agent.chrome.attach_tab.call_args_list] == ["U1", "U2"]
    agent.chrome.new_tab.assert_not_called()


# --- ожидание элементов для действия (экран загрузки) ----------------------------------------------------------


def empty_page(text="Loading…"):
    """Снимок без fill/click/select: экран загрузки (есть только scroll и wait)."""
    state = page(text)
    state["url"] = "https://example.test/?token=abc"
    state["actions"] = [
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
        {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
    ]
    state["fingerprint"] = fingerprint(state)
    return state


def fake_clock(monkeypatch, tab, step_s=5.0):
    """Подмена time в агенте; каждое ожидание изменения страницы двигает часы на `step_s`."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(loop, "time", SimpleNamespace(monotonic=lambda: clock["now"], perf_counter=time.perf_counter))

    def await_change():
        clock["now"] += step_s
        return {}

    tab.await_change.side_effect = await_change
    return clock


@pytest.mark.parametrize("owned", [True, False], ids=["own-tab", "user-tab"])
def test_no_interactive_elements_delays_the_model_until_they_appear(monkeypatch, caplog, owned):
    tab = make_tab(owned=owned)
    tab.observe.side_effect = [empty_page(), empty_page(), empty_page(), page(), page()]
    agent = make_agent(tab) if owned else make_agent(user_tab=tab)
    seen = []

    def choose(_clients, state, *_a, **_k):
        seen.append((state, list(agent.steps)))
        return decision("DONE", "DONE")

    monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
    logger = logging.getLogger("browser_hands.agent")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="browser_hands.agent"):
            result = agent.run()
    finally:
        logger.removeHandler(caplog.handler)
    assert result.status == "done" and result.model_calls == 1 and result.steps == []
    assert len(seen) == 1 and seen[0][1] == []
    assert any(a["kind"] == "click" for a in seen[0][0]["actions"])  # Jev увидел уже готовую страницу
    assert tab.await_change.call_count == 3 and all(c.args == () for c in tab.await_change.call_args_list)
    tab.pause.assert_not_called()  # ждём изменения страницы, а не опрашиваем по таймеру
    waiting = [r.getMessage() for r in caplog.records if "Нет элементов" in r.getMessage()]
    # одна строка на ожидание (запись может прийти дважды: handler на логгере и корень), только хост, без токена
    assert set(waiting) == {"Нет элементов для действия, жду до 25 с (example.test)"}


def test_empty_page_wait_stops_at_ceiling_then_asks_the_model(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = None
    tab.observe.return_value = empty_page()
    fake_clock(monkeypatch, tab)
    agent = make_agent(tab, timeout_s=90)
    choose = Mock(return_value=decision("DONE", "DONE"))
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.model_calls == 1
    assert tab.await_change.call_count == 5  # 25 «секунд» по 5, потом Jev решает по пустой странице
    assert choose.call_args.args[1]["actions"][0]["kind"] == "scroll"


def test_empty_page_ceiling_survives_a_stale_snapshot(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = [empty_page(), empty_page(), StalePage("navigating")] + [empty_page()] * 10
    fake_clock(monkeypatch, tab)
    agent = make_agent(tab, timeout_s=90)
    choose = Mock(return_value=decision("DONE", "DONE"))
    monkeypatch.setattr(loop, "choose", choose)
    assert agent.run().status == "done"
    assert tab.await_change.call_count == 5  # StalePage не обнуляет потолок: всё равно 25 с


def test_empty_page_wait_respects_the_deadline(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = None
    tab.observe.return_value = empty_page()
    fake_clock(monkeypatch, tab)
    agent = make_agent(tab, timeout_s=10)
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "timeout" and result.model_calls == 0
    assert tab.await_change.call_count == 2
    choose.assert_not_called()
    tab.close.assert_called_once()


@pytest.mark.parametrize("owned", [True, False], ids=["own-tab", "user-tab"])
def test_empty_page_wait_stops_on_cancel(monkeypatch, owned):
    cancel = threading.Event()
    tab = make_tab(owned=owned)
    tab.observe.side_effect = None
    tab.observe.return_value = empty_page()
    tab.await_change.side_effect = lambda: cancel.set()  # Esc во время ожидания
    agent = make_agent(tab, cancel=cancel) if owned else make_agent(user_tab=tab, cancel=cancel)
    choose = Mock()
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert cancelled_result(result) and result.model_calls == 0
    assert tab.await_change.call_count == 1
    choose.assert_not_called()
    tab.screenshot.assert_not_called()
    if owned:
        tab.close.assert_called_once()
    else:
        never_closed(tab)


def test_empty_page_mid_run_waits_only_briefly_then_asks_the_model(monkeypatch):
    tab = make_tab()
    pages = [page(), empty_page("middle of a long article, no controls on screen")]
    tab.observe.side_effect = lambda *_a, **_k: pages.pop(0) if len(pages) > 1 else pages[0]
    clock = fake_clock(monkeypatch, tab, step_s=0.25)
    asked = []

    def choose(*_a, **_k):
        asked.append(clock["now"])
        return decision("e3", "CLICK") if len(asked) == 1 else DONE

    monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
    result = make_agent(tab, timeout_s=90).run()
    assert result.status == "done" and result.model_calls == 2
    assert loop.EMPTY_PAGE_WAIT_LATER_S == 1.0  # после действия страница уже успокоилась: форма после Submit — не ждать
    # не 25 с: после первого вызова Jev — 1 с (ожидание по событиям; каждое здесь — 0,25 «с»); ожидание после клика
    # (await_ready) часы не двигает
    assert tab.await_change.call_count == 4 and asked[1] - asked[0] == 1.0


def test_empty_page_mid_run_wait_respects_the_deadline(monkeypatch):
    tab = make_tab()
    pages = [page(), empty_page()]
    tab.observe.side_effect = lambda *_a, **_k: pages.pop(0) if len(pages) > 1 else pages[0]
    fake_clock(monkeypatch, tab, step_s=0.25)
    choose = scripted(decision("e3", "CLICK"))
    monkeypatch.setattr(loop, "choose", choose)
    result = make_agent(tab, timeout_s=0.5).run()  # дедлайн раньше потолка 1 с
    assert result.status == "timeout" and choose.call_count == 1 and tab.await_change.call_count == 2


def test_second_chance_end_to_end_on_fake_chrome_waits_once_then_reads_the_page(monkeypatch, tmp_path):
    with FakeCDPServer() as server:
        chrome = fake_user_chrome(server, tmp_path)
        marks = []

        def choose(*_a, **_k):
            marks.append(len(server.frames))
            return [BLOCKED, DONE][len(marks) - 1]

        monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
        clients = ModelClients(ModelConfig(jev_api_key="test", text_api_key="test"), http=Mock())
        result = Agent(chrome, clients, "https://web.whatsapp.com", "Open the chat", RunConfig(timeout_s=30.0)).run()
        chrome.close()
    assert result.status == "done" and result.tab == "user" and result.model_calls == 2 and result.steps == []
    between = server.frames[marks[0] : marks[1]]
    assert all(f["method"] == "Runtime.evaluate" for f in between)  # никаких Input.*, навигации и закрытий
    settles = [
        i
        for i, f in enumerate(between)
        if f["params"].get("awaitPromise") and "MutationObserver" in f["params"]["expression"]
    ]
    assert len(settles) == 1
    assert between[settles[0] + 1]["params"]["expression"] == READ_STATE  # сразу после ожидания — снимок
    assert {f["sessionId"] for f in between} == {"S-U1"}
    assert {"Target.closeTarget", "Page.navigate", "Emulation.setDeviceMetricsOverride"}.isdisjoint(server.methods())


# --- повтор текстовой модели ------------------------------------------------------------------------------------


def invalid(reason="null", cost=0.00004):
    return InvalidTextValue(reason, cost=cost)


def text_run(monkeypatch, answers, *, tab=None, perf=None, **run):
    """Прогон TYPE_TEXT → DONE; `answers` — по очереди: исключение или значение поля."""
    tab = tab or make_tab()
    agent = make_agent(tab, **run)
    monkeypatch.setattr(loop, "choose", scripted(decision("e1", "TYPE_TEXT", cost=0.00025), DONE))
    queue = list(answers)

    def field_text(*_a, **_k):
        answer = queue.pop(0)
        if perf is not None:
            perf["now"] += 0.3
        if callable(answer):
            answer = answer()
        if isinstance(answer, Exception):
            raise answer
        return answer, TextHelper("t", 5, cost=0.00005)

    helper = Mock(side_effect=field_text)
    monkeypatch.setattr(loop, "field_text", helper)
    return agent, tab, helper


def test_invalid_text_is_asked_again_once_then_typed(monkeypatch, caplog):
    perf = {"now": 0.0}
    monkeypatch.setattr(loop, "time", SimpleNamespace(monotonic=time.monotonic, perf_counter=lambda: perf["now"]))
    agent, tab, helper = text_run(monkeypatch, [invalid("null"), "это я через агента, проверка 👋"], perf=perf)
    logger = logging.getLogger("browser_hands.agent")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="browser_hands.agent"):
            result = agent.run()
    finally:
        logger.removeHandler(caplog.handler)
    assert result.status == "done" and helper.call_count == 2
    assert helper.call_args_list[0].args[1] == helper.call_args_list[1].args[1]  # тот же контекст
    tab.act.assert_called_once()
    assert tab.act.call_args.kwargs["text"] == "это я через агента, проверка 👋"
    assert result.model_calls == 4  # Jev TYPE_TEXT + 2 текстовых + Jev DONE
    assert result.steps[0].timing.text_ms == 600  # оба запроса
    assert result.cost == pytest.approx(0.00025 + 0.00004 + 0.00005)  # и неудачный платный
    assert {r.getMessage() for r in caplog.records if "Повторяю" in r.getMessage()} == {
        "Повторяю запрос к текстовой модели (null)"
    }


def test_two_invalid_texts_fail_and_type_nothing(monkeypatch):
    agent, tab, helper = text_run(monkeypatch, [invalid("not-json"), invalid("null")])
    result = agent.run()
    assert result.status == "failed" and "nothing typed" in result.error
    assert helper.call_count == 2 and result.model_calls == 3
    assert result.cost == pytest.approx(0.00025 + 2 * 0.00004)
    tab.act.assert_not_called()
    assert result.steps == []


def test_missing_text_key_is_not_retried(monkeypatch):
    agent, tab, helper = text_run(monkeypatch, [ValueError("TYPE_TEXT needs a text model key")])
    result = agent.run()
    assert result.status == "failed" and "text model key" in result.error
    assert helper.call_count == 1
    tab.act.assert_not_called()


def test_cancel_between_text_attempts_stops_before_the_second(monkeypatch):
    cancel = threading.Event()

    def cancelled_invalid():
        cancel.set()  # Esc, пока текстовая модель отвечала мусором
        return invalid()

    agent, tab, helper = text_run(monkeypatch, [cancelled_invalid, "book"], cancel=cancel)
    result = agent.run()
    assert cancelled_result(result) and helper.call_count == 1 and result.model_calls == 2
    tab.act.assert_not_called()


def test_deadline_between_text_attempts_is_a_timeout(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(loop, "time", SimpleNamespace(monotonic=lambda: clock["now"], perf_counter=time.perf_counter))

    def slow_invalid():
        clock["now"] += 40
        return invalid()

    agent, tab, helper = text_run(monkeypatch, [slow_invalid, "book"], timeout_s=30)
    result = agent.run()
    assert result.status == "timeout" and helper.call_count == 1
    tab.act.assert_not_called()


def test_text_timeout_is_asked_again_once_then_typed(monkeypatch, caplog):
    perf = {"now": 0.0}
    monkeypatch.setattr(loop, "time", SimpleNamespace(monotonic=time.monotonic, perf_counter=lambda: perf["now"]))
    slow = ModelTimeout("Text model did not answer in 8.0 s; no action executed.")
    agent, tab, helper = text_run(monkeypatch, [slow, "book"], perf=perf)
    logger = logging.getLogger("browser_hands.agent")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="browser_hands.agent"):
            result = agent.run()
    finally:
        logger.removeHandler(caplog.handler)
    assert result.status == "done" and helper.call_count == 2
    assert helper.call_args_list[0].args[1] == helper.call_args_list[1].args[1]  # тот же контекст
    tab.act.assert_called_once()
    assert tab.act.call_args.kwargs["text"] == "book"
    assert result.model_calls == 4  # Jev TYPE_TEXT + 2 текстовых + Jev DONE
    assert result.steps[0].timing.text_ms == 600  # и время запроса, оборванного по потолку
    assert result.cost == pytest.approx(0.00025 + 0.00005)  # у оборванного цены нет
    assert {r.getMessage() for r in caplog.records if "Повторяю" in r.getMessage()} == {
        "Повторяю запрос к текстовой модели (timeout)"
    }


@pytest.mark.parametrize("second", ["timeout", "invalid"])
def test_text_retry_is_one_in_total_whatever_the_reasons(monkeypatch, second):
    slow = ModelTimeout("Text model did not answer in 8.0 s; no action executed.")
    agent, tab, helper = text_run(monkeypatch, [slow, slow if second == "timeout" else invalid("provider-error")])
    result = agent.run()
    assert result.status == "failed" and helper.call_count == 2 and result.model_calls == 3
    expected = "ModelTimeout: Text model did not answer" if second == "timeout" else "nothing typed"
    assert expected in result.error
    tab.act.assert_not_called()
    assert result.steps == []


def test_text_timeout_at_the_run_deadline_is_a_timeout_without_a_second_call(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(loop, "time", SimpleNamespace(monotonic=lambda: clock["now"], perf_counter=time.perf_counter))

    def slow_until_the_deadline():
        clock["now"] += 30  # потолком был остаток дедлайна прогона
        return ModelTimeout("Text model did not answer in 30.0 s; no action executed.")

    agent, tab, helper = text_run(monkeypatch, [slow_until_the_deadline, "book"], timeout_s=30)
    result = agent.run()
    assert result.status == "timeout" and helper.call_count == 1 and result.model_calls == 2
    tab.act.assert_not_called()


@pytest.mark.parametrize("first", ["keepalive-timeout", "provider-error"])
def test_text_model_is_retried_over_real_http_and_both_calls_are_counted(monkeypatch, first):
    """Настоящий `ModelClients` и сокет: первый ответ — пробелы без конца (потолок 0,5 с) или 200 с `error`, второй —
    значение. Jev подменён; напечатано одно значение, учтены оба запроса: `model_calls`, `text_ms`, цена."""
    value = {"choices": [{"message": {"content": '{"text": "book"}'}}], "usage": {"cost": 0.00005}}
    upstream = {"error": {"code": 504, "message": "Upstream idle timeout exceeded"}, "usage": {"cost": 0.00001}}
    failed = keepalive(0.1, 600) if first == "keepalive-timeout" else answer(upstream)
    tab = make_tab()
    chrome = Mock()
    chrome.new_tab.return_value = tab
    chrome.find_user_tab.return_value = None
    monkeypatch.setattr(loop, "choose", scripted(decision("e1", "TYPE_TEXT", cost=0.00025), DONE))
    with FakeModelServer([failed, answer(value)]) as server:
        config = ModelConfig(jev_api_key="k", text_api_key="sk-text", text_base_url=server.url, text_timeout_s=0.5)
        clients = ModelClients(config)  # как в сервере: httpx.Client(http2=True), к http:// — HTTP/1.1
        started = time.monotonic()
        result = Agent(chrome, clients, "https://example.test/", "Find a book", RunConfig(timeout_s=30)).run()
        elapsed = time.monotonic() - started
        clients.close()
    assert result.status == "done" and tab.act.call_args.kwargs["text"] == "book"
    assert len(server.requests) == 2 and server.requests[0] == server.requests[1]  # тот же запрос
    assert result.model_calls == 4
    text_ms = result.steps[0].timing.text_ms
    if first == "keepalive-timeout":
        assert 500 <= text_ms < 1000 and elapsed < 2
        assert result.cost == pytest.approx(0.00025 + 0.00005)
    else:
        assert text_ms < 500
        assert result.cost == pytest.approx(0.00025 + 0.00001 + 0.00005)  # ошибка с `usage.cost` — тоже в цене


# --- сценарий (docs/plan-scenarios.md §4.2) -------------------------------------------------------------------------

TWO = [ScenarioStep("Open the search"), ScenarioStep("Open the result")]
CHAT = "Рабочий"
MESSAGE = "это я через агента, проверка 👋"
# DONE в режиме шага закрывает шаг только с подтверждением step_done ≥ 0.5 в том же ответе; 0.6 < 0.7 — закрывает
# именно DONE, а не порог step_done.
DONE_STEP = decision("DONE", "DONE", step_done=0.6)


def numbers(choose):
    """Номер шага сценария в каждом вызове Jev (None — режим цели)."""
    return [None if c.kwargs["step"] is None else c.kwargs["step"].number for c in choose.call_args_list]


def info_lines(caplog, run):
    logger = logging.getLogger("browser_hands.agent")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="browser_hands.agent"):
            result = run()
    finally:
        logger.removeHandler(caplog.handler)
    return result, [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


def test_scenario_steps_close_on_step_done_without_acting_and_on_done(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab, steps=TWO, goal="")
    choose = scripted(
        decision("e3", "CLICK", step_done=0.2),  # шаг 1 не выполнен — действие
        decision("e3", "CLICK", step_done=0.9),  # шаг 1 выполнен — CLICK не исполняется
        decision("e3", "CLICK", step_done=0.1),
        DONE_STEP,  # DONE в режиме шага, step_done ≥ 0.5 — шаг 2 выполнен
    )
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.error is None
    assert (result.scenario_done, result.scenario_total) == (2, 2)
    assert [s.scenario_step for s in result.steps] == [1, 2]
    assert result.jev_calls == result.model_calls == 4
    assert tab.act.call_count == 2
    assert numbers(choose) == [1, 1, 2, 2]  # один вызов Jev «на границе» шага
    step = choose.call_args_list[2].kwargs["step"]
    assert step.current() == "Step 2 of 2: Open the result" and step.goal is None and step.done == ["Open the search"]


def test_scenario_text_is_typed_verbatim_without_the_text_model_and_closes_the_step(monkeypatch):
    tab = make_tab()
    steps = [
        ScenarioStep("Type the chat name into the chat search box", CHAT),
        ScenarioStep("Type the message", MESSAGE),
    ]
    agent = make_agent(tab, steps=steps, goal="")
    choose = scripted(decision("e1", "TYPE_TEXT", step_done=0.0), decision("e1", "TYPE_TEXT", step_done=0.1))
    monkeypatch.setattr(loop, "choose", choose)
    field_text = Mock()
    monkeypatch.setattr(loop, "field_text", field_text)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2
    field_text.assert_not_called()
    assert [c.kwargs["text"] for c in tab.act.call_args_list] == [CHAT, MESSAGE]
    assert [s.text for s in result.steps] == [CHAT, MESSAGE] and [s.scenario_step for s in result.steps] == [1, 2]
    assert result.model_calls == result.jev_calls == 2  # последний текстовый шаг — done без нового вызова Jev
    assert numbers(choose) == [1, 2]
    assert agent._history[0]["text"] == CHAT  # Jev видит напечатанное в recent_actions


def test_scenario_text_step_then_step_without_text(monkeypatch):
    tab = make_tab()
    steps = [ScenarioStep("Type the chat name into the chat search box", CHAT), ScenarioStep("Open the chat")]
    agent = make_agent(tab, steps=steps, goal="")
    choose = scripted(decision("e1", "TYPE_TEXT", step_done=0.0), decision("e3", "CLICK", step_done=0.0), DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2 and numbers(choose) == [1, 2, 2]
    assert [(s.operation, s.scenario_step) for s in result.steps] == [("TYPE_TEXT", 1), ("CLICK", 2)]


def test_scenario_text_already_on_the_page_closes_the_step_without_typing(monkeypatch):
    tab = make_tab()
    steps = [ScenarioStep("Type the chat name into the chat search box", CHAT), ScenarioStep("Open the chat")]
    agent = make_agent(tab, steps=steps, goal="")
    field_text = Mock()
    monkeypatch.setattr(loop, "field_text", field_text)
    choose = scripted(decision("e1", "TYPE_TEXT", step_done=0.95), decision("DONE", "DONE", step_done=0.9))
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2 and result.steps == []
    tab.act.assert_not_called()
    field_text.assert_not_called()


def test_scenario_step_without_text_asks_the_text_model_with_the_current_step(monkeypatch):
    goal = "Say hi to the team"  # без goal — blocked, текстовая модель не зовётся (после ревью, п. 2)
    tab = make_tab()
    steps = [ScenarioStep("Type a greeting into the message box"), ScenarioStep("Send the message")]
    agent = make_agent(tab, steps=steps, goal=goal)
    monkeypatch.setattr(
        loop,
        "choose",
        scripted(decision("e1", "TYPE_TEXT", step_done=0.0), decision("e3", "CLICK", step_done=0.9), DONE_STEP),
    )
    field_text = Mock(return_value=("hi", TextHelper("t", 5)))
    monkeypatch.setattr(loop, "field_text", field_text)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2
    context = field_text.call_args.args[1]
    assert context["goal"] == f"{goal}\nCurrent step 1 of 2: Type a greeting into the message box"
    assert tab.act.call_args.kwargs["text"] == "hi"
    assert result.model_calls == result.jev_calls + 1 == 4  # текст шага без `text` — от текстовой модели


@pytest.mark.parametrize(("p", "closed"), [(0.69, False), (0.7, True)])
def test_scenario_step_done_threshold(monkeypatch, p, closed):
    assert loop.STEP_DONE_MIN_P == 0.7
    tab = make_tab()
    agent = make_agent(tab, steps=TWO, goal="")
    choose = scripted(decision("e3", "CLICK", step_done=p), DONE_STEP, DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2
    assert tab.act.call_count == (0 if closed else 1)
    assert numbers(choose) == ([1, 2] if closed else [1, 1, 2])


def test_thresholds_come_from_config_and_the_old_names_are_aliases():
    assert make_agent().thresholds == Thresholds() == Settings().thresholds
    defaults = Thresholds()
    assert (loop.STEP_DONE_MIN_P, loop.DONE_STEP_DONE_MIN_P, loop.MIN_ACTION_CONFIDENCE) == (
        defaults.step_done_min_p,
        defaults.done_step_done_min_p,
        defaults.min_action_confidence,
    )


@pytest.mark.parametrize(
    ("limits", "acts", "asked"),
    [
        (Thresholds(step_done_min_p=0.8), 1, [1, 1, 2]),  # p=0.75 шаг не закрывает, CLICK conf 0.4 ≥ 0.3 исполняется
        (Thresholds(step_done_min_p=0.75), 0, [1, 2]),  # p=0.75 на пороге — шаг закрыт, действие не исполняется
        (Thresholds(step_done_min_p=0.8, min_action_confidence=0.5), 0, [1, 1, 2]),  # conf 0.4 < 0.5 — не исполняется
    ],
)
def test_agent_reads_the_thresholds_it_was_given(monkeypatch, limits, acts, asked):
    tab = make_tab()
    chrome = Mock()
    chrome.new_tab.return_value = tab
    chrome.find_user_tab.return_value = None
    clients = ModelClients(ModelConfig(jev_api_key="test", text_api_key="test"), http=Mock())
    run = RunConfig(timeout_s=30.0)
    agent = Agent(chrome, clients, "https://example.test/", "", run, steps=TWO, thresholds=limits)
    choose = scripted(decision("e3", "CLICK", step_done=0.75, confidence=0.4), DONE_STEP, DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert agent.thresholds is limits
    assert result.status == "done" and result.scenario_done == 2
    assert tab.act.call_count == acts and numbers(choose) == asked


@pytest.mark.parametrize("action", ["e3", "wait"])
def test_scenario_step_limit_after_six_actions_per_step(monkeypatch, action):
    assert loop.STEP_ACTIONS_LIMIT == 6
    tab = make_tab()
    agent = make_agent(tab, steps=TWO, goal="")
    operation = "CLICK" if action == "e3" else "WAIT"
    busy = decision(action, operation, step_done=0.1)
    # шаг 1 — 5 действий и закрыт; счётчик сбрасывается: шаг 2 — ещё 6 действий, 7-е не исполняется
    choose = scripted(*[busy] * 5, decision(action, operation, step_done=0.9), *[busy] * 7)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "step_limit"
    assert result.error == "Step 2 of 2 not completed after 6 actions"
    assert result.scenario_done == 1 and len(result.steps) == 11
    # WAIT Jev — ожидание изменения страницы, без Tab.act
    assert (tab.act.call_count, tab.await_change.call_count) == ((11, 0) if action == "e3" else (0, 11))
    assert [s.scenario_step for s in result.steps] == [1] * 5 + [2] * 6
    assert result.jev_calls == 13 and choose.call_count == 13


def test_scenario_step_limit_on_the_first_step(monkeypatch):
    agent = make_agent(steps=TWO, goal="")
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3", "CLICK", step_done=0.1)))
    result = agent.run()
    assert result.status == "step_limit" and result.error == "Step 1 of 2 not completed after 6 actions"
    assert result.scenario_done == 0 and len(result.steps) == 6 and result.jev_calls == 7


def test_scenario_blocked_after_a_second_look_names_the_step(monkeypatch):
    tab = waiting_tab()
    agent = make_agent(tab, steps=TWO, goal="")
    blocked = decision("BLOCKED", "BLOCKED", step_done=0.0)
    monkeypatch.setattr(loop, "choose", scripted(blocked, blocked))
    result = agent.run()
    assert result.status == "blocked" and result.scenario_done == 0
    assert result.error == (
        "Model chose BLOCKED on step 1 of 2 after a second look; no supported operation can progress."
    )
    tab.await_change.assert_called_once_with()
    tab.act.assert_not_called()


def test_scenario_closing_a_step_restores_the_second_chance(monkeypatch):
    tab = waiting_tab()
    tab.observe.side_effect = None
    tab.observe.return_value = page()  # ничего не меняется: флаг сбрасывает только закрытие шага
    agent = make_agent(tab, steps=TWO, goal="")
    blocked = decision("BLOCKED", "BLOCKED", step_done=0.0)
    choose = scripted(blocked, decision("e3", "CLICK", step_done=0.9), blocked, DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2
    assert tab.await_change.call_count == 2 and numbers(choose) == [1, 1, 2, 2]
    tab.act.assert_not_called()


def test_scenario_stale_page_on_step_done_reobserves_without_closing(monkeypatch):
    tab = make_tab()
    # predict свеж, закрытие шага — страница уже другая; дальше всё свежо
    tab.fresh.side_effect = [True, False] + [True] * 10
    agent = make_agent(tab, steps=TWO, goal="")
    choose = scripted(decision("e3", "CLICK", step_done=0.9), decision("e3", "CLICK", step_done=0.9), DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2
    assert numbers(choose) == [1, 1, 2]  # устаревшее «выполнен» шаг не закрыло, решение потреблено
    tab.act.assert_not_called()


def test_scenario_cancel_before_the_action_keeps_an_honest_count(monkeypatch):
    cancel = threading.Event()
    tab = make_tab()
    agent = make_agent(tab, steps=TWO, goal="", cancel=cancel)
    queue = [decision("e3", "CLICK", step_done=0.9), decision("e3", "CLICK", step_done=0.1)]

    def choose(*_a, **k):
        if k["step"].number == 2:
            cancel.set()  # Esc, пока Jev думал над шагом 2
        return queue.pop(0)

    monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
    result = agent.run()
    assert cancelled_result(result) and (result.scenario_done, result.scenario_total) == (1, 2)
    tab.act.assert_not_called()
    tab.screenshot.assert_not_called()


def test_scenario_cancel_wins_over_step_done(monkeypatch):
    cancel = threading.Event()
    agent = make_agent(steps=TWO, goal="", cancel=cancel)
    monkeypatch.setattr(
        loop, "choose", Mock(side_effect=lambda *a, **k: cancel.set() or decision("e3", "CLICK", step_done=0.9))
    )
    result = agent.run()
    assert cancelled_result(result) and result.scenario_done == 0  # отменённое решение шаг не закрывает


def test_scenario_deadline_is_a_timeout_with_the_steps_done_so_far(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(loop, "time", SimpleNamespace(monotonic=lambda: clock["now"], perf_counter=time.perf_counter))
    agent = make_agent(steps=TWO, goal="", timeout_s=30)

    def choose(*_a, **k):
        if k["step"].number == 1:
            return decision("e3", "CLICK", step_done=0.9)
        clock["now"] += 40  # шаг 2: Jev думал до дедлайна
        return decision("e3", "CLICK", step_done=0.1)

    monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
    result = agent.run()
    assert result.status == "timeout" and (result.scenario_done, result.scenario_total) == (1, 2)
    assert result.jev_calls == 2


def test_scenario_waits_for_an_empty_page_before_asking_jev(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = [empty_page(), empty_page(), page(), page(), page()]
    agent = make_agent(tab, steps=TWO, goal="")
    seen = []

    def choose(_clients, state, *_a, **_k):
        seen.append(state)
        return decision("DONE", "DONE", step_done=0.9)

    monkeypatch.setattr(loop, "choose", Mock(side_effect=choose))
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2 and result.jev_calls == 2
    assert tab.await_change.call_count == 2 and all(interactive for interactive in map(loop.interactive, seen))


def test_scenario_logs_steps_without_personal_text(monkeypatch, caplog):
    steps = [ScenarioStep("Type the chat name into the chat search box", CHAT), ScenarioStep("Open the chat")]
    agent = make_agent(steps=steps, goal="")
    monkeypatch.setattr(
        loop, "choose", scripted(decision("e1", "TYPE_TEXT", step_done=0.0), decision("e3", "CLICK", step_done=0.95))
    )
    result, info = info_lines(caplog, agent.run)
    assert result.status == "done"
    lines = sorted(set(info))  # запись может прийти дважды: handler на логгере и корень
    assert any(
        m.startswith("step 1 TYPE_TEXT 'Search' len=7 @ example.test") and m.endswith(" [шаг 1/2]") for m in lines
    )
    assert "шаг 1/2 выполнен (text typed, действий 1)" in lines
    assert "шаг 2/2 выполнен (p=0.95, действий 0)" in lines
    assert any(m.startswith("browse done: 2/2 steps of scenario, 1 actions, 2 model calls (Jev 2), ") for m in lines)
    assert all(CHAT not in m and "Open the chat" not in m for m in lines)


def test_goal_mode_passes_no_step_and_reports_no_scenario(monkeypatch, caplog):
    tab = make_tab()
    agent = make_agent(tab)
    choose = scripted(decision("e3", "CLICK"), DONE)
    monkeypatch.setattr(loop, "choose", choose)
    result, info = info_lines(caplog, agent.run)
    assert result.status == "done" and numbers(choose) == [None, None]
    assert (result.scenario_done, result.scenario_total, result.jev_calls) == (None, None, 2)
    assert [s.scenario_step for s in result.steps] == [None]
    assert any(m.startswith("browse done: 1 steps, 2 model calls, ") for m in info)
    assert not any("шаг" in m or "scenario" in m for m in info)


def test_agent_needs_a_goal_or_steps_and_checks_the_steps():
    with pytest.raises(ValueError, match="Supply a goal or steps"):
        Agent(Mock(), Mock(), "https://example.test/", " ", RunConfig(), steps=None)
    with pytest.raises(ValueError, match="Supply a goal or steps"):
        Agent(Mock(), Mock(), "https://example.test/", "", RunConfig(), steps=[])
    with pytest.raises(ScenarioError, match="step 2: do is empty"):
        Agent(Mock(), Mock(), "https://example.test/", "", RunConfig(), steps=[ScenarioStep("Open"), ScenarioStep(" ")])
    agent = Agent(Mock(), Mock(), "https://example.test/", "", RunConfig(), steps=[{"do": " Open the chat "}])
    assert agent.scenario == (ScenarioStep("Open the chat"),) and agent.goal == ""


@pytest.mark.parametrize("focused", [True, False], ids=["focus-ok", "focus-lost"])
def test_scenario_text_end_to_end_reaches_the_page_only_through_insert_text_after_the_focus_check(
    monkeypatch, tmp_path, focused
):
    """Настоящий Tab/CDP на фейковом Chrome (вкладка пользователя): текст шага — только в `Input.insertText`, сразу
    после `Runtime.evaluate` с FOCUSED и selectAll; фокус не на поле — не напечатан нигде."""
    steps = [ScenarioStep("Type the message into the message box", MESSAGE)]
    blocked = decision("BLOCKED", "BLOCKED", step_done=0.0)
    choose = scripted(decision("e1", "TYPE_TEXT", step_done=0.0), blocked, blocked)
    monkeypatch.setattr(loop, "choose", choose)
    field_text = Mock()
    monkeypatch.setattr(loop, "field_text", field_text)
    with FakeCDPServer() as server:
        chrome = fake_user_chrome(server, tmp_path)
        base = server.on["Runtime.evaluate"]

        def evaluate(frame, ws):
            if frame["params"]["expression"].startswith(FOCUSED):
                reply(ws, frame, {"result": {"type": "boolean", "value": focused}})
            else:
                base(frame, ws)

        def insert_text(frame, ws):  # страница: напечатанное остаётся в поле, снимок его покажет
            server.state["actions"][0]["value"] = server.state["actions"][1]["value"] = frame["params"]["text"]
            reply(ws, frame, {})

        server.on["Runtime.evaluate"] = evaluate
        server.on["Input.insertText"] = insert_text
        clients = ModelClients(ModelConfig(jev_api_key="test", text_api_key="test"), http=Mock())
        result = Agent(chrome, clients, "https://web.whatsapp.com", "", RunConfig(timeout_s=30.0), steps=steps).run()
        chrome.close()
    field_text.assert_not_called()
    frames = server.frames
    inserts = [i for i, f in enumerate(frames) if f["method"] == "Input.insertText"]
    checks = [
        i
        for i, f in enumerate(frames)
        if f["method"] == "Runtime.evaluate" and f["params"]["expression"].startswith(FOCUSED)
    ]
    carrying = [i for i, f in enumerate(frames) if MESSAGE in json.dumps(f, ensure_ascii=False)]
    if focused:
        assert result.status == "done" and result.scenario_done == 1 and result.jev_calls == result.model_calls == 1
        assert len(inserts) == 1 and frames[inserts[0]]["params"] == {"text": MESSAGE}
        assert len(checks) == 1 and checks[0] < inserts[0]
        assert [f["method"] for f in frames[checks[0] + 1 : inserts[0]]] == ["Input.dispatchKeyEvent"] * 2  # selectAll
        assert carrying == inserts  # больше нигде: ни в клике, ни в проверках, ни в снимках
    else:
        assert result.status == "blocked" and result.scenario_done == 0 and "step 1 of 1" in result.error
        assert len(checks) == 1 and inserts == [] and carrying == []
    assert {f["sessionId"] for f in frames if f["method"].startswith("Input.")} == {"S-U1"}
    assert "Target.closeTarget" not in server.methods()


# --- сценарий после ревью (docs/core-notes.md, «Сценарии» → «После ревью») ------------------------------------------


@pytest.mark.parametrize(("p", "closed"), [(0.0, False), (0.49, False), (0.5, True)])
def test_scenario_done_closes_the_step_only_when_step_done_confirms_it(monkeypatch, p, closed):
    tab = make_tab()
    agent = make_agent(tab, steps=TWO, goal="")
    choose = scripted(decision("DONE", "DONE", step_done=p), decision("e3", "CLICK", step_done=0.9), DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2
    assert numbers(choose) == ([1, 2] if closed else [1, 1, 2])  # DONE без подтверждения шаг не закрыл
    tab.act.assert_not_called()
    assert result.steps == []  # отклонённый DONE — не действие; дальше — проверка: только запись ожидания
    assert [h["kind"] for h in agent._history] == ([] if closed else ["wait"])
    assert loop.DONE_STEP_DONE_MIN_P == 0.5


def test_scenario_repeated_unconfirmed_done_is_unconfirmed_after_one_check(monkeypatch, caplog):
    tab = make_tab()
    agent = make_agent(tab, steps=TWO, goal="")
    choose = Mock(return_value=decision("DONE", "DONE", step_done=0.1))
    monkeypatch.setattr(loop, "choose", choose)
    result, info = info_lines(caplog, agent.run)
    assert result.status == "unconfirmed"
    assert result.error == "step 1 of 2: probably done, not confirmed — check the screenshot"
    assert result.scenario_done == 0 and result.steps == [] and result.jev_calls == 2
    tab.act.assert_not_called()
    tab.await_change.assert_called_once_with()
    assert "шаг 1/2: DONE без подтверждения (p=0.10) — проверяю" in info
    assert "шаг 1/2 не подтверждён при проверке (DONE, p=0.10)" in info


def with_message_box(pages, typed):
    """Снимки `pages` плюс поле сообщения e4 (узел 30), которое показывает напечатанное в него."""
    for state in pages:
        state["actions"].insert(3, {"id": "e4", "kind": "fill", "label": "Message", "role": "textbox", "node": 30})
        state["actions"][3]["value"] = typed.get(30, "")
        state["fingerprint"] = fingerprint(state)
        yield state


def test_scenario_texts_never_reach_the_text_model(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = with_message_box(changing_pages(tab.typed), tab.typed)
    steps = [
        ScenarioStep("Type the chat name into the chat search box", CHAT),
        ScenarioStep("Type a greeting into the message box"),
    ]
    agent = make_agent(tab, steps=steps, goal="Say hi to the team")
    choose = scripted(decision("e1", "TYPE_TEXT", step_done=0.0), decision("e4", "TYPE_TEXT", step_done=0.0), DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    field_text = Mock(return_value=("hi", TextHelper("t", 5)))
    monkeypatch.setattr(loop, "field_text", field_text)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2
    context = field_text.call_args.args[1]
    assert context["recent_actions"] == [{"action": "Search"}]  # без поля text
    assert CHAT not in json.dumps(context, ensure_ascii=False)
    assert agent._history[0]["text"] == CHAT  # Jev по-прежнему видит напечатанное (recent_actions запроса)


def test_goal_mode_text_model_still_sees_typed_texts(monkeypatch):
    agent = make_agent()
    choose = scripted(decision("e1", "TYPE_TEXT"), decision("e1", "TYPE_TEXT"), DONE)
    monkeypatch.setattr(loop, "choose", choose)
    field_text = Mock(side_effect=[("book", TextHelper("t", 5)), ("author", TextHelper("t", 5))])
    monkeypatch.setattr(loop, "field_text", field_text)
    assert agent.run().status == "done"
    assert field_text.call_args.args[1]["recent_actions"] == [{"action": "Search", "text": "book"}]


def test_scenario_step_without_text_and_without_goal_is_blocked_before_the_text_model(monkeypatch):
    tab = make_tab()
    steps = [ScenarioStep("Open the chat"), ScenarioStep("Type a greeting into the message box")]
    agent = make_agent(tab, steps=steps, goal="")
    choose = scripted(decision("e3", "CLICK", step_done=0.9), decision("e1", "TYPE_TEXT", step_done=0.0))
    monkeypatch.setattr(loop, "choose", choose)
    field_text = Mock()
    monkeypatch.setattr(loop, "field_text", field_text)
    result = agent.run()
    assert result.status == "blocked" and result.error == "step 2 of 2: no text given for typing"
    assert result.scenario_done == 1 and result.jev_calls == result.model_calls == 2 and choose.call_count == 2
    field_text.assert_not_called()
    tab.act.assert_not_called()


def test_scenario_decision_budget_leaves_room_for_one_stale_decision_per_step(monkeypatch):
    steps = [ScenarioStep(f"Click thing {i}") for i in range(1, 21)]
    tab = make_tab()
    agent = make_agent(tab, steps=steps, goal="", max_steps=25, timeout_s=300.0)
    effects, queue = [], []
    for _ in steps:  # на шаг: решение устарело (act → StalePage), действие, закрытие по step_done
        effects += [StalePage("changed"), None]
        queue += [decision("e3", "CLICK", step_done=0.0)] * 2 + [decision("e3", "CLICK", step_done=0.95)]
    tab.act.side_effect = effects
    monkeypatch.setattr(loop, "choose", scripted(*queue))
    result = agent.run()
    assert result.status == "done" and (result.scenario_done, result.scenario_total) == (20, 20)
    assert result.jev_calls == 60 > 2 * 25 and len(result.steps) == 20


def test_scenario_decision_budget_is_twice_max_steps_plus_the_steps(monkeypatch):
    tab = make_tab()
    tab.act.side_effect = StalePage("changed")  # каждое решение устарело: ни действия, ни закрытия
    agent = make_agent(tab, steps=TWO, goal="", max_steps=3)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e3", "CLICK", step_done=0.0)))
    result = agent.run()
    assert result.status == "step_limit" and result.error == "Model-call budget exhausted (8 decisions)"
    assert result.jev_calls == 2 * 3 + 2


def test_scenario_no_progress_counts_only_actions_of_the_current_step(monkeypatch):
    tab = make_tab()

    def same_page(*_a, **_k):  # отпечаток — как до действий: страница «не меняется», но текст в поле виден
        state = page()
        state["actions"][0]["value"] = tab.typed.get(10, "")
        return state

    tab.observe.side_effect = same_page
    steps = [ScenarioStep("Fill A", "a"), ScenarioStep("Fill B", "b"), ScenarioStep("Fill C", "c")]
    agent = make_agent(tab, steps=[*steps, ScenarioStep("Submit")], goal="")
    typed = decision("e1", "TYPE_TEXT", step_done=0.0)
    click = decision("e3", "CLICK", step_done=0.0)
    monkeypatch.setattr(loop, "choose", scripted(typed, typed, typed, click, click, click))
    result = agent.run()
    # три текстовых шага по одному действию — не «3 подряд без изменений»; внутри шага 4 — да
    assert result.status == "blocked" and result.error == "No page change after 3 consecutive actions"
    assert result.scenario_done == 3 and [s.scenario_step for s in result.steps] == [1, 2, 3, 4, 4, 4]


LONG_LABEL = "Very long chat name that goes on and on well beyond forty characters"


def long_label_pages():
    n = 0
    while True:
        n += 1
        state = page(f"Search {n}")
        state["actions"][2]["label"] = LONG_LABEL
        state["fingerprint"] = fingerprint(state)
        yield state


@pytest.mark.parametrize("scenario", [False, True], ids=["goal", "scenario"])
def test_step_log_cuts_the_element_label_to_40_characters(monkeypatch, caplog, scenario):
    tab = make_tab()
    tab.observe.side_effect = long_label_pages()
    agent = make_agent(tab, steps=TWO[:1] if scenario else None, goal="" if scenario else "Open the chat")
    last = DONE_STEP if scenario else DONE
    monkeypatch.setattr(loop, "choose", scripted(decision("e3", "CLICK", step_done=0.0 if scenario else None), last))
    result, info = info_lines(caplog, agent.run)
    assert result.status == "done" and result.steps[0].target == LONG_LABEL  # в результате — полностью
    short = LONG_LABEL[:39] + "…"
    assert len(short) == 40
    assert any(m.startswith(f"step 1 CLICK {short!r} @ example.test") for m in info)
    assert all(LONG_LABEL not in m for m in info)
    assert loop.LOG_LABEL_MAX == 40


# --- живая проверка WhatsApp 26.09 (docs/core-notes.md, «Живая проверка WhatsApp 26.09 и правки») --------------------

VANISHED = "text vanished after typing (page re-rendered)"
TYPE_AND_SEND = [ScenarioStep("Type the message into the message box", MESSAGE), ScenarioStep("Send the message")]


def field_page(value, *, node=10, text="Рабочий", label="Type a message"):
    """Чат с полем сообщения (узел `node`, в поле — `value`) и кнопкой Send."""
    state = {
        "url": "https://web.whatsapp.com/",
        "title": "WhatsApp",
        "text": text,
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": label, "role": "textbox", "value": value, "node": node},
            {"id": "e2", "kind": "click", "label": f"Open {label}", "role": "textbox", "value": value, "node": node},
            {"id": "e3", "kind": "click", "label": "Send", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def numbered_pages():
    """Каждый снимок — свой url и title (`…/n`, `Page n`): видно, с какого снимка url и title результата."""
    n = 0
    while True:
        n += 1
        state = page(f"Search {n}")
        state["url"], state["title"] = f"https://example.test/{n}", f"Page {n}"
        state["fingerprint"] = fingerprint(state)
        yield state


TYPE_MESSAGE = decision("e1", "TYPE_TEXT", step_done=0.0)


# п. 1: шаг с `text` закрывается только по видимому результату


def test_text_vanished_after_typing_keeps_the_step_open_and_jev_decides_on_the_fresh_page(monkeypatch, caplog):
    tab = make_tab()
    tab.observe.side_effect = [
        field_page("", text="Рабочий (загрузка)"),
        field_page("", node=11),  # WhatsApp догрузил чат и перерисовал поле: текста нет
        field_page("", node=11, text="Рабочий (догружен)"),  # и после следующего изменения страницы — нет
        field_page(MESSAGE, node=11),  # повторный ввод в открытом чате — остался
    ]
    agent = make_agent(tab, steps=TYPE_AND_SEND, goal="")
    choose = scripted(TYPE_MESSAGE, TYPE_MESSAGE, DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result, info = info_lines(caplog, agent.run)
    assert result.status == "done" and result.scenario_done == 2
    assert numbers(choose) == [1, 1, 2]  # после пропажи — новое решение Jev на том же шаге
    assert [c.kwargs["text"] for c in tab.act.call_args_list] == [MESSAGE, MESSAGE]
    fresh = choose.call_args_list[1].args[1]
    assert fresh["actions"][0]["node"] == 11 and fresh["actions"][0]["value"] == ""  # по свежему снимку, поле пусто
    assert agent._history[0]["note"] == VANISHED and "note" not in agent._history[1]
    assert [s.scenario_step for s in result.steps] == [1, 1]
    assert sorted(set(info)).count("шаг 1/2 выполнен (text typed, действий 2)") == 1


def test_text_that_never_stays_in_the_field_is_blocked_after_two_retypes(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = (field_page("", text=f"Рабочий {n}") for n in range(100))
    agent = make_agent(tab, steps=TYPE_AND_SEND, goal="")
    choose = Mock(return_value=TYPE_MESSAGE)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "blocked" and result.error == "step 1 of 2: typed text does not stay in the field"
    assert tab.act.call_count == 3 and choose.call_count == 3  # ввод и 2 повторных
    assert result.scenario_done == 0 and [h.get("note") for h in agent._history] == [VANISHED] * 3


@pytest.mark.parametrize(
    ("shown", "closed"),
    [
        (MESSAGE, True),
        (" это я через агента,\n проверка 👋 ", True),  # пробелы нормализуются
        ("это я через агента, проверка", True),  # эмодзи картинкой (<img alt>): в innerText его нет
        ("это я через агента", False),
        ("", False),
    ],
    ids=["same", "spaces", "emoji-img", "part", "empty"],
)
def test_text_step_closes_only_when_the_field_shows_the_text(monkeypatch, shown, closed):
    tab = make_tab()
    tab.observe.side_effect = [field_page(""), field_page(shown, text="Рабочий 1"), field_page(shown, text="Рабочий 2")]
    agent = make_agent(tab, steps=TYPE_AND_SEND, goal="")
    choose = scripted(TYPE_MESSAGE, decision("e3", "CLICK", step_done=0.9), DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 2 and tab.act.call_count == 1
    assert numbers(choose) == ([1, 2] if closed else [1, 1, 2])


@pytest.mark.parametrize(
    ("node", "label", "closed"),
    [(10, "Type a message", True), (11, "Type a message", True), (11, "Search or start a new chat", False)],
    ids=["same-node", "same-role-and-label", "other-field"],
)
def test_typed_field_is_found_by_node_then_by_role_and_label(monkeypatch, node, label, closed):
    tab = make_tab()
    after = field_page(MESSAGE, node=node, label=label, text="Рабочий 1")
    tab.observe.side_effect = [field_page(""), after, after]  # не видно — ещё одно наблюдение после изменения
    agent = make_agent(tab, steps=TYPE_AND_SEND, goal="")
    choose = scripted(TYPE_MESSAGE, decision("e3", "CLICK", step_done=0.9), DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and tab.act.call_count == 1
    assert numbers(choose) == ([1, 2] if closed else [1, 1, 2])


def test_stale_snapshot_after_typing_is_taken_again_after_settling(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = [field_page(""), StalePage("navigating"), field_page(MESSAGE, text="Рабочий 1")]
    agent = make_agent(tab, steps=TYPE_AND_SEND, goal="")
    choose = scripted(TYPE_MESSAGE, DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and numbers(choose) == [1, 2]
    tab.await_change.assert_called_once_with()
    assert result.steps[0].page_changed is True


def test_text_step_is_not_closed_when_both_snapshots_after_typing_are_stale(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = [
        field_page(""),
        StalePage("navigating"),
        StalePage("still navigating"),
        field_page(MESSAGE, text="Рабочий 1"),  # переснимок в следующем тике
    ]
    agent = make_agent(tab, steps=TYPE_AND_SEND, goal="")
    choose = scripted(TYPE_MESSAGE, decision("wait", "WAIT", step_done=0.9), DONE_STEP)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and numbers(choose) == [1, 1, 2]  # шаг закрыл Jev, а не ввод вслепую
    assert tab.act.call_count == 1 and result.steps[0].page_changed is None
    tab.await_change.assert_called_once_with()
    tab.location.assert_not_called()


def test_last_text_step_closes_on_the_fresh_snapshot_with_its_url_and_title(monkeypatch):
    tab = make_tab()
    after = field_page(MESSAGE, text="Рабочий 1")
    after["url"], after["title"] = "https://web.whatsapp.com/typed", "Typed"
    after["fingerprint"] = fingerprint(after)
    tab.observe.side_effect = [field_page(""), StalePage("navigating"), after]
    agent = make_agent(tab, steps=TYPE_AND_SEND[:1], goal="")
    monkeypatch.setattr(loop, "choose", scripted(TYPE_MESSAGE))
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 1 and result.jev_calls == 1
    assert (result.url, result.title) == ("https://web.whatsapp.com/typed", "Typed")
    tab.location.assert_not_called()


@pytest.mark.parametrize("second", [CDPTimeout("slow"), TabGone("closed")], ids=["cdp", "gone"])
def test_failed_second_snapshot_after_typing_does_not_close_the_step(monkeypatch, second):
    tab = make_tab()
    tab.observe.side_effect = [field_page(""), StalePage("navigating"), second]
    agent = make_agent(tab, steps=TYPE_AND_SEND[:1], goal="")
    monkeypatch.setattr(loop, "choose", scripted(TYPE_MESSAGE))
    result = agent.run()
    assert result.status == "failed" and result.scenario_done == 0


# п. 2: после неподтверждённого DONE новых действий нет


def verifies(choose):
    """Флаг режима проверки в каждом вызове Jev."""
    return [c.kwargs.get("verify", False) for c in choose.call_args_list]


def test_whatsapp_send_step_ends_unconfirmed_without_an_extra_click(monkeypatch):
    """26.09: CLICK Send → DONE p=0.36 → DONE p=0.47 → раньше CLICK «00:31 Sent» (conf 0.25) и step_limit."""
    tab = make_tab()
    tab.observe.side_effect = numbered_pages()
    agent = make_agent(tab, steps=[ScenarioStep("Send the message")], goal="")
    choose = scripted(
        decision("e3", "CLICK", step_done=0.1, confidence=0.99),
        decision("DONE", "DONE", step_done=0.36),
        decision("DONE", "DONE", step_done=0.47),
        decision("e3", "CLICK", step_done=0.2, confidence=0.25),  # лишний клик в чужом месте
    )
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "unconfirmed"
    assert result.error == "step 1 of 1: probably done, not confirmed — check the screenshot"
    assert tab.act.call_count == 1 and [s.operation for s in result.steps] == ["CLICK"]
    assert choose.call_count == 3 and verifies(choose) == [False, False, True]
    tab.await_change.assert_called_once_with()  # перед проверкой — ожидание изменения и свежий снимок
    assert result.scenario_done == 0 and result.screenshot_jpeg == JPEG
    last = tab.observe.call_count  # url и title — последнего снимка, снятого после ответа Jev
    assert (result.url, result.title) == (f"https://example.test/{last}", f"Page {last}")
    assert choose.call_args_list[2].args[1]["url"] != result.url


WAIT_CHECK = decision("wait", "WAIT", step_done=0.1)


@pytest.mark.parametrize(
    ("answers", "status", "waits"),
    [
        ([decision("DONE", "DONE", step_done=0.5)], "done", 1),
        ([WAIT_CHECK, decision("DONE", "DONE", step_done=0.6)], "done", 2),
        ([decision("wait", "WAIT", step_done=0.8)], "done", 1),  # P(yes) ≥ 0.7 закрывает шаг и в проверке
        ([decision("DONE", "DONE", step_done=0.2)], "unconfirmed", 1),
        ([WAIT_CHECK, WAIT_CHECK], "unconfirmed", 2),
        ([WAIT_CHECK, decision("DONE", "DONE", step_done=0.1)], "unconfirmed", 2),
    ],
    ids=["done", "wait-done", "wait-yes", "done-again", "wait-wait", "wait-done-again"],
)
def test_check_after_unconfirmed_done(monkeypatch, answers, status, waits):
    tab = make_tab()
    agent = make_agent(tab, steps=[ScenarioStep("Send the message")], goal="")
    choose = scripted(decision("DONE", "DONE", step_done=0.3), *answers)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == status and choose.call_count == 1 + len(answers)
    assert verifies(choose) == [False] + [True] * len(answers)
    tab.act.assert_not_called()
    assert result.steps == [] and tab.await_change.call_count == waits
    assert all(h["kind"] == "wait" for h in agent._history)  # Jev видит, что ждали


def test_done_on_a_text_step_before_typing_is_ignored_not_checked(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab, steps=[ScenarioStep("Type the query into the search box", text="Gödel")], goal="")
    choose = scripted(
        decision("DONE", "DONE", step_done=0.36),  # Википедия 26.09: DONE до ввода текста шага
        decision("DONE", "DONE", step_done=0.6),  # и даже с p ≥ 0.5 DONE не закрывает шаг с текстом
        decision("e1", "TYPE_TEXT", step_done=0.1),
    )
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert verifies(choose) == [False, False, False]  # без режима проверки
    assert tab.act.call_count == 1 and result.status != "unconfirmed"


def test_check_never_executes_an_action_even_if_one_comes_back(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab, steps=TWO, goal="")
    monkeypatch.setattr(
        loop, "choose", scripted(decision("DONE", "DONE", step_done=0.3), decision("e3", "CLICK", step_done=0.2))
    )
    result = agent.run()
    assert result.status == "failed" and "not allowed while checking" in result.error
    tab.act.assert_not_called()


def test_check_mode_ends_with_the_step(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab, steps=TWO, goal="")
    choose = scripted(
        decision("DONE", "DONE", step_done=0.3),
        decision("DONE", "DONE", step_done=0.9),  # проверка подтвердила шаг 1
        decision("e3", "CLICK", step_done=0.1),  # шаг 2 — обычный вопрос со всеми действиями
        DONE_STEP,
    )
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and verifies(choose) == [False, True, False, False]
    assert numbers(choose) == [1, 1, 2, 2] and tab.act.call_count == 1


# п. 3: порог уверенности для действий


def test_uncertain_action_is_not_executed_the_first_time_and_jev_is_asked_again(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab)
    choose = scripted(decision("e3", "CLICK", confidence=0.25), decision("e3", "CLICK", confidence=0.9), DONE)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and choose.call_count == 3
    assert tab.act.call_count == 1 and [s.confidence for s in result.steps] == [0.9]
    tab.await_change.assert_called_once_with()  # как WAIT: ожидание изменения, свежий снимок, новый вопрос
    assert agent._history[0]["kind"] == "wait" and choose.call_args_list[1].args[3][0]["kind"] == "wait"


@pytest.mark.parametrize("scenario", [False, True], ids=["goal", "scenario"])
def test_two_uncertain_actions_in_a_row_are_blocked(monkeypatch, scenario):
    tab = make_tab()
    tab.observe.side_effect = long_label_pages()
    agent = make_agent(tab, steps=TWO if scenario else None, goal="" if scenario else "Open the chat")
    p = 0.2 if scenario else None
    choose = scripted(
        decision("e1", "TYPE_TEXT", step_done=p, confidence=0.1), decision("e3", "CLICK", step_done=p, confidence=0.25)
    )
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "blocked" and result.error == f"uncertain action: CLICK on {LONG_LABEL[:39]}… (conf 0.25)"
    tab.act.assert_not_called()
    assert choose.call_count == 2 and result.steps == []


def test_only_consecutive_uncertain_actions_block_and_the_threshold_is_0_3(monkeypatch):
    tab = make_tab()
    agent = make_agent(tab)
    choose = scripted(
        decision("e3", "CLICK", confidence=0.29),
        decision("e3", "CLICK", confidence=0.3),  # на пороге — исполняется
        decision("e3", "CLICK", confidence=0.2),  # снова неуверенно, но не подряд
        decision("e3", "CLICK", confidence=0.95),
        DONE,
    )
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and [s.confidence for s in result.steps] == [0.3, 0.95]
    assert tab.act.call_count == 2 and tab.await_change.call_count == 2
    assert loop.MIN_ACTION_CONFIDENCE == 0.3


# --- инварианты сценария (docs/plan-waits.md §6.1) -------------------------------------------------------------------

CHAT_STEPS = [
    ScenarioStep("Type the chat name into the chat search box", CHAT),
    ScenarioStep("Open the chat"),
    ScenarioStep("Type the message into the message box", MESSAGE),
    ScenarioStep("Send the message"),
]
TYPE_CHAT = decision("e1", "TYPE_TEXT", step_done=0.0)
FOCUS_SEARCH = decision("e2", "CLICK", step_done=0.0)  # клик в само поле поиска: текст не использован
OPEN_CHAT = decision("e3", "CLICK", step_done=0.0)
TYPE_MSG = decision("e4", "TYPE_TEXT", step_done=0.0)
SEND = decision("e5", "CLICK", step_done=0.0)
CONFIRM = decision("DONE", "DONE", step_done=0.9)


def chat_page(tab, n):
    """Чат: поле поиска (узел 10), строка чата (20), поле сообщения (30), Send (40); значения полей — `tab.typed`."""
    search, message = tab.typed.get(10, ""), tab.typed.get(30, "")
    state = {
        "url": "https://web.whatsapp.com/",
        "title": "WhatsApp",
        "text": f"Рабочий {n}",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": search, "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": search, "node": 10},
            {"id": "e3", "kind": "click", "label": "Рабочий 12:52", "role": "button", "value": "", "node": 20},
            {"id": "e4", "kind": "fill", "label": "Type a message", "role": "textbox", "value": message, "node": 30},
            {"id": "e5", "kind": "click", "label": "Send", "role": "button", "value": "", "node": 40},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def chat_tab():
    tab = make_tab()
    count = iter(range(1, 1000))
    tab.observe.side_effect = lambda *_a, **_k: chat_page(tab, next(count))
    return tab


def remounting(tab, answers, before):
    """choose: ответы по очереди; перед вызовами с номерами из `before` (с 1) сайт пересоздаёт поля (`remount`)."""
    queue, calls = list(answers), []

    def choose(*_a, **_k):
        calls.append(1)
        if len(calls) in before:
            tab.remount()
        return queue.pop(0)

    return Mock(side_effect=choose)


def labels(tab):
    return [c.args[0]["label"] for c in tab.act.call_args_list]


def test_field_re_rendered_between_steps_3_and_4_rolls_back_to_step_3_and_retypes(monkeypatch, caplog):
    tab = chat_tab()
    agent = make_agent(tab, steps=CHAT_STEPS, goal="")
    answers = [TYPE_CHAT, OPEN_CHAT, CONFIRM, TYPE_MSG, SEND, TYPE_MSG, SEND, CONFIRM]
    choose = remounting(tab, answers, before={5})  # поле сообщения пересоздано, пока Jev думал над Send
    monkeypatch.setattr(loop, "choose", choose)
    result, info = info_lines(caplog, agent.run)
    assert result.status == "done" and result.scenario_done == 4
    assert numbers(choose) == [1, 2, 2, 3, 4, 3, 4, 4]  # решение под шаг 4 отброшено, Jev спрошен под шаг 3
    assert labels(tab) == ["Search", "Рабочий 12:52", "Type a message", "Type a message", "Send"]  # Send — раз
    assert [c.kwargs["text"] for c in tab.act.call_args_list if c.kwargs["text"]] == [CHAT, MESSAGE, MESSAGE]
    assert agent._history[2]["note"] == "text of step 3 vanished (field re-rendered)"  # Jev видит в recent_actions
    assert "шаг 4/4: текст шага 3 пропал — возвращаюсь к шагу 3 (1-й раз)" in info
    assert [s.scenario_step for s in result.steps] == [1, 2, 3, 3, 4]


def test_blocked_after_the_field_was_re_rendered_rolls_back_instead_of_a_second_look(monkeypatch):
    """Стенд search-remount: поле пересоздано — Send пропал, Jev отвечает BLOCKED; это откат к вводу, не blocked."""
    tab = chat_tab()
    agent = make_agent(tab, steps=CHAT_STEPS, goal="")
    blocked = decision("BLOCKED", "BLOCKED", step_done=0.0)
    answers = [TYPE_CHAT, OPEN_CHAT, CONFIRM, TYPE_MSG, blocked, TYPE_MSG, SEND, CONFIRM]
    choose = remounting(tab, answers, before={5})
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and numbers(choose) == [1, 2, 2, 3, 4, 3, 4, 4]
    tab.await_change.assert_not_called()  # второго взгляда не было


def test_jev_confirming_a_later_step_drops_the_invariant(monkeypatch):
    """Текст шага 1 пропал после того, как Jev подтвердил шаг 2 — это не откат; в конце проверяется только шаг 3."""
    tab = chat_tab()
    steps = [ScenarioStep("Type A", "a"), ScenarioStep("Check the preview"), ScenarioStep("Type B into the box", "b")]
    agent = make_agent(tab, steps=steps, goal="")
    answers = [TYPE_CHAT, decision("wait", "WAIT", step_done=0.9), TYPE_MSG]
    choose = remounting(tab, answers, before={2})
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 3 and numbers(choose) == [1, 2, 3]
    assert [c.args[0] for c in tab.field_values.call_args_list] == [[{"node": 30, "label": "Type a message"}]]


def test_send_that_clears_the_field_is_not_a_rollback_even_before_jev_confirms(monkeypatch):
    """Клик не по полю использует текст (Send очистил поле): инвариант снят, DONE с p < 0,5 — проверка, не откат."""
    tab = chat_tab()
    act = tab.act.side_effect

    def act_and_send(action, page, text=None):
        if action["label"] == "Send":
            tab.remount()  # сообщение ушло, поле пусто
        return act(action, page, text=text)

    tab.act.side_effect = act_and_send
    agent = make_agent(tab, steps=CHAT_STEPS, goal="")
    answers = [TYPE_CHAT, OPEN_CHAT, CONFIRM, TYPE_MSG, SEND, decision("DONE", "DONE", step_done=0.3), CONFIRM]
    monkeypatch.setattr(loop, "choose", scripted(*answers))
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 4 and labels(tab).count("Send") == 1
    assert tab.field_values.call_count == 2  # перед кликом по чату (шаг 1) и перед Send (шаг 3), после — нечего


def test_click_into_the_same_field_keeps_the_invariant(monkeypatch):
    tab = chat_tab()
    agent = make_agent(tab, steps=CHAT_STEPS[:2], goal="")
    answers = [TYPE_CHAT, FOCUS_SEARCH, OPEN_CHAT, TYPE_CHAT, OPEN_CHAT, CONFIRM]
    choose = remounting(tab, answers, before={3})
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and numbers(choose) == [1, 2, 2, 1, 2, 2]
    assert labels(tab) == ["Search", "Open Search", "Search", "Рабочий 12:52"]


def test_text_that_keeps_vanishing_is_blocked_on_the_third_time(monkeypatch):
    tab = chat_tab()
    agent = make_agent(tab, steps=CHAT_STEPS, goal="")
    answers = [TYPE_CHAT, OPEN_CHAT, CONFIRM, TYPE_MSG, SEND, TYPE_MSG, SEND, TYPE_MSG, SEND]
    monkeypatch.setattr(loop, "choose", remounting(tab, answers, before={5, 7, 9}))
    result = agent.run()
    assert result.status == "blocked" and result.error == "step 3 of 4: typed text does not stay in the field"
    assert result.scenario_done == 2 and labels(tab).count("Type a message") == 3 and "Send" not in labels(tab)


def test_last_text_step_waits_for_readiness_and_checks_the_field_before_done(monkeypatch):
    tab = chat_tab()
    waits = []

    def await_ready(*args):
        waits.append(args)
        if len(waits) == 2:
            tab.remount()  # после снимка с текстом, до done: подсказка/автосохранение перерисовали поле
        return {}

    tab.await_ready.side_effect = await_ready
    agent = make_agent(tab, steps=[CHAT_STEPS[2]], goal="")
    choose = scripted(TYPE_MSG, TYPE_MSG)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and result.scenario_done == 1 and result.jev_calls == 2
    assert labels(tab) == ["Type a message", "Type a message"]
    assert [len(args) for args in waits] == [1, 0, 1, 0]  # после ввода — с действием, перед done — без
    assert agent._history[0]["note"] == "text of step 1 vanished (field re-rendered)"


# --- находки ревью 2–5 и режим цели (docs/plan-waits.md §6.2) ---------------------------------------------------------


def test_field_replaced_with_another_role_and_the_same_label_closes_the_text_step(monkeypatch):
    """Википедия: поле поиска после ввода — новый узел с ролью combobox и той же подписью."""
    tab = make_tab()
    after = field_page(MESSAGE, node=11, text="Рабочий 1")
    for action in after["actions"][:2]:
        action["role"] = "combobox"
    after["fingerprint"] = fingerprint(after)
    tab.observe.side_effect = [field_page(""), after]
    agent = make_agent(tab, steps=TYPE_AND_SEND, goal="")
    choose = scripted(TYPE_MESSAGE, decision("e3", "CLICK", step_done=0.9))
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and numbers(choose) == [1, 2] and "note" not in agent._history[0]


def without_field(state):
    state["actions"] = [a for a in state["actions"] if a.get("node") != state["actions"][0]["node"]]
    state["fingerprint"] = fingerprint(state)
    return state


@pytest.mark.parametrize("first", ["empty", "absent"])
def test_text_seen_only_after_the_next_page_change_closes_the_step(monkeypatch, first):
    tab = make_tab()
    right_after = field_page("", node=11, text="Рабочий 1")
    if first == "absent":
        right_after = without_field(right_after)
    tab.observe.side_effect = [field_page(""), right_after, field_page(MESSAGE, node=11, text="Рабочий 2")]
    agent = make_agent(tab, steps=TYPE_AND_SEND, goal="")
    choose = scripted(TYPE_MESSAGE, decision("e3", "CLICK", step_done=0.9))
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and numbers(choose) == [1, 2] and tab.act.call_count == 1
    tab.await_change.assert_called_once_with()  # одно наблюдение по событию перед пометкой «пропал»
    assert "note" not in agent._history[0]


@pytest.mark.parametrize(
    ("value", "text", "shown"),
    [
        ("+7 (777) 123-45-67", "77771234567", True),  # маска телефона
        ("4111 1111 1111 1111", "4111111111111111", True),  # маска карты
        ("26/09/2026", "26.09.2026", True),  # маска даты
        ("это я через агента, проверка", MESSAGE, True),  # эмодзи картинкой (<img alt>)
        (" это я через агента,\n проверка 👋 ", MESSAGE, True),  # пробелы
        ("Рабочий чат", CHAT, True),  # подсказка дописала хвост
        ("Re: Рабочий", CHAT, False),  # текст в середине чужого значения
        ("Рабочий", "Рабочий чат", False),  # начало текста
        ("+7 (777) 123-45-67 доб. 9", "77771234567", False),  # запасное — равенство, не префикс
        ("", CHAT, False),
        (None, CHAT, False),
    ],
    ids=["phone", "card", "date", "emoji-img", "spaces", "tail", "middle", "part", "mask-extra", "empty", "none"],
)
def test_field_value_matches_typed_text_by_prefix_or_by_letters_and_digits(value, text, shown):
    assert loop.matches(value, text) is shown


def test_stale_page_on_the_second_done_in_check_mode_is_not_unconfirmed_at_once(monkeypatch):
    tab = make_tab()
    tab.fresh.side_effect = [True, True, False] + [True] * 5  # вердикт по устаревшей странице — переснять
    agent = make_agent(tab, steps=[ScenarioStep("Send the message")], goal="")
    done = [decision("DONE", "DONE", step_done=p) for p in (0.3, 0.2, 0.9)]
    choose = scripted(*done)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and choose.call_count == 3 and verifies(choose) == [False, True, True]


def test_goal_done_with_low_confidence_looks_again_then_is_unconfirmed(monkeypatch):
    """Трасса search-remount (цель): DONE conf 0,32 и 0,35 при неотправленном сообщении было `done`."""
    tab = make_tab()
    tab.observe.side_effect = numbered_pages()
    agent = make_agent(tab, goal="Send hi to Рабочий")
    low = [decision("DONE", "DONE", confidence=c) for c in (0.32, 0.35)]
    choose = scripted(decision("e3", "CLICK"), *low)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "unconfirmed"
    assert result.error == "goal probably done, not confirmed — check the screenshot"
    assert choose.call_count == 3 and tab.await_change.call_count == 1 and result.scenario_done is None
    assert agent._history[-1]["kind"] == "wait"  # Jev видит, что ждали
    last = tab.observe.call_count
    assert (result.url, result.title) == (f"https://example.test/{last}", f"Page {last}")


def test_goal_done_below_the_configured_threshold_gets_one_more_look(monkeypatch):
    tab = make_tab()
    chrome = Mock()
    chrome.new_tab.return_value = tab
    chrome.find_user_tab.return_value = None
    clients = ModelClients(ModelConfig(jev_api_key="test", text_api_key="test"), http=Mock())
    limits = Thresholds(done_min_confidence=0.95)
    agent = Agent(chrome, clients, "https://example.test/", "Find a book", RunConfig(timeout_s=30.0), thresholds=limits)
    choose = scripted(decision("DONE", "DONE", confidence=0.9), decision("DONE", "DONE", confidence=0.97))
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert result.status == "done" and choose.call_count == 2 and tab.await_change.call_count == 1


def test_goal_mode_notes_typed_text_missing_from_the_field_on_the_next_snapshot(monkeypatch):
    tab = make_tab()
    tab.observe.side_effect = [page(), page("Search 1"), page("Search 2")]  # поле пусто после ввода
    agent = make_agent(tab)
    choose = scripted(decision("e1", "TYPE_TEXT"), DONE)
    monkeypatch.setattr(loop, "choose", choose)
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", TextHelper("t", 5))))
    result = agent.run()
    assert result.status == "done" and agent._history[0]["note"] == VANISHED
    assert choose.call_args_list[1].args[3][0]["note"] == VANISHED  # Jev видит пометку


def test_steps_carry_the_wait_reason_and_pending_requests_of_their_wait(monkeypatch):
    tab = make_tab()
    tab.await_ready.side_effect = [{"reason": "fuse", "ms": 1500, "pending_requests": 2}, {}]
    tab.await_change.return_value = {"reason": "change", "ms": 120}
    agent = make_agent(tab)
    choose = scripted(decision("e3", "CLICK"), decision("wait", "WAIT"), decision("e3", "CLICK"), DONE)
    monkeypatch.setattr(loop, "choose", choose)
    result = agent.run()
    assert [(s.operation, s.wait_reason, s.pending_requests) for s in result.steps] == [
        ("CLICK", "fuse", 2),
        ("WAIT", "change", 0),
        ("CLICK", None, 0),  # {} — не ждали
    ]
    assert tab.await_ready.call_args_list[0].args[0]["id"] == "e3"
    assert labels(tab) == ["Go", "Go"]  # WAIT — ожидание изменения, без Tab.act
    assert tab.after_input is None  # ждали сами: observe() второй раз не ждёт
