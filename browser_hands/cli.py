"""Командная строка: `browser-hands run` — один прогон в терминале, `browser-hands serve` — MCP-сервер по stdio.

Флаги перекрывают переменные `BROWSER_HANDS_*`. Ядро импортируется лениво (через server.BrowseService).
`run` берёт цель (`--goal`), сценарий (`--steps-file` и/или `--step`) или оба.
"""

import argparse
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from types import FrameType
from typing import Any

from browser_hands.config import ConfigError, Settings, apply_overrides
from browser_hands.logging import get_logger
from browser_hands.scenario import ScenarioError, ScenarioStep, parse_steps
from browser_hands.server import BrowseService, build_server, format_result
from browser_hands.types import RunResult

EXIT_DONE = 0
EXIT_NOT_DONE = 2  # не done (и unconfirmed тоже), ошибки конфигурации и аргументов (как у argparse)

log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    browser = argparse.ArgumentParser(add_help=False)
    group = browser.add_argument_group("браузер (перекрывают BROWSER_HANDS_*)")
    group.add_argument("--mode", choices=["attach", "launch"], help="attach — ваш Chrome, launch — свой процесс")
    group.add_argument("--headless", action="store_true", default=None, help="launch без окна")
    group.add_argument("--ws", metavar="URL", help="готовый ws://…/devtools/browser/<id>; важнее --mode")
    group.add_argument("--data-dir", metavar="P", help="launch: профиль; attach: каталог с DevToolsActivePort")
    group.add_argument("--fresh-profile", action="store_true", help="launch с временным профилем (удаляется по выходу)")

    parser = argparse.ArgumentParser(prog="browser-hands", description="Быстрый браузерный агент: Chrome по CDP + Jev.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="{run,serve}")

    run = commands.add_parser("run", parents=[browser], help="один прогон: шаги и замеры в stdout")
    run.add_argument("--url", required=True, help="стартовая страница")
    run.add_argument("--goal", default="", help="что сделать и когда остановиться; необязателен при сценарии")
    run.add_argument(
        "--steps-file",
        metavar="P",
        type=Path,
        help='сценарий: JSON-список [{"do": "…", "text": "…"}]; do — по-английски, text печатается дословно',
    )
    run.add_argument(
        "--step", metavar="DO", action="append", default=[], help="шаг сценария без текста; повторяемый, после файла"
    )
    run.add_argument("--max-steps", type=int, metavar="N")
    run.add_argument("--timeout", type=float, metavar="S", help="общий дедлайн прогона, секунды")
    run.add_argument("--keep-open", action="store_true", default=None, help="не закрывать свою вкладку (attach)")
    run.add_argument(
        "--new-tab", action="store_true", default=None, help="attach: своя вкладка, даже если сайт уже открыт у вас"
    )
    run.add_argument("--json", action="store_true", help="RunResult как JSON (без байтов скриншота)")
    run.add_argument("--screenshot", metavar="out.jpg", type=Path, help="сохранить финальный скриншот")

    commands.add_parser("serve", parents=[browser], help="MCP-сервер по stdio (для Claude Code)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    factories: Mapping[str, Any] | None = None,
) -> int:
    """Точка входа `browser-hands`. `env` и `factories` (фабрики ядра для BrowseService) — для тестов."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run" and not args.goal.strip() and args.steps_file is None and not args.step:
        parser.error("run: нужен --goal или сценарий (--steps-file, --step)")
    try:
        settings = _settings(args, os.environ if env is None else env)
        steps = _scenario(args) if args.command == "run" else None
    except (ConfigError, ScenarioError) as exc:
        print(f"browser-hands: {exc}", file=sys.stderr)
        return EXIT_NOT_DONE

    with _fresh_profile(settings, enabled=args.fresh_profile) as (settings, cleanup):
        service = BrowseService(settings, **(factories or {}))
        if args.command == "serve":
            return _serve(service, cleanup)
        return _run(service, args, steps)


def _scenario(args: argparse.Namespace) -> list[ScenarioStep] | None:
    """Шаги из `--steps-file` (JSON-список `{do, text?}`), затем из `--step`; сценария нет — None."""
    if args.steps_file is None and not args.step:
        return None
    raw: list[object] = []
    if args.steps_file is not None:
        path: Path = args.steps_file
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ScenarioError(f"--steps-file {path}: {exc.strerror or exc}") from None
        except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError
            raise ScenarioError(f"--steps-file {path}: не JSON ({exc})") from None
        if not isinstance(loaded, list):
            raise ScenarioError(f"--steps-file {path}: нужен JSON-список шагов, а не {type(loaded).__name__}")
        raw.extend(loaded)
    raw.extend({"do": do} for do in args.step)
    return parse_steps(raw)


def _settings(args: argparse.Namespace, env: Mapping[str, str]) -> Settings:
    run_flags: dict[str, Any] = {}
    if args.command == "run":
        run_flags = {
            "max_steps": args.max_steps,
            "timeout_s": args.timeout,
            "keep_open": args.keep_open,
            "new_tab": args.new_tab,
        }
    if args.fresh_profile and (args.mode == "attach" or args.ws):
        raise ConfigError("--fresh-profile — только для launch (без --mode attach и --ws)")
    settings = apply_overrides(
        Settings.from_env(env),
        mode="launch" if args.fresh_profile else args.mode,  # временный профиль бывает только у launch
        ws_url=args.ws,
        data_dir=args.data_dir,
        headless=args.headless,
        **run_flags,
    )
    if args.fresh_profile and settings.browser.ws_url:
        raise ConfigError("--fresh-profile не сочетается с BROWSER_HANDS_WS_URL")
    return settings


@contextlib.contextmanager
def _fresh_profile(settings: Settings, *, enabled: bool) -> Iterator[tuple[Settings, Callable[[], None]]]:
    """Временный профиль Chrome для launch; удаляется по выходу (после закрытия Chrome)."""
    if not enabled:
        yield settings, lambda: None
        return
    profile = tempfile.mkdtemp(prefix="browser-hands-profile-")

    def cleanup() -> None:
        shutil.rmtree(profile, ignore_errors=True)

    try:
        yield apply_overrides(settings, data_dir=profile), cleanup
    finally:
        cleanup()


def _run(service: BrowseService, args: argparse.Namespace, steps: list[ScenarioStep] | None = None) -> int:
    try:
        with _signal_exit(signal.SIGTERM):  # kill → finally: launch-Chrome не остаётся сиротой
            # лимиты, keep_open и new_tab — уже в settings.run
            result = service.browse(args.url, args.goal, steps=steps)
    finally:
        service.close()

    screenshot = _save_screenshot(result, args.screenshot)
    if args.json:
        print(json.dumps(result_to_json(result, screenshot, scenario=steps), ensure_ascii=False, indent=2))
    else:
        print(format_result(result, verbose=True, steps=steps))
        if screenshot is not None:
            print(f"screenshot saved: {screenshot}")
    return EXIT_DONE if result.status == "done" else EXIT_NOT_DONE


def _serve(service: BrowseService, cleanup: Callable[[], None]) -> int:
    settings = service.settings
    browser = settings.browser
    log.info("serve: stdio, режим %s", "ws" if browser.ws_url else browser.mode)
    try:
        settings.validate()
    except ConfigError as exc:
        log.warning("%s — browse будет отвечать ошибкой", exc)
    server = build_server(service)
    stop = _stop_handler(service, cleanup)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        server.run(transport="stdio")  # конец stdin (клиент закрылся) → штатный выход
    finally:
        service.close()
    return EXIT_DONE


def _stop_handler(
    service: BrowseService, cleanup: Callable[[], None], exit_: Callable[[int], object] = os._exit
) -> Callable[[int, FrameType | None], None]:
    """Обработчик SIGTERM/SIGINT/SIGHUP для serve: отменить прогон и закрыть Chrome, не дожидаясь лока, и выйти.

    stdin читается в потоке anyio — штатный выход завис бы на нём, поэтому `os._exit`. Повторный сигнал, пока идёт
    закрытие, только пишет в лог: Python вызвал бы обработчик вложенно, и `os._exit` случился бы до остановки Chrome.
    """
    stopping = False

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        name = signal.Signals(signum).name
        if stopping:
            log.info("serve: сигнал %s — уже закрываю Chrome", name)
            return
        stopping = True
        log.info("serve: сигнал %s — отменяю прогон, закрываю Chrome и выхожу", name)
        try:
            service.abort()  # событие отмены + chrome.close() сразу, без лока сервиса
            cleanup()
        finally:
            exit_(0)

    return stop


@contextlib.contextmanager
def _signal_exit(sig: signal.Signals) -> Iterator[None]:
    def raise_exit(signum: int, _frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    previous = signal.signal(sig, raise_exit)
    try:
        yield
    finally:
        signal.signal(sig, previous)


def _save_screenshot(result: RunResult, path: Path | None) -> Path | None:
    if path is None:
        return None
    if not result.screenshot_jpeg:
        print("browser-hands: скриншота нет — файл не записан", file=sys.stderr)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.screenshot_jpeg)
    return path


def result_to_json(
    result: RunResult, screenshot: Path | None = None, *, scenario: Sequence[ScenarioStep] | None = None
) -> dict[str, Any]:
    """RunResult → dict для `--json`: байты скриншота заменены размером и путём файла.

    `scenario` — входной сценарий (ключ `scenario`: `[{do, text}]`); в режиме цели ключа нет.
    """
    data = asdict(result)
    shot = data.pop("screenshot_jpeg")
    data["screenshot_bytes"] = len(shot) if shot else 0
    data["screenshot_path"] = str(screenshot) if screenshot is not None else None
    if scenario is not None:
        data["scenario"] = [asdict(step) for step in scenario]
    return data


if __name__ == "__main__":
    sys.exit(main())
