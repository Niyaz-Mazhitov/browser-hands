import json
import os
import signal
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from browser_hands.cli import _stop_handler, main
from browser_hands.scenario import MAX_STEPS, ScenarioStep
from browser_hands.server import BROWSE_DESCRIPTION, INSTRUCTIONS
from tests.fakes import JPEG, FakeCore, make_result

ROOT = Path(__file__).resolve().parent.parent
ENV = {"OPENROUTER_API_KEY": "test-key"}
RUN = ["run", "--url", "https://example.test", "--goal", "g"]


def test_run_prints_steps_and_returns_0_on_done(capsys):
    core = FakeCore()

    code = main(RUN, env=ENV, factories=core.factories())

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("status: done\n")
    assert "1. CLICK Target 1  [0.7 с, conf 0.90" in out
    assert "elapsed 1.7s (model 1.2 / text 0.3 / browser 0.1 / wait 0.1)" in out
    assert "\nвкладка: новая\n" in out
    assert core.agents[0]["run"].new_tab is False  # без --new-tab — искать открытую вкладку сайта
    assert core.chromes[0].close_calls == 1  # run закрывает Chrome и клиентов
    assert core.clients[0].close_calls == 1


def test_run_returns_2_when_not_done(capsys):
    code = main(RUN, env=ENV, factories=FakeCore(result=make_result("blocked")).factories())

    assert code == 2
    assert capsys.readouterr().out.startswith("status: blocked\n")


def test_run_json_has_no_screenshot_bytes(capsys, tmp_path):
    shot = tmp_path / "sub" / "shot.jpg"

    code = main([*RUN, "--json", "--screenshot", str(shot)], env=ENV, factories=FakeCore().factories())

    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["status"] == "done"
    assert len(data["steps"]) == 2
    assert data["timing"] == {"model_ms": 1200, "text_ms": 300, "browser_ms": 80, "wait_ms": 100}
    assert "screenshot_jpeg" not in data
    assert data["screenshot_bytes"] == len(JPEG)
    assert data["screenshot_path"] == str(shot)
    assert shot.read_bytes() == JPEG
    assert data["tab"] == "new"


def test_flags_override_env(tmp_path):
    binary = tmp_path / "chrome"
    binary.write_text("")
    env = {**ENV, "BROWSER_HANDS_CHROME_BINARY": str(binary), "BROWSER_HANDS_MAX_STEPS": "9"}
    core = FakeCore()

    argv = [*RUN, "--mode", "launch", "--headless", "--max-steps", "3", "--timeout", "12", "--keep-open", "--new-tab"]
    assert main([*argv, "--data-dir", str(tmp_path / "p")], env=env, factories=core.factories()) == 0

    browser = core.chromes[0].config
    assert (browser.mode, browser.headless, browser.launch_data_dir) == ("launch", True, tmp_path / "p")
    run = core.agents[0]["run"]
    assert (run.max_steps, run.timeout_s, run.keep_open, run.new_tab) == (3, 12.0, True, True)


def test_fresh_profile_is_temporary_and_launch_only(tmp_path, capsys):
    binary = tmp_path / "chrome"
    binary.write_text("")
    core = FakeCore()

    code = main(
        [*RUN, "--fresh-profile"],  # launch подразумевается
        env={**ENV, "BROWSER_HANDS_CHROME_BINARY": str(binary)},
        factories=core.factories(),
    )

    profile = core.chromes[0].config.launch_data_dir
    assert code == 0
    assert profile.name.startswith("browser-hands-profile-")
    assert not profile.exists()  # удалён после закрытия Chrome

    assert main([*RUN, "--mode", "attach", "--fresh-profile"], env=ENV, factories=FakeCore().factories()) == 2
    assert "--fresh-profile" in capsys.readouterr().err


def test_config_error_is_one_line_not_traceback(capsys):
    code = main(RUN, env={**ENV, "BROWSER_HANDS_MAX_STEPS": "abc"}, factories=FakeCore().factories())

    err = capsys.readouterr().err
    assert code == 2
    assert "BROWSER_HANDS_MAX_STEPS" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("flags", "name"), [(["--max-steps", "51"], "--max-steps"), (["--timeout", "301"], "--timeout")]
)
def test_run_limits_above_ceiling_are_config_errors(capsys, flags, name):
    core = FakeCore()

    code = main([*RUN, *flags], env=ENV, factories=core.factories())

    err = capsys.readouterr().err
    assert code == 2
    assert name in err and "Traceback" not in err
    assert core.chromes == [] and core.agents == []


def test_stop_handler_ignores_repeated_signal_until_chrome_is_closed():
    events: list[str] = []

    class Service:
        def abort(self) -> None:
            events.append("abort")
            stop(signal.SIGTERM, None)  # повторный сигнал приходит, пока закрываем Chrome
            events.append("chrome closed")

    stop = _stop_handler(Service(), lambda: events.append("cleanup"), exit_=lambda code: events.append(f"exit {code}"))

    stop(signal.SIGINT, None)

    assert events == ["abort", "chrome closed", "cleanup", "exit 0"]


def test_run_without_core_is_clear_failure(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "browser_hands.chrome", None)  # ядро не влито

    code = main(["run", "--url", "x", "--goal", "y"], env={})

    captured = capsys.readouterr()
    assert code == 2
    assert "status: failed\nerror: модуль browser_hands.chrome ещё не готов\n" in captured.out
    assert "Traceback" not in captured.out + captured.err


def test_help_lists_both_commands(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "run" in out and "serve" in out

    with pytest.raises(SystemExit) as info:
        main(["run", "--help"])
    assert info.value.code == 0
    assert "--new-tab" in capsys.readouterr().out

    with pytest.raises(SystemExit) as info:
        main(["serve", "--help"])
    assert info.value.code == 0
    serve_help = capsys.readouterr().out
    assert "--mode" in serve_help
    assert "--new-tab" not in serve_help  # параметр прогона, у serve — в browse(new_tab=…)


def test_serve_answers_initialize_and_tools_list_over_stdio():
    env = {k: v for k, v in os.environ.items() if not k.startswith("BROWSER_HANDS_") and k != "OPENROUTER_API_KEY"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "browser_hands.cli", "serve"], env=env, cwd=ROOT)

    async def main_async():
        with anyio.fail_after(20):
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                return init, tools

    init, tools = anyio.run(main_async)

    assert init.serverInfo.name == "browser-hands"
    assert init.instructions == INSTRUCTIONS
    assert [t.name for t in tools.tools] == ["browse"]
    assert tools.tools[0].description == BROWSE_DESCRIPTION
    assert len(BROWSE_DESCRIPTION) <= 200
    new_tab = tools.tools[0].inputSchema["properties"]["new_tab"]
    assert (new_tab["type"], new_tab["default"]) == ("boolean", False)


def test_run_prints_user_tab_and_json_tab(capsys):
    core = FakeCore(result=make_result(tab="user", url="https://web.whatsapp.com/"))

    assert main([*RUN, "--url", "web.whatsapp.com"], env=ENV, factories=core.factories()) == 0
    assert "\nвкладка: твоя (web.whatsapp.com)\n" in capsys.readouterr().out

    assert main([*RUN, "--json"], env=ENV, factories=core.factories()) == 0
    assert json.loads(capsys.readouterr().out)["tab"] == "user"


def test_run_without_tab_prints_no_tab_line(capsys):
    core = FakeCore(result=make_result("failed", steps=0, screenshot=None, error="нет вкладки", tab=None))

    assert main(RUN, env=ENV, factories=core.factories()) == 2
    assert "вкладка:" not in capsys.readouterr().out

    assert main([*RUN, "--json"], env=ENV, factories=core.factories()) == 2
    assert json.loads(capsys.readouterr().out)["tab"] is None


# --- сценарий: --steps-file, --step ---------------------------------------------------------------------------------

SCENARIO = [
    {"do": "Type the message into the message box", "text": "это я через агента, проверка 👋"},
    {"do": "Send the message"},
]
SCN_RUN = ["run", "--url", "https://example.test"]


def steps_file(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "steps.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_steps_file_reaches_agent_without_goal(tmp_path, capsys):
    core = FakeCore(result=make_result(scenario=(2, 2)))

    code = main([*SCN_RUN, "--steps-file", str(steps_file(tmp_path, SCENARIO))], env=ENV, factories=core.factories())

    assert code == 0
    agent = core.agents[0]
    assert agent["goal"] == ""
    assert agent["steps"] == [
        ScenarioStep("Type the message into the message box", "это я через агента, проверка 👋"),
        ScenarioStep("Send the message"),
    ]
    out = capsys.readouterr().out
    assert "\nсценарий: 2 из 2 выполнено\n" in out and "остановился" not in out
    assert "1. CLICK Target 1 [шаг 1]  [0.7 с" in out


def test_repeated_step_flags_follow_the_file(tmp_path):
    core = FakeCore()

    main([*SCN_RUN, "--step", "Open the chat", "--step", " Send "], env=ENV, factories=core.factories())
    main(
        [*SCN_RUN, "--goal", "g", "--steps-file", str(steps_file(tmp_path, SCENARIO[:1])), "--step", "Send"],
        env=ENV,
        factories=core.factories(),
    )

    assert core.agents[0]["steps"] == [ScenarioStep("Open the chat"), ScenarioStep("Send")]
    assert core.agents[0]["goal"] == ""
    assert [s.do for s in core.agents[1]["steps"]] == ["Type the message into the message box", "Send"]
    assert core.agents[1]["goal"] == "g"


def test_goal_mode_passes_no_steps_and_json_has_no_scenario(capsys):
    core = FakeCore()

    assert main([*RUN, "--json"], env=ENV, factories=core.factories()) == 0

    assert core.agents[0]["steps"] is None
    data = json.loads(capsys.readouterr().out)
    assert "scenario" not in data
    assert (data["scenario_done"], data["scenario_total"]) == (None, None)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ([{"do": "x"}] * (MAX_STEPS + 1), "browser-hands: steps: 21 steps, max 20"),
        ([{"do": "Open"}, {"do": "Send", "url": "x"}], "browser-hands: step 2: unknown keys url (allowed: do, text)"),
        ([{"do": "Type", "text": ""}], "browser-hands: step 1: text is empty"),
        ({"do": "Open"}, "нужен JSON-список шагов, а не dict"),
        ("{not json", "не JSON"),
        (None, "No such file or directory"),
    ],
)
def test_bad_steps_file_is_one_line_and_code_2(tmp_path, capsys, content, message):
    core = FakeCore()
    if content is None:
        path = tmp_path / "missing.json"
    elif isinstance(content, str):
        path = tmp_path / "bad.json"
        path.write_text(content, encoding="utf-8")
    else:
        path = steps_file(tmp_path, content)

    code = main([*SCN_RUN, "--steps-file", str(path)], env=ENV, factories=core.factories())

    err = capsys.readouterr().err
    assert code == 2
    assert message in err and len(err.strip().splitlines()) == 1 and "Traceback" not in err
    assert core.chromes == [] and core.agents == []


def test_step_over_limit_via_flags_is_code_2(capsys):
    core = FakeCore()
    flags = [arg for i in range(MAX_STEPS + 1) for arg in ("--step", f"Step {i}")]

    assert main([*SCN_RUN, *flags], env=ENV, factories=core.factories()) == 2
    assert capsys.readouterr().err.strip() == "browser-hands: steps: 21 steps, max 20"
    assert core.agents == []


@pytest.mark.parametrize("goal", [[], ["--goal", "  "]])
def test_run_without_goal_and_steps_is_usage_error(capsys, goal):
    core = FakeCore()

    with pytest.raises(SystemExit) as info:
        main([*SCN_RUN, *goal], env=ENV, factories=core.factories())

    assert info.value.code == 2
    assert "нужен --goal или сценарий (--steps-file, --step)" in capsys.readouterr().err
    assert core.chromes == []


def test_run_json_has_scenario_and_step_numbers(tmp_path, capsys):
    core = FakeCore(result=make_result("blocked", steps=3, scenario=(1, 2), jev_calls=4))

    code = main(
        [*SCN_RUN, "--steps-file", str(steps_file(tmp_path, SCENARIO)), "--json"], env=ENV, factories=core.factories()
    )

    data = json.loads(capsys.readouterr().out)
    assert code == 2
    assert (data["scenario_done"], data["scenario_total"], data["jev_calls"]) == (1, 2, 4)
    assert [step["scenario_step"] for step in data["steps"]] == [1, 2, 2]
    assert data["scenario"] == [
        {"do": "Type the message into the message box", "text": "это я через агента, проверка 👋"},
        {"do": "Send the message", "text": None},
    ]


def test_run_text_names_the_step_it_stopped_on(tmp_path, capsys):
    core = FakeCore(result=make_result("step_limit", steps=6, scenario=(1, 2), error="Step 2 of 2 not completed"))

    code = main([*SCN_RUN, "--steps-file", str(steps_file(tmp_path, SCENARIO))], env=ENV, factories=core.factories())

    lines = capsys.readouterr().out.splitlines()
    assert code == 2
    assert lines[:4] == [
        "status: step_limit",
        "error: Step 2 of 2 not completed",
        "сценарий: 1 из 2 выполнено",
        "остановился на шаге 2 из 2: Send the message",
    ]


def test_run_help_lists_scenario_flags(capsys):
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    out = capsys.readouterr().out
    assert "--steps-file" in out and "--step DO" in out and "по-английски" in out
