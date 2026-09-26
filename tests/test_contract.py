import dataclasses
import json
import threading
from pathlib import Path
from typing import get_args

import pytest

from browser_hands import model, server
from browser_hands.cli import result_to_json
from browser_hands.config import RunConfig, Settings, Thresholds, apply_overrides
from browser_hands.scenario import (
    MAX_DO,
    MAX_STEPS,
    MAX_TEXT,
    ScenarioError,
    ScenarioStep,
    parse_steps,
    render,
)
from browser_hands.types import RunResult, Status, Step, Timing
from tests.fakes import FakeChrome, FakeClients, FakeCore, make_result, unconfirmed_result


def test_contract_defaults_and_timing_sum():
    s = Settings()

    assert s.browser.mode == "attach"
    assert s.browser.ws_url is None
    assert s.browser.chrome_data_dir == Path.home() / "Library/Application Support/Google/Chrome"
    assert s.browser.launch_data_dir == Path.home() / ".cache/browser-hands/chrome-profile"
    assert s.browser.chrome_binary == Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    assert s.browser.headless is False
    assert s.browser.viewport == (1120, 780)
    assert s.browser.connect_timeout_s == 60.0
    assert s.browser.call_timeout_s == 30.0
    assert s.browser.screenshot_quality == 60
    assert s.browser.screenshot_scale == 1.0

    assert s.models.jev_url == "https://openrouter.ai/api/v1/systemone"
    assert s.models.jev_model == "jev-latest"
    assert s.models.jev_api_key == ""
    assert s.models.text_base_url == "https://openrouter.ai/api/v1"
    assert s.models.text_model == "inception/mercury-2.5"
    assert s.models.text_api_key == ""
    assert s.models.text_reasoning == "none"
    assert s.models.jev_timeout_s == 10.0
    assert s.models.text_timeout_s == 8.0

    assert s.run.max_steps == 25
    assert s.run.timeout_s == 90.0
    assert s.run.keep_open is False

    # вложенные конфиги не общие между экземплярами
    assert Settings().browser is not s.browser

    # ключи не попадают в repr
    s.models.jev_api_key = "secret-jev"
    s.models.text_api_key = "secret-text"
    assert "secret" not in repr(s)

    a = Timing(model_ms=1, text_ms=2, browser_ms=3, wait_ms=4)
    b = Timing(model_ms=10, text_ms=20, browser_ms=30, wait_ms=40)
    assert a + b == Timing(model_ms=11, text_ms=22, browser_ms=33, wait_ms=44)
    assert a == Timing(model_ms=1, text_ms=2, browser_ms=3, wait_ms=4)  # операнды не меняются
    assert sum([a, b], Timing()) == a + b
    assert Timing() + Timing() == Timing()


def test_parse_steps_accepts_1_and_20_steps():
    assert parse_steps([{"do": "Open the chat"}]) == [ScenarioStep(do="Open the chat")]
    twenty = parse_steps([{"do": f"Step {i}", "text": f"t{i}"} for i in range(MAX_STEPS)])
    assert len(twenty) == MAX_STEPS == 20
    assert twenty[3] == ScenarioStep(do="Step 3", text="t3")


def test_parse_steps_normalizes_do_but_keeps_text_verbatim():
    steps = parse_steps([{"do": "  Type the message  ", "text": "  привет 👋 "}, {"do": "Send", "text": None}])
    assert steps == [ScenarioStep("Type the message", "  привет 👋 "), ScenarioStep("Send")]
    # готовые шаги (ядро зовёт мимо сервера) проверяются заново
    assert parse_steps(steps) == steps
    with pytest.raises(ScenarioError, match="step 1: do is empty"):
        parse_steps([ScenarioStep(do=" ")])


def test_parse_steps_limits():
    assert parse_steps([{"do": "x" * MAX_DO, "text": "y" * MAX_TEXT}])[0].text == "y" * MAX_TEXT
    assert (MAX_DO, MAX_TEXT) == (300, 2000)
    assert MAX_TEXT == model.MAX_TEXT_VALUE


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"do": "Open"}, "steps: expected a list, got dict"),
        ("Open the chat", "steps: expected a list, got str"),
        (None, "steps: expected a list, got NoneType"),
        ([], "steps: empty list"),
        ([{"do": "x"}] * (MAX_STEPS + 1), "steps: 21 steps, max 20"),
        ([{"do": "Open"}, "Send"], "step 2: expected an object with do and optional text"),
        ([{"do": "Open"}, {"do": "Send", "url": "x"}], "step 2: unknown keys url (allowed: do, text)"),
        ([{"text": "hi"}], "step 1: do is missing"),
        ([{"do": 5}], "step 1: do must be a string"),
        ([{"do": "Open"}, {"do": "Open"}, {"do": " \n "}], "step 3: do is empty"),
        ([{"do": "x" * (MAX_DO + 1)}], "step 1: do is longer than 300 characters"),
        ([{"do": "Type", "text": ""}], "step 1: text is empty"),
        ([{"do": "Type", "text": 7}], "step 1: text must be a string or null"),
        ([{"do": "Type", "text": "y" * (MAX_TEXT + 1)}], "step 1: text is longer than 2000 characters"),
    ],
)
def test_parse_steps_rejects(raw, message):
    with pytest.raises(ScenarioError) as info:
        parse_steps(raw)
    assert str(info.value) == message
    assert isinstance(info.value, ValueError)


def test_render_step():
    steps = parse_steps([{"do": "Search"}, {"do": "open the chat"}, {"do": "Type"}, {"do": "Send"}])
    assert render(2, steps) == "Step 2 of 4: open the chat"
    assert render(4, steps) == "Step 4 of 4: Send"
    for bad in (0, 5):
        with pytest.raises(ValueError):
            render(bad, steps)


def test_scenario_fields_default_to_goal_mode():
    step = Step(1, "CLICK", "Send", None, True, "https://x.test", 0.9, Timing(), None)
    assert step.scenario_step is None
    result = RunResult("done", [step], "https://x.test", "X", None, None, 0, Timing(), 1)
    assert result.scenario_done is None
    assert result.scenario_total is None
    assert result.jev_calls == 0


def test_fakes_carry_scenario():
    goal = make_result(steps=3)
    assert (goal.scenario_done, goal.scenario_total, goal.jev_calls) == (None, None, 0)
    assert [s.scenario_step for s in goal.steps] == [None, None, None]

    result = make_result("blocked", steps=5, scenario=(2, 4), jev_calls=6)
    assert (result.scenario_done, result.scenario_total, result.jev_calls) == (2, 4, 6)
    assert [s.scenario_step for s in result.steps] == [1, 2, 3, 4, 4]

    core = FakeCore()
    steps = [ScenarioStep("Open the chat")]
    kwargs = {"screenshot_quality": 60, "screenshot_scale": 1.0, "cancel": threading.Event()}
    core.agent_factory(FakeChrome(), FakeClients(), "https://x.test", "", RunConfig(), steps=steps, **kwargs)
    core.agent_factory(FakeChrome(), FakeClients(), "https://x.test", "goal", RunConfig(), **kwargs)
    assert core.agents[0]["steps"] == steps
    assert core.agents[1]["steps"] is None


def test_unconfirmed_is_a_status_and_the_fake_carries_it():
    assert get_args(Status) == ("done", "blocked", "failed", "timeout", "step_limit", "unconfirmed")
    result = unconfirmed_result()
    assert result.status == "unconfirmed" and (result.scenario_done, result.scenario_total) == (3, 4)
    assert result.error == "step 4 of 4: probably done, not confirmed — check the screenshot"


def test_thresholds_are_settings_without_env():
    t = Settings().thresholds
    assert t == Thresholds(
        step_done_min_p=0.7,
        done_step_done_min_p=0.5,
        min_action_confidence=0.3,
        done_min_confidence=0.5,
        wait_fuse_s=1.5,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.step_done_min_p = 0.9  # type: ignore[misc]
    # меняет расчёт (docs/calibration.md), не оператор: env порогов нет, флаги их не трогают
    env = {"BROWSER_HANDS_STEP_DONE_MIN_P": "0.9", "BROWSER_HANDS_WAIT_FUSE_S": "9", "BROWSER_HANDS_THRESHOLDS": "x"}
    assert Settings.from_env(env).thresholds == Thresholds()
    custom = Settings(thresholds=Thresholds(wait_fuse_s=2.0))
    assert apply_overrides(custom, max_steps=5, timeout_s=10.0).thresholds.wait_fuse_s == 2.0


def test_service_passes_the_thresholds_to_the_agent(monkeypatch):
    core = FakeCore()
    settings = Settings(thresholds=Thresholds(step_done_min_p=0.8))
    settings.models.jev_api_key = settings.models.text_api_key = "k"
    monkeypatch.setattr(server, "import_core", lambda name: None)
    server.BrowseService(settings, **core.factories()).browse("https://x.test", "goal")
    assert core.agents[0]["thresholds"] is settings.thresholds

    calls: list[dict] = []
    monkeypatch.setattr(
        server, "import_core", lambda name: type("Core", (), {"Agent": lambda *a, **kw: calls.append(kw)})
    )
    common = {"screenshot_quality": 60, "screenshot_scale": 1.0, "cancel": threading.Event()}
    server.default_agent_factory(None, None, "https://x.test", "g", RunConfig(), **common)  # type: ignore[arg-type]
    server.default_agent_factory(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        "https://x.test",
        "g",
        RunConfig(),
        thresholds=settings.thresholds,
        **common,
    )
    assert calls[0]["thresholds"] is None and calls[1]["thresholds"] is settings.thresholds


def test_step_wait_fields_default_to_not_waited_and_reach_the_json():
    step = Step(1, "CLICK", "Send", None, True, "https://x.test", 0.9, Timing(), None)
    assert (step.wait_reason, step.pending_requests) == (None, 0)
    result = make_result(steps=2)
    assert [(s.wait_reason, s.pending_requests) for s in result.steps] == [("quiet", 0), ("quiet", 0)]
    # eval.py и bench.py пишут строку прогона через cli.result_to_json — новые поля шагов в ней есть
    row = json.loads(json.dumps(result_to_json(result)))
    assert [(s["wait_reason"], s["pending_requests"]) for s in row["steps"]] == [("quiet", 0), ("quiet", 0)]
