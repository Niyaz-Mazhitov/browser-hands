from pathlib import Path

from browser_hands.config import Settings
from browser_hands.types import Timing


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
    assert s.models.request_timeout_s == 25.0

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
