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

- `Agent(...)` бросает `ValueError` при пустых url/goal и `max_steps < 1`; `run()` — `ValueError`, если нет ключа Jev.
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
