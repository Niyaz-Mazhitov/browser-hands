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


def test_flags_override_env(tmp_path):
    binary = tmp_path / "chrome"
    binary.write_text("")
    env = {**ENV, "BROWSER_HANDS_CHROME_BINARY": str(binary), "BROWSER_HANDS_MAX_STEPS": "9"}
    core = FakeCore()

    argv = [*RUN, "--mode", "launch", "--headless", "--max-steps", "3", "--timeout", "12", "--keep-open"]
    assert main([*argv, "--data-dir", str(tmp_path / "p")], env=env, factories=core.factories()) == 0

    browser = core.chromes[0].config
    assert (browser.mode, browser.headless, browser.launch_data_dir) == ("launch", True, tmp_path / "p")
    run = core.agents[0]["run"]
    assert (run.max_steps, run.timeout_s, run.keep_open) == (3, 12.0, True)


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
        main(["serve", "--help"])
    assert info.value.code == 0
    assert "--mode" in capsys.readouterr().out


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
