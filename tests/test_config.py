from pathlib import Path

import pytest

from browser_hands.config import ConfigError, Settings, apply_overrides


def test_empty_env_gives_defaults():
    assert Settings.from_env({}) == Settings()


def test_empty_values_mean_default():
    env = {
        "BROWSER_HANDS_MODE": "",
        "BROWSER_HANDS_WS_URL": "  ",
        "BROWSER_HANDS_MAX_STEPS": "",
        "OPENROUTER_API_KEY": "",
    }
    assert Settings.from_env(env) == Settings()


def test_overrides_from_env():
    env = {
        "BROWSER_HANDS_MODE": "launch",
        "BROWSER_HANDS_WS_URL": "ws://127.0.0.1:9333/devtools/browser/abc",
        "BROWSER_HANDS_CHROME_DATA_DIR": "~/chrome-data",
        "BROWSER_HANDS_LAUNCH_DATA_DIR": "/tmp/bh-profile",
        "BROWSER_HANDS_CHROME_BINARY": "/opt/chrome",
        "BROWSER_HANDS_HEADLESS": "yes",
        "BROWSER_HANDS_CONNECT_TIMEOUT_S": "5.5",
        "BROWSER_HANDS_SCREENSHOT_QUALITY": "40",
        "BROWSER_HANDS_SCREENSHOT_SCALE": "0.5",
        "BROWSER_HANDS_JEV_URL": "https://jev.test/v1/systemone",
        "BROWSER_HANDS_JEV_MODEL": "jev-next",
        "BROWSER_HANDS_TEXT_BASE_URL": "https://text.test/v1",
        "BROWSER_HANDS_TEXT_MODEL": "some/model",
        "BROWSER_HANDS_TEXT_REASONING": "LOW",
        "BROWSER_HANDS_MAX_STEPS": "7",
        "BROWSER_HANDS_TIMEOUT_S": "30",
        "BROWSER_HANDS_LOG": "debug",
    }
    s = Settings.from_env(env)

    assert s.browser.mode == "launch"
    assert s.browser.ws_url == "ws://127.0.0.1:9333/devtools/browser/abc"
    assert s.browser.chrome_data_dir == Path.home() / "chrome-data"
    assert s.browser.launch_data_dir == Path("/tmp/bh-profile")
    assert s.browser.chrome_binary == Path("/opt/chrome")
    assert s.browser.headless is True
    assert s.browser.connect_timeout_s == 5.5
    assert s.browser.screenshot_quality == 40
    assert s.browser.screenshot_scale == 0.5
    assert s.models.jev_url == "https://jev.test/v1/systemone"
    assert s.models.jev_model == "jev-next"
    assert s.models.text_base_url == "https://text.test/v1"
    assert s.models.text_model == "some/model"
    assert s.models.text_reasoning == "low"
    assert s.run.max_steps == 7
    assert s.run.timeout_s == 30.0
    assert s.run.keep_open is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("false", False), ("No", False)],
)
def test_booleans(value, expected):
    assert Settings.from_env({"BROWSER_HANDS_HEADLESS": value}).browser.headless is expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BROWSER_HANDS_MAX_STEPS", "abc"),
        ("BROWSER_HANDS_MAX_STEPS", "0"),
        ("BROWSER_HANDS_MAX_STEPS", "51"),
        ("BROWSER_HANDS_TIMEOUT_S", "fast"),
        ("BROWSER_HANDS_TIMEOUT_S", "-1"),
        ("BROWSER_HANDS_TIMEOUT_S", "300.5"),
        ("BROWSER_HANDS_CONNECT_TIMEOUT_S", "nan"),
        ("BROWSER_HANDS_SCREENSHOT_QUALITY", "101"),
        ("BROWSER_HANDS_SCREENSHOT_SCALE", "2"),
        ("BROWSER_HANDS_HEADLESS", "maybe"),
        ("BROWSER_HANDS_MODE", "ws"),
        ("BROWSER_HANDS_TEXT_REASONING", "high"),
        ("BROWSER_HANDS_WS_URL", "http://127.0.0.1:9222"),
        ("BROWSER_HANDS_LOG", "loud"),
    ],
)
def test_invalid_value_names_the_variable(name, value):
    with pytest.raises(ValueError) as info:
        Settings.from_env({name: value})

    assert isinstance(info.value, ConfigError)
    assert name in str(info.value)


def test_keys_fall_back_to_openrouter_and_stay_out_of_repr():
    s = Settings.from_env({"OPENROUTER_API_KEY": "shared-secret-value"})

    assert s.models.jev_api_key == "shared-secret-value"
    assert s.models.text_api_key == "shared-secret-value"
    assert "secret" not in repr(s)
    assert "secret" not in str(s)


@pytest.mark.parametrize(
    ("variable", "url"),
    [
        ("BROWSER_HANDS_JEV_URL", "https://jev.example.test/v1/systemone"),
        ("BROWSER_HANDS_TEXT_BASE_URL", "https://openrouter.ai.evil.test/api/v1"),
        ("BROWSER_HANDS_TEXT_BASE_URL", "https://evil.test/openrouter.ai/api/v1"),
    ],
)
def test_openrouter_key_is_not_sent_to_other_hosts(variable, url):
    s = Settings.from_env({"OPENROUTER_API_KEY": "shared-secret-value", variable: url})

    key_name = "BROWSER_HANDS_JEV_API_KEY" if "JEV" in variable else "BROWSER_HANDS_TEXT_API_KEY"
    own, other = (
        (s.models.jev_api_key, s.models.text_api_key)
        if "JEV" in variable
        else (s.models.text_api_key, s.models.jev_api_key)
    )
    assert own == ""  # общий ключ на чужой хост не подставлен
    assert other == "shared-secret-value"  # у второй модели адрес openrouter.ai
    with pytest.raises(ConfigError) as info:
        s.validate()
    message = str(info.value)
    assert key_name in message and variable in message
    assert "только на openrouter.ai" in message
    assert "shared-secret-value" not in message


def test_own_key_for_other_host_passes_validate():
    env = {
        "OPENROUTER_API_KEY": "shared",
        "BROWSER_HANDS_JEV_URL": "https://jev.example.test/v1/systemone",
        "BROWSER_HANDS_JEV_API_KEY": "jev-own",
        "BROWSER_HANDS_TEXT_BASE_URL": "https://OpenRouter.AI/api/v1",  # хост без учёта регистра
    }
    s = Settings.from_env(env)

    assert (s.models.jev_api_key, s.models.text_api_key) == ("jev-own", "shared")
    s.validate()


def test_specific_keys_win_over_openrouter():
    env = {
        "OPENROUTER_API_KEY": "shared",
        "BROWSER_HANDS_JEV_API_KEY": "jev-only",
        "BROWSER_HANDS_TEXT_API_KEY": "text-only",
    }
    s = Settings.from_env(env)

    assert (s.models.jev_api_key, s.models.text_api_key) == ("jev-only", "text-only")


def test_validate_missing_key_is_clear_and_prints_no_values():
    with pytest.raises(ConfigError, match="нет ключа: задайте OPENROUTER_API_KEY"):
        Settings.from_env({}).validate()

    with pytest.raises(ConfigError) as info:
        Settings.from_env({"BROWSER_HANDS_JEV_API_KEY": "jev-secret"}).validate()
    assert "BROWSER_HANDS_TEXT_API_KEY" in str(info.value)
    assert "jev-secret" not in str(info.value)

    Settings.from_env({"OPENROUTER_API_KEY": "k"}).validate()


def test_validate_launch_needs_chrome_binary(tmp_path):
    env = {"OPENROUTER_API_KEY": "k", "BROWSER_HANDS_MODE": "launch"}

    with pytest.raises(ConfigError, match="BROWSER_HANDS_CHROME_BINARY"):
        Settings.from_env({**env, "BROWSER_HANDS_CHROME_BINARY": str(tmp_path / "nope")}).validate()

    binary = tmp_path / "chrome"
    binary.write_text("")
    Settings.from_env({**env, "BROWSER_HANDS_CHROME_BINARY": str(binary)}).validate()


def test_apply_overrides_beats_env_and_keeps_original():
    base = Settings.from_env({"BROWSER_HANDS_MODE": "attach", "BROWSER_HANDS_MAX_STEPS": "9"})

    s = apply_overrides(base, mode="launch", headless=True, data_dir="~/p", max_steps=3, timeout_s=12, keep_open=True)

    assert s.browser.mode == "launch"
    assert s.browser.headless is True
    assert s.browser.launch_data_dir == Path.home() / "p"
    assert s.browser.chrome_data_dir == base.browser.chrome_data_dir
    assert (s.run.max_steps, s.run.timeout_s, s.run.keep_open) == (3, 12, True)
    assert base.browser.mode == "attach" and base.run.max_steps == 9  # исходные не изменились


def test_apply_overrides_none_changes_nothing_and_data_dir_follows_mode():
    base = Settings()
    assert apply_overrides(base) == base

    s = apply_overrides(base, data_dir="/tmp/devtools", ws_url="ws://127.0.0.1:1/devtools/browser/x")
    assert s.browser.chrome_data_dir == Path("/tmp/devtools")
    assert s.browser.ws_url == "ws://127.0.0.1:1/devtools/browser/x"

    with pytest.raises(ConfigError, match="--max-steps"):
        apply_overrides(base, max_steps=0)
    with pytest.raises(ConfigError, match="--ws"):
        apply_overrides(base, ws_url="127.0.0.1:9222")


def test_run_limits_have_ceilings():
    base = Settings()
    edge = apply_overrides(base, max_steps=50, timeout_s=300)
    assert (edge.run.max_steps, edge.run.timeout_s) == (50, 300)
    env = {"BROWSER_HANDS_MAX_STEPS": "50", "BROWSER_HANDS_TIMEOUT_S": "300"}
    assert (Settings.from_env(env).run.max_steps, Settings.from_env(env).run.timeout_s) == (50, 300.0)

    for kwargs, flag in (
        ({"max_steps": 51}, "--max-steps"),
        ({"timeout_s": 301}, "--timeout"),
        ({"timeout_s": float("nan")}, "--timeout"),
        ({"timeout_s": float("inf")}, "--timeout"),
    ):
        with pytest.raises(ConfigError, match=flag):
            apply_overrides(base, **kwargs)


@pytest.mark.parametrize(("field", "value"), [("max_steps", 51), ("max_steps", 0), ("timeout_s", 301.0)])
def test_validate_checks_run_ceilings(field, value):
    s = Settings.from_env({"OPENROUTER_API_KEY": "k"})
    setattr(s.run, field, value)  # в обход from_env/apply_overrides — например, при встраивании

    with pytest.raises(ConfigError, match=field):
        s.validate()
