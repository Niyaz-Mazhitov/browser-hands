"""README, описания инструмента и decisions.md не расходятся с кодом вкладки пользователя."""

import ast
import re
from pathlib import Path

import pytest

from browser_hands import agent, browser, model
from browser_hands.config import ModelConfig
from browser_hands.scenario import MAX_DO, MAX_STEPS, MAX_TEXT, ScenarioStep, parse_steps
from browser_hands.server import BROWSE_DESCRIPTION, INSTRUCTIONS, STEPS_DESCRIPTION, URL_DESCRIPTION, format_result
from tests.fakes import make_result

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
RULE = "если задан только сайт или ровно эта страница"


def section(text: str, title: str) -> str:
    start = text.index(title)
    end = text.find("\n#", start + len(title))
    return text[start : end if end != -1 else None]


def flat(text: str) -> str:
    return " ".join(text.split())


def test_user_tab_rule_is_the_same_in_readme_and_tool_descriptions():
    assert RULE in flat(URL_DESCRIPTION) and RULE in flat(BROWSE_DESCRIPTION)
    assert "`url` задан только сайт или ровно эта страница" in flat(section(README, "### Какую вкладку берёт агент"))
    assert "того же сайта" not in URL_DESCRIPTION + BROWSE_DESCRIPTION


def test_readme_states_both_empty_page_ceilings():
    text = flat(README)
    assert f"в начале прогона — до {agent.EMPTY_PAGE_WAIT_S:g} с" in text
    assert f"в середине (после первого решения Jev) — до {agent.EMPTY_PAGE_WAIT_LATER_S:g} с" in text
    assert f"пауза до {agent.EMPTY_PAGE_WAIT_LATER_S:g} с" in text  # «Ограничения»


def test_readme_limitations_state_settle_and_single_retries():
    limits = flat(section(README, "## Ограничения"))
    quiet = f"{browser.SETTLE_QUIET_MS / 1000:g}".replace(".", ",")
    ceiling = f"{browser.SETTLE_CEILING_MS / 1000:g}".replace(".", ",")
    assert f"агент ждёт тишины DOM {quiet} с (не дольше {ceiling} с)" in limits
    assert f"добавляет до {ceiling} с на шаг" in limits
    assert "На BLOCKED переспрашивает один раз" in limits
    assert agent.TEXT_ATTEMPTS == 2 and "повторяет запрос один раз" in limits


def test_readme_states_model_request_limits():
    m = ModelConfig()
    assert f"| `BROWSER_HANDS_JEV_TIMEOUT_S` | `{m.jev_timeout_s:g}` |" in README
    assert f"| `BROWSER_HANDS_TEXT_TIMEOUT_S` | `{m.text_timeout_s:g}` |" in README
    limits = flat(section(README, "## Ограничения"))
    assert f"Jev — {m.jev_timeout_s:g} с, текстовая модель — {m.text_timeout_s:g} с" in limits
    assert "на невалидный ответ текстовой модели, ошибку провайдера или таймаут повторяет запрос один раз" in limits


def test_readme_security_warns_about_drafts_and_focus_emulation():
    security = flat(section(README, "## Безопасность и приватность"))
    assert "неотправленный черновик в этом поле сотрётся" in security
    assert "focus emulation" in security and "прочитанными" in security and "«в сети»" in security


def test_decisions_have_the_post_review_items():
    decisions = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
    after = section(decisions, "### После ревью")
    assert decisions.index("## Вкладка пользователя (feat/reuse-tab)") < decisions.index("### После ревью")
    assert re.findall(r"^(\d)\. ", after, re.MULTILINE) == [str(n) for n in range(1, 8)]


SCENARIO = "### Сценарий (`steps`)"


def test_readme_states_scenario_limits_from_scenario_py():
    text = flat(section(README, SCENARIO))
    assert f"до {MAX_STEPS} шагов (и не больше `max_steps`)" in text
    assert f"`do` — 1–{MAX_DO} символов, `text` — 1–{MAX_TEXT} символов" in text
    assert "**по-английски**" in text and "**дословно**" in text
    assert "`browse(url, goal?, steps?, max_steps?, timeout_seconds?, keep_open?, new_tab?)`" in flat(README)


def test_tool_texts_offer_steps():
    assert "goal или steps" in BROWSE_DESCRIPTION and "goal|steps" in INSTRUCTIONS
    assert "по-английски" in STEPS_DESCRIPTION and "дословно" in STEPS_DESCRIPTION


def shape(line: str) -> str:
    """Строка итога сценария без чисел и `do`: «остановился на шаге # из #» — сравнить README и format_result."""
    return re.sub(r"\b(\d+|[kNM])\b", "#", line.split(":")[0] if "остановился" in line else line)


def test_scenario_lines_in_readme_match_format_result():
    steps = [ScenarioStep(f"Step {i}") for i in range(1, 5)]
    lines = format_result(make_result("blocked", steps=3, scenario=(2, 4)), steps=steps).splitlines()
    summary, stopped = lines[1], lines[2]
    assert stopped == "остановился на шаге 3 из 4: Step 3"
    readme = flat(README)
    assert "`сценарий: k из M выполнено`" in readme and shape(summary) == shape("сценарий: k из M выполнено")
    assert "`остановился на шаге N из M: <do>`" in readme
    assert shape(stopped) == shape("остановился на шаге N из M: <do>")
    assert "`[шаг k]`" in readme and "[шаг 2]" in "\n".join(lines)


def test_readme_whatsapp_example_is_a_valid_scenario():
    (block,) = [b for b in re.findall(r"```python\n(.*?)```", README, re.S) if "web.whatsapp.com" in b]
    call = ast.parse(block).body[0].value
    assert isinstance(call, ast.Call) and ast.literal_eval(call.args[0]) == "https://web.whatsapp.com"
    (keyword,) = call.keywords
    steps = parse_steps(ast.literal_eval(keyword.value))
    assert len(steps) == 4
    assert all(step.do.isascii() or "«" in step.do for step in steps)  # do — по-английски (имя чата — как есть)
    assert [step.text for step in steps] == ["Рабочий", None, "это я через агента, проверка 👋", None]
    assert "необратимо" in section(README, SCENARIO)


def core_constant(name: str) -> object:
    """Константа сценариев ядра (§4.2 плана) — после слияния `feat/scenarios-core`; до него тест пропускается."""
    for module in (agent, model):
        if hasattr(module, name):
            return getattr(module, name)
    pytest.skip(f"{name}: ядро без сценариев (ещё не влито)")


def test_readme_states_core_scenario_limits():
    text = flat(section(README, SCENARIO))
    limit = core_constant("STEP_ACTIONS_LIMIT")
    threshold = core_constant("STEP_DONE_MIN_P")
    assert f"На шаг — не больше {limit} действий" in text
    assert f"с вероятностью не ниже {threshold:g}".replace(".", ",") in text


def test_readme_says_step_texts_never_reach_the_text_model():
    text = flat(section(README, SCENARIO))
    assert "текстовой модели тексты шагов не передаются ни на каком шаге" in text
    assert "без `goal` прогон останавливается — `blocked` с ошибкой `step k of M: no text given for typing`" in text
    assert "текстовая модель на таком шаге не вызывается" not in text
    security = flat(section(README, "## Безопасность и приватность"))
    assert "сценарий `steps` (`do` и `text`; `text` — только Jev)" in security
