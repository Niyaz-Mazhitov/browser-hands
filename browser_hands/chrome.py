"""Подключение к Chrome: attach (DevToolsActivePort профиля пользователя), launch (свой процесс), ws (готовый url).

Одно постоянное соединение на процесс; `connect()` идемпотентен и переподключается, перечитывая DevToolsActivePort
(порт меняется после перезапуска Chrome). Без тихого перехода attach → launch. В attach браузер пользователя не
трогаем: `close()` закрывает только свои вкладки (targetId из своего `createTarget`) и websocket.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from websockets.exceptions import InvalidHandshake, InvalidURI

from .browser import Tab
from .cdp import CDPClient, CDPError, CDPException, CDPTimeout, ChromeDisconnected
from .config import BrowserConfig
from .types import ChromeLike

log = logging.getLogger(__name__)

PORT_FILE = "DevToolsActivePort"
HINT = "включите chrome://inspect/#remote-debugging или BROWSER_HANDS_MODE=launch"
ALIVE_TIMEOUT_S = 2.0
CLOSE_TAB_TIMEOUT_S = 1.0  # на одну свою вкладку при закрытии и уборке сирот
TERMINATE_TIMEOUT_S = 5.0
SECRET_ENV = "OPENROUTER_API_KEY"

ClientFactory = Callable[..., CDPClient]
Launcher = Callable[[BrowserConfig], subprocess.Popen]


class ChromeUnavailable(RuntimeError):
    """К Chrome не подключиться: не запущен, отладка выключена, «Разрешить» не нажали или файл порта устарел."""


def read_devtools_active_port(data_dir: Path) -> str:
    """Две строки файла `<data_dir>/DevToolsActivePort` (порт и путь) → `ws://127.0.0.1:<port><path>`."""
    path = Path(data_dir).expanduser() / PORT_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ChromeUnavailable(f"Нет {path}: {HINT}") from None
    port = lines[0].strip() if lines else ""
    ws_path = lines[1].strip() if len(lines) > 1 else ""
    if not port.isdigit() or not 0 < int(port) < 65536 or not ws_path.startswith("/devtools/browser/"):
        raise ChromeUnavailable(f"{path} пуст или повреждён: {HINT}")
    return f"ws://127.0.0.1:{port}{ws_path}"


def resolve_ws_url(config: BrowserConfig) -> str:
    """`ws_url` из конфига, иначе DevToolsActivePort профиля (attach) или своего каталога (launch)."""
    if config.ws_url:
        return config.ws_url
    data_dir = config.chrome_data_dir if config.mode == "attach" else config.launch_data_dir
    return read_devtools_active_port(data_dir)


def chrome_args(config: BrowserConfig) -> list[str]:
    args = [
        str(config.chrome_binary),
        f"--user-data-dir={config.launch_data_dir}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
    ]
    if config.headless:
        args.append("--headless=new")
    args.append("about:blank")
    return args


def profile_owner(data_dir: Path) -> int | None:
    """PID живого Chrome, держащего профиль (симлинк `SingletonLock` → `<host>-<pid>`), иначе None."""
    try:
        target = os.readlink(Path(data_dir) / "SingletonLock")
    except OSError:
        return None
    pid_text = target.rsplit("-", 1)[-1]
    if not pid_text.isdigit():
        return None
    pid = int(pid_text)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None  # замок от упавшего Chrome: Chrome сам его перехватит
    except PermissionError:
        return pid
    return pid


def chrome_env() -> dict[str, str]:
    """Копия окружения без `*_API_KEY` и `OPENROUTER_API_KEY`: Chrome и его помощникам ключи не нужны."""
    return {k: v for k, v in os.environ.items() if not k.upper().endswith("_API_KEY") and k.upper() != SECRET_ENV}


def launch_chrome(config: BrowserConfig) -> subprocess.Popen:
    """Запустить свой Chrome и дождаться DevToolsActivePort в `launch_data_dir` (не дольше `connect_timeout_s`)."""
    data_dir = Path(config.launch_data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    owner = profile_owner(data_dir)
    if owner is not None:
        # Иначе новый Chrome отдаст управление старому и выйдет, а мы стёрли бы его DevToolsActivePort.
        raise ChromeUnavailable(
            f"Профиль {data_dir} занят Chrome (pid {owner}), например оставшимся от прошлого сервера: "
            f"закройте его (kill {owner}) или используйте другой профиль (--fresh-profile)"
        )
    (data_dir / PORT_FILE).unlink(missing_ok=True)  # иначе прочитаем порт прошлого запуска
    try:
        # stdout занят MCP stdio: вывод Chrome — в DEVNULL.
        proc = subprocess.Popen(
            chrome_args(config),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=chrome_env(),
        )
    except OSError as exc:
        raise ChromeUnavailable(f"Не удалось запустить {config.chrome_binary}: {exc}") from None
    deadline = time.monotonic() + config.connect_timeout_s
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise ChromeUnavailable(
                f"Chrome завершился при запуске (код {code}); профиль {data_dir} может быть занят другим Chrome"
            )
        try:
            read_devtools_active_port(data_dir)
            return proc
        except ChromeUnavailable:
            time.sleep(0.05)
    _stop_process(proc)
    raise ChromeUnavailable(f"Chrome не открыл порт отладки за {config.connect_timeout_s:.0f} с")


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=TERMINATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=TERMINATE_TIMEOUT_S)


class Chrome(ChromeLike):
    """Постоянное соединение с Chrome (ChromeLike) и фабрика своих фоновых вкладок."""

    def __init__(
        self,
        config: BrowserConfig,
        *,
        client_factory: ClientFactory = CDPClient,
        launcher: Launcher = launch_chrome,
    ) -> None:
        self.config = config
        self.owned_targets: set[str] = set()  # только id из своего createTarget, ещё не закрытые
        self._tabs: dict[str, Tab] = {}  # target → вкладка; закрытая (tab.closed), но не забытая — сирота
        self._client: CDPClient | None = None
        self._proc: subprocess.Popen | None = None
        self._client_factory = client_factory
        self._launcher = launcher

    @property
    def launched(self) -> bool:
        """Chrome запущен нами (launch без ws_url)."""
        return self.config.mode == "launch" and not self.config.ws_url

    @property
    def client(self) -> CDPClient:
        if self._client is None:
            raise ChromeDisconnected("Chrome не подключён: вызовите connect()")
        return self._client

    def alive(self) -> bool:
        """ws открыт и `Target.getTargets` отвечает за 2 с."""
        if self._client is None or not self._client.connected:
            return False
        try:
            self._client.call("Target.getTargets", timeout=ALIVE_TIMEOUT_S)
        except CDPException:
            return False
        return True

    def connect(self) -> None:
        """Идемпотентно: живое соединение не трогает (только закрывает свои вкладки-сироты); иначе перечитывает порт и
        подключается заново."""
        if self.alive():
            self._close_orphans()
            return
        if self._client is not None:
            self._client.close()
            self._client = None
        if self.launched and (self._proc is None or self._proc.poll() is not None):
            log.info("Запускаю Chrome (профиль %s)", self.config.launch_data_dir)
            self._proc = self._launcher(self.config)
            self.owned_targets.clear()  # вкладки прошлого процесса умерли вместе с ним
            self._tabs.clear()
        url = resolve_ws_url(self.config)
        if self.config.mode == "attach" and not self.config.ws_url:
            log.warning("Подключаюсь к Chrome пользователя: если появится окно «Разрешить» — нажмите его")
        self._client = self._open(url)
        self._close_orphans(everything=True)  # сессии прошлого соединения мертвы: все свои вкладки оттуда — сироты

    def _open(self, url: str) -> CDPClient:
        timeout = self.config.connect_timeout_s
        try:
            return self._client_factory(url, call_timeout=self.config.call_timeout_s, connect_timeout=timeout)
        except ConnectionRefusedError:
            raise ChromeUnavailable(f"Chrome не запущен или отладка выключена ({url}): {HINT}") from None
        except TimeoutError:
            raise ChromeUnavailable(
                f"Chrome не ответил за {timeout:.0f} с: нажмите «Разрешить» в окне Chrome и повторите"
            ) from None
        except (InvalidHandshake, InvalidURI) as exc:
            raise ChromeUnavailable(
                f"Chrome отклонил подключение ({exc}); DevToolsActivePort устарел? {HINT}"
            ) from None
        except OSError as exc:
            raise ChromeUnavailable(f"Не подключиться к Chrome ({url}): {exc}") from None

    def _close_orphans(self, *, everything: bool = False) -> None:
        """Закрыть свои вкладки, которые агент уже закрыл (или не создал), а Chrome не подтвердил; вкладку, с которой
        идёт работа, не трогаем. `everything` — после переподключения: сироты все свои вкладки прошлого соединения."""
        orphans = [t for t in self.owned_targets if everything or (tab := self._tabs.get(t)) is None or tab.closed]
        self._close_targets(self.client, sorted(orphans))

    def _forget(self, target_id: str) -> None:
        self.owned_targets.discard(target_id)
        self._tabs.pop(target_id, None)

    def _close_targets(self, client: CDPClient, target_ids: list[str]) -> None:
        """Закрыть свои вкладки по targetId (≤1 с на вкладку); ошибки — в лог. Чужие id сюда не попадают."""
        for target_id in target_ids:
            try:
                client.call("Target.closeTarget", {"targetId": target_id}, timeout=CLOSE_TAB_TIMEOUT_S)
            except CDPError as exc:
                log.debug("closeTarget %s: %s", target_id, exc)  # уже закрыта
            except CDPTimeout as exc:
                log.warning("Своя вкладка %s не закрыта: %s", target_id, exc)
                continue  # остаётся в owned_targets: следующий connect() попробует снова
            except CDPException as exc:
                log.warning("Своя вкладка %s не закрыта: %s", target_id, exc)
                return  # соединение закрыто
            self._forget(target_id)

    def new_tab(self, *, screenshot_quality: int | None = None, screenshot_scale: float | None = None) -> Tab:
        """Фоновая вкладка about:blank (`background=True`), flatten-сессия, эмуляция viewport и фокуса."""
        client = self.client
        target_id = client.call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
        self.owned_targets.add(target_id)
        try:
            session_id = client.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
            tab = Tab(
                client,
                session_id,
                target_id,
                screenshot_quality=self.config.screenshot_quality if screenshot_quality is None else screenshot_quality,
                screenshot_scale=self.config.screenshot_scale if screenshot_scale is None else screenshot_scale,
                viewport=self.config.viewport,
                on_release=self._forget,
            )
            self._tabs[target_id] = tab
            tab.setup()
        except BaseException:
            self._tabs.pop(target_id, None)  # не удалось закрыть — останется сиротой в owned_targets
            try:
                client.call("Target.closeTarget", {"targetId": target_id}, timeout=CLOSE_TAB_TIMEOUT_S)
                self._forget(target_id)
            except CDPException:
                pass
            raise
        return tab

    def close(self) -> None:
        """launch: закрыть ws (разблокирует recv рабочего потока) и сразу погасить процесс — вкладки умрут с ним;
        attach/ws: закрыть свои вкладки (≤1 с на вкладку), затем websocket. Вкладки пользователя не трогаем."""
        client, self._client = self._client, None
        if client is not None:
            if not self.launched:
                self._close_targets(client, sorted(self.owned_targets))
            client.close()
        if self.launched:
            self.owned_targets.clear()
            self._tabs.clear()
        if self._proc is not None:
            _stop_process(self._proc)
            self._proc = None

    def __enter__(self) -> Chrome:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
