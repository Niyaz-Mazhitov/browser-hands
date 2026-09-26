# browser-hands — план реализации

Дата: 2026-09-25. Источник: `browser-use/jev-ultrafast` (MIT), коммит `1231850a0bf1a0c0341fe408ef1668dbbfdfac46` от 2026-09-18,
клон в `<исходник jev-ultrafast>` (далее `<src>`).
Проект: `<путь к клону>` (далее `<proj>`): `git init -b main`, коммитов нет, есть `.gitignore` (не в индексе) и `.env` (600).

## 0. Что решает пользователь

1. **Где живёт контракт пакетов.** Рекомендация: `main` = один коммит с `.gitignore`; от него ветка `feat/initial` с «контрактным
   коммитом» (pyproject, uv.lock, `types.py`, `config.py`-датаклассы, пустой `__init__.py`); исполнители ветвятся от `feat/initial`
   (`feat/core`, `feat/shell`) и вливаются обратно в `feat/initial`. Альтернатива: контракт в `main` — нарушает «main только .gitignore».
2. **CDP-клиент: свой на `websockets`** (обоснование в §2). Playwright — только если нужны iframes/shadow DOM, чего в исходнике нет.
3. **Где ключ для сервера.** Рекомендация: `uv run --env-file <proj>/.env` в команде регистрации; без python-dotenv и без `-e KEY=` в
   `claude mcp add` (ключ попал бы в `~/.claude.json`).
4. **Скриншот в ответе:** JPEG 1120×780, quality 60 (~80–150 КБ, ~110–200 КБ base64 в контексте на каждый `browse`). Альтернатива —
   `BROWSER_HANDS_SCREENSHOT_SCALE=0.5` по умолчанию (в 3–4 раза меньше, читаемость хуже).
5. **Профиль launch:** постоянный `~/.cache/browser-hands/chrome-profile` (куки переживают перезапуск) или временный (`tempfile`, чисто).
   Рекомендация: постоянный по умолчанию, временный — флагом `--fresh-profile` и в live/bench-скриптах.
6. **Второй параллельный `browse`:** ждать в очереди (рекомендация, лок) или сразу вернуть `failed: busy`.
7. Ревью и живая проверка `attach` — основная сессия с пользователем (окно «Разрешить»), не исполнители.

## 1. Что строим

MCP-сервер (stdio, Python SDK `mcp`) с одним инструментом `browse(url, goal, max_steps?, timeout_seconds?, keep_open?)`.
Цикл (из `<src>/jev_ultrafast/agent.py`): снимок страницы одним `Runtime.evaluate` (`snapshot.js`) → один запрос к Jev с вопросом
`operation` и спекулятивными `*_target` → исполнение только выбранной цели по наблюдаемому узлу → снимок. `TYPE_TEXT` → текстовая модель.
Ответ инструмента: статус `done|blocked|failed|timeout|step_limit`, шаги, итоговые url/title, стоимость (`usage.cost`), замеры, скриншот.

Факты (проверены 25.09): Jev через `POST https://openrouter.ai/api/v1/systemone`, `Bearer <OPENROUTER_API_KEY>`, модель `jev-latest`,
0,5–0,8 с, в ответе `usage.cost`; у них адрес вшит в `<src>/jev_ultrafast/model.py:119`. Текстовая модель `inception/mercury-2.5`,
`reasoning: none` (`<src>/.env.example`) — живой вызов не проверен. Chrome 154 arm64, `~/Library/Application Support/Google/Chrome/DevToolsActivePort`
= `9222` + `/devtools/browser/<id>`; HTTP `/json/version` → 404, только ws. Инструменты: uv 0.9.26, Python 3.12.10, Node 22.22, Claude Code 2.1.282.
В кэше uv есть `mcp 1.28.1` (`FastMCP(name, instructions=)`, `Image(data=, format=)`, `run(transport="stdio")`), `websockets 15.0.1`
(`websockets.sync.client.connect`, `max_size` по умолчанию 1 МиБ — поднять), `httpx 0.28.1`, `h2 4.4.1`, `pytest 8.4.2`, `ruff 0.16.8`.

## 2. CDP: что реально используется в `<src>` и как ложится на свой клиент

Событий (подписок) у них нет: готовность страницы — опрос `document.readyState` (`browser.py:29-33`), ожидания — промисы внутри
`Runtime.evaluate awaitPromise` (`browser.py:49-74`). Все вызовы идут через `browser_harness.helpers.cdp(method, session_id=, **params)`
(демон, IPC на каждый вызов) — заменяем прямым websocket к `ws://127.0.0.1:<port>/devtools/browser/<id>` (browser-level endpoint,
поддерживает `Target.*` и flatten-сессии).

| Метод | Где в `<src>` | Уровень | В нашем клиенте |
| --- | --- | --- | --- |
| `Target.createTarget(url="about:blank", background=True)` | browser.py:23 | браузер | `client.call(...)` без `sessionId` |
| `Target.attachToTarget(targetId, flatten=True)` | browser.py:24 | браузер | то же → `sessionId` |
| `Target.closeTarget(targetId)` | browser.py:111 | браузер | в `Tab.close()` |
| `Emulation.setDeviceMetricsOverride(1120×780, dsf 1)` | browser.py:25 | сессия | `client.call(..., session_id=)` |
| `Emulation.setFocusEmulationEnabled(enabled=True)` | browser.py:27 | сессия | то же (фоновая вкладка без троттлинга rAF) |
| `Page.navigate(url)` | browser.py:28 | сессия | то же |
| `Runtime.evaluate(expression, returnByValue, [awaitPromise])` | browser.py:39,50,128; snapshot | сессия | то же; `exceptionDetails` → `StalePage` |
| `Input.dispatchMouseEvent(mouseWheel / mousePressed / mouseReleased)` | browser.py:139,168 | сессия | то же |
| `Input.dispatchKeyEvent(keyDown/keyUp, commands=["selectAll"])` | browser.py:171-184 | сессия | то же (macOS modifiers=4) |
| `Input.insertText(text)` | browser.py:185 | сессия | то же |
| `Page.captureScreenshot(format="jpeg", quality=72)` | browser.py:193 | сессия | то же; quality из конфига |

События, которые клиент обязан **переваривать** (не терять ответ): любые сообщения без `id` — складывать в ограниченный deque;
`Target.detachedFromTarget` / `Target.targetCrashed` с нашим `sessionId` → пометить вкладку мёртвой (`TabGone`). `Page.enable` /
`Runtime.enable` не нужны (не используются и в исходнике).

**Выбор: свой синхронный клиент на `websockets` (~120 строк).** Причины: покрывает все 11 методов и 0 подписок; прямой ws без демона
и IPC (у них каждый вызов — через демон browser-harness); одна зависимость и нет Node-драйвера Playwright (~150 МБ, subprocess,
свой event loop); `connect_over_cdp` в Playwright всё равно потребовал бы `new_cdp_session` для `Input.*`/`Emulation.*`, а `new_page`
создаёт видимую вкладку — фоновая (`background=True`) достижима только сырым `Target.createTarget`. Минусы: нет автоматической
обработки frames/shadow DOM — их нет и в `snapshot.js` (README исходника: «Shadow roots, frames … remain outside this MVP»).

## 3. Структура файлов и владение

```
<proj>/
  .gitignore .env(600, игнор) .env.example              — контракт (координатор)
  pyproject.toml uv.lock                                — контракт; далее Пакет 2
  LICENSE (MIT, свой) LICENSE-jev-ultrafast (их текст с «Copyright (c) 2026 Browser Use»)  — Пакет 2
  README.md                                             — Пакет 2
  docs/plan.md (этот файл), docs/decisions.md (итоги)   — координатор
  browser_hands/
    __init__.py      — контракт: только докстринг; НИКТО не правит (без реэкспортов: ветка 2 не имеет agent.py)
    types.py         — контракт: Status, Timing, Step, RunResult, AgentLike, ChromeLike; правит только Пакет 1
    config.py        — контракт: датаклассы BrowserConfig/ModelConfig/RunConfig/Settings; далее Пакет 2 (from_env, парсинг)
    cdp.py           — Пакет 1: CDPClient (websockets.sync), CDPError, TabGone
    chrome.py        — Пакет 1: разбор DevToolsActivePort, режимы attach/launch/ws, класс Chrome (постоянное соединение, реконнект)
    browser.py       — Пакет 1: Tab (перенос Browser/browser_operation из <src>), StalePage
    snapshot.js      — Пакет 1: перенос без изменений (window.__jevFast → window.__browserHands не обязательно; оставить)
    model.py         — Пакет 1: ModelClients (тёплый httpx), choose(), field_text(), validate_choice(), action_space()
    questions.py     — Пакет 1: NEXT_ACTION, TARGET, TEXT_VALUE (перенос как есть)
    agent.py         — Пакет 1: Agent, RunResult сборка, лимиты, замеры
    server.py        — Пакет 2: FastMCP, инструмент browse, лок, ленивый Chrome
    cli.py           — Пакет 2: `browser-hands run|serve`
    logging.py       — Пакет 2: логгер в stderr
  scripts/
    live_wikipedia.py — Пакет 1 (живая проверка ядра, headless launch, платно — доли цента)
    bench.py          — Пакет 2 (N прогонов, медиана/p95)
    check.sh          — Пакет 2 (ruff + pytest + node --check)
  tests/
    test_cdp.py test_chrome.py test_browser.py test_model.py test_agent.py — Пакет 1
    fakes.py test_config.py test_server.py test_cli.py test_bench.py     — Пакет 2
    test_contract.py                                                     — контракт
```

Не переносим: `demo.py`, `static/`, `scripts/record_*`, `render_*`, `check_guards.py`, `measure_flights.py`, `examples/flights.py`,
`docs/*` исходника, тест `test_flight_verification_rejects_wrong_trip` (`<src>/tests/test_agent.py:279-302`).

## 4. Контракт между пакетами (фиксируется контрактным коммитом, §5)

```python
# browser_hands/types.py
from dataclasses import dataclass, field
from typing import Literal, Protocol

Status = Literal["done", "blocked", "failed", "timeout", "step_limit"]

@dataclass(slots=True)
class Timing:                      # миллисекунды, суммируемые
    model_ms: int = 0              # Jev (systemone)
    text_ms: int = 0               # текстовая модель
    browser_ms: int = 0            # CDP: снимок + исполнение (без ожиданий)
    wait_ms: int = 0               # ожидания: settle после ввода, WAIT, загрузка
    def __add__(self, other: "Timing") -> "Timing": ...

@dataclass(slots=True)
class Step:
    index: int                     # с 1
    operation: str                 # CLICK | TYPE_TEXT | SELECT | SCROLL_UP | SCROLL_DOWN | WAIT
    target: str                    # подпись элемента ("Search", "Open Search", "Scroll down")
    text: str | None               # что напечатали (TYPE_TEXT)
    page_changed: bool | None
    url: str                       # url после шага
    confidence: float
    timing: Timing
    cost: float | None             # usage.cost этого шага (Jev + текст), если пришёл

@dataclass(slots=True)
class RunResult:
    status: Status
    steps: list[Step]
    url: str
    title: str
    screenshot_jpeg: bytes | None  # та же вкладка, в конце; None если вкладка погибла
    cost: float | None             # сумма по шагам, None если ни один usage.cost не пришёл
    elapsed_ms: int
    timing: Timing                 # сумма по шагам + старт (навигация)
    model_calls: int
    error: str | None = None       # текст причины для failed/timeout
    tab_kept: bool = False

class AgentLike(Protocol):
    def run(self) -> RunResult: ...

class ChromeLike(Protocol):
    def connect(self) -> None: ...     # идемпотентно; переподключается, если соединение мертво
    def alive(self) -> bool: ...
    def close(self) -> None: ...       # launch: гасит свой процесс; attach: только закрывает ws
```

```python
# browser_hands/config.py (в контракте — только датаклассы и значения по умолчанию; from_env добавляет Пакет 2)
@dataclass(slots=True)
class BrowserConfig:
    mode: Literal["attach", "launch"] = "attach"
    ws_url: str | None = None                                  # BROWSER_HANDS_WS_URL — приоритет над mode
    chrome_data_dir: Path = Path("~/Library/Application Support/Google/Chrome").expanduser()   # attach
    launch_data_dir: Path = Path("~/.cache/browser-hands/chrome-profile").expanduser()         # launch
    chrome_binary: Path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    headless: bool = False
    viewport: tuple[int, int] = (1120, 780)
    connect_timeout_s: float = 60.0                            # ждём «Разрешить» в attach
    call_timeout_s: float = 30.0                               # один CDP-вызов
    screenshot_quality: int = 60
    screenshot_scale: float = 1.0

@dataclass(slots=True)
class ModelConfig:
    jev_url: str = "https://openrouter.ai/api/v1/systemone"
    jev_model: str = "jev-latest"
    jev_api_key: str = ""                                      # BROWSER_HANDS_JEV_API_KEY, иначе OPENROUTER_API_KEY
    text_base_url: str = "https://openrouter.ai/api/v1"
    text_model: str = "inception/mercury-2.5"
    text_api_key: str = ""                                     # BROWSER_HANDS_TEXT_API_KEY, иначе OPENROUTER_API_KEY
    text_reasoning: Literal["none", "low"] = "none"
    request_timeout_s: float = 25.0

@dataclass(slots=True)
class RunConfig:
    max_steps: int = 25
    timeout_s: float = 90.0
    keep_open: bool = False

@dataclass(slots=True)
class Settings:
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    run: RunConfig = field(default_factory=RunConfig)
```

Публичный API Пакета 1 (Пакет 2 импортирует лениво, внутри функций):

```python
# browser_hands/chrome.py
class Chrome(ChromeLike):
    def __init__(self, config: BrowserConfig) -> None: ...
    def connect(self) -> None
    def alive(self) -> bool                    # ws открыт и Target.getTargets отвечает
    def new_tab(self) -> "Tab"                 # createTarget(background) + attach + emulation; регистрирует targetId
    def close(self) -> None
def resolve_ws_url(config: BrowserConfig) -> str      # ws_url | DevToolsActivePort(chrome_data_dir) | launch → DevToolsActivePort(launch_data_dir)

# browser_hands/model.py
class ModelClients:
    def __init__(self, config: ModelConfig) -> None    # один httpx.Client(http2=True) на процесс
    def warmup(self) -> None                            # TLS/HTTP2 к обоим хостам, ошибки глотает, ≤2 с
    def close(self) -> None

# browser_hands/agent.py
class Agent(AgentLike):
    def __init__(self, chrome: Chrome, clients: ModelClients, url: str, goal: str, run: RunConfig,
                 *, screenshot_quality: int = 60, screenshot_scale: float = 1.0) -> None
    def run(self) -> RunResult      # блокирующий; вкладку закрывает в finally, если не keep_open; исключений наружу не бросает,
                                    # кроме ошибок конфигурации (нет ключа) и ChromeUnavailable при подключении
```

Правила для обоих пакетов: type hints и dataclasses; без глобального состояния, кроме явных клиентов (`Chrome`, `ModelClients`) в
`server.py`; логи только в stderr (`logging.py`, stdout занят MCP stdio); секреты только из env; `ruff` (`E,F,I`, line-length 120);
`pytest` офлайн, без сети. Оба исполнителя правят только свои файлы (§3); изменение контракта — через координатора.

Общий откат для всех шагов: работа в ветке, `git reset --hard <предыдущий коммит>` или `git checkout -- <файл>`; ветка целиком —
`git branch -D`. Где откат другой — указан в шаге.

## 5. Шаг 0 — координатор: репозиторий и контракт (последовательно, ~15 мин)

0.1 Начальный коммит `main`: `git add .gitignore && git commit -m "chore: gitignore"`.
    Проверка: `git log --oneline` → 1 коммит; `git status --short` → пусто (`.env` игнорируется).
0.2 Ветка `feat/initial`: `git switch -c feat/initial`. Создать: `pyproject.toml` (name `browser-hands`, `requires-python >=3.12`,
    deps `mcp>=1.28,<2`, `httpx[http2]>=0.28,<1`, `websockets>=15,<16`; dev `pytest>=8.4,<9`, `ruff>=0.14,<1`; `[project.scripts]
    browser-hands = "browser_hands.cli:main"`; `[tool.ruff] line-length=120`, `select=["E","F","I"]`; `[tool.pytest.ini_options]
    testpaths=["tests"]`; hatchling), `browser_hands/__init__.py` (докстринг), `types.py` и `config.py` из §4, `.env.example`
    (все `BROWSER_HANDS_*` из §7.3 с дефолтами, `OPENROUTER_API_KEY=` пустой), `tests/test_contract.py` (дефолты Settings,
    `Timing.__add__`), `uv lock`.
    Проверка: `uv sync` → `.venv` создан; `uv run ruff check .` → «All checks passed»; `uv run pytest -q` → `1 passed`;
    `uv run python -c "import mcp.server.fastmcp, websockets.sync.client, httpx; print('ok')"` → `ok`.
0.3 Коммит `chore: contract for core/shell packages`. Ветки: `git worktree add ../browser-hands-core -b feat/core feat/initial`,
    `git worktree add ../browser-hands-shell -b feat/shell feat/initial`.
    Проверка: `git worktree list` → 3 строки. Откат: `git worktree remove ../browser-hands-core --force; git branch -D feat/core`.
0.4 Каждому исполнителю — свой worktree, `.env` не копировать в worktree (симлинк `ln -s <proj>/.env` или `--env-file <proj>/.env`).

## 6. Пакет 1 «ядро» — исполнитель A, ветка `feat/core`

Порядок: 1.1 → 1.2 → 1.3 (параллельно с 1.4) → 1.5 → 1.6 → 1.7 → 1.8. Все тесты офлайн; `uv run pytest -q` и `ruff` — секунды,
локально допустимо. Коммит после каждого шага.

1.1 `cdp.py`. `CDPClient(ws_url, *, call_timeout)`: `websockets.sync.client.connect(ws_url, max_size=None, open_timeout=connect_timeout)`;
    `call(method, params=None, *, session_id=None, timeout=None) -> dict`: `id` инкрементом, `threading.Lock` вокруг send/recv-цикла,
    сообщения без `id` → `self.events` (deque maxlen 200) + обработчик detach/crash; `{"error"}` → `CDPError(message)`; закрытый ws →
    `ChromeDisconnected`. `close()`. Никаких потоков-читателей: один вызов за раз.
    Тест `tests/test_cdp.py`: локальный `websockets.sync.server.serve` на 127.0.0.1:0, сценарии: ответ по id, событие перед ответом
    не ломает вызов, `error` → `CDPError`, `sessionId` попадает в кадр, таймаут → `CDPTimeout`, обрыв → `ChromeDisconnected`.
    Проверка: `uv run pytest tests/test_cdp.py -q` → все passed, <2 с.
1.2 `chrome.py`. `read_devtools_active_port(data_dir) -> str` (две строки файла → `ws://127.0.0.1:<port><path>`; нет файла/пусто →
    `ChromeUnavailable` с подсказкой «включите chrome://inspect/#remote-debugging или BROWSER_HANDS_MODE=launch»).
    `launch_chrome(config) -> subprocess.Popen`: `<binary> --user-data-dir=<dir> --remote-debugging-port=0 --no-first-run
    --no-default-browser-check --disable-background-timer-throttling --disable-renderer-backgrounding
    --disable-backgrounding-occluded-windows [--headless=new] about:blank`; ждать появления DevToolsActivePort в `<dir>` до
    `connect_timeout_s`; **перед запуском удалить старый DevToolsActivePort** (иначе прочитаем устаревший). `Chrome.connect()`:
    если `alive()` — ничего; иначе закрыть старое, `resolve_ws_url` заново (файл перечитывается — устаревший порт после перезапуска
    Chrome так и лечится), новый `CDPClient`; при `ConnectionRefusedError` в attach — `ChromeUnavailable("Chrome не запущен или
    отладка выключена")`, без тихого перехода в launch. `alive()`: `Target.getTargets` с таймаутом 2 с. `new_tab()`: их
    `browser.py:23-27` + список `owned_targets`. `close()`: attach — только `client.close()` (браузер пользователя не трогать);
    launch — `Target.closeTarget` своих вкладок, `client.close()`, `proc.terminate()`, 5 с, `kill()`.
    Тест `tests/test_chrome.py`: tmp-каталог с файлом → ws url; отсутствие → `ChromeUnavailable`; `launch_chrome` с подменой
    `subprocess.Popen` (mock пишет файл) → url; `Chrome.close()` в attach не вызывает `closeTarget` и не трогает процесс;
    reconnect при `alive()==False` создаёт новый клиент.
    Проверка: `uv run pytest tests/test_chrome.py -q` → passed.
1.3 `browser.py` + `snapshot.js`. Перенести `<src>/jev_ultrafast/browser.py` в класс `Tab(client, session_id, target_id, *,
    screenshot_quality, screenshot_scale)`: `evaluate`, `observe(screenshot)`, `fresh`, `act`, `close`, `screenshot() -> bytes`
    (последний кадр той же вкладки; при `TabGone` → None). `browser_operation` → методы `Tab._observe`/`Tab._act` без глобального
    `cdp`. `Page.navigate` + ожидание `readyState` (15 с) — в `Tab.navigate(url)`; замер в `Timing.wait_ms`. Settle-ожидание после
    ввода (`browser.py:49-74`, 200 мс/2 кадра/50 мс) — сохранить дословно; его длительность — `wait_ms`, сам CDP — `browser_ms`.
    `snapshot.js` — копия; `node --check browser_hands/snapshot.js`.
    Тесты `tests/test_browser.py`: перенести `test_observation_is_one_atomic_browser_read`, `test_executor_rejects_a_stale_page_before_browser_input`,
    `test_interrupted_dropdown_mutation_cannot_be_retried_as_stale`, `test_fingerprint_tracks_values_and_identity_not_screenshots`
    (`<src>/tests/test_agent.py:230-276`) на `Tab` с `Mock`-клиентом; добавить: `screenshot()` при `TabGone` → None; `close()`
    идемпотентен и глотает `ChromeDisconnected`.
    Проверка: `uv run pytest tests/test_browser.py -q` → passed; `node --check browser_hands/snapshot.js` → без вывода, код 0.
1.4 (параллельно с 1.3) `model.py` + `questions.py`. Перенести `<src>/jev_ultrafast/model.py`: `post_json` → метод
    `ModelClients.post(url, key, body, *, timeout)` (тот же retry 429/503/529, `timeout` = min(request_timeout_s, остаток дедлайна));
    `choose(clients, config, state, goal, history) -> Decision` (dataclass: choice, operation, target, confidence, probabilities,
    usage, cost, latency_ms); `field_text(clients, config, context) -> tuple[str, TextHelper]`; url/model/ключи — из `ModelConfig`
    (вшитый `api.typesafe.ai` убрать); `reasoning` → `{"reasoning": {"enabled": False}}` при `none`, `{"reasoning": {"effort": "low"}}`
    при `low` (deepseek-ветку не переносить); стоимость: `result.get("usage", {}).get("cost")`; для текстовой модели добавить в тело
    `"usage": {"include": true}` (OpenRouter отдаёт cost в usage — **не проверено**, проверить в 1.8 по логу). `warmup()`: в потоке
    `GET <jev host>/api/v1/auth/key` (бесплатно, маленький ответ — **не проверено**; при сомнении — `HEAD` на хост) с таймаутом 2 с,
    ошибки в лог. `questions.py` — копия без `MAX_STEPS` (лимит — в `RunConfig`).
    Тесты `tests/test_model.py`: перенести `<src>/tests/test_agent.py:48-157, 305-312` (validate_choice, action_space, один запрос
    на шаг, чужая голова не исполняется, состояние контролов в target-голове, кавычки из цели идут в LLM, нет ключа → ошибка до
    генерации, невалидный JSON → ничего не печатать); добавить: url и модель берутся из `ModelConfig`; `cost` суммируется, None без
    `usage.cost`; `post` использует один и тот же `httpx.Client` (mock `Client.post`).
    Проверка: `uv run pytest tests/test_model.py -q` → passed.
1.5 `agent.py`. Перенести цикл `<src>/jev_ultrafast/agent.py` (tick/predict/act, StalePage → переснять, «3 шага подряд без изменения
    страницы и не WAIT → blocked», кэш `pending_text` только при идентичном контексте, история пишется до наблюдения) в
    `Agent.run() -> RunResult`: `deadline = monotonic() + timeout_s`, проверка перед каждым вызовом модели и передача остатка в
    `timeout`; `len(steps) >= max_steps` → `step_limit`; исключения модели/CDP → `failed` с `error`; `ChromeDisconnected`/`TabGone` →
    `failed`, скриншот None; `finally`: скриншот (лимит 5 с), затем `tab.close()`, если не `keep_open`. Замеры: `perf_counter`
    вокруг `choose` (model_ms), `field_text` (text_ms), `observe`/`act` (browser_ms), settle/WAIT/navigate (wait_ms).
    Правило AGENTS.md: никакого повтора мутаций — `decision` обнуляется до `act` (как `agent.py:91`), retry только в `post` (сеть, до
    исполнения). Скриншоты по ходу не снимаем (`screenshot=False` в observe), только финальный.
    Тесты `tests/test_agent.py`: перенести `<src>/tests/test_agent.py:160-228, 315-320` на новый Agent (fixture с Mock-Tab и
    Mock-clients); добавить: `timeout` при исчерпанном дедлайне до первого действия (подмена `time.monotonic`); `step_limit` при
    max_steps=2; `failed` при `RuntimeError` из `choose` — вкладка закрыта; `keep_open=True` → `tab.close` не вызван, `tab_kept`;
    `cost` = сумма; `model_calls` считается.
    Проверка: `uv run pytest -q` → все passed; `uv run ruff check .` → чисто.
1.6 `scripts/live_wikipedia.py` (платно, доли цента; запускать можно): `BrowserConfig(mode="launch", headless=True,
    launch_data_dir=tempfile.mkdtemp())`, url `https://en.wikipedia.org/wiki/Main_Page`, goal «Find and open the Wikipedia article
    about Gödel's incompleteness theorems.», печать шагов, `Timing`, cost, статус; скриншот в `traces/live-<ts>.jpg`; выход 0 только
    при `status=="done"` и `"incompleteness_theorems" in result.url`. В finally — `chrome.close()` и удаление temp-профиля.
    Проверка (локально, нужен Chrome): `uv run --env-file <proj>/.env python scripts/live_wikipedia.py` → `status=done`, url статьи,
    `elapsed_ms` ≈ 3000–6000 (у них 2,8 с), `cost` число, JPEG открывается (`file traces/live-*.jpg` → `JPEG image data`).
    Откат: `pkill -f chrome-profile-live` не нужен — процесс гасится в finally; при зависании `pkill -f "remote-debugging-port=0"`.
1.7 `README`-фрагмент для Пакета 2: в `docs/core-notes.md` (Пакет 1 владеет) — что вернул live-прогон (время, стоимость, замечания
    по `usage.cost` текстовой модели, окно «Разрешить»), чтобы Пакет 2 вставил в README.
1.8 Критерии готовности Пакета 1: `uv run ruff check .` чисто; `uv run pytest -q` ≥ 25 тестов passed, без сети (проверка:
    `uv run pytest -q -p no:cacheprovider` при выключенном Wi-Fi или с `HTTPS_PROXY=http://127.0.0.1:1` — passed); `node --check
    browser_hands/snapshot.js`; live-скрипт `done` за <10 с; `grep -rn "typesafe.ai\|browser_harness\|posthog" browser_hands` →
    только дефолт `jev_url`/докстринги, импорта нет.

## 7. Пакет 2 «обвязка» — исполнитель B, ветка `feat/shell`

Порядок: 2.1 → 2.2 → 2.3 (параллельно с 2.4, 2.5) → 2.6 → 2.7. До слияния работает на `tests/fakes.py`.

2.1 `tests/fakes.py`: `FakeChrome(ChromeLike)` (счётчики connect/close, флаг alive), `FakeAgent(AgentLike)` (возвращает заданный
    `RunResult`, может бросать), `make_result(status=..., steps=N, screenshot=b"\xff\xd8...")`. `logging.py`: `get_logger()` →
    `logging.StreamHandler(sys.stderr)`, уровень из `BROWSER_HANDS_LOG` (default `INFO`), формат `%(asctime)s %(levelname)s %(name)s %(message)s`.
    Проверка: `uv run pytest tests -q -k fakes` (пустой набор допустим) и `uv run python -c "from tests.fakes import FakeAgent"`.
2.2 `config.py`: `Settings.from_env(env: Mapping[str, str]) -> Settings` с префиксом `BROWSER_HANDS_`:
    `MODE`, `WS_URL`, `CHROME_DATA_DIR`, `LAUNCH_DATA_DIR`, `CHROME_BINARY`, `HEADLESS` (`1/true/yes`), `CONNECT_TIMEOUT_S`,
    `SCREENSHOT_QUALITY`, `SCREENSHOT_SCALE`, `JEV_URL`, `JEV_MODEL`, `JEV_API_KEY`, `TEXT_BASE_URL`, `TEXT_MODEL`, `TEXT_API_KEY`,
    `TEXT_REASONING`, `MAX_STEPS`, `TIMEOUT_S`, `LOG`; ключи по умолчанию — `OPENROUTER_API_KEY`; `validate()` → понятная ошибка
    «нет ключа: задайте OPENROUTER_API_KEY» без печати значений. `apply_overrides(settings, **cli)` для флагов CLI.
    Тесты `tests/test_config.py`: пустой env → дефолты; переопределения; булевы; невалидное число → `ValueError` с именем переменной;
    ключ не попадает в `repr(Settings)` (`field(repr=False)`).
    Проверка: `uv run pytest tests/test_config.py -q` → passed.
2.3 `server.py`: `create_server(settings, *, chrome_factory=None, agent_factory=None, clients_factory=None) -> FastMCP`.
    `FastMCP("browser-hands", instructions="Управляет Chrome: browse(url, goal) сам кликает и печатает; результат проверяй по скриншоту.")`.
    Инструмент `browse(url: str, goal: str, max_steps: int | None = None, timeout_seconds: float | None = None, keep_open: bool = False)`,
    описание одной фразой + «DONE не гарантирует успех — смотри скриншот». Внутри: `threading.Lock` (решение №6), ленивое создание
    `Chrome` и `ModelClients` при первом вызове (фабрики по умолчанию — `importlib` `browser_hands.chrome` / `.agent` / `.model`,
    импорт внутри функции), `chrome.connect()` (реконнект при мёртвом ws), `clients.warmup()` в потоке параллельно с `new_tab`
    (warmup вызывать один раз на процесс), `Agent(...).run()`. Ответ: `[text, Image(data=jpeg, format="jpeg")]` — текст:
    `status`, `url`, `title`, `steps: N`, список `N. OP target — "text"`, `cost: $0.0042`, `elapsed 3.1s (model 1.9 / text 0.4 / browser 0.3 / wait 0.5)`,
    `error`, если есть; без скриншота — только текст. Ошибки конфигурации/ChromeUnavailable → текст `status: failed` + причина
    (не исключение — Claude Code увидит сообщение). `atexit`/`finally` в `serve`: `chrome.close()`, `clients.close()`.
    **Проверить при реализации:** FastMCP превращает `list[str | Image]` в `[TextContent, ImageContent]` (`mcp/server/fastmcp/server.py`,
    `_convert_to_content`); если нет — вернуть `list[TextContent | ImageContent]` из `mcp.types` явно.
    Тесты `tests/test_server.py` (через `mcp.shared.memory.create_connected_server_and_client_session` или прямой вызов
    `server._tool_manager.call_tool`): `list_tools` → один инструмент, описание ≤ 200 символов; `browse` с FakeAgent → 2 content,
    второй `type=="image"`, `mimeType=="image/jpeg"`; FakeAgent бросает → текст `status: failed`, без image; второй вызов не создаёт
    новый Chrome (`FakeChrome.connect_calls == 2`, `created == 1`); `alive()==False` → `connect` вызван снова; лок сериализует
    (два потока → второй ждёт).
    Проверка: `uv run pytest tests/test_server.py -q` → passed.
2.4 `cli.py` (`argparse`, подкоманды): `browser-hands run --url U --goal G [--max-steps N] [--timeout S] [--keep-open]
    [--mode attach|launch] [--headless] [--ws URL] [--data-dir P] [--fresh-profile] [--json] [--screenshot out.jpg]` — печать шагов и
    замеров в stdout, `--json` → `RunResult` как JSON без байтов скриншота, код возврата 0 при `done`, 2 иначе; `browser-hands serve
    [--mode ...]` → `create_server(...).run(transport="stdio")`. Флаги перекрывают env.
    Тесты `tests/test_cli.py`: `run` с подменой фабрик → вывод содержит `status: done`, код 0; `--json` парсится; `--mode launch --headless`
    попадает в Settings; `serve --help` работает.
    Проверка: `uv run pytest tests/test_cli.py -q` → passed; `uv run browser-hands --help` → две подкоманды.
2.5 `scripts/bench.py`: `--runs N --url --goal [--headless] [--fresh-profile]`; каждый прогон — новый `Agent` на одном `Chrome`
    и одних `ModelClients` (мерим именно тёплое соединение); вывод: таблица по прогонам + медиана и p95 по `elapsed_ms`, `model_ms`,
    `text_ms`, `browser_ms`, `wait_ms`, сумма `cost`; JSONL в `traces/bench-<ts>.jsonl`. `scripts/check.sh`: `uv run ruff check . &&
    uv run ruff format --check . && uv run pytest -q && node --check browser_hands/snapshot.js`. Опционально
    `.github/workflows/ci.yml` (ruff + pytest на `ubuntu-latest`, uv, Python 3.12) — без Chrome и без ключей.
    Тест `tests/test_bench.py`: статистика на фейковых результатах (медиана/p95 корректны).
    Проверка: `uv run pytest tests/test_bench.py -q`; `bash scripts/check.sh` в ветке → зелёный (pytest по своим тестам).
2.6 `README.md` (по-русски, коротко): что это (1 абзац, происхождение: jev-ultrafast, MIT, коммит `1231850a…`, что переделано —
    свой CDP-клиент, OpenRouter, MCP, без телеметрии); установка (`uv sync`); ключ (`.env`, `OPENROUTER_API_KEY`); режимы
    (`attach` — включить `chrome://inspect/#remote-debugging`, окно «Разрешить»; `launch`; `ws`); таблица переменных; CLI; регистрация:
    `claude mcp add -s user browser-hands -- uv run --frozen --directory <путь к клону> --env-file <путь к клону>/.env browser-hands serve`;
    как проверять результат (DONE ≠ успех, смотри скриншот); ограничения (frames, shadow DOM, попапы, загрузки файлов); разработка
    (`scripts/check.sh`, `scripts/live_wikipedia.py`, `scripts/bench.py`). `LICENSE` (MIT, ваш копирайт) и `LICENSE-jev-ultrafast`
    (их текст дословно, `Copyright (c) 2026 Browser Use`), `.env.example` дополнить.
    Проверка: `grep -c BROWSER_HANDS_ README.md` ≥ 15; `grep -n "1231850a" README.md` есть; `diff <(sed -n 1,21p LICENSE-jev-ultrafast) <src>/LICENSE` → пусто.
2.7 Критерии готовности Пакета 2: `bash scripts/check.sh` зелёный в ветке; `uv run browser-hands run --url x --goal y` до слияния
    даёт понятную ошибку `failed: модуль browser_hands.chrome ещё не готов` (не traceback); `uv run pytest -q` ≥ 15 тестов; в
    `browser_hands/server.py`, `cli.py` нет импортов `agent`/`chrome`/`model` на уровне модуля (`grep -n "^from browser_hands\.\(agent\|chrome\|model\)"` → пусто).

## 8. Слияние, живые проверки, регистрация (координатор + основная сессия)

3.1 Слияние: сначала та ветка, что готова; `git switch feat/initial && git merge --no-ff feat/core && git merge --no-ff feat/shell`.
    Конфликты возможны только в `types.py`/`config.py`/`pyproject.toml`, если кто-то нарушил владение.
    Проверка: `bash scripts/check.sh` → зелёный; `uv run pytest -q` = сумма тестов обоих пакетов. Откат: `git reset --hard ORIG_HEAD`.
3.2 Живая проверка launch (исполнитель или координатор, платно, копейки): `uv run --env-file .env python scripts/live_wikipedia.py`
    → `done`; `uv run --env-file .env browser-hands run --mode launch --headless --fresh-profile --url https://en.wikipedia.org/wiki/Main_Page
    --goal "Find and open the Wikipedia article about Gödel's incompleteness theorems." --screenshot traces/cli.jpg` → `status: done`,
    код 0, `traces/cli.jpg` показывает статью.
3.3 Бенчмарк: `uv run --env-file .env python scripts/bench.py --runs 5 --headless --fresh-profile --url ... --goal ...` → медиана
    `elapsed_ms` < 6000, `browser_ms` медиана < 500 (если больше — искать в CDP-клиенте, не в моделях); записать в `docs/decisions.md`.
3.4 Живая проверка `attach` — **основная сессия с пользователем**: Chrome запущен, отладка включена; `uv run --env-file .env
    browser-hands run --url https://en.wikipedia.org/wiki/Main_Page --goal "..." --screenshot traces/attach.jpg`; ожидать окно
    «Разрешить» в Chrome (нажимает пользователь), результат `done`, вкладка пользователя не менялась, фоновая вкладка закрыта
    (`chrome://inspect/#pages` не показывает about:blank/Wikipedia от агента). Повтор с `--keep-open` → вкладка осталась.
3.5 Регистрация в Claude Code (пользователь решает; обратимо): команда из README; проверка `claude mcp list` → `browser-hands: … - ✓ Connected`;
    в новой сессии Claude Code: «открой Википедию и найди статью …» → инструмент вернул текст + картинку.
    Откат: `claude mcp remove -s user browser-hands`.
3.6 Ревью (Opus) diff `main..feat/initial`: владение файлов, отсутствие телеметрии/секретов (`git grep -n "posthog\|sk-or-\|api_key="`),
    `finally` вокруг вкладки, stderr-логи, отсутствие повторных мутаций. Push и merge в `main` — только по решению пользователя.

## 9. Скорость: что мерим и что не терять

- Постоянное соединение: один `Chrome` и один `ModelClients` на процесс сервера, ленивое подключение при первом `browse`,
  реконнект по `alive()==False` (перечитать DevToolsActivePort). В attach это одно окно «Разрешить» на сессию Claude Code.
- Прямой ws без демона: каждый CDP-вызов — один кадр туда и обратно; ожидаемо <5 мс на вызов против IPC демона.
- Тёплый `httpx.Client(http2=True)` на процесс; `warmup()` параллельно с `new_tab()`+`navigate`.
- Сохранить оптимизации исходника: один запрос Jev на шаг со спекулятивными целями (`model.py:81-117`), атомарный снимок одним
  `evaluate` (`browser.py:188`), потолки ожиданий 200 мс / 2 кадра / 50 мс (`browser.py:49-74`), `setFocusEmulationEnabled`
  (`browser.py:27`), только видимый текст ≤ 6000 символов (`snapshot.js:82-92`), скриншоты по ходу не снимать.
- Сначала мерить (`Timing` в каждом `Step`, итог в `browse`/CLI/bench), потом оптимизировать. Ожидание: `model_ms` ≈ 70–80 % времени;
  оптимизации ниже модели дают мало.

## 10. Риски и как обрабатывать

- **Окно «Разрешить» в attach.** Chrome может показать диалог при первом ws-подключении; `connect_timeout_s=60`, в stderr/CLI —
  «Нажмите Разрешить в Chrome». Отказ/тайм-аут → `failed` с этим текстом. Постоянное соединение → окно один раз на процесс сервера.
- **DevToolsActivePort устарел** (Chrome перезапущен: файл переписан с новым портом/путём; Chrome закрыт: файл может остаться).
  Всегда перечитывать при `connect()`; `ConnectionRefused`/404-handshake → `ChromeUnavailable` с подсказкой; в launch — удалять файл
  перед стартом процесса. Не переключаться в launch автоматически.
- **Вкладка при ошибке.** `Agent.run` закрывает вкладку в `finally` (кроме `keep_open`); при мёртвом ws `closeTarget` невозможен —
  вкладка-сирота остаётся (лог `WARNING`, `owned_targets` чистятся при следующем `connect()` через `Target.getTargets` по url? —
  нельзя надёжно отличить; документировать: закрыть руками). Падение самого процесса сервера (kill -9) — то же.
- **Тайм-ауты.** Общий дедлайн проверяется перед каждым вызовом модели и передаётся в `httpx` как остаток; CDP-вызов —
  `call_timeout_s`; финальный скриншот — свой лимит 5 с; результат `timeout` возвращается, а не бросается. Один `browse` не может
  висеть дольше `timeout_s + 5 с + закрытие вкладки`.
- **Фоновая вкладка и троттлинг.** `setFocusEmulationEnabled` держит rAF; таймеры Chrome в фоне режутся до 1 Гц через ~10 с
  (интенсивно — через 5 мин). В launch добавлены `--disable-background-timer-throttling --disable-renderer-backgrounding`; в attach
  флагов нет — если видим зависание `WAIT` на сайтах с setTimeout-логикой, пробовать `Page.setWebLifecycleState(state="active")`
  (экспериментальный метод, **не проверено**). Вкладку пользователя не активировать.
- **Попапы / `target=_blank`.** Агент остаётся в своей вкладке; новая вкладка — сирота. В MVP не обрабатывать, задокументировать.
- **Размер ответа.** Скриншот ~110–200 КБ base64 в контексте на вызов; решение №4. Снимок JSON от `snapshot.js` — до сотен КБ →
  `max_size=None` в `websockets`.
- **Параллельные вызовы.** Один лок на сервер; второй `browse` ждёт (решение №6).
- **Стоимость.** Jev ~0,5–0,8 с и `usage.cost` за шаг; текстовая модель — `usage.cost` не проверен: если нет — `cost` суммирует
  только Jev и помечается `cost (Jev only)`.
- **Секреты.** Ключ только из env через `--env-file`; `repr` конфигов без ключей; логи не печатают заголовки; `.env` в `.gitignore`.
- **Отладка Chrome включена постоянно** (attach): любой локальный процесс может подключиться к 127.0.0.1:9222 — это состояние
  профиля пользователя, не сервера; отметить в README.

## 11. Что не делать и почему

- Не переносить `browser-harness`, `demo.py`, `static/`, запись видео, PostHog — телеметрия и лишний слой IPC.
- Не давать модели селекторы/координаты/JS: цели только по `node` из снимка (правило AGENTS.md, `browser.py:141-143`).
- Не повторять мутации после ошибки: retry только сетевой до исполнения; `StalePage` → переснять и выбрать заново.
- Не закрывать браузер пользователя и не трогать его вкладки в attach (`Chrome.close()` только `ws.close()`).
- Не делать fallback attach→launch молча: пользователь получит второй Chrome и не поймёт, где куки.
- Не хранить состояние между `browse` кроме соединения и клиентов: без кэшей страниц, без «памяти» задач.
- Не запускать live/bench в pytest; не класть ключ в `claude mcp add -e`.
- Не пушить и не вливать в `main` без решения пользователя; `main` — только `.gitignore`.
- Не оптимизировать до замеров: сначала `Timing` и bench, потом изменения.
- Отдельная машина для этого проекта не нужна: Chrome и `.env` локальные, тесты — секунды; live-проверки — только локально.
