"""Настройки (контракт, docs/plan.md §4): датаклассы и значения по умолчанию; разбор env и флагов — Пакет 2.

Пороги модели и предохранитель ожидания — `Thresholds` (docs/plan-waits.md §4.1), без env."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from browser_hands.logging import LEVEL_ENV, parse_level

ENV_PREFIX = "BROWSER_HANDS_"
FALLBACK_KEY_ENV = "OPENROUTER_API_KEY"
FALLBACK_KEY_HOST = "openrouter.ai"  # общий ключ уходит только сюда
MAX_STEPS_LIMIT = 50  # потолок шагов одного прогона (env, флаги, MCP)
TIMEOUT_LIMIT_S = 300.0  # потолок дедлайна одного прогона, секунды

Mode = Literal["attach", "launch"]
Reasoning = Literal["none", "low"]
_MODES: dict[str, Mode] = {"attach": "attach", "launch": "launch"}
_REASONING: dict[str, Reasoning] = {"none": "none", "low": "low"}

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class ConfigError(ValueError):
    """Ошибка настройки: имя переменной и что ожидается. Значения ключей в текст не попадают."""


@dataclass(slots=True)
class BrowserConfig:
    mode: Literal["attach", "launch"] = "attach"
    ws_url: str | None = None  # BROWSER_HANDS_WS_URL — приоритет над mode
    chrome_data_dir: Path = Path("~/Library/Application Support/Google/Chrome").expanduser()  # attach
    launch_data_dir: Path = Path("~/.cache/browser-hands/chrome-profile").expanduser()  # launch
    chrome_binary: Path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    headless: bool = False
    viewport: tuple[int, int] = (1120, 780)
    connect_timeout_s: float = 60.0  # ждём «Разрешить» в attach
    call_timeout_s: float = 30.0  # один CDP-вызов
    screenshot_quality: int = 60
    screenshot_scale: float = 1.0


@dataclass(slots=True)
class ModelConfig:
    jev_url: str = "https://openrouter.ai/api/v1/systemone"
    jev_model: str = "jev-latest"
    jev_api_key: str = field(default="", repr=False)  # BROWSER_HANDS_JEV_API_KEY; OPENROUTER_API_KEY — см. from_env
    text_base_url: str = "https://openrouter.ai/api/v1"
    text_model: str = "inception/mercury-2.5"
    text_api_key: str = field(default="", repr=False)  # BROWSER_HANDS_TEXT_API_KEY; OPENROUTER_API_KEY — см. from_env
    text_reasoning: Literal["none", "low"] = "none"
    # Потолок всего HTTP-запроса по часам (соединение, ожидание, тело, повторы 429/503), не дольше остатка дедлайна
    # прогона. Обычно Jev — 0,5–1 с, текст — 1–1,5 с; 25.09 провайдер держал соединение ~120 с и отдал 200 с ошибкой.
    jev_timeout_s: float = 10.0  # BROWSER_HANDS_JEV_TIMEOUT_S
    text_timeout_s: float = 8.0  # BROWSER_HANDS_TEXT_TIMEOUT_S; вышел — агент повторяет запрос один раз


@dataclass(slots=True)
class RunConfig:
    max_steps: int = 25
    timeout_s: float = 90.0
    keep_open: bool = False
    new_tab: bool = False  # attach: всегда своя вкладка, даже если сайт открыт у пользователя


# Кадр страницы при 60 Гц — запас к p99 «действие → последнее изменение страницы» в расчёте `wait_fuse_s`
# (scripts/calibrate.py, scripts/sweep_report.py): изменение из отчёта страницы ложится в DOM к следующему кадру.
FRAME_S = 1 / 60


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Пороги модели и предохранитель одного ожидания (docs/plan-waits.md §4.1). Меняет их расчёт
    (`scripts/calibrate.py` → docs/calibration.md), не оператор: env `BROWSER_HANDS_*` для них нет (§12)."""

    step_done_min_p: float = 0.75  # значение — docs/calibration.md §1; P(yes) `step_done`, с которой шаг выполнен
    done_step_done_min_p: float = 0.45  # значение — docs/calibration.md §1; DONE закрывает шаг при P(yes) не ниже
    min_action_confidence: float = 0.3  # значение — docs/calibration.md §2; CLICK/TYPE_TEXT/SELECT ниже — не исполнять
    done_min_confidence: float = 0.5  # значение — docs/calibration.md §3; DONE цели ниже — второй взгляд (заглушка)
    wait_fuse_s: float = 1.5  # значение — docs/calibration.md §4; потолок одного ожидания (пока = потолок успокоения)


@dataclass(slots=True)
class Settings:
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    run: RunConfig = field(default_factory=RunConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """Настройки из `BROWSER_HANDS_*` (пустое значение = по умолчанию).

        Ключ модели — свой (`BROWSER_HANDS_JEV_API_KEY`/`TEXT_API_KEY`), иначе `OPENROUTER_API_KEY`, но только если
        адрес этой модели — openrouter.ai. Неверное значение → ConfigError с именем переменной. Наличие ключей
        проверяет `validate()`.
        """
        b, m, r = BrowserConfig(), ModelConfig(), RunConfig()
        _level(env)
        fallback_key = (env.get(FALLBACK_KEY_ENV) or "").strip()
        browser = BrowserConfig(
            mode=_choice(_get(env, "MODE"), _MODES, ENV_PREFIX + "MODE") or b.mode,
            ws_url=_ws_url(_get(env, "WS_URL"), ENV_PREFIX + "WS_URL"),
            chrome_data_dir=_path(env, "CHROME_DATA_DIR", b.chrome_data_dir),
            launch_data_dir=_path(env, "LAUNCH_DATA_DIR", b.launch_data_dir),
            chrome_binary=_path(env, "CHROME_BINARY", b.chrome_binary),
            headless=_bool(env, "HEADLESS", b.headless),
            connect_timeout_s=_float(env, "CONNECT_TIMEOUT_S", b.connect_timeout_s),
            screenshot_quality=_int(env, "SCREENSHOT_QUALITY", b.screenshot_quality, 1, 100),
            screenshot_scale=_float(env, "SCREENSHOT_SCALE", b.screenshot_scale, maximum=1.0),
        )
        jev_url = _get(env, "JEV_URL") or m.jev_url
        text_base_url = _get(env, "TEXT_BASE_URL") or m.text_base_url
        models = ModelConfig(
            jev_url=jev_url,
            jev_model=_get(env, "JEV_MODEL") or m.jev_model,
            jev_api_key=_get(env, "JEV_API_KEY") or _fallback(fallback_key, jev_url),
            text_base_url=text_base_url,
            text_model=_get(env, "TEXT_MODEL") or m.text_model,
            text_api_key=_get(env, "TEXT_API_KEY") or _fallback(fallback_key, text_base_url),
            text_reasoning=_choice(_get(env, "TEXT_REASONING"), _REASONING, ENV_PREFIX + "TEXT_REASONING")
            or m.text_reasoning,
            jev_timeout_s=_float(env, "JEV_TIMEOUT_S", m.jev_timeout_s, maximum=TIMEOUT_LIMIT_S),
            text_timeout_s=_float(env, "TEXT_TIMEOUT_S", m.text_timeout_s, maximum=TIMEOUT_LIMIT_S),
        )
        run = RunConfig(
            max_steps=_int(env, "MAX_STEPS", r.max_steps, 1, MAX_STEPS_LIMIT),
            timeout_s=_float(env, "TIMEOUT_S", r.timeout_s, maximum=TIMEOUT_LIMIT_S),
        )
        return cls(browser=browser, models=models, run=run)

    def validate(self) -> None:
        """Проверка перед запуском: ключи есть, лимиты прогона в потолках; в launch — Chrome на месте.

        Значения ключей не печатаются.
        """
        models = self.models
        keys = (
            ("JEV_API_KEY", models.jev_api_key, "JEV_URL", models.jev_url),
            ("TEXT_API_KEY", models.text_api_key, "TEXT_BASE_URL", models.text_base_url),
        )
        # без ключа: адрес на openrouter.ai — хватит OPENROUTER_API_KEY; чужой хост — только свой ключ
        shared = [ENV_PREFIX + name for name, value, _, url in keys if not value and _is_fallback_host(url)]
        own = [
            f"{ENV_PREFIX}{name} ({ENV_PREFIX}{url_name} ведёт на {_host(url) or url})"
            for name, value, url_name, url in keys
            if not value and not _is_fallback_host(url)
        ]
        if shared or own:
            wanted = [*own, f"{FALLBACK_KEY_ENV} (или {' и '.join(shared)})"] if shared else own
            note = f"; {FALLBACK_KEY_ENV} отправляется только на {FALLBACK_KEY_HOST}" if own else ""
            raise ConfigError(f"нет ключа: задайте {'; '.join(wanted)}{note}")
        _check_run_limits(self.run.max_steps, self.run.timeout_s, steps_source="max_steps", timeout_source="timeout_s")
        browser = self.browser
        if browser.mode == "launch" and not browser.ws_url and not browser.chrome_binary.is_file():
            raise ConfigError(f"Chrome не найден: {browser.chrome_binary}; задайте {ENV_PREFIX}CHROME_BINARY")


def apply_overrides(
    settings: Settings,
    *,
    mode: str | None = None,
    ws_url: str | None = None,
    data_dir: str | Path | None = None,
    headless: bool | None = None,
    max_steps: int | None = None,
    timeout_s: float | None = None,
    keep_open: bool | None = None,
    new_tab: bool | None = None,
) -> Settings:
    """Флаги CLI поверх env (None = не задан); возвращает новые Settings, исходные не меняет.

    `data_dir` — профиль Chrome для launch или каталог с DevToolsActivePort для attach (по итоговому режиму).
    """
    browser, run = settings.browser, settings.run
    parsed_mode = _choice(mode, _MODES, "--mode")
    if parsed_mode is not None:
        browser = replace(browser, mode=parsed_mode)
    if ws_url is not None:
        browser = replace(browser, ws_url=_ws_url(ws_url.strip() or None, "--ws"))
    if headless is not None:
        browser = replace(browser, headless=headless)
    if data_dir is not None:
        path = Path(data_dir).expanduser()
        if browser.mode == "launch":
            browser = replace(browser, launch_data_dir=path)
        else:
            browser = replace(browser, chrome_data_dir=path)
    if max_steps is not None:
        _check_run_limits(max_steps, None, steps_source="--max-steps", timeout_source="--timeout")
        run = replace(run, max_steps=max_steps)
    if timeout_s is not None:
        _check_run_limits(None, timeout_s, steps_source="--max-steps", timeout_source="--timeout")
        run = replace(run, timeout_s=timeout_s)
    if keep_open is not None:
        run = replace(run, keep_open=keep_open)
    if new_tab is not None:
        run = replace(run, new_tab=new_tab)
    return replace(settings, browser=browser, run=run)


def _check_run_limits(
    max_steps: int | None, timeout_s: float | None, *, steps_source: str, timeout_source: str
) -> None:
    """Потолки прогона: шагов 1…MAX_STEPS_LIMIT, дедлайн (0; TIMEOUT_LIMIT_S]; None — не проверять."""
    if max_steps is not None and not 1 <= max_steps <= MAX_STEPS_LIMIT:
        raise ConfigError(f"{steps_source}: ожидается целое число от 1 до {MAX_STEPS_LIMIT}, получено {max_steps}")
    if timeout_s is not None and not 0 < timeout_s <= TIMEOUT_LIMIT_S:  # NaN тоже не проходит
        raise ConfigError(f"{timeout_source}: ожидается число > 0 и ≤ {TIMEOUT_LIMIT_S:g}, получено {timeout_s}")


def _host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:  # кривой адрес (например, «http://[::1»)
        return None


def _is_fallback_host(url: str) -> bool:
    return _host(url) == FALLBACK_KEY_HOST


def _fallback(key: str, url: str) -> str:
    """`OPENROUTER_API_KEY` — только для адреса на openrouter.ai; для чужого хоста ключ не подставляется."""
    return key if _is_fallback_host(url) else ""


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(ENV_PREFIX + name)
    if value is None:
        return None
    return value.strip() or None


def _choice[T: str](value: str | None, choices: Mapping[str, T], source: str) -> T | None:
    """Значение из допустимых (без учёта регистра), суженное до Literal; None → None; иное → ConfigError."""
    if value is None:
        return None
    choice = choices.get(value.strip().lower())
    if choice is None:
        raise ConfigError(f"{source}: ожидается {' или '.join(choices)}, получено {value!r}")
    return choice


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _get(env, name)
    if value is None:
        return default
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    raise ConfigError(f"{ENV_PREFIX}{name}: ожидается 1/0, true/false, yes/no, получено {value!r}")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int | None) -> int:
    value = _get(env, name)
    if value is None:
        return default
    expected = f"целое число от {minimum} до {maximum}" if maximum is not None else f"целое число ≥ {minimum}"
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{ENV_PREFIX}{name}: ожидается {expected}, получено {value!r}") from None
    if number < minimum or (maximum is not None and number > maximum):
        raise ConfigError(f"{ENV_PREFIX}{name}: ожидается {expected}, получено {value!r}")
    return number


def _float(env: Mapping[str, str], name: str, default: float, *, maximum: float | None = None) -> float:
    value = _get(env, name)
    if value is None:
        return default
    expected = f"число > 0 и ≤ {maximum:g}" if maximum is not None else "число > 0"
    try:
        number = float(value)
    except ValueError:
        raise ConfigError(f"{ENV_PREFIX}{name}: ожидается {expected}, получено {value!r}") from None
    if not number > 0 or (maximum is not None and number > maximum) or number == float("inf"):
        raise ConfigError(f"{ENV_PREFIX}{name}: ожидается {expected}, получено {value!r}")
    return number


def _path(env: Mapping[str, str], name: str, default: Path) -> Path:
    value = _get(env, name)
    return Path(value).expanduser() if value is not None else default


def _ws_url(value: str | None, source: str) -> str | None:
    if value is not None and not value.startswith(("ws://", "wss://")):
        raise ConfigError(f"{source}: ожидается адрес ws://… или wss://…, получено {value!r}")
    return value


def _level(env: Mapping[str, str]) -> None:
    try:
        parse_level(env.get(LEVEL_ENV))
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
