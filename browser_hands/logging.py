"""Логи browser-hands — только в stderr: stdout занят протоколом MCP (stdio)."""

import logging
import os
import sys

LOGGER_NAME = "browser_hands"
LEVEL_ENV = "BROWSER_HANDS_LOG"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# HTTP-стек моделей: на DEBUG hpack/h2 пишут заголовки запроса, включая `authorization: Bearer <ключ>`
QUIET_LOGGERS = ("hpack", "h2", "httpcore", "httpx")


class _StderrHandler(logging.StreamHandler):
    """Обработчик пакета; свой класс — чтобы не добавить его дважды."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setFormatter(logging.Formatter(FORMAT))


def parse_level(value: str | None) -> int:
    """`BROWSER_HANDS_LOG` → уровень logging; пусто → INFO; иное → ValueError с именем переменной."""
    name = (value or "").strip().upper() or "INFO"
    if name not in LEVELS:
        raise ValueError(f"{LEVEL_ENV}: ожидается один из {', '.join(LEVELS)}, получено {value!r}")
    return logging.getLevelNamesMapping()[name]


def quiet_http_loggers() -> None:
    """HTTP-логгерам — не ниже WARNING при любом `BROWSER_HANDS_LOG` (FastMCP на DEBUG ставит DEBUG корню)."""
    for name in QUIET_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(max(logger.level, logging.WARNING))  # NOTSET (0) → WARNING


def get_logger(name: str | None = None) -> logging.Logger:
    """Логгер `browser_hands[.name]` с одним обработчиком stderr; уровень — `BROWSER_HANDS_LOG` (по умолчанию INFO)."""
    package = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(handler, _StderrHandler) for handler in package.handlers):
        package.addHandler(_StderrHandler())
        package.propagate = False  # корневой логгер настраивает FastMCP — без дублей
        try:
            package.setLevel(parse_level(os.environ.get(LEVEL_ENV)))
        except ValueError:
            package.setLevel(logging.INFO)  # неверное значение сообщит Settings.from_env
        quiet_http_loggers()
    return package.getChild(name) if name else package
