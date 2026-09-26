# Ядро (Пакет 1): заметки для README и сервера

Дата: 2026-09-25. Ветка `feat/core`. Что проверено живьём — только режим `launch` + headless + временный профиль
(`scripts/live_wikipedia.py`). Режим `attach` живьём не запускался (окно «Разрешить» — шаг 3.4 плана, основная сессия).

## Живые прогоны: Wikipedia → «Gödel's incompleteness theorems»

Команда (из клона): `uv run --frozen --env-file <путь к клону>/.env python scripts/live_wikipedia.py [--verbose]`.
Chrome 154 arm64, macOS, headless, новый временный профиль на каждый прогон.

| # | статус | elapsed, мс | model / text / browser / wait, мс | шаги | вызовы моделей | стоимость |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | done | 6558 | 2478 / 1355 / 1048 / 1633 | 2 | 6 (5 Jev + 1 текст) | $0.00127 |
| 2 | done | 10102 | 2663 / 1229 / 927 / 5234 | 2 | 7 (6 + 1) | $0.00145 |
| 3 | done | 7367 | 2363 / 1102 / 331 / 3529 | 2 | 6 (5 + 1) | $0.00130 |

Шаги во всех трёх: `1. TYPE_TEXT 'Search Wikipedia' — "Gödel's incompleteness theorems"`, `2. CLICK 'Search'`; итоговый url
`https://en.wikipedia.org/wiki/G%C3%B6del%27s_incompleteness_theorems`, скриншот — JPEG 1120×780, ~108 КБ (quality 60).
Запуск Chrome и подключение — ~410 мс, в `elapsed_ms` не входят (сервер держит Chrome между вызовами).

Прогон 1 — до того, как снимок, прерванный навигацией, стал считаться `wait_ms` (отсюда `browser` 1048 мс).
Прогон 2 — первая загрузка Википедии в новом профиле заняла ~5 с (`wait` 5234 мс).

Что видно из замеров:

- `elapsed_ms` — весь `browse`: вкладка + навигация + цикл + финальный скриншот + закрытие. У источника 2,8 с — только цикл
  после первого снимка, на прогретом профиле; сравнивать напрямую нельзя.
- Jev: 0,39–0,72 с на вызов, ~$0.00025 за вызов (6–7 тыс. входных токенов). `usage.cost` приходит всегда.
- Текстовая модель `inception/mercury-2.5` (`reasoning: {"enabled": false}`): 1,1–1,4 с на вызов, `usage.cost` приходит
  (~$0.00004; в теле запроса `"usage": {"include": true}`), `reasoning_tokens: 0`. Пометка «cost (Jev only)» не нужна:
  `RunResult.cost` = Jev + текст.
- CDP-вызовы сами по себе — 0,3–25 мс (снимок `snapshot.js` на Википедии — 8–50 мс). Всё, что дольше, — загрузка:
  после клика по ссылке/submit Chrome держит `Runtime.evaluate` ~1 с, пока грузится новый документ; такие попытки
  (`StalePage`) идут в `wait_ms`.
- Лишние решения Jev (кандидаты на оптимизацию после bench, сейчас не трогал — §9/§11 плана):
  1. первое решение часто устаревает: Википедия перестраивает поле поиска JS-ом после `readyState=complete` (−0,4–0,7 с);
  2. после клика `Search` Jev 1–2 раза выбирает `WAIT`, пока страница ещё меняется; решение отбрасывается как устаревшее
     (по ~0,4 с каждое). Лечится ожиданием «тихого» DOM после навигации — это изменение правил ожидания источника.

## Для README (Пакет 2)

- Скорость: Wikipedia-задача — 6,6–10,1 с на весь `browse` в headless launch с холодным профилем; модели (Jev + текст) — 40–60 % времени, остальное в основном загрузка страниц (`wait_ms`).
- Стоимость: ~$0.0013–0.0015 за такую задачу (5–6 вызовов Jev + 1 текстовый).
- `DONE` ≠ успех: итог проверять по скриншоту и url.
- Попапы (`target=_blank`) и новые вкладки агент не видит; frames и shadow DOM вне снимка (как у источника).
- Если свою вкладку не удалось закрыть (обрыв соединения, Chrome не ответил), `Chrome.connect()` при следующем вызове
  закрывает её по `targetId` — и при живом соединении (если Chrome тот же). `close()` в attach закрывает свои вкладки
  (≤1 с на каждую), затем ws; вкладки пользователя не трогаются никогда. После `kill -9` сервера — закрыть руками.
- `close()` в launch не ждёт идущий `browse`: закрывает ws (рабочий поток сразу получает `ChromeDisconnected`) и гасит
  процесс (terminate → wait → kill). Chrome запускается без `*_API_KEY` и `OPENROUTER_API_KEY` в окружении.
- TYPE_TEXT печатает только в проверенное поле: после клика один `Runtime.evaluate` сверяет `document.activeElement` с
  целью (или её contenteditable-потомком) и отсекает password/file/hidden; иначе `StalePage`, ничего не напечатано.
- launch с постоянным профилем: если Chrome от прошлого сервера ещё жив (сервер убит без `finally`, например SIGTERM без
  обработчика), `connect()` вернёт `ChromeUnavailable` «Профиль … занят Chrome (pid N) … kill N / --fresh-profile» —
  проверка по `SingletonLock` до удаления DevToolsActivePort. Серверу (Пакет 2) стоит превращать SIGTERM в `sys.exit`,
  чтобы `chrome.close()` успел погасить свой Chrome.
- `keep_open`: вкладка остаётся, сессия отсоединяется — эмуляция viewport 1120×780 снимается, вкладка выглядит обычной.
- Окно «Разрешить» в attach: при подключении `Chrome.connect()` пишет в stderr (WARNING) «если появится окно
  «Разрешить» — нажмите его»; отказ или 60 с без ответа → `ChromeUnavailable`. Живьём не проверено.

## Как вызывать ядро (сервер, CLI, bench)

```python
from browser_hands.agent import Agent            # импорт лениво, внутри функций
from browser_hands.chrome import Chrome, ChromeUnavailable
from browser_hands.model import ModelClients

chrome = Chrome(settings.browser)                # один на процесс
clients = ModelClients(settings.models)          # один на процесс
chrome.connect()                                 # ChromeUnavailable — текст для пользователя
threading.Thread(target=clients.warmup, daemon=True).start()   # один раз на процесс, ≤2 с, ошибки глотает
result = Agent(chrome, clients, url, goal, settings.run,
               screenshot_quality=settings.browser.screenshot_quality,
               screenshot_scale=settings.browser.screenshot_scale,
               cancel=cancel_event).run()        # threading.Event | None
# при выходе: chrome.close(); clients.close()
```

- `Agent(...)` бросает `ValueError` при пустом url, пустых goal и steps вместе, невалидном сценарии (`ScenarioError`) и
  `max_steps < 1`; `run()` — `ValueError`, если нет ключа Jev. Сценарий — `steps=parse_steps(raw)`, раздел «Сценарии».
  Всё остальное (CDP, модели, дедлайн, погибшая вкладка) — в `RunResult.status/error`, без исключений.
- Отмена: `cancel.set()` из другого потока. `cancel.is_set()` проверяется перед каждым вызовом модели и каждым действием;
  итог — `status="failed"`, `error="cancelled"`, без новых мутаций и без финального скриншота; вкладка закрывается (или
  остаётся при `keep_open`) как обычно. Идущий HTTP-запрос к модели не прерывается — отмена срабатывает после него.
- `RunResult.error` заполнен и для `blocked`/`step_limit` (причина: «Model chose BLOCKED…», «No page change after 3…»,
  «Stopped at max_steps=N»), а не только для `failed`/`timeout`.
- `RunResult.timing` — весь прогон: старт (вкладка, навигация, первый снимок) + шаги + хвост (решение DONE, отброшенные
  решения, финальный скриншот). `Step.timing`/`Step.cost` — всё с предыдущего шага, включая отброшенные решения.
  `model_calls` — все запросы к моделям (Jev + текст), в том числе неудачные.
- Логи ядра — логгеры `browser_hands.chrome`, `browser_hands.agent` (шаги, INFO: операция, метка, `len=N`, хост;
  текст и полный url — DEBUG), `browser_hands.model` (usage, DEBUG);
  обработчики не ставятся — это задача `logging.py`. В stdout ядро ничего не пишет; вывод Chrome — в DEVNULL.

## Живые прогоны после исправлений ревью (25.09.2026)

Та же команда и задача, launch + headless + временный профиль; цель — убедиться, что проверка фокуса перед вводом не
сломала TYPE_TEXT.

| # | статус | elapsed, мс | model / text / browser / wait, мс | шаги | вызовы моделей | стоимость |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | done | 8580 | 2393 / 1341 / 1901 / 2901 | 2 | 6 | $0.00120 |
| 2 | done | 8238 | 2599 / 1031 / 581 / 3975 | 2 | 7 | $0.00155 |

Шаги те же (`TYPE_TEXT 'Search Wikipedia'` — в логе INFO `len=31 @ en.wikipedia.org`, затем `CLICK 'Search'`).
Холодный профиль: сверх медианы bench (5236 мс) — в основном загрузка страниц (`wait`). `browser` 1901 мс в прогоне 1
не разбирал: шаг 1 — 327 мс, шаг 2 (клик с переходом) — 830 мс, остальное — старт и хвост; в прогоне 2 — 581 мс всего.
Проверка фокуса — один `Runtime.evaluate` на TYPE_TEXT.

## Вкладка пользователя (`feat/reuse-tab-core`, 25.09.2026)

План — `docs/plan-reuse-tab.md` §2, §4. Живьём режим attach к Chrome пользователя не запускался; всё ниже — офлайн-тесты и
свой headless Chrome 154 с временным профилем.

### Что шлём и чего не шлём

- Поиск (`Chrome.find_user_tab(url)`): только attach без `ws_url` и только для http(s)-адреса; один `Target.getTargets`
  (≤5 с). Кандидат: `type == "page"`, без `subtype`, http(s), тот же хост (без учёта регистра), не своя, `attached is False`.
  Несколько — точный url без `#fragment` (пустой путь = `/`), иначе первая по порядку. В INFO — хост и число кандидатов;
  все подходящие заняты — INFO «вкладка <host> занята другим клиентом» и своя вкладка.
- Подключение (`Chrome.attach_tab`): `Target.attachToTarget {flatten: true}` → `Emulation.setFocusEmulationEnabled true`
  → `Runtime.evaluate("[innerWidth, innerHeight, devicePixelRatio]")` (не удался — не ошибка: страница может грузиться).
  Ошибка attach/focus → `release()` (detach) и исключение: прогон `failed`, без тихого перехода в свою вкладку.
- Работа: те же `Runtime.evaluate` (снимок, свежесть, геометрия), `Input.*`; размер окна обновляется из каждого снимка
  (`w`/`h`) — точка колеса скролла внутри окна пользователя.
- Конец: финальный кадр, затем только `Tab.release()` — `setFocusEmulationEnabled false` (если включали) и
  `Target.detachFromTarget`, один таймаут на оба вызова; ошибки (`TabGone`, обрыв, молчание) — DEBUG.
- Никогда для вкладки пользователя: `Target.createTarget`, `Target.closeTarget`, `Target.activateTarget`, `Page.navigate`,
  `Page.reload`, `Emulation.setDeviceMetricsOverride`, `Target.setDiscoverTargets`. Защита в нескольких местах:
  `Tab.setup()` шлёт metrics только при `owned`; `Tab.close()` чужой вкладки = `release()`; `Tab.navigate()` чужой —
  `RuntimeError`; `Chrome.close()` сначала отпускает `_borrowed` (≤1 с на вкладку), `_close_targets` пропускает их id;
  `connect()` после обрыва забывает `_borrowed` (сессии мертвы); `attach_tab` не берёт свою вкладку и работает только в attach.
- Сквозной офлайн-тест `test_user_tab_end_to_end_on_fake_chrome_never_closes_navigates_or_resizes`: настоящие `Agent` и
  `Chrome` на фейковом CDP — в кадрах нет запрещённых методов, клики идут в сессию вкладки, в конце focus off → detach.

### Скриншот без изменения размера

`scale = min(screenshot_scale, 1120 / (cssVisualViewport.clientWidth × devicePixelRatio))`, clip — видимая область
(`pageX/pageY/clientWidth/clientHeight`). DPR снимается перед кадром заново. Окно уже ≤1120 физ. px — кадр как есть.

Замер (headless, `--force-device-scale-factor=2 --window-size=1728,1000`, innerWidth 1728, DPR 2):

| Как снимали | Картинка |
| --- | --- |
| без clip | 3456×1826 |
| clip.scale = 1120 / (1728 × 2) — формула плана | **1120×592** |
| clip.scale = 1120 / 1728 (без DPR) | 2240×1184 |
| эмуляция DPR 2 (`setDeviceMetricsOverride`), та же формула | 1120×648 |
| путь кода: `find_user_tab` → `attach_tab` → `Tab.screenshot()` | **1120×592**, 15,9 КБ |

Размер картинки = `clip.width × clip.scale × DPR` — подтверждено; `× dpr` в формуле нужен. Прокрученная страница
(scrollY 600/1500) снимается с нужного места. После кадров innerWidth/innerHeight/scrollY/url вкладки те же.

### Что проверено на своём headless Chrome (без Jev, бесплатно)

- `attached` в `Target.getTargets` = `true`, пока к вкладке прицеплена flatten-сессия другого CDP-клиента; `false` после
  его detach и после обрыва его ws. `find_user_tab` в первом случае вернул None, во втором — id вкладки.
- Focus emulation: фоновая вкладка `document.hasFocus()` false → включили — true → detach без выключения — false; обрыв
  ws — false. То есть Chrome сам снимает её при detach и обрыве; явное выключение в `release()` оставлено (дёшево).
  После `release()` у фоновой вкладки false, у активной true — как до подключения.
- `Tab.close()` + `Chrome.close()` на вкладке «пользователя»: вкладка жива, размер/url/скролл не изменились.
- Поля TargetInfo Chrome 154: `attached, browserContextId, canAccessOpener, targetId, title, type, url`.

### Ожидание элементов (решение 6)

`Agent._await_interactive()` — в `_predict` перед Jev: пока в снимке нет `fill|click|select` (scroll и wait не считаются),
`Tab.pause(0.25)` и переснимок, не дольше `EMPTY_PAGE_WAIT_S = 25 с` и дедлайна; отмена — сразу. Шаги и `model_calls` не
растут, время — `wait_ms`. Потолок считается от начала ожидания и переживает `StalePage`; вышел — Jev решает по пустой
странице (DONE/WAIT), и следующее ожидание снова до 25 с. Одна строка INFO «Нет элементов для действия, жду (host)».
Относится к любой вкладке. Следствие: страница без контролов после последнего действия («Спасибо, отправлено» без
ссылок) даёт DONE на ≤25 с позже.

### Живые прогоны (launch, headless, временный профиль; 3 платных)

| # | задача | статус | elapsed, мс | model / text / browser / wait, мс | вызовы | tab | стоимость |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Wikipedia | failed | 4922 | 671 / 1723 / 81 / 2420 | 2 | new | $0.00025 |
| 2 | Wikipedia | done | 7181 | 2463 / 1028 / 391 / 3270 | 7 | new | $0.00145 |
| 3 | кнопка через 3 с (data:) | done | 3993 | 736 / 0 / 95 / 3112 | 2 | new | $0.00012 |

1 — текстовая модель вернула не `{"text": …}` («Text helper returned no valid field value; nothing typed»), `model.py` в
этой ветке не менялся; ничего не напечатано. 2 — как раньше: `TYPE_TEXT 'Search Wikipedia'`, `CLICK 'Search'`, статья
открыта, кадр 1120×780, строки ожидания нет. 3 — «Нет элементов для действия, жду (data)», шаг 1 `CLICK 'Continue'` с
`wait 3085 мс`: Jev не вызывался, пока кнопки не было; затем DONE.

### Не проверено до живого attach (§7 плана)

- Кадр видимой вкладки в обычном (не headless) Chrome: `clip.scale` Chrome применяет временной эмуляцией на время
  снимка — возможна вспышка масштаба в окне пользователя; фоновая вкладка без override может прийти пустой или ждать до 5 с.
- `attached` при открытом DevTools пользователя (проверено только для второго CDP-клиента).
- Масштаб страницы (Cmd +/−) — `devicePixelRatio` его включает; формула на нём не мерилась.
- Окно «Разрешить» и реальный WhatsApp: экран загрузки без контролов, отсутствие «Использовать здесь».
- В странице пользователя остаётся `window.__jevFast` (кэш узлов снимка) до перезагрузки; не удаляем (лишний вызов в
  чужой странице при release) — вынести на решение, если важно.

## Надёжность (`feat/reliability-core`, 25.09.2026)

План — `docs/plan-reliability.md` §4, решения §0 приняты. Коммит на каждый пункт §4.1–4.7.

### Что сделано

- **Успокоение** (`browser.py`, `SETTLE`, `Tab.settle`): один `Runtime.evaluate` с `awaitPromise`: `MutationObserver` на
  `document.body`, значимые записи — `childList`, `characterData`, атрибуты `SETTLE_ATTRIBUTES` (без `style`) при реальной
  смене значения, цель не внутри `script/style/template/noscript`. Готово — ≥2 кадров rAF, ≥50 мс и 200 мс без значимых
  записей; потолок 1500 мс, но не дальше `дедлайн − 0,5 с` (места нет — не ждём). Итог `{reason, ms, mutations}` →
  `Tab.last_settle` и DEBUG «settle <reason> <ms> мс, мутаций N»; время — `wait_ms`. `reason`: `quiet` — тишина,
  `options` — видимые подсказки комбобокса после ввода (сразу, как раньше), `ceiling` — потолок, `frames` — тишина по
  запасному таймеру без кадров (фоновая вкладка). Где: `observe()` после любого действия, в том числе `WAIT` (100 мс +
  успокоение); `navigate()` после `readyState=complete`; второй шанс. Прервано навигацией — `None`, снимок как обычно.
- **Второй шанс** (`agent.py`, `_second_look`): свежий BLOCKED → успокоение (`kind: retry`) → переснимок → ещё один вызов
  Jev. Шаг не создаётся; в истории — «Wait for the page to update» с `page_changed`. Снова доступен только после действия
  с `page_changed=True`; ожидание самого шанса флаг не сбрасывает. Второй BLOCKED — `blocked`, «Model chose BLOCKED after
  a second look…». INFO — «BLOCKED: жду успокоения и спрашиваю ещё раз (<host>)».
- **NEXT_ACTION**: строка про WAIT — «…or results are still loading: right after typing a query or submitting, if the
  matching results or the sent message have not appeared yet, WAIT once.» Строки «Recent WAIT actions are not evidence…»
  и «Prefer a useful visible control over WAIT» не тронуты.
- **Текстовая модель** (`model.py`): `InvalidTextValue(ValueError)` с `reason` (`no-content | not-json | null | extra-keys
  | not-string | empty | too-long`) и ценой неудачного запроса; текст ошибки прежний. INFO — «Текстовая модель: невалидный
  ответ (<reason>), len=N»; DEBUG — «text model raw (N chars): …» (только `message.content`, до 500 символов).
  `PAGE_TEXT_FOR_TEXT = 2000`. `TEXT_VALUE`: явно заданное значение — дословно, без кавычек, двоеточий и тире из goal.
- **Повтор** (`agent.py`, `_field_text`): на `InvalidTextValue` — INFO «Повторяю запрос к текстовой модели (<reason>)» и
  второй запрос с тем же контекстом (до ввода, браузер не тронут). Отмена и дедлайн — перед каждым; `model_calls`,
  `text_ms`, цена — за оба. Нет ключа и сеть — без повтора.

Гарантии прежние: успокоение только читает страницу; вкладку пользователя не закрывает, не переводит, размер не меняет
(сквозной тест второго шанса на `FakeCDPServer` — между двумя вызовами Jev только `Runtime.evaluate`: проверка свежести,
успокоение, снимок); действия, меняющие страницу, не повторяются.

### Отклонения от плана

1. `Tab.cancel` (нет в плане): агент передаёт вкладке свой `cancel`; выставлен — успокоение не начинается. Идущее
   ожидание прервать нельзя (синхронный CDP-вызов) — отмена срабатывает не позже потолка 1,5 с.
2. В страницу уходит только `{kind, node}` действия (не метка и не значение поля).
3. Проверка JS в node — с фейковыми DOM, `MutationObserver` и виртуальным временем (`tests/settle_harness.js`, кадры по
   16 мс): бесконечная анимация (style каждый кадр + счётчик каждые 100 мс) → `ceiling` за 1500 мс ровно; незначимые
   мутации; комбобокс; фоновая вкладка без кадров; потолок по дедлайну; страница без `body`. Работает в CI. Настоящий
   headless Chrome — тот же сценарий, по `BROWSER_HANDS_CHROME_TESTS=1` (запускает Chrome — в `check.sh` по умолчанию
   не идёт).
4. Запись ожидания второго шанса попадает в историю до успокоения (`page_changed: None`) и обновляется после снимка: при
   `StalePage` запись остаётся, как у действий.
5. `RunResult.error` при двух невалидных ответах — «InvalidTextValue: Text helper returned no valid field value; nothing
   typed.» (было «ValueError: …»): текст после двоеточия прежний.

### Наблюдения

Настоящий headless Chrome 154 (бесплатно): страница с бесконечной анимацией и счётчиком — `ceiling 1501 мс, мутаций 15`,
вызов 1,504 с (три раза подряд одинаково); только style-анимация — `quiet 231 мс, мутаций 0`; статичная страница —
`quiet 228–232 мс` (200 мс тишины + выравнивание по кадру).

Настоящий Chrome + `Agent`, Jev подменён сценарием (бесплатно): кнопка, результат через 300 мс после клика. После клика
`settle quiet 215 мс, мутаций 0` — синхронная правка DOM в обработчике клика прошла до начала ожидания, результата ещё нет
(риск §7 плана); BLOCKED → второй шанс `quiet 285 мс, мутаций 1` → Jev видит результат, `done`.

`--fixtures-only` стенда здесь не запускался (стенд не влит). Ожидание §5.2 плана «`settle ceiling` для
`search-spinner`» под вопросом: спиннер шумит до результатов (700 мс), потом тишина — скорее `quiet` около 0,9 с;
`ceiling` будет, только если шум длится дольше 1,5 с.

### Живые прогоны (launch, headless, временный профиль; 4 платных)

`scripts/bench.py` (один Chrome на серию) — Википедия, «Gödel's incompleteness theorems»; DEBUG `browser_hands.browser`
(строки settle), в 4-м прогоне и `browser_hands.model`.

| # | статус | elapsed, мс | model / text / browser / wait, мс | вызовы | успокоение, мс (мутаций) | стоимость |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | failed | 5923 | 438 / 3104 / 107 / 2247 | 3 (1 Jev + 2 текст) | load 321 (21) | $0.00035 |
| 2 | done | 4968 | 1588 / 707 / 232 / 2415 | 5 (4 + 1) | load 279 (22), fill 213 (0), click 687 (7611) | $0.00114 |
| 3 | done | 4188 | 1593 / 758 / 229 / 1591 | 5 (4 + 1) | load 276 (23), fill 215 (0), click 359 (7605) | $0.00114 |
| 4 | done | 5526 | 1681 / 815 / 302 / 2700 | 5 (4 + 1) | load 342 (21), fill 213 (0), click 222 (0) | $0.00104 |

- Прогоны 1–3 — одна серия (`--runs 3`), 4 — отдельный процесс (`--runs 1`). Шаги: `TYPE_TEXT 'Search Wikipedia'`
  (`len=31`), затем `CLICK` подсказки (2, 3) или `Search` (4); статья открыта.
- Было (bench N = 5, `decisions.md`): медиана elapsed 5236, model 2604, wait 1304 мс, 5–6 вызовов Jev. Стало (3 `done`):
  медиана elapsed 4968, model 1593, wait 2415 мс, 4 вызова Jev. Успокоение добавило ~0,8–1,2 с `wait` на прогон
  (load 0,28–0,34 с, после ввода 0,21 с, после клика 0,22–0,69 с), но убрало 1–2 устаревших решения Jev (~1 с `model`).
  N мал — итог за замером §5 (N = 10).
- После клика с переходом успокоение иногда идёт уже в новом документе: 7600 мутаций — парсер вставляет статью, тишина
  через 0,36–0,69 с. Если вызов попал в старый документ — 0 мутаций, 0,22 с (4).
- Прогон 1: текстовая модель дважды вернула не JSON (`not-json`, `len=47` оба раза) — повтор не помог, ничего не
  напечатано. Сырой ответ не снят (DEBUG модели был выключен). Удачный ответ в прогоне 4 —
  `{"text": "Gödel's incompleteness theorems"}` (48 символов); что было в 47 символах — не проверено. Такой же
  сбой на первом прогоне — в прогоне 1 раздела «Вкладка пользователя» выше; риск §7 плана (одинаковый ответ на повтор)
  подтвердился один раз.

## Лимит запроса к моделям по часам (`feat/reuse-tab`, 25.09.2026)

### Факты (замер «было», ядро f2dcbaf, N = 10 на задачу)

Все неудачи стенда — от текстовой модели `inception/mercury-2.5` через OpenRouter, Jev ошибок не дал. 22 прямых вызова:

- 2 из 22 — после валидного JSON дописано «\n```»: исправлено раньше (`unfence_json`, b26ff20).
- 2 из 22 — HTTP 200 через ~120 с с телом `{"error": {"code": 504, "message": "Upstream idle timeout exceeded"}}`.
  `request_timeout_s = 25` и дедлайн прогона 60 с не сработали: прогоны шли 120–128 с. Таймаут httpx — на каждое
  чтение сокета, а не на весь запрос; OpenRouter, пока ждёт провайдера, держит соединение живым (пробелы в теле).
  Сырые байты этих ответов не сняты: механизм «пробелы» — вывод по времени, офлайн его воспроизводит заглушка
  `tests/fake_http.py`. С `openrouter.ai` httpx договаривается на HTTP/2 (HEAD без ключа, 25.09).

### Решения

- **Потолок по часам на весь запрос** (`model.py`, `ModelClients.post(..., limit)` и `_send`): одна граница на
  соединение, ожидание, тело и повторы 429/503/529 — меньшее из `limit` модели и остатка дедлайна прогона; вышел —
  `ModelTimeout` («Jev / Text model did not answer in N s»). Тело читается потоком (`send(stream=True)`, `iter_raw`):
  между кусками — сверка с часами, перед каждым чтением таймаут httpx = остаток (`request.extensions["timeout"]`).
  HTTP/2 httpcore перечитывает его при каждом чтении сокета — потолок точный; HTTP/1.1 берёт таймаут один раз на тело —
  если пробелы шли и оборвались, худший случай 2 × потолок. Опора на поведение httpcore 1.0.9 — её держит тест на
  h2c-заглушке (`test_http2_limit_holds_when_keepalive_stops_before_it`).
- **Настройки**: `ModelConfig.jev_timeout_s = 10`, `text_timeout_s = 8` (`BROWSER_HANDS_JEV_TIMEOUT_S`,
  `BROWSER_HANDS_TEXT_TIMEOUT_S`, (0; 300]); обычно Jev 0,5–1 с, текст 1–1,5 с. Общий `request_timeout_s = 25` убран.
- **200 с `error` в теле** (и любой 2xx без `choices`/`answers`, но с `error`) — `ProviderError(code, message)`, INFO
  «<Jev|Text model>: ошибка провайдера в ответе HTTP 200: <код> (<сообщение ≤200>)[, повторяю]», без ключа. Jev —
  повтор, как при 429/503 (0,5 с, 1 с, в пределах потолка), потом `failed`. Текст — без повтора внутри `post`:
  `InvalidTextValue("provider-error")` с ценой из `usage.cost`, если она есть, — и один повтор агента (§4.5).
- **Повтор текста при таймауте** (`agent.py`, `_field_text`): `ModelTimeout` — как невалидный ответ: INFO «Повторяю
  запрос к текстовой модели (timeout)», второй запрос с тем же контекстом. Всего 2 запроса на поле при любых причинах;
  дедлайн вышел — `timeout` без второго. `model_calls`, `text_ms` — за оба; у оборванного цены нет.
- **Пустая страница в середине прогона**: `EMPTY_PAGE_WAIT_LATER_S` 3 → 1 с. После действия страница уже прошла
  успокоение; на форме после Submit («Thanks», без элементов) агент ждал 3 с перед DONE.

### Ограничения

- Поток HTTP/2, брошенный по потолку, не сбрасывается: httpcore не шлёт RST_STREAM при `close()`. OpenRouter может
  довести запрос у провайдера; если тот ответит, цена спишется, а в `cost` прогона её нет. Соединение живо, следующий
  запрос идёт как обычно (тест `test_whitespace_before_the_answer_is_a_normal_answer_and_the_connection_is_reused`).
- Отмена (Esc) идущий запрос не прерывает, как и раньше, — но теперь он не дольше 8–10 с.

### Проверка

Офлайн: `tests/test_model.py` — заглушка шлёт пробел раз в 0,5 с 120 с → `ModelTimeout` за ~1,2 с (HTTP/1.1 и h2c);
пробелы оборвались — HTTP/2 держит 1,2 с; пробелы перед JSON — обычный ответ; 200 с `error` для Jev и текста;
`tests/test_agent.py` — повтор после таймаута и после ошибки провайдера на настоящем сокете (`model_calls` 4, `text_ms`,
цена), повтор один на поле при любых причинах, дедлайн. `scripts/check.sh` — 394 теста; `eval.py --fixtures-only` —
4 `ok`. Платный `eval.py --runs 1 --tasks search`: `done`, verified 1/1, 5 шагов, 9 вызовов (7 Jev + 2 текст), 9,3 с,
$0.0012. Полный замер «стало» — основная сессия.

## Сценарии (`feat/scenarios-core`, 25.09.2026)

План — `docs/plan-scenarios.md` §4, решения §0 приняты. Коммиты: снимок тела запроса (до правок), `questions`, `model`,
`agent`, этот раздел и `scripts/live_wikipedia.py --scenario`.

### Как вызывать

```python
from browser_hands.scenario import parse_steps   # ScenarioError(ValueError) — одна строка с номером шага
steps = parse_steps([{"do": "Type the chat name into the chat search box", "text": "Рабочий"},
                     {"do": "Open the chat named «Рабочий»"}])
result = Agent(chrome, clients, url, "", settings.run, steps=steps, cancel=cancel_event, ...).run()
# goal может быть пустым; с goal — Jev видит его как общую цель. Ни goal, ни steps — ValueError("Supply a goal or steps").
result.scenario_done, result.scenario_total   # 1, 2 — «остановился на шаге scenario_done + 1»; режим цели — None, None
result.jev_calls                              # текстовая модель = model_calls − jev_calls
result.steps[i].scenario_step                 # номер шага сценария у действия (с 1); режим цели — None
```

`Agent` прогоняет `steps` через `parse_steps` сам (прямой вызов мимо сервера), принимает и словари.

### Что сделано

- **Запрос** (`model.py`): `StepContext(steps, number, goal)`; `instructions()` — `{goal?, current_step: "Step k of M:
  do" (scenario.render), text_to_type?, done_steps, next_steps}` — вместо строки `goal` в `operation` и всех
  `*_target`. Правила — `NEXT_ACTION_STEP` (цели: `[NEXT_ACTION_STEP, TARGET]`), критерий DONE — «The current step is
  visibly complete.», подпись TYPE_TEXT «…The exact text is given in the current step.» — только у шага с `text`. В том
  же запросе голова `step_done`: choice `yes`/`no`, `instructions` — `question`, `current_step`, `text_to_type?`, `rules:
  STEP_DONE`. `choose(..., step=)` проверяет её `validate_choice` (нет/невалидна — «Invalid Jev response; no action
  executed.»), `Decision.step_done` = P(yes). Без `step` тело прежнее байт в байт: снимок
  `tests/snapshots/jev_goal_request.json` снят отдельным коммитом с кода до правок, тест сравнивает `json.dumps` с тем же
  порядком ключей.
- **Правила** (`questions.py`): `NEXT_ACTION` не тронут (тест — снимок-константа). `NEXT_ACTION_STEP` — только текущий
  шаг, прошлые не повторять, следующие не начинать, `text_to_type` — TYPE_TEXT в поле шага; дословно из `NEXT_ACTION`:
  untrusted, текущие значения и история, автоподсказка, даты, чекбоксы, WAIT (включая «WAIT once» и «Recent WAIT actions
  are not evidence…»). `STEP_DONE` — как в плане.
- **Цикл** (`agent.py`): шаг закрыт — (1) DONE операции или P(yes) ≥ `STEP_DONE_MIN_P = 0.7` на свежей странице:
  действие этого решения не исполняется, следующий тик — Jev под новый шаг; устаревшая страница — `StalePage`,
  переснимок, шаг не закрыт; (2) успешный `Tab.act` TYPE_TEXT с текстом шага — без вопроса к Jev. Последний шаг — `done`.
  Закрытие сбрасывает счётчик действий и возвращает второй шанс. `STEP_ACTIONS_LIMIT = 6` (WAIT и прокрутка считаются):
  7-е действие на шаге не исполняется — `step_limit` «Step k of M not completed after 6 actions». BLOCKED после
  второго шанса — «Model chose BLOCKED on step k of M after a second look; …». Отмена, дедлайн, пустая страница,
  успокоение, второй шанс, `max_steps` и бюджет `2 × max_steps` решений — как в режиме цели.
- **Текст**: у шага с `text` — дословно в `Tab.act(action, page, text=step.text)` в поле, которое выбрал Jev (клик →
  FOCUSED: фокус на цели, не password/file/hidden → selectAll → `Input.insertText`); текстовая модель не зовётся,
  `_pending_text` не трогается. Шаг без `text` — текстовая модель с целью «<goal>\nCurrent step k of M: <do>».
  Сквозной тест на `FakeCDPServer`: текст шага есть ровно в одном кадре — `Input.insertText` сразу после FOCUSED и
  selectAll; фокус потерян — текста нет ни в одном кадре.
- **Логи**: INFO «шаг k/M выполнен (p=0.86 | DONE, p=… | text typed, действий n)», у действий суффикс « [шаг k/M]», итог
  «browse done: 2/2 steps of scenario, 2 actions, 4 model calls (Jev 4), … ms, tab new». `do` и тексты шагов в INFO не
  пишутся (могут нести имена); режим цели — строки прежние.

### Отклонения от плана

1. `StepContext(steps, number, goal)` вместо полей `(number, total, do, text, goal, done, remaining)`: остальное —
   свойства, `current()` — `scenario.render`; одно место для «Step k of M», без рассинхронизации полей.
2. Текстовый шаг закрывается и тогда, когда снимок после ввода устарел (`StalePage` в `observe`): ввод уже прошёл
   проверку фокуса; последний шаг — `done` с прежним снимком (url/title до ввода, кадр — свежий). Иначе — лишний
   вызов Jev на этот шаг.
3. Лимит 6 проверяется после разбора `step_done`: решение после 6-го действия ещё может закрыть шаг, поэтому
   «6 действий без закрытия» = 7 вызовов Jev, 6 действий.
4. Текст ошибки без goal и steps — «Supply a goal or steps» и в режиме цели (было «Supply a goal»; тест по `goal`).
5. `NEXT_ACTION_STEP` не берёт из `NEXT_ACTION` правила о цели целиком: «Do not repeat satisfied steps», «Fill required
   fields before submitting», фильтры, «Submit populated search fields before opening a result», «If Search/Submit is
   visible … CLICK it immediately» (толкает к следующему шагу) и DONE/BLOCKED всей цели. Взяты «Use current field values
   and action history» и «Do not toggle a checkbox…» — в плане их нет в перечне, но они про текущее действие.
6. Критерии `step_done`: `yes` — «The current step is visibly complete on the CURRENT page.», `no` — «The current step is
   not complete yet, or the page does not show it.»; вопрос — «Is the current step complete?».
7. `scripts/live_wikipedia.py --scenario` (нет в списке файлов пакета): шаги как у задачи `wiki` стенда (§5.3), печать
   `jev_calls`, `text_calls`, `scenario`, `[шаг k]` у действий.

### Живые прогоны (launch, headless, временный профиль; 3 платных)

`uv run --frozen --env-file <клон>/.env python scripts/live_wikipedia.py [--scenario] --verbose`. Сценарий:
`[{"do": "Type the query into the Wikipedia search box", "text": "Gödel's incompleteness theorems"}, {"do": "Open the
matching article from the suggestions or search results"}]`, goal пустой.

| # | режим | статус | elapsed, мс | model / text / browser / wait, мс | Jev / текст | сценарий | стоимость |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | сценарий | done | 6258 | 2604 / 0 / 262 / 3336 | 4 / 0 | 2/2 | $0.00112 |
| 2 | цель | done | 6549 | 2206 / 1406 / 433 / 2472 | 4 / 1 | — | $0.00103 |
| 3 | сценарий | done | 6789 | 3081 / 0 / 240 / 3437 | 5 / 0 | 2/2 | $0.00139 |

- Сценарий: `TYPE_TEXT 'Search Wikipedia'` (`len=31`, текст шага) → «шаг 1/2 выполнен (text typed, действий 1)» без
  вызова Jev на границе; затем `CLICK` подсказки «Gödel's incompleteness theorems» → Jev DONE, `step_done` 0.86 → «шаг 2/2
  выполнен». Статья открыта, `text_calls` = 0 в обоих. `step_done` до выполнения — 0.00–0.05.
- Цель: `TYPE_TEXT` (текстовая модель, 1406 мс) → `CLICK 'Search'` → решение WAIT не исполнено — устарело (страница
  менялась после клика) → DONE; статья открыта — режим цели не сломан.
- Входные токены Jev: сценарий 6384–7041, цель 6139–6857 на сопоставимых вызовах — +3–4 % (голова `step_done` и объект
  `instructions`), цена вызова $0.00027–0.00030 против $0.00026–0.00029.
- На шаге 2 первые 1–2 решения CLICK (по 0,4–0,6 с) отброшены как устаревшие: подсказки Википедии перестраиваются после
  ввода (то же, что в режиме цели, раздел «Живые прогоны» выше). Отсюда `model` 2,6–3,1 с при 4–5 вызовах.
- N = 3 — не замер: сравнение режимов — стенд §6 (N = 10).

### Не проверено

- attach и WhatsApp (§7 — основная сессия), `do` по-русски, стенд `--mode scenario` (обвязка, §5.3).
- Jev выбрал для текстового шага не то поле — шаг закроется зря (риск §8, мера — `do` называет поле); офлайн не
  воспроизводится, видно только на стенде (`search`: `report.query`/`sent`).

### После ревью (26.09.2026)

Меняет пункты выше; тесты — `tests/test_agent.py`, блок «сценарий после ревью», и `test_model`/`test_browser`/`test_docs`.

1. DONE закрывает шаг, только если P(yes) `step_done` в том же ответе ≥ 0,5; иначе — без мутации, счётчик шага +1, как
   WAIT. `STEP_DONE`: открыт/активен (заголовок чата), не «есть в списке»; результат этого прогона; поле содержит текст.
2. Тексты шагов в текстовую модель не уходят (`recent_actions` без `text`); шаг без `text` и без `goal` при TYPE_TEXT —
   `blocked` «step k of M: no text given for typing».
3. Бюджет решений в сценарии — `2 × max_steps + M`: 20 шагов при `max_steps` 25 и одном устаревшем решении на шаг
   проходят.
4. Последний текстовый шаг, снимок после ввода устарел: ещё один `observe()`, затем url/title из `Tab.location()`
   (`Target.getTargetInfo`, ≤ 2 с), затем снимок до ввода; ошибки CDP исход `done` не меняют.
5. «3 действия без изменений → blocked» — только внутри текущего шага (`_advance` запоминает длину истории).
6. Подпись элемента в INFO — до 40 символов с «…» (`LOG_LABEL_MAX`), в обоих режимах; в результате и DEBUG — как была.

Проверка: `check.sh` — 528 тестов, pyright — 0, `eval.py --fixtures-only` — 4 `ok`. Платный `eval.py --mode scenario
--runs 1 --tasks search,form` (launch, headless): verified 2/2, Jev 8 + 8, текстовая модель 0, $0.0021. `search`: шаг 4
— DONE с p=0.21 отклонён, следующий DONE с p=0.69 закрыл шаг; подпись чата в INFO — «Рабочий 12:52 Кто заберёт ключи
от пере…». `form`: шаг 5 закрыт DONE с p=0.55.

## Живая проверка WhatsApp 26.09 и правки (`feat/fix2-core`, 26.09.2026)

База `60c8c4d`. Меняет «Сценарии»: «Что сделано» (текст шага закрывает шаг сразу после ввода), «Отклонения» п. 2 и
«После ревью» п. 1 (DONE без подтверждения = WAIT) и п. 4 (последний текстовый шаг при устаревшем снимке). Тесты —
`tests/test_agent.py`, блок «живая проверка WhatsApp 26.09», плюс `test_model`, `test_server`, `test_cli`,
`test_contract`, `test_docs`; каждый пункт падал до правки.

### Факты (WhatsApp Web, вкладка пользователя, сценарий)

1. **Поле стёрлось.** Шаг «открой чат Рабочий» — клик, через ~0,8 с шаг «введи сообщение» (`text` задан):
   `Input.insertText` прошёл, фокус проверен, страница изменилась, шаг закрыт кодом сразу после ввода («text typed»).
   WhatsApp, догрузив чат, перерисовал поле сообщения — текст пропал. Повторный ввод в открытом чате — остался.
2. **Лишний клик.** Шаг «нажми Send» (без `text`): CLICK Send (conf 0.99, страница изменилась) → DONE с P(yes) 0.36 и
   0.47 (порог 0,5, отклонены) → CLICK «00:31 Sent» (conf 0.25) в чужом месте → «Model-call budget exhausted
   (7 decisions)», `step_limit`.

### Решения (приняты пользователем)

1. **Шаг с `text` закрывается по видимому результату.** После TYPE_TEXT текста шага — успокоение (`after_input` в
   `observe`) и свежий снимок. Шаг закрыт, только если поле, куда печатали, показывает текст шага: тот же узел, если
   он ещё в снимке, иначе поле `fill` той же роли и подписи (`typed_fields`); значение содержит текст с нормализованными
   пробелами (`shows_text`: NBSP, переводы строк, края). Нет — у записи ввода в истории `note: "text vanished after
   typing (page re-rendered)"` (в `recent_actions` — только при пометке) и обычное решение Jev по этому снимку: поле
   пусто, повторный TYPE_TEXT допустим. Пропал в 3-й раз (ввод + `RETYPE_LIMIT = 2` повтора) — `blocked` «step k of
   M: typed text does not stay in the field». Снимок после ввода устарел — `settle({"kind": "retry"})` и ещё один
   `observe()`; снова устарел — `StalePage` наружу, шаг не закрыт (переснимок в `_tick`, решает Jev).
   `snapshot.js` не менялся: значение `contenteditable` уже отдаётся — `innerText.trim()` (`snapshot.js:76-77`),
   password/file/hidden отсеивает `safe` (`snapshot.js:9`, `:57`).
2. **После неподтверждённого DONE новых действий нет.** DONE с P(yes) < 0,5 — режим проверки: запись ожидания в
   истории, успокоение, свежий снимок и вопрос `choose(..., verify=True)`: `build_request` оставляет операции WAIT,
   DONE и BLOCKED, голов `*_target` нет, у элементов нет списка `operations` (поля и значения видны как состояние),
   голова `step_done` — та же. DONE с P ≥ 0,5 (или любой ответ с P ≥ 0,7) — шаг выполнен, режим снят. Снова DONE с
   P < 0,5 или второй WAIT — новый статус `unconfirmed`, `error` «step k of M: probably done, not confirmed — check the
   screenshot»; url и title — со снимка после ответа (`_refresh_page`, при `StalePage` — `Tab.location()`), кадр —
   `_finish`. Ответ вне DONE/WAIT/BLOCKED в режиме проверки невалиден в `choose` и не исполняется в `_act`.
   `Status` += `unconfirmed`; ответ сервера — «шаг k+1 из M не подтверждён (вероятно, выполнен — проверь скриншот):
   <do>» вместо «остановился на шаге…»; CLI — код 2, как у любого не-`done`; `tests/fakes.py` — `unconfirmed_result()`.
   Описание инструмента не менялось: уже ровно 200 символов (лимит — тест).
3. **Порог уверенности для действий — в обоих режимах.** CLICK, TYPE_TEXT и SELECT с уверенностью ниже
   `MIN_ACTION_CONFIDENCE = 0.3` не исполняются: первый раз — как WAIT (успокоение, свежий снимок, новый вопрос),
   второй подряд — `blocked` «uncertain action: CLICK on <подпись ≤ 40> (conf 0.25)». Любое другое решение сбрасывает
   счёт. Тело запроса режима цели не меняется (снимок `tests/snapshots/jev_goal_request.json` зелёный).

### Отклонения

1. Запасное сравнение текста — без эмодзи и символов (категории So, Sk, Cf, Mn, Me, Cs, Co после NFC) с обеих сторон,
   если от текста шага без них что-то остаётся. WhatsApp может рисовать эмодзи в поле картинкой `<img alt>`, в
   `innerText` её нет: без этого «проверка 👋» считалась бы пропавшей — повторы и `blocked`. На живом WhatsApp не
   проверено.
2. «Уверенность» для порога — `Decision.confidence`: голова `operation` (`model.py`, `choose`), её показывает `conf` в
   шагах ответа. Вероятность цели в голове `*_target` — отдельное число, порог к ней не применяется.
3. Режим проверки и неуверенное действие не увеличивают счётчик действий шага (не действия; режим проверки — не больше
   трёх вопросов, неуверенных пропусков — один). Прежде неподтверждённый DONE считался как WAIT.
4. BLOCKED в режиме проверки — как обычно: второй шанс (вопрос снова только с DONE/WAIT/BLOCKED), затем `blocked`.
5. Ошибка CDP во втором снимке после ввода (таймаут, вкладка закрыта) — `failed`, как любой сбой снимка: шаг не
   закрыт. Прежде последний текстовый шаг при этом был `done` (`_look_after_typing` удалён, стал `_refresh_page`).
6. Тесты: `make_tab` в `tests/test_agent.py` показывает в поле напечатанное (`tab.typed`), иначе старые текстовые шаги
   не закрывались бы; тесты «шаг закрыт при устаревшем снимке» и три `…last_text_step…` заменены новыми;
   `test_scenario_texts_never_reach_the_text_model` печатает шаг 2 в отдельное поле (см. «Наблюдения»).

### Наблюдения

- Текстовая модель видит значение поля (`field_context` → `field.value`): если шаг без `text` печатает в то поле, где
  уже стоит текст прошлого шага, этот текст уйдёт текстовой модели. Старый тест этого не видел (Mock-снимок не
  показывал напечатанное). Не правил — вне задачи.
- `scripts/eval.py`: колонка `status` шириной 10, `unconfirmed` — 11 символов, строка прогона сдвигается на символ;
  сводка считает статусы через `Counter` — не ломается.
- form: после Submit экран «Thanks» без элементов — `_await_interactive` ждёт 1 с и перед вопросом режима проверки,
  хотя действий там нет.

### Живые прогоны (launch, headless, временный профиль; 3 платных)

| # | прогон | статус | DOM | elapsed, мс | Jev / текст | стоимость |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `eval.py --mode scenario --runs 1 --tasks search,form`: search | done | да | 5780 | 8 / 0 | $0.0011 |
| 1 | то же: form | unconfirmed | да | 7487 | 9 / 0 | $0.0010 |
| 2 | `live_wikipedia.py --scenario` | done | — | 6609 | 4 / 0 | $0.00097 |
| 3 | `live_wikipedia.py` (цель) | done | — | 8088 | 6 / 1 | $0.00146 |

- search: шаги 1 и 3 (текст) закрыты проверкой поля; шаг 4 Send — DONE p=0.17 → проверка → DONE p=0.82 → `done`.
- form: шаг 5 Submit → «Thanks» → DONE p=0.34 → проверка → DONE p=0.46 → `unconfirmed` (DOM: форма отправлена). После
  ревью тот же шаг закрылся DONE с p=0.55: P(yes) на «Thanks» ходит у порога 0,5; теперь одна проверка, затем
  `unconfirmed` вместо новых решений.
- Википедия: сценарий — текст шага 1 в поле, шаг закрыт проверкой, статья открыта; режим цели — `done`, неуверенных
  действий нет (conf решений стенда — 0.63–1.0).

Проверка: `check.sh` — 557 тестов (+1 пропуск), pyright — 0.

## Ожидания по событиям (`feat/waits-events`, 26.09.2026)

Пакет «события» плана `docs/plan-waits.md` (§5). База — контракт b2eaa24. Правило пользователя: ждать событий и
состояний, а не времени; единственное время — предохранитель одного ожидания.

### Сигналы и алгоритм

- `Tab.await_ready(action)` (`browser.py`, `_ready`): промис `READY` в странице → запросы вкладки после эпохи действия
  (`client.pending_since`) → есть — насос `client.wait_events` до их завершения → снова `READY` (ответ мог поменять
  DOM) — пока оба условия не выполнятся в одном проходе. Итог — `{reason, wait_reason, ms, mutations, frames, passes,
  pending_requests}`, он же в `last_settle`; пусто — не ждали (отмена, нет места до дедлайна, страница не дала итога).
- `READY` (только чтение, один `Runtime.evaluate awaitPromise`): `QUIET_FRAMES = 2` кадра подряд без значимых мутаций
  (те же правила значимости, что у прежнего SETTLE), `readyState === 'complete'`, `document.fonts.status !== 'loading'`,
  нет идущих анимаций с конечным `endTime` (бесконечные — спиннеры — не ждём), нет видимых `[aria-busy="true"]`;
  подсказки комбобокса после ввода — готово сразу (`options`). Скрытая вкладка без rAF — ходы `MessageChannel` вместо
  кадров (`frames`); условие не выполнено — ход «паркуется» до мутации, `readystatechange` или `fonts.ready`, без
  холостого цикла. Таймер один — предохранитель.
- Почему 2 кадра: к первому кадру после действия обработчики и микрозадачи отработали, а поставленное ими на
  rAF/`setTimeout(0)`/`MessageChannel` легло в DOM; второй тихий кадр ловит цепочку «rAF → commit» фреймворков. Один
  кадр — второе звено не видно; три и больше — только кадр задержки, нового класса работы не ловят. Всё, что дольше
  кадра, закрывают сеть, анимации и `aria-busy`.
- Сеть (`cdp.py`): `seq` — принятых сообщений; эпоха — `client.seq` перед первой командой ввода (`Tab._act`), у WAIT
  и SELECT — перед `_act`, у навигации — перед `Page.navigate`. `requestWillBeSent` (+; редирект — тот же id),
  `loadingFinished` / `loadingFailed` / `requestServedFromCache` (−); `WebSocket`, `EventSource` и ответ
  `text/event-stream` не считаются. `Network.*` в очередь `events` не идут. Предохранитель вышел — всё, что в полёте,
  фон (`mark_background`) до конца сессии.
- Предохранитель: `min(Tab.fuse_s, дедлайн − WAIT_DEADLINE_MARGIN_S)`; `Tab.fuse_s` по умолчанию —
  `Thresholds().wait_fuse_s` (1,5 с). Отмена проверяется перед каждым проходом и на каждом событии насоса: задержка
  отмены — не больше одного прохода (≤ предохранителя, как у прежнего потолка SETTLE).
- `Tab.await_change()`: промис `CHANGE` (значимая мутация, `animationend`/`transitionend`) или, с сетью, любое
  завершение запроса / WS-кадр (`client.call_until` — промис не дожидается, его поздний ответ пропускается), смена
  документа; затем `await_ready`. Изменения не было за предохранитель — `fuse` без второго ожидания. Итог плюс
  `change` (что разбудило) и `ready`; `wait_reason` — `change`, если изменение было и страница затем готова.
- `observe()`: после WAIT — `await_change`, после остального — `await_ready`; `StalePage` — повтор после `READY` в
  новом документе (он ждёт `readyState` событием), не `sleep(0.02)`; итог повторов `last_settle` не трогает.
- `navigate()`: `Page.navigate` → `LOAD` (`readystatechange` → complete; предохранитель `NAVIGATE_TIMEOUT_S`) →
  `await_ready({"kind": "load"})`. Смена документа (редирект) — ждать его запросы и повторить.
- Вкладка пользователя: `USER_TAB_NETWORK = False` — `Network.enable` только в своей вкладке до замера WhatsApp
  (§0.2, §8); у пользователя — DOM-сигналы. `Tab(network=True)` — флаг в коде на после замера. `release()` шлёт
  `Network.disable` до `detach`, если включали. Размер и навигация вкладки пользователя — как прежде, не трогаются.

Цена: было 1 `Runtime.evaluate` (SETTLE) на действие. Стало 1 `READY`, если запросов после действия нет; иначе
`READY` + насос + ещё `READY` на каждый «ответ пришёл» (в прогонах — 1–2 прохода). WAIT: `CHANGE` + 1–2 `READY`.

### Какие числа остались и почему

| Число | Где | Почему |
| --- | --- | --- |
| `QUIET_FRAMES = 2` | `browser.py:61` | единица — кадр страницы; обоснование выше |
| `Thresholds.wait_fuse_s = 1.5` | `config.py` (контракт) | предохранитель §0.1; значение — калибровка §7.4 |
| `WAIT_DEADLINE_MARGIN_S = 0.5` | `browser.py:62` | ответ CDP успевает до дедлайна (план §1.1: оставить) |
| `NAVIGATE_TIMEOUT_S = 15` | `browser.py` | предохранитель загрузки (`LOAD`), как был |
| `STALE_RETRIES = 10` | `browser.py` | счётчик смен документа, не время |
| `NETWORK_PENDING_MAX = 1000` | `cdp.py:31` | граница памяти: запросов в полёте на сессию, счётчик |
| `EVENTS_MAX = 200` | `cdp.py` | очередь событий жизненного цикла (сеть в неё не идёт) |
| `Tab.pause()` / `_sleep` | `browser.py` | только для `agent.py` (`EMPTY_PAGE_POLL_S`) до пакета «сценарий»; у вкладки своих пауз нет |

`grep -n "_MS" browser_hands/browser.py` — пусто; тест проверяет, что в модуле нет имён `*_MS`.

### Слепое пятно (как в плане §2)

Таймеры страницы без сети и мутаций: `setTimeout` стенда (`delay` поиска при `net=0`, `remount`, подтверждение Send
300 мс) — два тихих кадра короче прежних 200 мс, снимок раньше результата; это закрывают `net=1` стенда (пакет
«стенд»), WAIT Jev и второй взгляд. Живой пример — Википедия: после ввода в поиск модуль typeahead догружается и
через ~0,3–0,5 с после тишины сети заменяет `searchbox` узлом `combobox` с тем же текстом (зонд ниже).

### Отклонения от плана

- `Page.enable` не включаю (план 5.2/5.4 — `Page.loadEventFired`): зонд — `Page.navigate` отвечает после commit
  (`location.href` сразу новый, `readyState` `interactive`), так что `readystatechange` в странице — тот же сигнал load
  без лишнего домена и его потока событий. Навигацию после клика ловят сетевой учёт (документ и подресурсы) и повтор
  `READY` при смене контекста. (alert в headless останавливает JS и с `Page.enable`, и без — зонд; не довод.)
- Повтор снимка при `StalePage` — после `await_ready`, не `await_change` (план 5.4): на уже загруженном статичном
  документе `await_change` ждал бы изменения, которое уже случилось, весь предохранитель.
- `await_change` — два предохранителя подряд (изменение, затем готовность), не один общий.
- Смена документа во время `READY` — повтор в новом (≤ STALE_RETRIES), раньше — «прервано, снимок сразу». Ответ без
  значения (`undefined`) — конец ожидания без повтора, как раньше (фейки `test_agent` так отвечают).
- `settle()` (= `await_ready`, None вместо `{}`), `pause()` и `last_settle` оставлены: `agent.py` и `scripts/eval.py`
  ещё вызывают их (переведёт пакет «сценарий»).
- README («Ограничения», одно предложение про ожидание) и `tests/test_docs.py` (тот же тест) правлены здесь: тест
  читал `SETTLE_QUIET_MS`/`SETTLE_CEILING_MS`, без правки `check.sh` красный. При слиянии с «сценарием» возможен
  конфликт в том же абзаце README (соседнее предложение про пустую страницу).
- Harness переписан под `READY`/`CHANGE`, имя файла прежнее (`tests/settle_harness.js`).

### Проверено

- `bash scripts/check.sh` → 0 (ruff, format, pytest, `node --check`); pyright (`browser_hands scripts`) — 0 ошибок.
- `tests/settle_harness.js`: статичная страница — `quiet` 32 мс (было ≈228); мутации в кадрах 1–5 — `quiet` на 112 мс;
  бесконечная анимация, только `style`, скрытый `aria-busy` — 32 мс; конечная анимация 300 мс — 304 мс; `aria-busy`
  снят на 200 мс — 240 мс; шрифты на 100 мс — 112; `readyState` на 150 мс — 160; без кадров — `frames` за 2 хода
  `MessageChannel`, грузящийся документ без кадров — ход ждёт `readystatechange` (3 хода всего); текст каждый кадр —
  `fuse` 1500 (с дедлайном — 300); `CHANGE`: мутация 80, `animationend` 50, иначе `fuse`.
- Headless Chrome (`BROWSER_HANDS_CHROME_TESTS=1`, 3 ok): `Network.enable` с нулевыми буферами — без ошибки; тикер
  100 мс и JS-анимация `style` — `quiet` 33 мс; текст каждый кадр — `fuse` 1502 мс (90 мутаций); `fetch` 0,7 с —
  `quiet` 728 мс, 2 прохода, `pending 0`; long-poll 10 с — `fuse` 1505 мс, `pending 1`, следующий `await_ready` —
  `quiet` 4 мс, `pending 0` (фон).
- `eval.py --fixtures-only --tasks all` (бесплатно, настоящий Chrome) — все `ok`; ожидания после действий 9–179 мс
  вместо ≈200+.
- Википедия (бесплатно, без моделей, 4 попытки как у агента: ввод → `observe`): новый код — 4/4 после ожидания поле
  уже `combobox` (узел 54), `typed_fields` его не находит (роль другая) → «текста нет в поле»; прежний SETTLE на той же
  странице — 2/4 снимок до замены (проверка прошла), 2/4 — после.

### Живые прогоны (launch, headless, временный профиль; 2 платных из 3)

`eval.py --runs 1 --tasks search,wiki --mode scenario` (DEBUG только `browser_hands.browser` — причины ожиданий).

| # | задача | статус | DOM | elapsed, мс | wait всего, мс | Jev | стоимость |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | search | done | да | 4716 | 225 | 9 | $0.0013 |
| 1 | wiki | done | да | 10662 | 3574 | 14 | $0.0039 |
| 2 | search | done | да | 5048 | 202 | 8 | $0.0011 |
| 2 | wiki | done | да | 10622 | 3393 | 14 | $0.0039 |

Ожидание после действия по шагам (`wait_ms` шага; все `wait_reason` — `quiet`, `pending_requests` 0):

- search: 27, 30, 16, 14, 18, 31 мс (прогон 1) и 34, 10, 16, 27, 34 мс (прогон 2), 1 проход; было ≈200–270 мс на шаг
  (708a595, reg-scn). Лишний CLICK по полю поиска перед чатом — как и раньше (результаты через `setTimeout` 700 мс).
- wiki: TYPE_TEXT 709 мс (2 прохода — ждал запрос подсказок) и 473 мс; CLICK по подсказке (переход на статью) 1391 и
  1365 мс (`READY` 1299/1240 мс, 1 проход); было 209–695 и 842–1164 мс.
- wiki в обоих прогонах: «текста нет в поле после ввода» (замена поля, см. выше) → 10 раз «DONE до ввода текста шага»
  с ожиданием 5–19 мс → шаг закрыт DONE p≈0,85 → статья открыта, `done`. Отсюда 14 вызовов Jev и ~10,6 с вместо
  4–6 с. Причина — `typed_fields` требует ту же роль (находка 2, §6.2, пакет «сценарий»), не ожидание.

### Для пакета «сценарий»

- Итог ожидания для `Step.wait_reason`/`pending_requests`: возврат `await_ready`/`await_change` или `tab.last_settle`
  после `observe()` (ключи `wait_reason`, `pending_requests`).
- `tab.fuse_s = self.thresholds.wait_fuse_s` рядом с `tab.deadline, tab.cancel` — иначе вкладка берёт
  `Thresholds()` по умолчанию.
- `typed_fields` без роли (находка 2) — без неё wiki-сценарий каждый раз уходит в цикл «DONE до ввода текста».
- `tab.settle({"kind": "retry"})` в `_look_again`/после ввода теперь стоит 2–20 мс на тихой странице; для «второго
  взгляда» по плану — `await_change()`.

### Не проверено

- Эффект нулевых `maxTotalBufferSize`/`maxResourceBufferSize` на память вкладки (приняты без ошибки — и только).
- `Network.enable` во вкладке WhatsApp (поток WS-кадров) — ждёт замера §8; учёт к нему готов (WS-кадры считаются
  отдельно, в очередь не идут, запросов в полёте ≤ 1000).
- Путь без кадров (`MessageChannel`) в настоящем Chrome — только harness: с focus emulation вкладка видима и rAF идёт.
- Постоянный видимый `aria-busy` у WhatsApp дал бы `fuse` на каждом шаге — смотреть `wait_reason` в живой проверке §9.3.

## Сценарий и пороги (`feat/waits-agent`, 26.09.2026)

База — контракт `b2eaa24` (docs/plan-waits.md §6). Файлы: `browser_hands/agent.py`, `scripts/calibrate.py`,
`tests/test_agent.py`, `tests/test_calibrate.py`, `docs/calibration.md`. `model.py`/`questions.py` не менялись: тело
запроса режима цели прежнее (`tests/snapshots/jev_goal_request.json`).

### Что сделано

1. **Инварианты сценария (§6.1).** Текст шага, закрытого кодом, — `Invariant(step, node, label, text)`. Перед каждым
   решением, кроме подтверждения текущего шага, и перед `done` последнего текстового шага (после `await_ready`) —
   один `Tab.field_values`; значение не `matches` — откат к шагу j: он снова текущий, шаги после него не выполнены,
   решение отброшено, у последней записи истории `note: "text of step j vanished (field re-rendered)"`, INFO «шаг k/M:
   текст шага j пропал — возвращаюсь к шагу j (n-й раз)». Счёт пропаж шага общий с «vanished» сразу после ввода;
   больше `RETYPE_LIMIT` — `blocked` «step j of M: typed text does not stay in the field». Снимают инвариант:
   подтверждение Jev более позднего шага; исполненный клик не по полю (у узла нет `fill`: Send, строка чата, Submit);
   новый ввод в то же поле.
2. **Находки ревью (§6.2).** `typed_fields` — ключ поля без роли (`_field_key`: подпись; поле без имени подписано
   ролью — такие сравниваются между собой при любой роли); текста не видно — `await_change` и ещё один снимок до
   пометки. `matches` — префикс после нормализации пробелов, запасное — **равенство** букв и цифр (маски, эмодзи
   картинкой), подстроки нет. `_unconfirmed` — только по свежей странице, иначе `StalePage`.
3. **Режим цели (§0.5).** DONE с `confidence < done_min_confidence` — `_look_again`; снова такой же (без действия,
   сменившего страницу) — `unconfirmed`, `error` «goal probably done, not confirmed — check the screenshot». TYPE_TEXT
   в режиме цели, текст не виден на следующем снимке — `note` в истории (Jev видит).
4. **Ожидания.** После действия — `tab.await_ready(action)`, затем `tab.after_input = None` (observe второй раз не
   ждёт; `await_ready` пакета «события» снимает его и сам); итог → `Step.wait_reason` (`wait_reason`, иначе `reason`)
   и `pending_requests` (`{}` — None и 0). WAIT Jev — свежесть и `await_change()`, без `Tab.act`. `await_change` —
   второй взгляд, проверка шага, повтор снимка после ввода, пустая страница (вместо опроса 0,25 с).
   `tab.fuse_s = thresholds.wait_fuse_s` рядом с `tab.deadline`.
5. **Калибровка (§6.3).** `scripts/calibrate.py` → `docs/calibration.md` (разделы §1–§4 = ссылки в `config.py`).
   Правда шага — исход, а не прокси «закрылся следующим решением» (он повторяет сам порог); с историей отчётов (§7.2)
   — состояние страницы в момент снимка. Сетка 0,05, `FP·1 + FN·0,05`, Wilson 95 %, допустимые θ, правило «мало
   данных у θ* (< 30 пар) — оставить текущее» с одним исключением: корзины между текущим и допустимой границей
   однозначны по Wilson относительно безубыточной доли 0,95.

### Итог калибровки (513 прогонов, docs/calibration.md)

| порог | текущее | рекомендуемое | основание |
| --- | --- | --- | --- |
| `step_done_min_p` | 0.7 | 0.75 | не-DONE с P(yes) 0,70–0,75: 0 из 8 шагов выполнены (Wilson ≤ 0,32) |
| `done_step_done_min_p` | 0.5 | 0.35 | 55 пар у θ*; допустимые [0,35; 0,45]; ниже 0,35 — ложные DONE |
| `min_action_confidence` | 0.3 | 0.3 | автоматический выбор выключен: «нет» — лишнее, не вредное действие |
| `done_min_confidence` | 0.5 | 0.5 | разрыв 0,35 → 0,79, в корзинах у 0,5 пар нет; допустимые [0,40; 0,85] |
| `wait_fuse_s` | 1.5 | 1.5 | 4 замера (развёртка не в счёт: p99 там = максимум оси) |

`Thresholds` **не менялись**: 0,7 названо в README («с вероятностью не ниже 0,7», `test_docs`) — README не этого
пакета; понижение DONE-порога ослабляет решение «после ревью» (DONE ≥ 0,5), а данные — только стенд, форма и
Википедия. Принято основной сессией 26.09 по правилу калибровки (ближайшая граница допустимых) — docs/decisions.md
«Пороги по калибровке».

### Отклонения от плана

1. Инвариант снимает не только подтверждение Jev (§6.1), но и исполненный клик не по полю и новый ввод в то же поле.
   Без этого после Send (поле очищено) DONE с p < 0,5 — обычное дело (стенд, WhatsApp 26.09: 0,36 и 0,47) — дал бы
   откат и повторный ввод: риск второй отправки.
2. Проверка — перед каждым решением, не только перед `tab.act`: на стенде после пересоздания поля Send пропадает, и
   Jev отвечает BLOCKED — это откат, а не второй взгляд и `blocked`.
3. Запасное сравнение — равенство букв и цифр (указание координатора), не префикс (§0.3).
4. Потолки пустой страницы (`EMPTY_PAGE_WAIT_S` 25 с, `EMPTY_PAGE_WAIT_LATER_S` 1 с) оставлены предохранителями —
   их называет README (`test_docs`); ожидание внутри — `await_change`, не опрос. «Thanks» формы по-прежнему до 1 с.
5. Текст ответа сервера для `unconfirmed` в режиме цели не добавлен — `server.py` не в пакете; строка `error:` его
   уже несёт.
6. Тест согласия `docs/calibration.md` с `config.py` — в `tests/test_calibrate.py`, не в `test_docs.py` (общий файл).
   Проверяет, что документ снят при нынешних порогах («текущее» = config), а не что рекомендация = config.
7. `tab.fuse_s` — `# pyright: ignore`, пока `Tab.fuse_s` нет в контракте (его вводит пакет «события»).

### Проверка

- `check.sh` — 624 теста (+2 пропуска), pyright — 0. Новые тесты (29 в `test_agent` — 17 функций, 19 в `test_calibrate`) падали
  до правки: список — блоки «инварианты сценария» и «находки ревью 2–5 и режим цели».
- Слияние с `feat/waits-events` (c690bab) во временной копии (scratchpad): 650 тестов, pyright 0; после — одна
  правка теста e2e второго шанса (с событиями `await_change` — два промиса: изменение и готовность).

### Живые прогоны (launch, headless, временный профиль; 4 платных)

| # | код | search-remount | wiki | form | стоимость |
| --- | --- | --- | --- | --- | --- |
| 1 | c0b9985 | done, DOM да, 7,8 с, откат к шагу 3 | done, 4,8 с | done, 6,6 с | $0.0036 |
| 2 | c0b9985 | done, DOM да, 5,8 с, откат к шагу 3 | done, 5,1 с | done, 6,2 с | $0.0039 |
| 3 | c0b9985 | done, DOM да, 6,6 с, откат к шагу 3 | done, 5,8 с | done, 6,4 с | $0.0037 |
| 4 | d6ebe2e + events c690bab | blocked, 8,7 с | done, 5,7 с, 3 вызова Jev | done, 7,6 с | $0.0029 |

- search-remount (было 1/15): во всех трёх прогонах Jev выбрал Send (или BLOCKED) после пересоздания поля — проверка
  инварианта увидела пустое поле, откат, повторный ввод, Send, `done`.
- С событиями: Википедия — 3 вызова Jev и 5,7 с (у пакета «события» без правки ключа поля — 14 и 10,6 с).
  search-remount — `blocked`, причина не в инвариантах: шаг 2 «открой чат» закрыт по `step_done` = 0,74 при решении
  «CLICK строка чата» (чат не открыт, отчёт `openChat: null`); на шаге 3 восстановление — тот же клик с conf 0,29 и
  0,27 (< 0,3), дважды BLOCKED. Это ровно корзина 0,70–0,75 из §1 калибровки.

### Не проверено

- Живой WhatsApp (§9.3) — только основная сессия.
- Поле без доступного имени, пересозданное с другой ролью: `typed_fields` его находит, а `Tab.field_values` ищет по
  подписи `name || role` — роль другая, значение None, возможен ложный откат (`browser.py` — пакет «события»).

## Медленные ответы и текст в режиме цели (`feat/waits-fix`, 26.09.2026)

База — `feat/waits` 853e788. Замер «стало» (развёртка по осям, 180 прогонов): сценарий 82/90, цель 73/90; провалы двух
классов — A (медленный ответ, delay 3000, `No page change after 3 consecutive actions`) и B (режим цели при пересоздании
поля, remount 1500–2000: `done`/`unconfirmed` при неотправленном сообщении). Правило то же: ждать событий и состояний,
новых чисел времени нет.

### Разбор трасс (что подтвердилось)

- A, net=1 (прогон `search-remount-5-47bf61`): TYPE_TEXT в поиск → `wait_reason=fuse`, `pending_requests=1`; Jev три
  раза кликает по полю поиска («Open Search or start a new chat», уверенность 0,73–0,61) → blocked на 4,27 с. Гипотеза
  «клик перезапускает поиск» **не подтвердилась**: поиск в `tests/fixtures/app.html` запускает только `input`
  (`search.addEventListener('input', …)`), клик по contenteditable его не шлёт. `api_calls` пуст, потому что стенд пишет
  запрос в журнал после серверной паузы (`_api`: `time.sleep` → `store.call`), а прогон кончился раньше: ввод на ~1,28 с,
  ответ на ~4,28 с.
- A, net=0: задержка — таймер, сигнала нет; Jev WAIT не выбирает, три клика по полю — blocked за ~2–3 с.
- B (d700-r1600-s0): чат открыт на ~2,5 с, текст напечатан на ~4,07 с, поле пересоздано на ~4,13 с; Jev выбрал Send по
  снимку с текстом (4,68 с) → Send скрыт → `StalePage` → DONE 0,43/0,51 → `done`/`unconfirmed`.
- Попутно: ложный `done` в сценарии «стало» (d3000-r0-s0-n0) — Jev кликнул «Monday» по неотфильтрованному списку, шаг 2
  закрыт DONE p=0,54. Тот же класс в споте ниже.

### Что сделано

A — страница с незаконченной работой действия не выглядит готовой:
1. Учёт: `CDPClient.in_flight_since` (запросы после эпохи, **фоновые тоже**), `Tab.action_epoch` (эпоха исполненного
   действия, без навигации), `Tab.in_flight(since)`. Агент хранит эпоху в каждой записи истории; «запросы действий» —
   начатые с последней смены страницы (`Agent._anchor`: последнее действие или ожидание, после которого страница
   изменилась).
2. Факт для Jev: `state.page.loading` = «Page is still loading: N network request(s) started by recent actions have not
   finished yet.» (`model.LOADING`) — только число, одинаково в цели, сценарии и проверке; при 0 ключа нет (снимок тела
   режима цели не изменился). Стенд пишет `loading` в решения.
3. Перед вопросом Jev (`Agent._await_loading`): запросы действий пережили ожидание после действия — ещё одно
   `await_change` (тот же предохранитель, будит ответ) и свежий снимок; одно на якорь.
4. «No page change after 3» (`Agent._stalled`): перед blocked — `await_change` и свежий снимок (`_look_again`), раз на
   смену страницы, и снова, пока запросы действий в полёте; запись ожидания разрывает серию.
5. WAIT и `await_change` будились завершением любого запроса вкладки и раньше, в том числе фонового (`moved()` сравнивает
   `network_marks`, `_finish_request` считает и фоновые) — добавлен тест-закрепление.

B — режим цели: TYPE_TEXT исполнен и поле показывает текст → `Invariant` (узел, ключ поля, текст); проверка
`Tab.field_values` — там же, где в сценарии: перед исполнением каждого решения, значит и перед DONE. Снимают — клик не
по полю и новый ввод в то же поле. Пропал → решение отброшено, запись TYPE_TEXT убрана из истории для Jev
(`_roll_back_typed`), текст запомнен (`_retype`) — повторный ввод в то же поле берёт его без текстовой модели; пропаж
одного текста в поле больше `RETYPE_LIMIT` (2) — `blocked` «typed text does not stay in the field '<поле>'». Не виден
сразу после ввода — прежняя пометка `TEXT_VANISHED` плюс тот же счёт и тот же повторный текст.

### Какие числа остались и почему

| Число | Где | Почему |
| --- | --- | --- |
| `Thresholds.wait_fuse_s = 1.5` | `config.py` | не менялся; лишнее ожидание перед решением — им же |
| одно лишнее ожидание на якорь | `agent.py` `_await_loading` | счётчик: long-poll действия держит не больше одного |
| `NO_PROGRESS_STEPS = 3` | `agent.py` | прежний счётчик; перед blocked — одно ожидание (флаг, не время) |
| `RETYPE_LIMIT = 2` | `agent.py` | тот же, что в сценарии |

Сколько раз `_stalled` ждёт, пока запросы в полёте, ограничивают прежние лимиты: `max_steps`, бюджет решений
`2·max_steps + M`, `STEP_ACTIONS_LIMIT` в сценарии (на каждое ожидание — ещё 3 действия без изменений).

### Отклонения от задания

1. Факт (1) считает не «запросы последнего действия», а запросы действий с последней смены страницы. Спот A1: после ввода
   Jev кликает по полю (клик запросов не начинает) — с «последним действием» факт исчезал на втором же решении, хотя
   ответ поиска ещё в пути; следующее решение без него — клик «Monday», ложный `done`.
2. Добавлено ожидание перед решением (`_await_loading`) — в направлении его не было. Jev на факт не выбирает WAIT: в спотах
   A1 и A2 21 решение с `loading` > 0 — 0 WAIT (все CLICK); в A2 (факт на каждом решении) — 5/12 ложных `done` («Monday»).
   С ожиданием (A3) — 11/12, ложных `done` 0, после ожидания Jev сразу кликает «Рабочий».
3. (2) по коду уже было — добавлен только тест-закрепление; до правки он падал лишь потому, что нет `Tab.in_flight`,
   само пробуждение завершением фонового запроса работало.
4. Флаг второго шанса перед «No page change» — свой (`_stall_look_used`), не общий с BLOCKED: иначе BLOCKED и серия
   кликов на одной странице отнимали бы шанс друг у друга.
5. Инвариант в цели — только если поле показало текст после ввода; не показало — прежняя пометка (Jev решает), чтобы поле,
   которое законно превращает текст (метки-«чипсы», маски), не давало откатов. Счёт пропаж — по (ключ поля, текст):
   разные тексты в одно поле не копятся.
6. Повторный ввод без модели — отдельной памятью `_retype`: прежний кэш `_pending_text` работает только при идентичном
   контексте, а после пересоздания поля контекст (история, снимок) другой.
7. Фильтр ячеек в `eval.py` не добавлял: `--sweep grid` с узкими диапазонами даёт те же id ячеек, что оси
   (`--delay-range 3000 --remount-range 0 --sendstatus 0 --net 0,1`).
8. Проверка инварианта цели — перед исполнением решения (как в сценарии), не до вызова Jev: решение по снимку с текстом
   («Send») отбрасывается ценой одного лишнего вызова Jev.

### Проверка

- `bash scripts/check.sh` → 0 (703 теста, 3 пропуска); `pyright browser_hands scripts` — 0 ошибок; `eval.py
  --fixtures-only` — все `ok` (настоящий headless Chrome, бесплатно).
- Новые тесты: `test_cdp` (фоновые в `in_flight_since`), `test_browser` (4: запросы действия после предохранителя,
  WAIT будит ответ фонового запроса, навигация — не эпоха действия, вкладка пользователя без сети), `test_model` (2:
  факт во всех режимах, `choose` его шлёт), `test_eval` (`loading` в решениях), `test_agent` (11 новых + 2 переписаны:
  «3 без изменений» теперь 3 + ожидание + 3). До правки падали все, кроме закрепления «клик не по полю снимает
  инвариант» (он о том, чего правка не должна сломать).
- Точечно (`--sweep grid` с узкими диапазонами, по 3 прогона, $0,064 всего):

| спот | код | ячейки | verified | ложный `done` |
| --- | --- | --- | --- | --- |
| A1 | fca9d9a | d3000 n0/n1 × цель/сценарий | 11/12 | 1 («Monday», факт только на первом решении) |
| A2 | 864ace9 | то же | 6/12 | 5 («Monday»; 15 решений с `loading` > 0 — 0 WAIT) |
| A3 | 041060d | то же | 11/12 | 0 (провал — шаг 2 закрыт по `step_done` 0,76) |
| B | fca9d9a | d700 r1500/1600/2000 s1500, цель | 9/9 | 0 |
| B2 | fca9d9a | d700 r1600 s0, цель | 3/3 | 0 |

- Полная развёртка (041060d, `--sweep axes --runs 5 --mode goal,scenario --label fix`, $0,194; отчёт `sweep_report.py
  after.jsonl fix.jsonl`): **цель 73 → 89 из 90, сценарий 82 → 87 из 90**, ложных `done` 5 → 1. Ни одна ячейка не
  хуже «стало». Медиана `elapsed` 5,7 → 5,4 с; `wait` на d3000 net=1 — 2,0 → 3,4–3,5 с (лишнее ожидание ответа).

| ячейка | режим | стало | fix |
| --- | --- | --- | --- |
| d3000 net=1 | цель | 3/5 | 5/5 |
| d3000 net=1 | сценарий | 2/5 | 5/5 |
| d3000 net=0 | цель | 1/5 | 4/5 (ложный `done` — «Monday») |
| d3000 net=0 | сценарий | 0/5 (ложный `done` 1) | 2/5 |
| d1500 net=0 | цель | 4/5 | 5/5 |
| remount 1500 | цель | 3/5 (ложный `done` 1) | 5/5 |
| remount 2000 | цель | 2/5 | 5/5 |
| sendstatus 0, remount 1600 | цель | 2/5 (ложных `done` 2) | 5/5 |
| sendstatus 1500, remount 1600 | цель | 3/5 (ложный `done` 1) | 5/5 |

- В развёртке: ожиданий перед решением (`_await_loading`) — 10, ожиданий перед «No page change» — 19, откатов текста в
  режиме цели — 13, `blocked` по пропаже текста — 0; повторный ввод в поле сообщения — в 14 прогонах цели, лишних
  вызовов текстовой модели нет (3 прогона с тремя вызовами — повторы из-за невалидного ответа модели). Факт `loading`
  в развёртке Jev не увидел ни разу: ответ приходил во время ожидания перед решением.

### Где не дотянул и почему

- **d3000 net=0, сценарий — 2/5** (цель — 4/5 из 4/5 нужных). Сигнала нет (таймер); ожидание перед blocked пропускает
  дальше, но два прогона кончились тем, что шаг 2 «открыть чат» закрыт по `step_done` 0,76/0,77, когда строка «Рабочий»
  видна в результатах, а чат не открыт (действие решения — тот же клик по строке — по правилу не исполняется) → шаг 3
  без чата → `blocked`. Это порог `step_done_min_p` (0,75; калибровка §1), не ожидание; менять порог без расчёта нельзя.
  Третий провал — `ModelTimeout` Jev (10 с, провайдер).
- **Ложный `done` 1** (d3000 net=0, цель): третьим действием Jev кликнул «Monday» по неотфильтрованному списку, до
  правила «3 без изменений»; сети нет — ни факта, ни ожидания. Класс есть и в «стало» (сценарий d3000 net=0); кодом
  не ловится — сообщение ушло в чужой чат по решению Jev.

### Не проверено

- Живой WhatsApp: во вкладке пользователя сеть не включается (`USER_TAB_NETWORK = False`) — A там не действует
  (`in_flight` 0), B действует (DOM-проверка поля).
- delay 5000 и long-poll на настоящем сайте: одно лишнее ожидание на действие и ожидания `_stalled` — только тестами.
- Влияние факта `loading` на Jev: в спотах 21 решение с фактом — 0 WAIT; в развёртке факт не понадобился.
- Шаги сценария без `text` (текст от модели) — инварианта по-прежнему нет; поле, которое законно очищается без клика
  (автоотправка), в режиме цели даст откат и повторный ввод (после 2 — `blocked`).
