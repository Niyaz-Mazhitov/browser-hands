"""Командная строка: `browser-hands run` — один прогон в терминале, `browser-hands serve` — MCP-сервер по stdio.

Флаги перекрывают переменные `BROWSER_HANDS_*`. Ядро импортируется лениво (через server.BrowseService).
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
from browser_hands.server import BrowseService, build_server, format_result
from browser_hands.types import RunResult

EXIT_DONE = 0
EXIT_NOT_DONE = 2  # и ошибки конфигурации/аргументов, как у argparse

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
    run.add_argument("--goal", required=True, help="что сделать и когда остановиться")
    run.add_argument("--max-steps", type=int, metavar="N")
    run.add_argument("--timeout", type=float, metavar="S", help="общий дедлайн прогона, секунды")
    run.add_argument("--keep-open", action="store_true", default=None, help="не закрывать вкладку (attach)")
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
    args = build_parser().parse_args(argv)
    try:
        settings = _settings(args, os.environ if env is None else env)
    except ConfigError as exc:
        print(f"browser-hands: {exc}", file=sys.stderr)
        return EXIT_NOT_DONE

    with _fresh_profile(settings, enabled=args.fresh_profile) as (settings, cleanup):
        service = BrowseService(settings, **(factories or {}))
        if args.command == "serve":
            return _serve(service, cleanup)
        return _run(service, args)


def _settings(args: argparse.Namespace, env: Mapping[str, str]) -> Settings:
    run_flags: dict[str, Any] = {}
    if args.command == "run":
        run_flags = {"max_steps": args.max_steps, "timeout_s": args.timeout, "keep_open": args.keep_open}
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


def _run(service: BrowseService, args: argparse.Namespace) -> int:
    try:
        with _signal_exit(signal.SIGTERM):  # kill → finally: launch-Chrome не остаётся сиротой
            result = service.browse(args.url, args.goal)  # лимиты и keep_open — уже в settings.run
    finally:
        service.close()

    screenshot = _save_screenshot(result, args.screenshot)
    if args.json:
        print(json.dumps(result_to_json(result, screenshot), ensure_ascii=False, indent=2))
    else:
        print(format_result(result, verbose=True))
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


def result_to_json(result: RunResult, screenshot: Path | None = None) -> dict[str, Any]:
    """RunResult → dict для `--json`: байты скриншота заменены размером и путём файла."""
    data = asdict(result)
    shot = data.pop("screenshot_jpeg")
    data["screenshot_bytes"] = len(shot) if shot else 0
    data["screenshot_path"] = str(screenshot) if screenshot is not None else None
    return data


if __name__ == "__main__":
    sys.exit(main())
