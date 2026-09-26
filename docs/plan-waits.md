# План PR «ожидания по событиям и состояниям» (`feat/waits`)

Дата: 26.09.2026. Ветка `feat/waits` = main 86a21b0 + 8 коммитов fix2 (до f3c6008). Правило пользователя (26.09):
ждать событий и состояний, а не времени; единственное число — общий дедлайн-предохранитель; пороги модели — по
трассам с записанным расчётом; стенд проверяет диапазон параметров, а не одну точку.

Проверено в коде и трассах (пути ниже). Не проверено — помечено.

## 0. Что решает пользователь

| # | Вопрос | Рекомендация |
| --- | --- | --- |
| 0.1 | **Предохранитель одного ожидания.** Только общий дедлайн прогона (90 с) — или ещё потолок на одно ожидание? Без потолка long-poll, начатый после клика, держит ожидание до конца прогона. | Потолок на одно ожидание оставить, но не «магическим»: `fuse = min(остаток дедлайна − 0,5 с, WAIT_FUSE_S)`, где `WAIT_FUSE_S` берётся из данных (p99 «действие → последняя значимая мутация» по стенду и замеру WhatsApp, §7.4/§8) и записан в `docs/calibration.md`. Запрос, переживший предохранитель, до конца прогона считается фоновым (не блокирует). |
| 0.2 | **Сетевые события: включать `Network.enable` во вкладке пользователя (WhatsApp)?** Цена — поток событий (WS-кадры), память буфера ответов. | Да, после замера §8 (события/с, память). Если > ~200 событий/с или Chrome даёт буфер, который нельзя обнулить, — сеть только в своей вкладке (`owned`), у пользователя — DOM-сигналы. |
| 0.3 | **Сравнение текста в поле** (находки 3 и 5): равенство, префикс или подстрока после нормализации? | Префикс: напечатанное — начало значения (`selectAll + insertText` заменяет всё; inline-подсказки дописывают хвост). Нормализация: пробелы → один; запасной путь — только буквы и цифры (`str.isalnum`) — снимает маски телефона/карты/даты и эмодзи-картинки. Подстроку убрать. |
| 0.4 | **Откат к шагу j при нарушенном инварианте** (текст шага j пропал): сколько раз? | Общий счётчик повторов на шаг j = `RETYPE_LIMIT` (2), как сейчас; на третий — `blocked` с тем же текстом ошибки. Считать вместе с прежними «vanished». |
| 0.5 | **Режим цели, DONE с низкой уверенностью** (трассы: DONE conf 0,32 и 0,35 → `done`, сообщение не отправлено). | DONE ниже порога `DONE_MIN_CONFIDENCE` (калибровка, §6.3) — один «второй взгляд» (ожидание по событиям + свежий снимок + вопрос); второй такой DONE — статус `unconfirmed` и в режиме цели. Плюс мягкий инвариант: напечатанный текст пропал из поля до Submit — пометка в истории (Jev видит). |
| 0.6 | **Стенд: сетевые задержки настоящими запросами** к `FixtureServer` (`/api/search`, `/api/send` с серверной паузой) вместо `setTimeout`? Иначе сигнал «сеть» на стенде не проверяется. | Да, параметр `net=1` (по умолчанию для `delay` и подтверждения Send); `remount` остаётся таймером (у WhatsApp это WS-событие, сети нет). Обе разновидности — в развёртку. |
| 0.7 | **Объём развёртки.** Оси (18 ячеек × 2 режима × 5 = 180 прогонов, ≈$0,21, ≈25 мин) или полная сетка (98 ячеек, 980 прогонов, ≈$1,15, ≈2,3 ч)? Медиана цены $0,00116, медиана времени 6,7 с, p95 14,6 с (342 прогона в трассах). | Оси для «было» и «стало»; полная сетка — один раз для «стало», если оси покажут неожиданный провал. |
| 0.8 | **Где гонять развёртку.** Нужен Chrome (launch, headless). На home-pc наличие Chrome не проверено. | Проверить `hp run 'ls /usr/bin/google-chrome* /opt/google/chrome 2>/dev/null; which chromium'`; есть — развёртка там, нет — на маке в фоне. |

Пока решений нет, стартовать можно §4 (контракт), §5, §8 и калибровку §6.3 по старым трассам — они от решений не зависят.

Приняты 26.09.2026 (основная сессия): 0.1–0.7 — как рекомендовано; 0.8 — развёртка на маке в фоне (на home-pc нет пространства репо и ключа OpenRouter — завести ключ может только пользователь).

## 1. Факты

### 1.1. Числа в коде (полный список; тип: П — предохранитель, О — опрос/ожидание по времени, М — порог модели, Л — лимит повторов, С — стенд)

| Где | Число | Тип | Судьба |
| --- | --- | --- | --- |
| `browser.py:38-40` | `SETTLE_FLOOR_MS=50`, `SETTLE_QUIET_MS=200`, `SETTLE_CEILING_MS=1500` | О | убрать: кадры + события; потолок → предохранитель §0.1 |
| `browser.py:41` | `SETTLE_DEADLINE_MARGIN_S=0.5` | П | оставить (запас, чтобы CDP-вызов вернулся до дедлайна) |
| `browser.py:100` | `Math.max(16, …)` запасной таймер без кадров | О | заменить на счёт макрозадач (`MessageChannel`), не мс |
| `browser.py:337`, `:442` | `_sleep(0.02)` опрос `readyState` и повтор снимка | О | `readyState` — ждать `Page.loadEventFired`/`Runtime.executionContextCreated`; повтор снимка — после следующего кадра |
| `browser.py:494` | WAIT: `_sleep(0.1)` | О | WAIT = ждать следующее событие (§2.4) |
| `browser.py:145-153` | `NAVIGATE_TIMEOUT_S=15`, `STALE_RETRIES=10`, `SCREENSHOT/MEASURE/LOCATION/CLAIM_TIMEOUT_S` | П | оставить |
| `agent.py:61-65` | `EMPTY_PAGE_WAIT_S=25`, `EMPTY_PAGE_WAIT_LATER_S=1.0`, `EMPTY_PAGE_POLL_S=0.25` | О | до первого вызова Jev — ждать появления интерактивных элементов по мутациям до дедлайна; после — не ждать вовсе (страница тиха и без сети → решает Jev) |
| `agent.py:68-69,76` | `STEP_DONE_MIN_P=0.7`, `DONE_STEP_DONE_MIN_P=0.5`, `MIN_ACTION_CONFIDENCE=0.3` | М | калибровка §6.3, перенести в `config.py` |
| `agent.py:58,70,73,75,77,67` | `NO_PROGRESS_STEPS=3`, `STEP_ACTIONS_LIMIT=6`, `RETYPE_LIMIT=2`, `CHECK_WAITS=2`, `UNCERTAIN_LIMIT=2`, `TEXT_ATTEMPTS=2` | Л | оставить (счётчики, не время); `CHECK_WAITS`: второй WAIT в проверке — после ожидания *события*, а не тишины |
| `agent.py:582` | бюджет решений `2·max_steps + M` | Л | оставить |
| `config.py:56-57` | `jev_timeout_s=10`, `text_timeout_s=8` | П | оставить (в README) |
| `config.py:39-40,63` | `connect_timeout_s=60`, `call_timeout_s=30`, `timeout_s=90`, `TIMEOUT_LIMIT_S=300` | П | оставить; `timeout_s` — тот самый общий дедлайн |
| `model.py:30-31,206` | `WARMUP_TIMEOUT_S=2`, `KEEPALIVE_S=300`, повтор `0.5·2^n` ×3 | П/Л | оставить |
| `chrome.py:36-45,162` | `ALIVE 2`, `CLOSE_TAB 1`, `LOOKUP 5`, `TERMINATE 5`, `sleep(0.05)` | П/О | оставить (жизненный цикл процесса, не страница) |
| `server.py:55` | `CLOSE_WAIT_S=3` | П | оставить |
| `cdp.py:24` | `EVENTS_MAX=200` | Л | сетевые события в очередь не класть (§5.1) |
| `snapshot.js:84,99,102` | текст 6000, действий 250, прокрутка 560 | Л | вне темы |
| `eval.py:72-73,182-187` | `REMOUNT_MS=1600`, `SEND_STATUS_MS=1500`, `delay=700`, `boot=4000` | С | развёртка §7.3; в задаче остаётся точка по умолчанию |
| `eval.py:102-104,896-897` | `REPORT_QUIET 0.3`, `REPORT_WAIT 2`, `REPORT_MISSING 0.2`, `--timeout 60`, `--max-steps 12` | П | оставить |
| `eval.py:772-801` | `sleep 1.0/0.4/1.0/0.3` в `check_remount` | О | привязать к параметрам (`remount/1000 + кадр`), не константы |
| `app.html:156-158,249,362-370,420` | `DELAY 700`, `HISTORY 3000`, спиннер 100 мс, подтверждение Send 300 мс, прогресс boot 200 мс | С | `net=1` (§0.6); 300 мс → серверная пауза `senddelay` |
| README:309 | «пересоздаётся через 0,9 с» | — | расходится с `REMOUNT_MS=1600` (находка 6) |

### 1.2. Трассы (проверено `python3` по `~/Personal/browser-hands*/traces/*.jsonl`)

- 20 JSONL, **342 прогона** (317 `eval`, 25 `bench`), не 400–700. Решений Jev 2160; со `step_done` и `scenario_step` — 833;
  `t_ms` есть только у 801 (новые трассы). Отчёт страницы — только итоговый, без времени: точной метки «шаг выполнен в
  момент решения» в старых трассах нет (§6.3).
- `search-remount`, сценарий, 708a595: 1/10 (`after-scn`) + 0/5 (`reg-scn`) = **1/15**; режим цели 9/10 + 5/5. Ход
  провала: `TYPE_TEXT «Type a message» s3` → `CLICK Send s4` → BLOCKED×2 (Send исчез после пересоздания).
- `wiki`, сценарий, 708a595: 4/5; провал — `TYPE_TEXT «Search Wikipedia» s1 → DONE → DONE → unconfirmed`.
- Ложный `done` в режиме цели: 2 прогона `search-remount` (goal), последнее решение DONE conf 0,35 и 0,32, DOM: «чат
  открыт, сообщение не отправлено».
- Предварительная кривая `step_done` (прокси-метка «шаг закрылся следующим решением», без разделения по операции):
  ≥0,8 — 137/137; 0,7–0,8 — 25/30; 0,6–0,7 — 16/59; 0,5–0,6 — 13/32; <0,5 — 10/213 (кроме текстовых шагов, закрытых
  кодом). Уверенность действий: <0,3 — 7/41 решений в успешных прогонах; 0,3–0,4 — 5/26; ≥0,5 — 1918/2071.

### 1.3. Устройство, от которого зависит план

- `cdp.py`: один websocket, лок вокруг send/recv, события читаются только внутри `call()` (`_call_locked`,
  `:130-146`) и складываются в `deque(maxlen=200)`; подписок нет. `Network.*` события не приходят, пока домен не включён.
- Сессии: `Target.attachToTarget(flatten)` в `chrome.py:343` (своя) и `:422` (пользователя); `Tab.setup()`
  (`browser.py:269`) — место для `Network.enable`.
- Успокоение — один `Runtime.evaluate awaitPromise` (`browser.py:59-114`); пока он ждёт, `_call_locked` обрабатывает
  входящие события — счётчики сети в Python обновляются во время ожидания.
- Тесты успокоения: `tests/settle_harness.js` (node, виртуальное время, кадры 16 мс), `tests/fake_cdp.py`.

## 2. Алгоритм «страница готова к решению» (без фиксированных мс)

Эпоха ожидания — `seq` последнего принятого CDP-сообщения в момент отправки команды действия (`Input.*`, `Page.navigate`).

Готово, когда одновременно:

1. **Кадры.** Прошло ≥ 1 кадра после действия (обработчики и микрозадачи отработали) и **2 кадра подряд без значимых
   мутаций** (`SETTLE_ATTRIBUTES`, `significant` — как сейчас). Обоснование: эффекты обработчика ложатся к следующему
   кадру, второй кадр ловит цепочку rAF/commit фреймворка. Единица — кадр страницы, не мс. Без кадров (фон без focus
   emulation) — 2 хода `MessageChannel` без мутаций.
2. **Сеть.** Нет незавершённых запросов вкладки, начатых после эпохи: `requestWillBeSent` (+), `loadingFinished` /
   `loadingFailed` / `requestServedFromCache` (−). Не считать `type ∈ {WebSocket, EventSource}` и ответы
   `text/event-stream` (по `responseReceived.mimeType`). Запросы, начатые до эпохи, — фон.
3. **Документ.** `document.readyState === 'complete'` (после навигации), `document.fonts.ready` разрешён.
4. **Анимации.** `document.getAnimations()` с конечной длительностью — дождаться `finished` (меню, переходы);
   бесконечные (спиннеры) не ждать.
5. **Занятость.** Нет видимых `[aria-busy="true"]`.
6. **Комбобокс.** После ввода видимы `[role=option]` — готово сразу (как сейчас).
7. **Предохранитель** (§0.1): истёк — вернуть `reason: fuse`, запросы, оставшиеся в полёте, пометить фоновыми.

Цикл (Python, `Tab.await_ready(action)`): JS-промис «п. 1, 3–6» → по возврату проверить п. 2 по счётчикам → есть
незавершённые → `client.wait_events(session, until=pending==0, timeout=fuse)` → снова JS-промис (ответ мог изменить DOM)
→ пока оба условия не выполнятся в одном проходе или предохранитель. Каждая итерация заканчивается событием, не таймером.

Слепое пятно: таймеры страницы (`setTimeout` без сети, как `remount` стенда и догрузка WhatsApp по WS). Их закрывают
инварианты сценария (§6.1), второй взгляд и WAIT Jev по видимому спиннеру — не время.

Оставшиеся ожидания:
- **WAIT Jev / WAIT в проверке / второй взгляд** — `Tab.await_change()`: ждать *следующее* событие (значимая мутация,
  завершение запроса, конец анимации, WS-кадр — как подсказка «сейчас что-то изменится»), затем `await_ready`; предохранитель.
- **Пустая страница до первого вызова Jev** — ждать мутацию, после которой в снимке есть интерактивные элементы;
  предохранитель — дедлайн прогона. После первого вызова — не ждать.
- **Навигация** — `Page.enable` + `Page.loadEventFired`/`Page.frameNavigated` вместо опроса `readyState` каждые 20 мс,
  затем `await_ready`.

## 3. Порядок и пакеты

```
сейчас, параллельно:  §8 замер WhatsApp (основная сессия) | §6.3 калибровка по старым трассам | §9.1 замер «было» (оси)
контракт §4 (один коммит в feat/waits) → три worktree без пересечения файлов:
  «события»  feat/waits-events : cdp.py, browser.py, tests/test_cdp.py, tests/test_browser.py, tests/settle_harness.js, tests/fake_cdp.py
  «сценарий» feat/waits-agent  : agent.py, model.py, questions.py, scripts/calibrate.py, tests/test_agent.py, tests/test_model.py, docs/calibration.md
  «стенд»    feat/waits-stand  : tests/fixtures/app.html, scripts/eval.py, scripts/sweep_report.py, tests/test_eval.py, README (раздел стенда)
слияние в feat/waits: события → сценарий → стенд → §9.2 «стало» → §9.3 живая проверка → PR в main
```

Критерий готовности пакета — в конце его раздела. Каждый шаг — отдельный коммит; откат — `git revert <sha>` (пакет целиком
— удалить worktree и ветку, `feat/waits` не тронут).

## 4. Контракт (первый коммит в `feat/waits`)

4.1. `config.py`: `@dataclass Thresholds` — `step_done_min_p=0.7`, `done_step_done_min_p=0.5`, `min_action_confidence=0.3`,
`done_min_confidence=0.5` (новый, режим цели, §0.5; до калибровки — заглушка), `wait_fuse_s=1.5` (текущий потолок, до
§7.4); env `BROWSER_HANDS_*` не добавлять. Комментарий у каждого: «значение — docs/calibration.md §N». `Settings.thresholds`.
`agent.py` читает из `Thresholds`, старые имена констант — алиасы (тесты не трогать).
- Проверка: `uv run --locked pytest -q tests/test_config.py tests/test_agent.py` → зелёные, число тестов не меньше 105+.
- Откат: revert.

4.2. `browser.py` — сигнатуры без реализации (тело = сегодняшнее поведение):
`Tab.await_ready(action) -> dict` (= `settle`), `Tab.await_change() -> dict` (= `_sleep(0.1)` + `settle`),
`Tab.field_values(specs: list[{node, label}]) -> list[str | None]` (один `Runtime.evaluate`: значение поля по узлу из
кэша, иначе первого редактируемого поля с той же подписью; реализовать сразу — чистое чтение, нужно пакету «сценарий»).
- Проверка: `uv run --locked pytest -q tests/test_browser.py`; новый тест `field_values` на `FakeCDPServer`.

4.3. `types.py`: `Step.wait_reason: str | None` (`quiet|fuse|options|frames|change`), `Step.pending_requests: int`;
`tests/fakes.py` — те же поля. `eval.py` их и так сериализует через `result_to_json` (проверить — `scripts/eval.py`, не проверено).
- Проверка: `uv run --locked pytest -q tests/test_contract.py tests/test_server.py`.

4.4. Worktree: `git worktree add ../browser-hands-waits-events -b feat/waits-events feat/waits` (и `-agent`, `-stand`).
- Проверка: `git worktree list` → три новых.

## 5. Пакет «события» (`feat/waits-events`)

5.1. `cdp.py`: учёт сети и насос событий.
- `self.seq` — счётчик принятых сообщений; `self.network: dict[session_id, NetworkState]` (`pending: dict[requestId,
  (seq, type)]`, `background: set[requestId]`). `_on_event`: `Network.requestWillBeSent/loadingFinished/loadingFailed/
  requestServedFromCache/responseReceived` — обновить; `Network.*` в `self.events` не класть.
- `wait_events(session_id, until: Callable[[], bool], timeout) -> bool`: под локом читать кадры без отправки команды,
  каждое — в `_on_event`, ответы с чужим `id` — DEBUG; `until()` → True; таймаут → False. `TabGone` — как в `call`.
- `pending_since(session_id, seq) -> int`, `mark_background(session_id)`.
- Проверка: `tests/test_cdp.py` на `FakeCDPServer`: события во время `call` и в `wait_events` меняют `pending`; WS и
  event-stream не считаются; `wait_events` возвращает по предикату и по таймауту. `uv run --locked pytest -q tests/test_cdp.py`.

5.2. `browser.py`: `Tab.setup()` — `Network.enable` (`maxTotalBufferSize`/`maxResourceBufferSize` минимальные — имена и
эффект **не проверены**, зонд §5.5) и `Page.enable`; `release()` — `Network.disable`, `Page.disable` (вкладка пользователя
не должна остаться с включёнными доменами). `Tab.act`/`navigate` запоминают `self._epoch = client.seq` перед командой.
- Проверка: тест «между двумя вызовами Jev во вкладке пользователя — только чтение» (`test_browser`/`test_agent`, есть)
  дополнить `Network.enable/disable` в разрешённые; `release` шлёт `disable` до `detach`.

5.3. `browser.py`: `READY` JS вместо `SETTLE` — промис по §2 п. 1, 3–6: rAF-цикл, `frames_quiet ≥ 2`, `fonts.ready`,
конечные анимации через `Promise.all(a.finished)`, `aria-busy`, опции комбобокса; параметры `{action, fuse_ms, attributes}`,
итог `{reason, ms, mutations, frames}`. `Tab.await_ready(action)` — цикл §2 с `wait_events`. `Tab.await_change()` —
JS-промис «первая значимая мутация или конец анимации» + `wait_events(until=любое сетевое завершение или WS-кадр)` —
что раньше; затем `await_ready`. `SETTLE_*_MS` удалить; `wait_fuse_s` из `Thresholds` через `Tab.fuse_s`.
- Проверка: `tests/settle_harness.js` — сценарии: статичная страница → `quiet` за 2 кадра (≈32 мс виртуальных, было
  ≈228); мутация на 5-м кадре → `quiet` через 2 кадра после неё; бесконечная анимация → игнорируется, `quiet`; конечная
  анимация 300 мс → ждёт её `finished`; `aria-busy` → ждёт снятия; без кадров → `frames` через 2 хода `MessageChannel`;
  предохранитель → `fuse`. `uv run --locked pytest -q tests/test_browser.py`; `node --check browser_hands/snapshot.js`.

5.4. `browser.py`: навигация и повтор снимка — `navigate()` ждёт `Page.loadEventFired` через `wait_events` (предохранитель
`NAVIGATE_TIMEOUT_S`), затем `await_ready({"kind":"load"})`; `observe()` при `StalePage` — повтор после `await_change()`
вместо `sleep(0.02)`, `STALE_RETRIES` остаётся счётчиком.
- Проверка: `test_browser` — порядок кадров: `Page.navigate` → событие `loadEventFired` → `Runtime.evaluate` READY.

5.5. Зонд на настоящем headless Chrome (бесплатно, ≤2 мин, мак): `BROWSER_HANDS_CHROME_TESTS=1 uv run --locked pytest -q
tests/test_browser.py -k chrome` — дополнить: `Network.enable` с буферами → нет ошибки; страница с `fetch` к локальному
серверу с паузой 700 мс → `await_ready` возвращает после ответа (`pending` 1 → 0), `reason: quiet`; long-poll (пауза 10 с)
→ `fuse`, второй `await_ready` не ждёт его (фон).
- Ожидание: три `ok`. Не прошёл `Network.enable` с параметрами — параметры убрать, записать в core-notes.

5.6. `docs/core-notes.md`, раздел «Ожидания по событиям»: что сигналы, цена (вызовов CDP на действие: было 1 settle,
стало 1–3), слепое пятно таймеров.

Готовность пакета: `bash scripts/check.sh` → 0; harness-сценарии 5.3 зелёные; зонд 5.5 три `ok`; в коде `browser.py` нет
констант `*_MS` кроме `fuse` из конфига (`grep -n "_MS" browser_hands/browser.py` → пусто).

## 6. Пакет «сценарий и пороги» (`feat/waits-agent`)

6.1. Инварианты сценария (находка 1).
- `self._invariants: list[Invariant(step_no, node, label, text)]` — добавляется в `_advance("text typed")`; **снимается**,
  когда закрыт более поздний шаг с подтверждением Jev (`step_done`/DONE): его видимое свидетельство означает, что текст
  использован (Send очистил поле). Инварианты шагов, закрытых кодом после последнего подтверждённого, — живые.
- Перед каждым действием (`_act`, до `tab.act`) и перед `_Stop("done")`: `values = tab.field_values(specs)`; для каждого
  живого инварианта `matches(value, text)` (§0.3). Нарушен инвариант шага j → пометка в истории
  `"text of step j vanished (field re-rendered)"`, `self._scenario_no = j`, `self._scenario_done = j − 1`,
  `_step_actions` шага j = 0, `self._vanished[j] += 1` (> `RETYPE_LIMIT` → `blocked` «step j of M: typed text does not
  stay in the field»); решение отброшено без мутации, следующий тик спрашивает Jev под шаг j. INFO: «шаг k: текст шага j
  пропал — возвращаюсь к шагу j (n-й раз)».
- Перед финальным `done`, если последний шаг закрыт кодом: `tab.await_change()` с предохранителем **не ждать событие
  зря** — здесь ждём `await_ready` (сеть после ввода: подсказки, автосохранение) и повторяем проверку инвариантов;
  нарушен → откат как выше.
- Проверка (`tests/test_agent.py`, новый блок «инварианты»): remount между шагами 3 и 4 (`FakeTab.typed` очищается по
  сигналу) → повтор ввода, затем Send, `done`, `scenario_done == 4`; снятие инварианта после подтверждённого Send (поле
  пусто — не откат); третий пропад → `blocked`; финальная проверка на последнем текстовом шаге. Старый
  `test_text_that_never_stays_in_the_field_is_blocked_after_two_retypes` — тот же исход через новый путь.

6.2. Находки 2–5 и режим цели.
- (2) `typed_fields` (`agent.py:93-104`): запасной поиск — `kind == "fill"` и та же подпись, роль не требовать; перед
  пометкой `TEXT_VANISHED` — одно `tab.await_change()` + `observe()` и повторная проверка. Тест: поле
  `searchbox` заменено на `combobox` с той же подписью → шаг закрыт; поле появилось после второго снимка → закрыт.
- (3,5) `shows_text` (`agent.py:117-125`) → `matches(value, text)`: префикс после `_spaces`; запасной — префикс после
  `isalnum`-фильтра (обе стороны). Тесты: маска `+7 (777) 123-45-67` против `77771234567`; эмодзи-картинка; «текст есть в
  середине чужого значения» → False.
- (4) `_unconfirmed` (`agent.py:491`): перед вынесением `tab.fresh(page)`; устарел → `StalePage` (переснимок, ещё один
  вопрос проверки; `CHECK_WAITS` считает как прежде). Тест: устаревшая страница на втором DONE → не `unconfirmed` сразу.
- Режим цели (§0.5): DONE с `confidence < done_min_confidence` → `_look_again(page, "DONE conf …")` один раз; второй —
  `unconfirmed` («goal probably done, not confirmed — check the screenshot»); после TYPE_TEXT в режиме цели — мягкий
  инвариант: поле пусто на следующем снимке до Submit → `note` в истории. Тест: сценарий трассы (DONE 0,32 при пустом поле)
  → `unconfirmed`, не `done`; сервер и CLI уже знают `unconfirmed` (`server.py:419`, `cli.py:28`) — текст ответа для
  режима цели добавить, тест `test_server`.
- `_await_interactive` (`agent.py:397-421`): до первого вызова Jev — `tab.await_change()` в цикле до появления интерактивных
  элементов, предохранитель — дедлайн; после первого — сразу решать. Тест `test_scenario_waits_for_an_empty_page_before_asking_jev`
  обновить; «form: Thanks без элементов» (core-notes:519) — нет ожидания 1 с.
- WAIT Jev и WAIT в проверке — через `tab.await_change()` (без `sleep(0.1)`); второй WAIT в проверке по-прежнему `unconfirmed`.
- README и `docs/core-notes.md`: правила инвариантов и статусов.
- Проверка: `uv run --locked pytest -q tests/test_agent.py tests/test_model.py tests/test_server.py`; снимок запроса
  режима цели `tests/snapshots/jev_goal_request.json` без изменений.

6.3. Калибровка порогов — `scripts/calibrate.py` (офлайн, без моделей; можно сразу, по старым трассам).
- Вход: `--traces` (glob, по умолчанию `traces/*.jsonl` + `~/Personal/browser-hands*/traces/eval-*.jsonl`), `--out docs/calibration.md`.
- Пары «оценка → правда»:
  1. `step_done` (решение под шаг k): правда — для новых трасс (§7.2) состояние отчёта страницы в момент `t_ms`
     (открыт ли чат, есть ли сообщение, что в поле); для старых — прокси «следующее решение уже под шаг > k или прогон
     `done` и verified», исключая решения `TYPE_TEXT` на текстовых шагах (закрыты кодом). Отдельно для операций
     DONE и не-DONE (порог `done_step_done_min_p` — только по DONE).
  2. `confidence` CLICK/TYPE_TEXT/SELECT: правда — «действие полезно»: `page_changed` и в следующие ≤2 решения шаг
     продвинулся (сценарий) / прогон verified и после действия не было `note` (цель). Прокси — записать как прокси.
  3. `confidence` DONE в режиме цели → verified.
- Выход: таблица по θ с шагом 0,05 — n, TPR, FPR, precision, Wilson 95 %; рекомендация θ* = минимум стоимости
  `FP·C_fp + FN·C_fn`, где ложное закрытие/действие стоит прогон (C_fp = 1), ложный отказ — один лишний вызов Jev
  (C_fn ≈ 0,05: $0,0002 и ~0,7 с против $0,001 и ~7 с прогона). Мало данных (n < 30 в двух корзинах вокруг θ*) — вывести
  «диапазон допустимых θ [a; b], оставить текущий» и сколько прогонов стенда собрать (по ширине Wilson).
- Предварительно (§1.2): `step_done_min_p` — 0,7–0,8 (0,7–0,8: 25/30); `done_step_done_min_p` 0,5 — данных по DONE в
  0,5–0,7 мало; `min_action_confidence` — 0,3–0,5 (0,3–0,4: 5/26 в успешных) — пересчитать скриптом с честными метками.
- `docs/calibration.md`: дата, команда, список трасс с числом прогонов, таблицы, выбранные θ, расчёт `wait_fuse_s` (§7.4).
  `config.py` — значения из файла, комментарий-ссылка. `tests/test_docs.py`: каждое значение `Thresholds` встречается в
  `docs/calibration.md`. `tests/test_calibrate.py`: синтетические трассы → ожидаемые θ; мало данных → «оставить текущий».
- Проверка: `uv run --locked python scripts/calibrate.py --traces '../browser-hands*/traces/eval-*.jsonl'` → таблицы,
  строка `recommended:` по каждому порогу; `pytest -q tests/test_calibrate.py tests/test_docs.py`.
- Откат: значения по умолчанию в `Thresholds` вернуть, файл остаётся как замер.

Готовность пакета: `check.sh` → 0; `grep -nE "= 0\.[0-9]" browser_hands/agent.py` → только алиасы на `Thresholds`;
`docs/calibration.md` есть и согласован с `config.py` (test_docs).

## 7. Пакет «стенд» (`feat/waits-stand`)

7.1. `app.html`: `net=1` — результаты поиска через `fetch('/api/search?q=…&delay=DELAY')`, подтверждение Send через
`fetch('/api/send?delay=SENDDELAY')` (серверная пауза в `FixtureServer`, ответ JSON); `net=0` — таймеры как сейчас.
`remount` — список (`remount=800,1600` — несколько пересозданий, после §8), таймер. Статус исходящего — разметка как у
WhatsApp по итогам §8 (`data-icon`, `role`, `aria-label`), пока — как есть. В `state()` добавить `composer` (текст поля).
- Проверка: `uv run --frozen python scripts/eval.py --fixtures-only` → все задачи `ok`, в `check_remount` два пересоздания
  при `remount=800,1600`; тайминги в `check_*` считать от параметров, не литералов.

7.2. `eval.py`: история отчётов — `ReportStore.put` хранит список `(monotonic, report)`; строка прогона получает
`reports: [{t_ms, openChat, query, sent, composer}]` относительно того же старта, что `t_ms` решений (передать `started`
из `record_decisions` в `run_all`). `seen`/`chosen` без изменений. Строка результата — `wait_reason`/`pending_requests` шагов.
- Проверка: `tests/test_eval.py` — отчёты с временем в строке; `--fixtures-only` даёт ≥3 отчётов на прогон.

7.3. Развёртка: `eval.py --sweep axes|grid --delay-range 0:3000:500 --remount-range 0:3000:500 --sendstatus 0,1500
--net 0,1 --runs 5 --mode goal|scenario`; ячейка — параметры в `start_url` и в строке (`params: {...}`); прогоны по кругу
(ячейка 1 всех, потом 2…), `--max-cost`. `scripts/sweep_report.py <jsonl…>` → markdown: по оси — успех k/N с Wilson,
медиана и p95 `elapsed`, `wait_ms`; две колонки «было/стало», если даны два файла; ASCII-кривая.
- Оси (§0.7): delay 0…3000/500 при remount 0; remount 0…3000/500 при delay 700; sendstatus {0,1500} × remount {0,1600};
  net {0,1} на delay-оси. ≈18 ячеек × 2 режима × 5 = 180 прогонов, ≈$0,21, ≈25 мин.
- Проверка: `tests/test_eval.py` — разбор диапазонов, число ячеек, отчёт по синтетическому JSONL; `--sweep axes --runs 1
  --max-cost 0.01 --tasks search-remount` (платно, ≈$0,02, ≈3 мин) → строки с `params`.

7.4. Предохранитель из данных: `sweep_report.py --fuse` — по `reports` и `t_ms` считает распределение «действие →
последняя значимая мутация» (стенд) и берёт замер §8; p99 + запас кадра → `wait_fuse_s`, запись в `docs/calibration.md`.
- Проверка: команда печатает p50/p90/p99 и рекомендуемое значение.

7.5. README (раздел «Разработка»): убрать «0,9 с», описать `--sweep`, `net`, `remount=список`; `tests/test_docs.py`
— README содержит `REMOUNT_MS`-значение по умолчанию (или не называет число).

Готовность пакета: `check.sh` → 0; `--fixtures-only` → все `ok`; `--sweep axes --runs 1 --max-cost 0.01` даёт JSONL и
`sweep_report.py` строит таблицу.

## 8. Замер настоящего WhatsApp (только основная сессия с пользователем; бесплатно; без моделей и отправки)

8.1. `scripts/observe_whatsapp.py` (новый, только чтение UI): `Chrome.find_user_tab("https://web.whatsapp.com/")` →
`attach_tab` (без навигации), `Network.enable`; `Runtime.evaluate` ставит `MutationObserver` на `document.body`, пишет в
`window.__bhObs` (как `__jevFast`; снять в конце): `t`, тип, путь цели (`tag#id.class[role][aria-label][data-icon]
[data-testid]`), добавлено/удалено; отдельно — идентичность поля сообщения (`[contenteditable][role=textbox]`, подпись
«Type a message»): каждое исчезновение/появление узла с меткой времени; элементы «HH:MM» у сообщений (`tag`, `role`,
кликабельность), `span[data-icon]` (значения). Python: события/с по типам, незавершённые запросы после клика, WS-кадры/с.
- Ход: 1) подключиться, наблюдатель включён; 2) скрипт кликает строку чата «Рабочий» из снимка (`Tab.act`, единственная
  мутация состояния; ничего не печатать и не отправлять); 3) писать 15 с; 4) снять наблюдатель, `Network.disable`,
  `release()`; 5) JSON в `traces/wa-observe-<ts>.json` и сводка: сколько раз и когда заменилось поле, разметка статусов,
  сеть.
- Проверка: `uv run --env-file .env python scripts/observe_whatsapp.py` → сводка на экране, файл есть; после — вкладка
  пользователя без `__bhObs`/`__jevFast` (`Runtime.evaluate` в конце скрипта печатает `false`).
- Откат: скрипт ничего не меняет; при ошибке — `release()` в `finally`.

8.2. Как ляжет в стенд: времена пересозданий → диапазон `remount` и значение по умолчанию (список); разметка статусов →
`meta()` в `app.html` и регулярки `STATUS_*` в `eval.py`; поток событий → решение §0.2; наличие XHR после открытия чата
→ нужен ли `net=1` для задачи `search-remount`.

## 9. Замеры и слияние

9.1. «Было» (до слияния пакетов, на `feat/waits` f3c6008, стенд из `feat/waits-stand` — `--sweep` в ядро не лезет):
`uv run --env-file .env python scripts/eval.py --sweep axes --runs 5 --mode scenario --label before-scn` и `--mode goal
--label before-goal`; где — по §0.8 (`hp run` или мак в фоне). ≈$0,21, ≈25 мин.
- Ожидание: `search-remount` при remount ≥ 1200 в сценарии ≤ 2/5, при remount 0 — 5/5; delay-ось без провалов.

9.2. Слияние в `feat/waits`: события → сценарий → стенд (файлы не пересекаются; конфликт только в `core-notes.md` —
разделы разные). После каждого: `bash scripts/check.sh` → 0, `pyright` → 0, `--fixtures-only` → `ok`.
«Стало»: те же две команды с `--label after-*`; `sweep_report.py before.jsonl after.jsonl` → таблица в `docs/decisions.md`.
- Критерий: `search-remount` сценарий ≥ 4/5 на всей remount-оси; остальные задачи не хуже «было»; медиана `elapsed`
  не выше «было» + 0,5 с (ожидание по кадрам должно её снизить: 2 кадра вместо 200 мс тишины на действие).
- Не прошёл — не сливать в main; разобрать по `wait_reason`/`reports`.

9.3. Живая проверка (основная сессия, платно, ≈$0,01): `live_wikipedia.py` цель и `--scenario` → `done`; во вкладке
пользователя WhatsApp — сценарий из `traces/wa-steps-send.json` с текстом-проверкой, один раз, после согласия
пользователя; смотреть: пересоздание поля → откат к шагу и повтор, статус `done`, `wait_reason` шагов.

9.4. Документация: `docs/core-notes.md` (события, инварианты, калибровка), `docs/decisions.md` (решения §0 и таблица
«было → стало»), README. PR `feat/waits → main` — после «стало» и живой проверки; merge — спросить пользователя.

## 10. fix2: что сохранить, что переделать

Сохранить: статус `unconfirmed` и режим проверки (form 26.09 — работает); порог уверенности действий как механизм
(значение — калибровка); проверка «текст шага в поле» как идея; «(done when …)» в `do`; стенд `search-remount`
(параметры — в развёртку); f3c6008 (DONE на шаге с текстом не закрывает шаг — согласуется с инвариантами); `_refresh_page`.

Переделать: `shows_text` подстрока → префикс/буквы-цифры (§6.2); `typed_fields` без роли; `_unconfirmed` со свежестью;
`_vanished`/`RETYPE_LIMIT` → инварианты с откатом к шагу (§6.1); `CHECK_WAITS` — ожидание события; `REMOUNT_MS=1600` —
точка по умолчанию, не единственная; README «0,9 с».

## 11. Риски

- `Network.enable` во вкладке WhatsApp: поток WS-кадров и буфер ответов в Chrome (память). Замер §8 до решения §0.2;
  запасной путь — сеть только в своей вкладке.
- Long-poll после действия держит ожидание до предохранителя один раз за прогон (§0.1); без потолка — до дедлайна.
- «2 кадра тишины» короче 200 мс: страницы, где обработчик ставит `setTimeout(…, 50–150)` без сети, будут сниматься
  раньше — компенсируют WAIT Jev и второй взгляд; кривая delay-оси стенда (`net=0`) это покажет.
- Фоновая вкладка без кадров: focus emulation уже включена (`browser.py:282`); если Chrome всё же не даёт rAF — путь
  `MessageChannel` (harness проверяет).
- События читаются только под локом: `wait_events` держит лок так же, как сегодняшний `settle`; `close()` из другого
  потока по-прежнему рвёт `recv` (`cdp.py:182`).
- Прокси-метки в старых трассах смещены (закрытие кодом ≠ `step_done`); скрипт печатает долю прокси и не выдаёт порог
  при n < 30.
- Инварианты снимаются по подтверждению Jev более позднего шага: ложное подтверждение снимет инвариант зря — цена та же,
  что сегодня (шаг закрыт ошибочно), не хуже.
- `Runtime.addBinding`/persistent observer не используем — не нужно проверять поведение подписок с синхронным клиентом.

## 12. Что не делать и почему

- Не подменять `setTimeout`/`setInterval` страницы, не ставить `Page.addScriptToEvaluateOnNewDocument` — запись в
  страницу пользователя; правило «только чтение» дороже полноты сигнала.
- Не подбирать `remount`/`delay` под одну точку и не «чинить» стенд под агента — точка 1600 остаётся, критерий — кривая.
- Не менять пороги руками «на глаз» — только через `calibrate.py` и запись в `docs/calibration.md`.
- Не запускать развёртку и живой WhatsApp из субагентов — платно/вкладка пользователя; только основная сессия.
- Не трогать `snapshot.js` без нужды (тело запроса режима цели зафиксировано снимком).
- Не вводить новые env-переменные для порогов — их меняет расчёт, не оператор.
- Не сливать в `main` до «стало» и живой проверки.
