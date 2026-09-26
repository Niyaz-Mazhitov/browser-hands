"""Калибровка порогов модели по трассам eval (docs/plan-waits.md §6.3). Офлайн: без моделей, сети и Chrome.

    uv run --locked python scripts/calibrate.py --out docs/calibration.md

Вход — строки прогонов `scripts/eval.py` (JSONL: решения Jev с `confidence`/`step_done`/`scenario_step`, исполненные
шаги, итог проверки по DOM `verified` и отчёт страницы `report`); строки-сводки и прогоны без решений (bench)
пропускаются. Трассы только читаются.

Пары «оценка → правда» (метки — docs/calibration.md, «Как размечено»):
- §1 `step_done` решения под шаг k: шаг k выполнен в момент решения. Нетекстовый шаг выполнен к концу прогона
  (прогон verified или отчёт страницы это показывает) — «да» у решений после последнего исполненного CLICK/TYPE_TEXT/
  SELECT этого шага, «нет» — до него; не выполнен к концу — «нет» у всех. Текстовый шаг — «нет» до первого ввода его
  текста (поле в начале пусто), после ввода — не размечается (шаг закрывает код). Отдельно DONE и не-DONE.
- §2 `confidence` исполненных CLICK/TYPE_TEXT/SELECT в сценарии: «полезно» — действие совпало (операция и элемент) с
  последним действием шага, выполненного к концу прогона. Режим цели — прокси (verified), только для справки.
- §3 `confidence` последнего DONE прогона в режиме цели → verified.
- §4 `wait_fuse_s`: «действие → последняя значимая мутация» — из отчётов страницы с `t_ms` (`reports`, §7.2) и файлов
  замера `wa-observe-*.json` (§8); нет данных — текущее.

Порог θ на сетке 0,05: принять (закрыть шаг, исполнить действие, поверить DONE) при оценке ≥ θ. Стоимость
`FP·C_FP + FN·C_FN` (ложное принятие стоит прогон, ложный отказ — лишний вызов Jev); θ* — минимум (при равенстве —
ближе к текущему). Допустимые θ — стоимость меньше, чем у θ*, плюс одна ложная приёмка. Мало данных (в двух корзинах
вокруг θ* меньше `MIN_BIN` пар) — оставить текущий и сказать, сколько собрать (ширина Wilson); данных хватает, но
текущий допустим — тоже оставить; иначе — θ*.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from browser_hands.config import Thresholds

ROOT = Path(__file__).resolve().parent.parent
# свои трассы и трассы worktree (`.wt/<пакет>/traces`); трассы других копий — `--traces`
DEFAULT_TRACES = ("traces/*.jsonl", ".wt/*/traces/*.jsonl")
DEFAULT_OBSERVE = ("traces/wa-observe-*.json",)
STEP = 0.05
GRID = tuple(round(i * STEP, 2) for i in range(int(round(1 / STEP)) + 1))
C_FP = 1.0  # ложное закрытие шага / действие / DONE — прогон ($0,001 и ~7 с)
C_FN = 0.05  # ложный отказ — лишний вызов Jev ($0,0002 и ~0,7 с)
MIN_BIN = 30  # пар в двух корзинах вокруг θ*, меньше — «мало данных»
Z = 1.96  # Wilson 95 %
TARGET_HALF_WIDTH = 0.1  # «сколько собрать»: половина ширины Wilson в корзинах вокруг θ* не больше
FRAME_S = 1 / 60  # запас кадра к p99 для wait_fuse_s
SNAPSHOT_MARGIN_MS = 30  # признак шага менялся ближе к моменту снимка — пара неоднозначна
ACTING = frozenset({"CLICK", "TYPE_TEXT", "SELECT"})

# Шаги стенда (scripts/eval.py, CHAT_SCENARIO): что показывает отчёт страницы, если шаг выполнен к концу прогона.
# Нужны только для прогонов без verified; задача не отсюда — такие прогоны не размечаются.
CHAT_TASKS = frozenset({"search", "search-spinner", "boot", "search-remount"})


def _chat_state(step: int, report: Mapping[str, Any], scenario: Sequence[Mapping[str, Any]]) -> bool | None:
    if len(scenario) != 4:
        return None
    chat, message = scenario[0].get("text"), scenario[2].get("text")
    if step == 2:
        return report.get("openChat") == chat
    if step == 4:
        return message in (report.get("sent") or [])
    return None


FINAL_STATE: dict[str, Callable[[int, Mapping[str, Any], Sequence[Mapping[str, Any]]], bool | None]] = {
    task: _chat_state for task in CHAT_TASKS
}


# --- чтение трасс ----------------------------------------------------------------------------------------------------


def expand(patterns: Iterable[str], base: Path = ROOT) -> list[Path]:
    """Файлы по шаблонам (`~` и относительные — от `base`), без повторов (realpath), по порядку имён."""
    found: dict[str, Path] = {}
    for pattern in patterns:
        pattern = os.path.expanduser(pattern)
        if not os.path.isabs(pattern):
            pattern = str(base / pattern)
        for name in glob.glob(pattern):
            found.setdefault(os.path.realpath(name), Path(name))
    return [found[key] for key in sorted(found)]


def load_runs(files: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Прогоны eval с решениями Jev (`decisions`); по файлу — сколько взято. Повтор `run_id` — один раз."""
    runs: list[dict[str, Any]] = []
    per_file: dict[str, int] = {}
    seen: set[str] = set()
    for path in files:
        taken = 0
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or "summary" in row or not isinstance(row.get("decisions"), list):
                continue
            key = str(row.get("run_id") or f"{path}:{n}")
            if key in seen:
                continue
            seen.add(key)
            row["_key"] = key
            runs.append(row)
            taken += 1
        per_file[str(path)] = taken
    return runs, per_file


def mode(run: Mapping[str, Any]) -> str:
    return "scenario" if run.get("mode") == "scenario" else "goal"


# --- разметка --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pair:
    score: float
    truth: bool
    run: str


def _same(decision: Mapping[str, Any], step: Mapping[str, Any]) -> bool:
    return (
        decision.get("op") == step.get("operation")
        and decision.get("target") == step.get("target")
        and decision.get("scenario_step") == step.get("scenario_step")
    )


def align(decisions: Sequence[Mapping[str, Any]], steps: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """Какое решение исполнило какой шаг: {индекс решения: индекс шага}. По порядку; из подряд идущих одинаковых
    решений исполненным считается последнее (первое устарело: `StalePage`, решение переспрошено)."""
    out: dict[int, int] = {}
    j = 0
    for i, decision in enumerate(decisions):
        if j >= len(steps) or not _same(decision, steps[j]):
            continue
        following = decisions[i + 1] if i + 1 < len(decisions) else None
        if following is not None and _same(following, steps[j]):
            continue
        out[i] = j
        j += 1
    return out


@dataclass(slots=True)
class StepView:
    """Решения одного шага сценария и его исход."""

    number: int
    text: str | None
    decisions: list[int]  # индексы решений под этот шаг
    last_action: int | None  # индекс решения, исполнившего последнее CLICK/TYPE_TEXT/SELECT шага
    first_typed: int | None  # индекс решения, напечатавшего текст шага (впервые)
    done_at_end: bool | None  # шаг выполнен к концу прогона; None — неизвестно
    timed: dict[int, bool | None] | None = None  # по истории отчётов: решение → выполнен ли в момент снимка


def _state_at(reports: Sequence[Mapping[str, Any]], t: float) -> Mapping[str, Any] | None:
    before = [r for r in reports if r["t_ms"] <= t]
    return before[-1] if before else None


def timed_truth(
    run: Mapping[str, Any], number: int, mine: Sequence[int], executed: Mapping[int, int]
) -> dict[int, bool | None] | None:
    """Правда шага в момент снимка, по истории отчётов страницы (`reports` с `t_ms`, §7.2), если у шага есть признак в
    отчёте (`FINAL_STATE`). Момент снимка решения i — ответ на решение i−1 плюс его шаг (`text_ms + browser_ms +
    wait_ms`: ввод, ожидание, снимок), не позже `t_ms` решения i без 150 мс (ответ Jev не быстрее). Признак менялся в
    ±`SNAPSHOT_MARGIN_MS` от этого момента — None (неоднозначно). Решение i−1 не исполнено: признак был уже в момент
    ответа на него — «да», не стал и к ответу на i — «нет», иначе None. Нет отчётов или признака — None."""
    reports = [r for r in run.get("reports") or () if isinstance(r, Mapping) and isinstance(r.get("t_ms"), int | float)]
    state = FINAL_STATE.get(str(run.get("task")))
    scenario = run.get("scenario") or []
    if not reports or state is None or state(number, reports[-1], scenario) is None:
        return None
    decisions, steps = run["decisions"], run.get("steps") or []

    def holds(t: float) -> bool:
        current = _state_at(reports, t)
        return bool(current is not None and state(number, current, scenario))

    changes = [r["t_ms"] for k, r in enumerate(reports) if k and holds(r["t_ms"]) != holds(reports[k - 1]["t_ms"])]
    out: dict[int, bool | None] = {}
    for i in mine:
        answered = decisions[i].get("t_ms")
        previous = decisions[i - 1].get("t_ms") if i else 0
        if not isinstance(answered, int | float) or not isinstance(previous, int | float):
            out[i] = None
            continue
        if i - 1 in executed:
            timing = steps[executed[i - 1]].get("timing") or {}
            spent = sum(v for k in ("text_ms", "browser_ms", "wait_ms") if isinstance(v := timing.get(k), int | float))
            shot = min(previous + spent, answered - 150)
            ambiguous = any(abs(t - shot) <= SNAPSHOT_MARGIN_MS for t in changes)
            out[i] = None if ambiguous else holds(shot)
        elif holds(previous):
            out[i] = True
        elif not holds(answered):
            out[i] = False
        else:
            out[i] = None
    return out


def step_views(run: Mapping[str, Any]) -> list[StepView]:
    decisions = run.get("decisions") or []
    steps = run.get("steps") or []
    scenario = run.get("scenario") or []
    executed = align(decisions, steps)
    views: list[StepView] = []
    for number, spec in enumerate(scenario, start=1):
        text = spec.get("text") if isinstance(spec, Mapping) else None
        mine = [i for i, d in enumerate(decisions) if d.get("scenario_step") == number]
        acts = [i for i in mine if i in executed and decisions[i].get("op") in ACTING]
        typed = [i for i in acts if text is not None and steps[executed[i]].get("text") == text]
        if run.get("verified") is True:
            done: bool | None = True
        else:
            state = FINAL_STATE.get(str(run.get("task")))
            report = run.get("report")
            done = state(number, report, scenario) if state is not None and isinstance(report, Mapping) else None
        timed = timed_truth(run, number, mine, executed) if text is None else None
        views.append(StepView(number, text, mine, acts[-1] if acts else None, typed[0] if typed else None, done, timed))
    return views


def step_done_pairs(runs: Iterable[Mapping[str, Any]]) -> tuple[list[Pair], list[Pair]]:
    """§1: пары (P(yes), шаг выполнен) — отдельно не-DONE и DONE (для DONE — только нетекстовые шаги)."""
    other: list[Pair] = []
    done_op: list[Pair] = []
    for run in runs:
        if mode(run) != "scenario":
            continue
        decisions = run["decisions"]
        for view in step_views(run):
            for i in view.decisions:
                decision = decisions[i]
                p = decision.get("step_done")
                if not isinstance(p, int | float) or decision.get("op") is None:
                    continue
                if view.text is not None:
                    # текстовый шаг: до первого ввода поле пусто — «нет»; после — закрывает код, не размечаем
                    if view.first_typed is not None and i > view.first_typed:
                        continue
                    truth = False
                elif view.timed is not None:
                    # история отчётов: состояние страницы в момент снимка, который видел Jev
                    if (known := view.timed.get(i)) is None:
                        continue
                    truth = known
                elif view.done_at_end is None:
                    continue
                elif not view.done_at_end:
                    truth = False
                else:
                    truth = view.last_action is None or i > view.last_action
                pair = Pair(float(p), truth, run["_key"])
                if decision.get("op") == "DONE":
                    if view.text is None:
                        done_op.append(pair)
                else:
                    other.append(pair)
    return other, done_op


def action_pairs(runs: Iterable[Mapping[str, Any]]) -> tuple[list[Pair], list[Pair]]:
    """§2: пары (confidence, действие полезно) для исполненных CLICK/TYPE_TEXT/SELECT: сценарий — по последнему
    действию шага, выполненного к концу прогона; режим цели — прокси «прогон verified» (только для справки)."""
    scenario: list[Pair] = []
    goal: list[Pair] = []
    for run in runs:
        decisions = run["decisions"]
        steps = run.get("steps") or []
        executed = align(decisions, steps)
        if mode(run) == "goal":
            if not isinstance(run.get("verified"), bool):
                continue
            for i in executed:
                decision = decisions[i]
                if decision.get("op") in ACTING and isinstance(decision.get("confidence"), int | float):
                    goal.append(Pair(float(decision["confidence"]), run["verified"], run["_key"]))
            continue
        for view in step_views(run):
            if not view.done_at_end or view.last_action is None:
                continue
            last = decisions[view.last_action]
            for i in view.decisions:
                decision = decisions[i]
                if i not in executed or decision.get("op") not in ACTING:
                    continue
                if not isinstance(decision.get("confidence"), int | float):
                    continue
                useful = decision.get("op") == last.get("op") and decision.get("target") == last.get("target")
                scenario.append(Pair(float(decision["confidence"]), useful, run["_key"]))
    return scenario, goal


def done_pairs(runs: Iterable[Mapping[str, Any]]) -> list[Pair]:
    """§3: режим цели, прогон `done`: уверенность последнего решения (DONE) → verified."""
    out: list[Pair] = []
    for run in runs:
        if mode(run) != "goal" or run.get("status") != "done" or not isinstance(run.get("verified"), bool):
            continue
        decisions = run["decisions"]
        last = decisions[-1] if decisions else {}
        if last.get("op") == "DONE" and isinstance(last.get("confidence"), int | float):
            out.append(Pair(float(last["confidence"]), run["verified"], run["_key"]))
    return out


def settle_samples(runs: Iterable[Mapping[str, Any]], observe: Iterable[Path]) -> tuple[list[float], dict[str, int]]:
    """§4: «действие → последняя значимая мутация», с. Отчёты страницы с `t_ms` (§7.2): последний отчёт между
    действием и следующим решением; момент действия — ответ Jev (`t_ms` решения) плюс время текстовой модели этого
    шага (`text_ms`: в режиме цели текст генерируется между решением и вводом). Прогоны развёртки (`--sweep`: ключи
    `cell`/`axes`) не берутся: их задержки задают оси, p99 там — максимум оси, а не свойство страницы. Файлы замера
    (§8): `mutations.last_significant_ms` после клика."""
    samples: list[float] = []
    sources = {"reports": 0, "observe": 0, "sweep_runs_skipped": 0}
    for run in runs:
        reports = run.get("reports")
        if not isinstance(reports, list) or not reports:
            continue
        if "cell" in run or "axes" in run:
            sources["sweep_runs_skipped"] += 1
            continue
        decisions = run["decisions"]
        steps = run.get("steps") or []
        executed = align(decisions, steps)
        times: list[float] = [
            float(r["t_ms"]) for r in reports if isinstance(r, Mapping) and isinstance(r.get("t_ms"), int | float)
        ]
        for i in sorted(executed):
            answered = decisions[i].get("t_ms")
            end = decisions[i + 1].get("t_ms") if i + 1 < len(decisions) else math.inf
            if not isinstance(answered, int | float) or not isinstance(end, int | float):
                continue
            text_ms = (steps[executed[i]].get("timing") or {}).get("text_ms") or 0
            start = answered + (text_ms if isinstance(text_ms, int | float) else 0)
            inside = [t for t in times if start < t < end]
            if inside:
                samples.append((max(inside) - start) / 1000)
                sources["reports"] += 1
    for path in observe:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        click = data.get("click") if isinstance(data, dict) else None
        last = (data.get("mutations") or {}).get("last_significant_ms") if isinstance(data, dict) else None
        if isinstance(click, dict) and click.get("done") and isinstance(last, int | float):
            samples.append(last / 1000)
            sources["observe"] += 1
    return samples, sources


# --- статистика ------------------------------------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """95 % интервал Wilson для доли k/n; n = 0 — (0, 1)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def needed(k: int, n: int, half_width: float = TARGET_HALF_WIDTH) -> int:
    """Сколько всего пар нужно, чтобы половина ширины Wilson при нынешней доле (нет данных — 0,5) была не больше."""
    p = k / n if n else 0.5
    m = max(n, 1)
    while True:
        low, high = wilson(round(p * m), m)
        if (high - low) / 2 <= half_width:
            return m
        m += 1


def percentile(values: Sequence[float], p: float) -> float:
    """Метод ближайшего ранга (как scripts/bench.py)."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p / 100 * len(ordered))) - 1]


@dataclass(frozen=True, slots=True)
class Row:
    theta: float
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def accepted(self) -> int:
        return self.tp + self.fp

    @property
    def cost(self) -> float:
        return self.fp * C_FP + self.fn * C_FN

    @property
    def tpr(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def fpr(self) -> float | None:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else None

    @property
    def precision(self) -> float | None:
        return self.tp / self.accepted if self.accepted else None


def curve(pairs: Sequence[Pair]) -> list[Row]:
    rows = []
    for theta in GRID:
        tp = sum(1 for p in pairs if p.score >= theta - 1e-9 and p.truth)
        fp = sum(1 for p in pairs if p.score >= theta - 1e-9 and not p.truth)
        fn = sum(1 for p in pairs if p.score < theta - 1e-9 and p.truth)
        tn = sum(1 for p in pairs if p.score < theta - 1e-9 and not p.truth)
        rows.append(Row(theta, tp, fp, fn, tn))
    return rows


def _bin(score: float) -> int:
    """Номер корзины [θ; θ+0,05) на сетке; 1,0 — в последней."""
    return min(int(score / STEP + 1e-9), len(GRID) - 2)


def bins(pairs: Sequence[Pair]) -> list[tuple[float, int, int]]:
    """Корзины [θ; θ+0,05): (θ, n, «да»)."""
    out = []
    for i, theta in enumerate(GRID[:-1]):
        inside = [p for p in pairs if _bin(p.score) == i]
        out.append((theta, len(inside), sum(p.truth for p in inside)))
    return out


@dataclass(slots=True)
class Verdict:
    name: str
    current: float
    recommended: float
    best: float | None  # θ* по стоимости; None — пар нет
    admissible: tuple[float, float] | None
    near: int  # пар в двух корзинах вокруг θ*
    enough: bool
    reason: str
    need_pairs: int = 0
    need_runs: int | None = None


BREAK_EVEN = C_FP / (C_FP + C_FN)  # доля «да», выше которой принимать выгодно (≈ 0,95)


def decide(name: str, pairs: Sequence[Pair], current: float, *, automatic: bool = True) -> Verdict:
    """θ* — минимум стоимости (при равенстве — ближе к текущему, затем ниже); допустимые — стоимость меньше
    стоимости θ* + C_FP (данные их не различают). Данных хватает (у θ* не меньше `MIN_BIN` пар): текущий допустим —
    оставить, иначе ближайшая к текущему граница допустимых (наименьший сдвиг, который данные требуют). Мало
    данных — оставить текущий, кроме случая, когда он вне допустимых и корзины между ним и ближайшей допустимой
    границей однозначны по Wilson: при повышении порога верхняя граница доли «да» в них ниже `BREAK_EVEN`, при
    понижении нижняя — выше (тогда — эта граница). `automatic=False` — только кривые, текущий остаётся."""
    if not pairs:
        return Verdict(name, current, current, None, None, 0, False, "пар нет — оставить текущий")
    rows = curve(pairs)
    low = min(r.cost for r in rows)
    best = min((r for r in rows if r.cost == low), key=lambda r: (abs(r.theta - current), r.theta)).theta
    ok = [r.theta for r in rows if r.cost < low + C_FP - 1e-9]
    admissible = (min(ok), max(ok))
    k = round(best / STEP)
    near_pairs = [p for p in pairs if _bin(p.score) in (k - 1, min(k, len(GRID) - 2))]
    near = len(near_pairs)
    current_ok = any(abs(theta - current) < 1e-9 for theta in ok) or admissible[0] < current < admissible[1]
    if not automatic:
        reason = "автоматический выбор выключен (метка — не вред, см. раздел) — оставить текущий"
        return Verdict(name, current, current, best, admissible, near, near >= MIN_BIN, reason)
    if near >= MIN_BIN:
        if current_ok:
            reason = f"данных хватает ({near} пар у θ*), текущий допустим — оставить"
            return Verdict(name, current, current, best, admissible, near, True, reason)
        bound = min(admissible, key=lambda theta: abs(theta - current))
        reason = f"данных хватает ({near} пар у θ*), текущий вне допустимых — ближайшая граница допустимых"
        return Verdict(name, current, bound, best, admissible, near, True, reason)
    total = needed(sum(p.truth for p in near_pairs), near)
    runs = {p.run for p in near_pairs}
    need_runs = math.ceil((total - near) / (near / len(runs))) if runs else None
    reason = f"мало данных ({near} пар в корзинах у θ* < {MIN_BIN}) — оставить текущий"
    if current_ok:
        return Verdict(name, current, current, best, admissible, near, False, reason, total - near, need_runs)
    bound = min(admissible, key=lambda theta: abs(theta - current))
    lo, hi = sorted((current, bound))
    between = [p for p in pairs if lo - 1e-9 <= p.score < hi - 1e-9]
    yes = sum(p.truth for p in between)
    low_share, high_share = wilson(yes, len(between))
    raising = bound > current
    if between and (high_share < BREAK_EVEN if raising else low_share > BREAK_EVEN):
        side = f"«да» {yes}/{len(between)}, Wilson [{low_share:.2f}; {high_share:.2f}]"
        reason = (
            f"мало данных у θ* ({near} пар), но в [{lo:.2f}; {hi:.2f}) {side} — "
            f"{'ниже' if raising else 'выше'} безубыточной доли {BREAK_EVEN:.2f}: граница допустимых"
        )
        return Verdict(name, current, bound, best, admissible, near, False, reason, total - near, need_runs)
    reason += (
        f"; текущий вне допустимых по точечной оценке, но в [{lo:.2f}; {hi:.2f}) «да» {yes}/{len(between)} "
        f"(Wilson [{low_share:.2f}; {high_share:.2f}]) — не однозначно"
    )
    return Verdict(name, current, current, best, admissible, near, False, reason, total - near, need_runs)


def fuse_verdict(samples: Sequence[float], current: float) -> Verdict:
    """§4: p99 + кадр, вверх до 0,1 с; меньше `MIN_BIN` замеров — текущий."""
    name = "wait_fuse_s"
    if len(samples) < MIN_BIN:
        reason = f"мало данных: {len(samples)} замеров < {MIN_BIN} — оставить текущий"
        return Verdict(name, current, current, None, None, len(samples), False, reason, MIN_BIN - len(samples))
    value = math.ceil((percentile(samples, 99) + FRAME_S) * 10) / 10
    return Verdict(name, current, value, value, None, len(samples), True, f"p99 + кадр по {len(samples)} замерам")


# --- отчёт -----------------------------------------------------------------------------------------------------------


def _f(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def curve_table(pairs: Sequence[Pair], current: float) -> list[str]:
    lines = [
        "| θ | принято | TP | FP | FN | TN | TPR | FPR | точность [Wilson 95 %] | стоимость |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in curve(pairs):
        low, high = wilson(row.tp, row.accepted)
        mark = " ←" if abs(row.theta - current) < 1e-9 else ""
        precision = f"{_f(row.precision)} [{low:.2f}; {high:.2f}]" if row.accepted else "—"
        lines.append(
            f"| {row.theta:.2f}{mark} | {row.accepted} | {row.tp} | {row.fp} | {row.fn} | {row.tn} | {_f(row.tpr)} | "
            f"{_f(row.fpr)} | {precision} | {row.cost:.2f} |"
        )
    return lines


def bin_table(pairs: Sequence[Pair]) -> list[str]:
    lines = ["| корзина | n | «да» | доля [Wilson 95 %] |", "| --- | --- | --- | --- |"]
    for theta, n, yes in bins(pairs):
        if n == 0:
            continue
        low, high = wilson(yes, n)
        lines.append(f"| {theta:.2f}–{theta + STEP:.2f} | {n} | {yes} | {yes / n:.2f} [{low:.2f}; {high:.2f}] |")
    return lines


def verdict_lines(verdict: Verdict) -> list[str]:
    lines = [f"- θ* (минимум стоимости): {_f(verdict.best)}"]
    if verdict.admissible is not None:
        lines.append(f"- допустимые θ: [{verdict.admissible[0]:.2f}; {verdict.admissible[1]:.2f}]")
    lines.append(f"- {verdict.reason}")
    if verdict.need_pairs and verdict.name == "wait_fuse_s":
        lines.append(f"- собрать ещё: {verdict.need_pairs} замеров")
    elif verdict.need_pairs:
        runs = f", ≈ {verdict.need_runs} прогонов при нынешней плотности" if verdict.need_runs else ""
        lines.append(f"- собрать ещё: {verdict.need_pairs} пар у θ*{runs} (Wilson ±{TARGET_HALF_WIDTH:g})")
    lines.append(f"- `recommended: {verdict.name} = {verdict.recommended:g}` (текущее {verdict.current:g})")
    return lines


def section(pairs: Sequence[Pair], verdict: Verdict, note: str) -> list[str]:
    runs = len({p.run for p in pairs})
    yes = sum(p.truth for p in pairs)
    out = [f"### `{verdict.name}`", "", note, "", f"Пар: {len(pairs)} из {runs} прогонов, «да» — {yes}.", ""]
    if pairs and yes == len(pairs):
        _, high = wilson(0, len(pairs))
        out += [
            f"Пар «нет» нет: ложная приёмка не наблюдалась ни разу, её долю данные не оценивают (Wilson сверху — "
            f"{high:.3f} на всю выборку). Кривая показывает только цену ложных отказов; опускать порог ниже текущего "
            "по ней нельзя — нужны прогоны, где оценка есть, а шаг не выполнен.",
            "",
        ]
    elif pairs and yes == 0:
        out += ["Пар «да» нет: кривая показывает только ложные приёмки.", ""]
    if pairs:
        out += ["Корзины:", "", *bin_table(pairs), "", "Кривая (← текущее):", "", *curve_table(pairs, verdict.current)]
        out.append("")
    out += verdict_lines(verdict)
    return [*out, ""]


@dataclass(slots=True)
class Report:
    verdicts: dict[str, Verdict]
    markdown: str


def calibrate(
    runs: Sequence[dict[str, Any]],
    per_file: Mapping[str, int],
    observe: Sequence[Path],
    thresholds: Thresholds,
    command: str,
    today: str | None = None,
) -> Report:
    other, done_op = step_done_pairs(runs)
    scenario_actions, goal_actions = action_pairs(runs)
    goal_done = done_pairs(runs)
    samples, sources = settle_samples(runs, observe)
    verdicts = {
        "step_done_min_p": decide("step_done_min_p", other, thresholds.step_done_min_p),
        "done_step_done_min_p": decide("done_step_done_min_p", done_op, thresholds.done_step_done_min_p),
        "min_action_confidence": decide(
            "min_action_confidence", scenario_actions, thresholds.min_action_confidence, automatic=False
        ),
        "done_min_confidence": decide("done_min_confidence", goal_done, thresholds.done_min_confidence),
        "wait_fuse_s": fuse_verdict(samples, thresholds.wait_fuse_s),
    }
    modes = {m: sum(1 for r in runs if mode(r) == m) for m in ("goal", "scenario")}
    heads = sorted({str(r.get("head")) for r in runs if r.get("head")})
    lines = [
        "# Калибровка порогов (docs/plan-waits.md §6.3)",
        "",
        f"Дата: {today or date.today().isoformat()}. Команда: `{command}`.",
        f"Прогонов с решениями Jev: {len(runs)} (цель — {modes['goal']}, сценарий — {modes['scenario']}); "
        f"коммиты ядра: {', '.join(heads) or '—'}.",
        "",
        "Значения `config.Thresholds` берутся отсюда (строка `recommended:` в каждом разделе); меняет их только этот"
        " расчёт (docs/plan-waits.md §12).",
        "",
        "## Трассы",
        "",
        "| файл | прогонов |",
        "| --- | --- |",
        *[f"| `{_short(path)}` | {n} |" for path, n in per_file.items()],
        "",
        "## Как размечено и как выбран порог",
        "",
        f"- Сетка θ: 0–1 с шагом {STEP:g}; принять при оценке ≥ θ. Стоимость `FP·{C_FP:g} + FN·{C_FN:g}`: ложное "
        "принятие стоит прогон, ложный отказ — лишний вызов Jev ($0,0002 и ~0,7 с против $0,001 и ~7 с прогона).",
        "- θ* — минимум стоимости (при равенстве — ближе к текущему). Допустимые θ — стоимость меньше стоимости θ* плюс"
        " одна ложная приёмка (данные их не различают).",
        f"- Исключение из «мало данных»: текущий вне допустимых, а корзины между ним и ближайшей допустимой границей "
        f"однозначны по Wilson — доля «да» в них целиком ниже (при повышении) или выше (при понижении) безубыточной "
        f"{BREAK_EVEN:.2f} = C_FP/(C_FP+C_FN); тогда — эта граница.",
        f"- Правило: в двух корзинах вокруг θ* меньше {MIN_BIN} пар — «мало данных», оставить текущее и назвать, "
        f"сколько собрать (Wilson ±{TARGET_HALF_WIDTH:g}); данных хватает, но текущее допустимо — оставить; "
        "иначе — θ*.",
        "- Правда шага (§1) — не прокси «закрылся следующим решением» (он повторяет сам порог), а исход: "
        "нетекстовый шаг, выполненный к концу прогона (verified или отчёт страницы: `openChat`, `sent`), — «да» у "
        "решений после его последнего исполненного CLICK/TYPE_TEXT/SELECT, «нет» до; не выполненный — «нет». "
        "Текстовый шаг — «нет» до первого ввода текста, после — не размечается (закрывает код). Допущение: после "
        "последнего действия шаг уже выполнен, до него — ещё нет (на стенде шаг — одно действие). Где есть история "
        "отчётов с `t_ms` (§7.2), правда — состояние страницы в момент снимка (ответ на прошлое решение + ввод, "
        "ожидание и снимок его шага), неоднозначное (изменение ближе 30 мс) не размечается: Send без `sendstatus` "
        "показывает сообщение через ~300 мс, снимок раньше — низкий P(yes) там верен, а прокси считал его ошибкой.",
        "- Полезность действия (§2) — совпадение с последним действием выполненного шага (операция и элемент); "
        "повторный ввод того же текста — тоже полезен. Неисполненные решения (ниже порога, устаревшие) не размечены.",
        "",
        "## §1. Шаг выполнен (`step_done`)",
        "",
        *section(
            other,
            verdicts["step_done_min_p"],
            "Решения не-DONE под шаг сценария: P(yes) головы `step_done` → шаг выполнен в момент решения.",
        ),
        *section(
            done_op,
            verdicts["done_step_done_min_p"],
            "Только DONE на нетекстовых шагах: P(yes) → шаг выполнен.",
        ),
        "## §2. Уверенность действия (`confidence` CLICK/TYPE_TEXT/SELECT)",
        "",
        *section(
            scenario_actions,
            verdicts["min_action_confidence"],
            "Исполненные действия в сценарии: `confidence` головы `operation` → действие полезно. «Нет» здесь — "
            "лишнее, а не вредное действие (на стенде — клик по полю поиска перед строкой чата): его цена — лишний "
            "шаг, а не прогон, так что `C_FP = 1` её завышает; и неисполненное действие не бесплатно: два "
            "неуверенных подряд — `blocked` (`UNCERTAIN_LIMIT`; стенд 26.09: верное восстановление — клик по строке "
            "чата с conf 0.29, затем 0.27). Цену ложного отказа трассы не дают, поэтому автоматический выбор здесь "
            "выключен: θ* ниже — верхняя граница, а не цель.",
        ),
        f"Для справки, режим цели (прокси «прогон verified», не для порога): {len(goal_actions)} пар, "
        f"«да» — {sum(p.truth for p in goal_actions)}; ниже 0,3 — "
        f"{sum(1 for p in goal_actions if p.score < 0.3)}, из них «да» — "
        f"{sum(1 for p in goal_actions if p.score < 0.3 and p.truth)}.",
        "",
        "## §3. DONE в режиме цели (`confidence` DONE)",
        "",
        *section(
            goal_done,
            verdicts["done_min_confidence"],
            "Последнее решение прогона `done` в режиме цели: `confidence` DONE → проверка по DOM (verified).",
        ),
        "## §4. Предохранитель одного ожидания (`wait_fuse_s`)",
        "",
        "«Действие → последняя значимая мутация»: отчёты страницы с `t_ms` (`reports`, §7.2) и замер "
        f"`wa-observe-*.json` (§8). Замеров: {len(samples)} (отчёты — {sources['reports']}, "
        f"замер — {sources['observe']}; прогонов развёртки пропущено — {sources['sweep_runs_skipped']}: их задержки "
        "задают оси, p99 = максимум оси). В старых eval-трассах этой метки нет: отчёт страницы только итоговый.",
        "",
    ]
    if samples:
        lines += [
            f"- p50 {percentile(samples, 50):.2f} с, p90 {percentile(samples, 90):.2f} с, "
            f"p99 {percentile(samples, 99):.2f} с",
        ]
    lines += verdict_lines(verdicts["wait_fuse_s"])
    lines += [
        "",
        "## Итог",
        "",
        "| порог | текущее | рекомендуемое | данных | основание |",
        "| --- | --- | --- | --- | --- |",
        *[
            f"| `{v.name}` | {v.current:g} | {v.recommended:g} | {v.near} у θ* | {v.reason} |"
            for v in verdicts.values()
        ],
        "",
    ]
    return Report(verdicts, "\n".join(lines))


def _short(path: str) -> str:
    """Путь в отчёте: внутри репозитория — относительный, снаружи — только имя файла (без личных каталогов)."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return Path(path).name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Калибровка порогов модели по трассам eval (офлайн).")
    parser.add_argument(
        "--traces", action="append", help=f"шаблон JSONL (можно несколько); по умолчанию {DEFAULT_TRACES}"
    )
    parser.add_argument("--observe", action="append", help=f"шаблон файлов замера §8; по умолчанию {DEFAULT_OBSERVE}")
    parser.add_argument("--out", type=Path, help="записать markdown (docs/calibration.md)")
    parser.add_argument("--date", help="дата в отчёте (YYYY-MM-DD), по умолчанию сегодня")
    args = parser.parse_args(argv)
    # свои шаблоны — от текущего каталога, по умолчанию — от корня репозитория
    files = expand(args.traces, Path.cwd()) if args.traces else expand(DEFAULT_TRACES)
    runs, per_file = load_runs(files)
    observe = expand(args.observe, Path.cwd()) if args.observe else expand(DEFAULT_OBSERVE)
    command = (
        "uv run --locked python scripts/calibrate.py"
        + "".join(f" --traces '{t}'" for t in args.traces or ())
        + (f" --out {args.out}" if args.out else "")
    )
    report = calibrate(runs, per_file, observe, Thresholds(), command, args.date)
    print(report.markdown)
    for verdict in report.verdicts.values():
        print(f"recommended: {verdict.name} = {verdict.recommended:g} (current {verdict.current:g}; {verdict.reason})")
    if args.out:
        args.out.write_text(report.markdown, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
