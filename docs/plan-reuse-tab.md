# План: работа в уже открытой вкладке пользователя (`feat/reuse-tab`)

Дата: 2026-09-25. База: `feat/reuse-tab` = `feat/initial` (29675f3, 209 тестов). Решения пользователя 1–7 из задания
приняты и здесь не пересматриваются. Строки кода — по состоянию 29675f3.

## 0. Что решает пользователь (до старта пакетов)

| # | Вопрос | Предлагаю |
| --- | --- | --- |
| 1 | Потолок ожидания «страница без элементов» (решение 6) | 25 с (`EMPTY_PAGE_WAIT_S`), но не дольше дедлайна прогона. Второй живой прогон WhatsApp занял 15,4 с целиком, из них загрузка — 3 WAIT; 25 с покрывает холодный кэш с запасом, а на пустой странице теряется не больше 25 с и 0 вызовов Jev |
| 2 | Несколько вкладок одного хоста | Сначала точное совпадение `url` (без `#fragment`), иначе первая по порядку `Target.getTargets`. CDP не даёт «активную/последнюю» вкладку (поля `TargetInfo`: `targetId, type, title, url, attached, openerId, parentId, browserContextId, subtype` — проверено по `devtools-protocol` 0.0.1638949). Дороже, но точнее: подключиться к каждой и спросить `document.visibilityState` — не делать в первой версии |
| 3 | Вкладка с `attached: true` (у неё уже есть клиент: DevTools пользователя или второй сервер browser-hands из другой сессии Claude Code) | Пропускать; если других нет — своя вкладка + строка INFO «вкладка <host> занята другим клиентом». Два агента в одной вкладке — гонка кликов |
| 4 | Значение `RunResult.tab`, когда вкладки не было (failed до старта: нет ключа, Chrome недоступен) | `None` (третье значение к `"user"/"new"`); строка «вкладка: …» тогда не печатается. Иначе «вкладка: новая» врёт |
| 5 | `keep_open`/`tab_kept` для вкладки пользователя | `tab_kept=False`, `keep_open` игнорируется (она и так остаётся). Строка ответа: «вкладка: твоя (host)» |
| 6 | Текст тестового сообщения в чат «Кеша» и время живой проверки (§7) | Согласовать заранее; один короткий текст без личных данных |
| 7 | Где гонять тесты | Офлайн, сейчас 209 тестов; на маке допустимо, если `bash scripts/check.sh` укладывается в 1–2 мин |
| 8 | Worktree | Новые `../browser-hands-reuse-core` (`feat/reuse-tab-core`) и `../browser-hands-reuse-shell` (`feat/reuse-tab-shell`) от контрактного коммита. Старые `../browser-hands-core` (feat/core) и `../browser-hands-shell` (feat/shell) не трогать — они не влиты в `feat/initial` по `git branch --merged`, удаление — отдельное решение |

Приняты 25.09.2026 (основная сессия): п. 1–5, 7, 8 — как предложено; п. 6 — текст согласуется с пользователем перед живой проверкой.

## 1. Факты из кода, на которые опирается план

- Вкладка всегда своя: `chrome.py:258` `Target.createTarget {url: about:blank, background: true}` → `owned_targets`;
  `chrome.py:261` `attachToTarget flatten`; `browser.py:189-196` `setup()` = `Emulation.setDeviceMetricsOverride`
  1120×780 DPR 1 + `setFocusEmulationEnabled true`; `agent.py:170` `navigate`; `agent.py:207-212` конец — `release()`
  при `keep_open`, иначе `close()` → `Target.closeTarget` (`browser.py:218`).
- Чужие id никогда не закрываются: `Chrome._close_targets` (`chrome.py:240`) и `_close_orphans` (`chrome.py:230`) ходят
  только по `owned_targets`; `Chrome.close()` в attach (`chrome.py:288-289`) — то же. Инвариант сохраняем: id вкладки
  пользователя в `owned_targets` не попадает.
- `Tab.release()` (`browser.py:227-237`) уже делает `Target.detachFromTarget`; `decisions.md:39` подтверждает, что Chrome
  при detach снимает эмуляцию viewport. Про focus emulation при detach — не проверено, поэтому выключать явно.
- `TabGone`: `cdp.py:167-170` помечает сессию мёртвой по `Target.detachedFromTarget` (Chrome шлёт его при закрытии
  вкладки любой сессией); `targetDestroyed` без `Target.setDiscoverTargets` не приходит — не полагаться. `release()`
  ловит `(CDPError, CDPTimeout, ChromeDisconnected)`, но не `TabGone` (`browser.py:234`) — при закрытой пользователем
  вкладке detach бросит наружу; `_finish` это проглотит с WARNING (`agent.py:213`), но лучше ловить.
- Скриншот: `browser.py:358-386` — при `scale != 1` берёт `Page.getLayoutMetrics.cssVisualViewport` и шлёт `clip{…,scale}`.
  Спецификация: `clip` в CSS-пикселях (`Page.Viewport`: dip), `cssVisualViewport` — CSS px, `visualViewport` — device px
  (deprecated). Размер картинки = `clip.width × clip.scale × DPR` — по опыту Puppeteer/Playwright, **живьём не
  проверено** (шаг 4.7).
- Скролл шлёт колесо в точку от `self.viewport` (`browser.py:299-305`): для вкладки пользователя нужны реальные
  `innerWidth/innerHeight`, иначе на низком окне точка (550, 650) вне viewport.
- Снимок (`snapshot.js:101-104`): `wait` есть всегда, `scroll_*` — по прокрутке, элементы — `e1..eN` с `kind`
  `fill|click|select`. «Нет элементов для действия» = нет ни одного `kind ∈ {fill, click, select}`.
- Jev вызывается в `_predict` сразу после `observe` (`agent.py:237-246`) — сюда встаёт ожидание из решения 6.
- Обвязка: `RunConfig` (`config.py:57-61`) несёт `keep_open` — параметр одного прогона; `apply_overrides` (`config.py:137`)
  принимает его; MCP-параметры (`server.py:387-394`), CLI (`cli.py:43-50`, `cli.py:77-93`), `format_result`
  (`server.py:339-369`, строка `tab: оставлена открытой`), `failed_result` (`server.py:320`). Тесты обвязки на фейках
  (`tests/fakes.py:14-53` `make_result`, `168-194` `agent_factory`). `tests/fake_cdp.py:94` по умолчанию отдаёт
  `Target.getTargets → []`.
- Ленивая проверка режима: `Chrome.launched` (`chrome.py:173`) = `mode == launch and not ws_url`; «ws» = `ws_url` задан.

## 2. Проектные решения

1. **Поиск вкладки** — `Chrome.find_user_tab(host) -> str | None` (`chrome.py`). Возвращает `None` сразу, если
   `config.mode != "attach"` или задан `ws_url` (решение 7). Иначе `Target.getTargets` и фильтр по порядку:
   `type == "page"`; нет `subtype` (prerender и т. п.); `targetId not in owned_targets`; `url_host(url) == host`
   (регистр — lower); `attached is False` (§0 п. 3). Несколько — §0 п. 2. Лог INFO: только host и число кандидатов.
2. **Подключение** — `Chrome.attach_tab(target_id, *, screenshot_quality, screenshot_scale) -> Tab`:
   `attachToTarget flatten` → `Tab(..., owned=False)`; `tab.setup()` без `setDeviceMetricsOverride`, с
   `setFocusEmulationEnabled true`; один `Runtime.evaluate("[innerWidth, innerHeight, devicePixelRatio]")` →
   `tab.viewport`, `tab.dpr`. Регистрируется в `self._borrowed: dict[str, Tab]` (не в `_tabs`, не в `owned_targets`).
   Ошибка на любом шаге → `detachFromTarget` (не `closeTarget`) и исключение наружу → агент падает в свою вкладку?
   Нет: возвращаем `failed` с причиной — проще и честнее; тихий переход маскирует проблему.
3. **Отсоединение** — только `Tab.release()`: `setFocusEmulationEnabled false` (если включали) → `detachFromTarget`;
   ошибки `(CDPError, CDPTimeout, ChromeDisconnected, TabGone)` — в DEBUG. `Tab.close()` при `owned=False`
   делегирует в `release()` — защита на уровне `Tab`, не только агента. `Chrome.close()` в attach/ws: сначала
   `release(timeout=CLOSE_TAB_TIMEOUT_S)` всем `_borrowed`, потом свои, потом ws. `_close_orphans` не трогает
   `_borrowed`; после переподключения `_borrowed` просто очищается (сессии мертвы, Chrome сам снял эмуляцию).
4. **Скриншот без изменения размера** — `Tab.screenshot()`: для `owned=False` `scale = min(screenshot_scale,
   TARGET_WIDTH / (viewport_w × dpr))`, `TARGET_WIDTH = 1120` (из `config.viewport[0]`); clip как сейчас
   (`cssVisualViewport.pageX/pageY/clientWidth/clientHeight`). 1728×1000 @ DPR 2 → scale ≈ 0,324 → ~1120×648.
   `fromSurface` не трогаем (default true), новых зависимостей нет. Для своей вкладки формула даёт 1.0 — поведение прежнее.
5. **Focus emulation** — включать и в чужой вкладке (нужна для rAF в `SETTLE`, меню и курсора в фоновой вкладке; в
   логах прошлых прогонов иначе не проверялось), возвращать в `release()` и в `Chrome.close()`. Побочный эффект —
   `document.hasFocus()` = true в этой вкладке на время прогона; для WhatsApp это плюс (не считает вкладку неактивной).
6. **Контракт** — `types.py`: `TabKind = Literal["user", "new"]`, `RunResult.tab: TabKind | None = None` (последнее
   поле; `slots=True` — порядок с дефолтами сохранить). `config.py`: `RunConfig.new_tab: bool = False`,
   `apply_overrides(..., new_tab: bool | None = None)`. Сигнатура фабрики агента не меняется (`new_tab` едет в `run`).
   Признак «чужая» — `Tab.owned: bool` (kw-only, default `True`), `Chrome._borrowed`.
7. **Ожидание элементов (решение 6)** — `Agent._await_interactive()` в `_predict` после `observe`: пока в
   `page["actions"]` нет `fill|click|select` и `now < min(start + EMPTY_PAGE_WAIT_S, deadline)`: `_check_cancel()`,
   `tab._sleep(0.25)` (идёт в `wait_ms`), `observe()` (StalePage → как сейчас, через `_tick`). По истечении потолка —
   Jev вызывается как раньше (страница без контролов — легитимный случай, Jev может сказать DONE). Шаги и
   `model_calls` не растут. Одна строка INFO «нет элементов для действия, жду (host)».
8. **Агент** — `_loop`: `host = url_host(self.url)`; `target = None if run.new_tab else chrome.find_user_tab(host)`;
   найдено → `attach_tab`, `self._tab_kind = "user"`, без `navigate`; иначе — как сейчас, `"new"`. `_finish`:
   `not tab.owned or keep_open` → `release()`; `kept = tab.owned and keep_open`. `RunResult(tab=self._tab_kind)`.
9. **Ответ** — `format_result`: `tab == "user"` → `вкладка: твоя (<url_host(result.url)>)`; `"new"` →
   `вкладка: новая` + `, оставлена открытой` при `tab_kept`; `None` → без строки. Старая строка `tab: оставлена открытой`
   уходит (её тест — в обвязке). `--json` получает `tab` через `asdict` без правок.
10. **Приватность** — README: в attach снимок и текст вкладки пользователя (в т. ч. соседние чаты на экране) уходят в
    OpenRouter и в контекст Claude так же, как для своей вкладки; `new_tab=true` — способ этого избежать.

## 3. Шаг 0 — контракт (основная сессия, один коммит на `feat/reuse-tab`)

Файлы: `browser_hands/types.py`, `browser_hands/config.py`, `docs/plan-reuse-tab.md` (этот файл).

1. `types.py`: `TabKind`, `RunResult.tab: TabKind | None = None`, докстринг «правит только контрактный коммит».
2. `config.py`: `RunConfig.new_tab: bool = False`; `apply_overrides(..., new_tab: bool | None = None)` →
   `replace(run, new_tab=new_tab)`. Env-переменной нет (как у `keep_open`).
3. Проверка: `uv run --locked pytest -q` → `209 passed` (дефолты ничего не ломают: `RunConfig(...)`-сравнения в
   `test_server.py:203`, `test_cli.py:70` совпадают за счёт default). `uv run --locked ruff check . && uv run --locked ruff format --check .` → без замечаний.
4. Коммит `contract: RunResult.tab, RunConfig.new_tab`. Откат: `git reset --hard 29675f3`.
5. Worktree (можно сразу оба): `git worktree add ../browser-hands-reuse-core -b feat/reuse-tab-core feat/reuse-tab`,
   `git worktree add ../browser-hands-reuse-shell -b feat/reuse-tab-shell feat/reuse-tab`. Проверка: `git worktree list`
   → 5 строк. Откат: `git worktree remove ../browser-hands-reuse-core && git branch -D feat/reuse-tab-core`.

Пакеты 4 и 5 идут параллельно, файлы не пересекаются:
ядро — `browser_hands/{chrome,browser,agent}.py`, `tests/{fake_cdp,test_chrome,test_browser,test_agent}.py`, `docs/core-notes.md`;
обвязка — `browser_hands/{server,cli}.py`, `README.md`, `tests/{fakes,test_server,test_cli,test_config}.py`.
`docs/decisions.md` — только при слиянии (§6). Каждый шаг — отдельный коммит; откат шага — `git revert <sha>` или `git checkout -- <файл>` до коммита.

## 4. Пакет «ядро» (`../browser-hands-reuse-core`)

Команда проверки пакета: `uv run --locked pytest -q tests/test_cdp.py tests/test_chrome.py tests/test_browser.py tests/test_agent.py`.

4.1 `tests/fake_cdp.py` — `FakeCDPServer.targets: list[dict]` (по умолчанию `[]`), `Target.getTargets` отдаёт его;
`Target.attachToTarget` — как есть (`S-<targetId>`); `Runtime.evaluate` по умолчанию отвечает `{"result": {"value": [1728, 1000, 2]}}`
только если хендлер не переопределён — лучше явно в тестах через `server.on`. Проверка: существующие тесты зелёные.

4.2 `browser.py` — `Tab(..., owned: bool = True)`, `self.dpr = 1.0`, `self._focus_emulated = False`;
`setup(*, emulate_viewport: bool = True)`: metrics только при `emulate_viewport`, focus — всегда, ставит `_focus_emulated`;
`release(timeout=SCREENSHOT_TIMEOUT_S)`: сначала `setFocusEmulationEnabled false` (если `_focus_emulated`), потом detach,
ловит и `TabGone`; `close()`: `if not self.owned: return self.release()`; `screenshot()`: авто-scale для `owned=False`
(§2 п. 4). Тесты `test_browser.py`:
- `test_borrowed_tab_close_only_detaches_and_turns_focus_emulation_off` → методы `["Emulation.setFocusEmulationEnabled", "Target.detachFromTarget"]`, `closeTarget` нет, `enabled: False`;
- `test_borrowed_tab_setup_never_overrides_device_metrics` → нет `Emulation.setDeviceMetricsOverride`;
- `test_borrowed_screenshot_scales_to_target_width` (viewport 1728×1000, dpr 2) → `clip.scale == pytest.approx(1120/3456)`, ширина клипа 1728;
- `test_release_swallows_tab_gone` → `TabGone` в `client.call` не выходит наружу; `on_release` вызван.
Ожидание: `test_browser.py` — 20 старых + 4 новых passed.

4.3 `chrome.py` — `find_user_tab(host)`, `attach_tab(target_id, ...)`, `_borrowed`, `close()`/`connect()` по §2 п. 3.
Тесты `test_chrome.py` (фейковый сервер, attach-конфиг с `write_port_file`):
- `test_find_user_tab_matches_host_only_pages_and_skips_own_and_attached` → `targets` = страница `https://web.whatsapp.com/x`, страница `https://example.test`, `type: "iframe"` с тем же хостом, своя вкладка (id из `new_tab()`), страница с `attached: True`, страница с `subtype: "prerender"` → возвращает ровно id первой;
- `test_find_user_tab_prefers_exact_url_then_list_order` (две подходящие);
- `test_find_user_tab_is_none_in_launch_and_ws_modes` → `Target.getTargets` не вызывался (по `server.methods()` после `connect()` — учесть вызов из `alive()`; проверять, что после `connect()` новых `getTargets` нет);
- `test_attach_tab_sends_no_create_no_metrics_and_close_never_closes_it` → кадры: `attachToTarget {targetId, flatten: True}`, `Emulation.setFocusEmulationEnabled`, нет `createTarget`/`setDeviceMetricsOverride`; `chrome.close()` → `setFocusEmulationEnabled false` + `detachFromTarget`, `closeTarget` не было, `owned_targets == set()`;
- `test_reconnect_does_not_close_a_borrowed_tab` (обрыв ws, `connect()` → `closeTarget` не было, `_borrowed` пуст);
- `test_attach_tab_failure_detaches_and_raises` (`server.on["Emulation.setFocusEmulationEnabled"] = error`) → исключение, `detachFromTarget` есть, `closeTarget` нет;
- `test_close_with_a_busy_worker_detaches_borrowed_in_about_a_second` (по образцу `test_launch_close_with_a_busy_worker…`, `chrome.py:254-277`).
Ожидание: 18 старых + 7 новых passed; в `test_attach_close_closes_only_own_tabs…` (`test_chrome.py:158`) список `Emulation.*` кадров не меняется.

4.4 `agent.py` — `_tab_kind`, выбор вкладки, `_finish`, `RunResult(tab=…)` по §2 п. 8. Тесты `test_agent.py`
(`make_agent`: `chrome.find_user_tab.return_value = "T-user"`, `chrome.attach_tab.return_value = tab`, `tab.owned = False`):
- `test_user_tab_is_used_without_navigation_and_only_released` → `navigate` не вызван, `release` один раз, `close` нет, `result.tab == "user"`, `tab_kept is False` даже при `keep_open=True`;
- `test_new_tab_flag_skips_the_search` (`RunConfig(new_tab=True)`) → `find_user_tab` не вызван, `new_tab` вызван, `result.tab == "new"`;
- `test_no_user_tab_falls_back_to_own` → `find_user_tab` вернул `None` → `new_tab` + `navigate`, `tab == "new"`;
- `test_user_tab_closed_by_user_is_failed_and_not_closed` (`tab.act.side_effect = TabGone`) → `failed`, `close` не вызван, `release` вызван, `screenshot_jpeg is None`;
- `test_cancel_in_user_tab_releases_and_never_closes` (по образцу `test_cancel_during_the_model_call…`, `test_agent.py:312`);
- `test_failed_before_tab_has_tab_none` (нет ключа → `ValueError` до вкладки — проверять `tab is None` на отменённом до старта прогоне `test_cancel_before_run_opens_no_tab…`).
Ожидание: 27 старых + 6 новых passed. Старые `tab.close.assert_called_once()` остаются — у Mock `owned` не задан, но агент смотрит на `tab.owned`; в `make_tab()` явно поставить `tab.owned = True`.

4.5 `agent.py` — `_await_interactive()` (§2 п. 7), `EMPTY_PAGE_WAIT_S = 25.0` (значение из §0 п. 1). Тесты:
- `test_no_interactive_elements_delays_the_model_until_they_appear` → `observe` отдаёт 3 страницы только с `wait`, затем нормальную; `choose` вызван 1 раз, `steps == []` до него, `tab._sleep` вызван 3 раза, `model_calls == 1`;
- `test_empty_page_wait_stops_at_ceiling_then_asks_the_model` (подмена `time` как в `test_timeout_when_deadline…`, `test_agent.py:197`) → после 25 «секунд» `choose` вызван на пустой странице;
- `test_empty_page_wait_respects_deadline_and_cancel` → `timeout` без вызова Jev; `cancel.set()` во время ожидания → `failed: cancelled`, `choose` не вызван.
Ожидание: +3 passed, `model_calls` не растёт в ожидании.

4.6 `docs/core-notes.md` — абзац «вкладка пользователя»: что шлём, что не шлём, как отцепляемся; формула scale. Проверка: `uv run --locked ruff format --check .` (md исключён) — просто глазами.

4.7 Замер живьём (основная сессия, не пакет; сюда — ожидаемое): `Page.captureScreenshot` с `clip.scale=1120/(innerWidth×dpr)`
на вкладке пользователя даёт JPEG шириной 1100–1140 px (проверить `python -c "import struct…"` или открыть файл). Если ширина
= 2× ожидаемой — DPR в формуле лишний: убрать `× dpr` (одна строка в `browser.py`), тест 4.2 поправить.

Критерии готовности ядра: `uv run --locked pytest -q` в worktree → все passed (209 + ~20); `ruff check` и `ruff format --check`
чистые; `grep -n "closeTarget" browser_hands/chrome.py browser_hands/browser.py` — только в `_close_targets`, `new_tab`-откате
и `Tab.close()` под `if self.owned`; в `chrome.py` нет `setDeviceMetricsOverride` вне `Tab.setup(emulate_viewport=True)`.

## 5. Пакет «обвязка» (`../browser-hands-reuse-shell`)

Команда проверки пакета: `uv run --locked pytest -q tests/test_server.py tests/test_cli.py tests/test_config.py tests/test_contract.py`.

5.1 `tests/fakes.py` — `make_result(..., tab: str | None = "new")`; `FakeCore.agent_factory` без изменений (`new_tab` уже в `run`).
Проверка: старые тесты зелёные.

5.2 `server.py` — параметр инструмента `new_tab: Annotated[bool, Field(description="всегда своя фоновая вкладка, даже если сайт открыт у вас")] = run.new_tab`;
`BrowseService.browse(..., new_tab: bool | None = None)` → `apply_overrides(..., new_tab=new_tab)`; `BROWSE_DESCRIPTION`:
«Выполняет goal в Chrome: в attach — в вашей открытой вкладке того же сайта, иначе в новой фоновой. Кликает, печатает, выбирает. DONE не гарантирует успех — смотри скриншот.» (≤200 символов, слово «скриншот» — `test_server.py:79-80`);
`format_result` по §2 п. 9. Тесты `test_server.py`:
- `test_arguments_reach_agent` дополнить `new_tab=True` → `agent["run"].new_tab is True`;
- `test_list_tools…`: `schema["properties"]["new_tab"]["default"] is False`;
- `test_format_result_marks_missing_parts`: `make_result(..., tab="new", tab_kept=True)` → `вкладка: новая, оставлена открытой`; `tab="user", url="https://web.whatsapp.com/"` → `вкладка: твоя (web.whatsapp.com)`; `tab=None` → строки `вкладка:` нет; старой `tab: оставлена открытой` нет;
- `test_failed_result_has_no_tab` (`failed_result` → `tab is None`).
Ожидание: 30 старых + 2 новых passed, `len(BROWSE_DESCRIPTION) <= 200`.

5.3 `cli.py` — `run.add_argument("--new-tab", action="store_true", default=None, help="attach: своя вкладка, даже если сайт уже открыт у вас")`;
`_settings`: `run_flags["new_tab"] = args.new_tab`. Тесты `test_cli.py`: в `test_flags_override_env` добавить `--new-tab` →
`run.new_tab is True`; `test_run_json_has_no_screenshot_bytes` → `data["tab"] == "new"`; `test_help_lists_both_commands` — `--new-tab` в help.
Ожидание: 12 старых passed с правками.

5.4 `tests/test_config.py` — `apply_overrides(settings, new_tab=True).run.new_tab is True`, `None` не меняет. Ожидание: +1 passed.

5.5 `README.md` — правки: «Режимы браузера / attach» (вкладка того же хоста → работа в ней; не закрываем, не меняем размер, не
переходим; `new_tab`), «CLI» (`--new-tab`), «Claude Code» (`browse(..., new_tab?)`), «Ответ browse» (строка `вкладка: …`,
JPEG своей вкладки 1120×780, вкладки пользователя — до ~1120 px по ширине при её пропорциях), «Безопасность и приватность»
(§2 п. 10: снимок и текст вашей вкладки, включая то, что на экране рядом, уходят в OpenRouter и в контекст Claude; агент
действует в вашей сессии сайта; `new_tab=true`/launch — чтобы этого избежать), «Ограничения» (без навигации в вашей
вкладке — агент начинает с того, что открыто; фоновая вкладка пользователя тоже троттлится). Проверка: `grep -n "new_tab\|--new-tab\|вкладка: твоя" README.md` → ≥4 строк.

Критерии готовности обвязки: `uv run --locked pytest -q` в worktree → все passed (209 + ~4, часть старых изменены); ruff чист;
`uv run --locked browser-hands run --help | grep -- --new-tab` → строка есть; `tools/list` через `test_serve_answers_initialize…` — без правок.

## 6. Слияние и проверка (основная сессия)

1. Влить `feat/reuse-tab-core`, затем `feat/reuse-tab-shell` в `feat/reuse-tab` (`git merge --no-ff`); конфликтов быть не должно —
   разные файлы. Проверка: `git diff --stat feat/reuse-tab~2 feat/reuse-tab` — только ожидаемые файлы.
2. `bash scripts/check.sh` → ruff чист, `pytest` все passed (~235), `node --check` ок.
3. `docs/decisions.md` — раздел «Работа во вкладке пользователя, 25.09.2026»: решения 1–7 пользователя, выбранные варианты §0, что не проверено.
4. Живая проверка — §7. Только после неё — `git push origin feat/reuse-tab` и PR в `feat/initial`/`main` (merge — спрашивать).
Откат всего: ветка `feat/reuse-tab` на 29675f3 (`git reset --hard 29675f3`), worktree удалить.

## 7. Живая проверка (только основная сессия с пользователем)

Подготовка: Chrome с включённой удалённой отладкой; WhatsApp Web открыт и залогинен в вкладке пользователя; пользователь
называет размер окна (или снимаем `innerWidth/innerHeight` в DevTools) и текст сообщения (§0 п. 6); `.env` с ключом;
MCP-сервер перезапущен на `feat/reuse-tab` (`claude mcp list` → Connected).

1. `browse(url="https://web.whatsapp.com", goal="Открой чат «Кеша» и отправь сообщение «<текст>». Остановись после отправки.")`.
   Ожидание: `status: done`; `вкладка: твоя (web.whatsapp.com)`; окна «Использовать здесь» нет; в чате ровно одно новое
   сообщение; вкладка открыта, размер прежний (пользователь подтверждает); скриншот ~1120 px по ширине, пропорции окна.
   Лог stderr: `find_user_tab` нашёл 1 кандидата; нет `setDeviceMetricsOverride`, `closeTarget` для этого id.
2. Экран загрузки: перезагрузить WhatsApp (F5) и в течение 2 с вызвать `browse(...goal="скажи, открыт ли чат «Кеша»…")` без
   подсказки «жди загрузку». Ожидание: не `blocked` за 3 с; в ответе `wait` ≥ 3 с, `model calls` без пустых вызовов (первая
   строка шага — не WAIT на логотипе). Стоимость ≤ предыдущего прогона.
3. Отмена: запустить `browse` с длинной целью и нажать Esc через ~3 с. Ожидание: `failed: cancelled`, вкладка пользователя
   открыта, курсор/фокус в порядке (focus emulation снята — проверить, что после отмены WhatsApp не считает вкладку активной вечно: свернуть/развернуть).
4. `new_tab=true` — **не на WhatsApp** (отберёт сессию у вкладки пользователя, а это как раз то, что чиним): на
   `https://en.wikipedia.org/wiki/Main_Page` с целью из README. Ожидание: `вкладка: новая`, вкладка закрыта в конце.
5. Сигнал: во время `browse` во вкладке пользователя — `kill -TERM <pid сервера>`. Ожидание: вкладка открыта, размер прежний.
6. Итоги (время, стоимость, что не сошлось) — в `docs/decisions.md` и `docs/core-notes.md`.

## 8. Риски и что не делать

- **Две вкладки одного хоста** — берём по правилу §0 п. 2; можно попасть не в ту. Для WhatsApp риска почти нет (сайт сам
  держит одну активную). Если всплывёт — вторая версия с пробой `visibilityState`.
- **Два сервера browser-hands** (две сессии Claude Code) — оба видят одну вкладку. Спасает только правило `attached: True`
  → пропускать (§0 п. 3). Не проверено живьём, что `attached` отражает flatten-сессии другого клиента — проверить в §7 шаге 1
  (открыть DevTools на вкладке WhatsApp → ожидание: агент уходит в свою вкладку с INFO-строкой).
- **Скриншот фоновой вкладки пользователя** без device metrics override может прийти пустым/чёрным или зависнуть до 5 с
  (`SCREENSHOT_TIMEOUT_S`) — Chrome не рисует скрытые вкладки. Своя фоновая вкладка с override снималась нормально
  (`traces/attach.jpg`), без override — не проверено (§7 шаг 1). Если пусто — обсуждать `Emulation.setDeviceMetricsOverride`
  с реальными размерами вкладки (не меняет её вид, но это уже отклонение от решения 3) — отдельным решением пользователя.
- **Формула scale × DPR** — не проверена (§4.7). Цена ошибки — картинка в 2 раза больше/меньше, правка одной строки.
- **Focus emulation при обрыве ws** — снимает ли Chrome сам, не проверено; при `kill -9` сервера вкладка пользователя может
  остаться «в фокусе» до перезагрузки страницы. В README — «после kill -9 перезагрузите вкладку».
- **Ожидание 25 с** на странице, где контролов нет по замыслу (чистый текст без ссылок): прогон стартует на 25 с позже.
  Редко; ограничено дедлайном.
- **Приватность**: всё, что видно во вкладке пользователя, уходит в модель. Не смягчать в коде — только README и `new_tab`.
- **Не делать:** тихий переход «не удалось прикрепиться → своя вкладка» (маскирует ошибки); `Target.activateTarget`
  (переключит окно пользователя); `Target.closeTarget`/`Page.navigate`/`Page.reload`/`setDeviceMetricsOverride` для
  `owned=False`; `Target.setDiscoverTargets` (поток событий на весь браузер ради одного признака — `detachedFromTarget`
  хватает); env-переменную для `new_tab` (параметр прогона, как `keep_open`); менять `feat/core`/`feat/shell` и их worktree;
  запускать `new_tab=true` на WhatsApp; писать в `docs/decisions.md` из пакетов (конфликт при слиянии).
