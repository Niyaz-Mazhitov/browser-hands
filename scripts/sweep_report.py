"""Отчёт развёртки стенда `scripts/eval.py --sweep` (docs/plan-waits.md §7.3–7.4): только stdlib, без сети и моделей.

    uv run --frozen python scripts/sweep_report.py traces/eval-<ts>.jsonl                  # одна развёртка
    uv run --frozen python scripts/sweep_report.py before.jsonl after.jsonl                # колонки «было / стало»
    uv run --frozen python scripts/sweep_report.py traces/eval-<ts>.jsonl --out traces/sweep-before.md
    uv run --frozen python scripts/sweep_report.py traces/eval-*.jsonl --fuse --wa traces/wa-observe-*.json

По каждой оси и режиму: значение → успех k/N (`verified` — проверка по DOM, не статус агента) с Wilson 95 %, медиана
и p95 `elapsed` и `wait` (с), ASCII-кривая доли успехов. Прогон считается во всех осях своей ячейки (`axes`): ячейка
на пересечении двух осей — в обеих. Несколько файлов — колонки по файлам (метка — `label` строк или имя файла).

`--fuse` (§7.4) — распределение «действие → последнее изменение страницы»: от решения Jev с действием
(CLICK/TYPE_TEXT/SELECT, `t_ms`) до последнего отчёта страницы (`reports[].t_ms`) раньше следующего решения; плюс
`mutations.last_significant_ms` замеров WhatsApp (`--wa`, §8). Печатает p50/p90/p99 и `wait_fuse_s` = p99 + кадр,
вверх до 0,05 с. Прокси: отчёт страницы — открытый чат, поиск, отправленные, текст поля, а не каждая мутация DOM;
момент действия — ответ Jev (ввод в режиме цели — позже, на время текстовой модели).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ACTIONS = frozenset({"CLICK", "TYPE_TEXT", "SELECT"})  # решения, после которых страница меняется от нас
FRAME_MS = 17  # запас кадра к p99 для wait_fuse_s
FUSE_STEP_S = 0.05  # wait_fuse_s округляем вверх до этого шага
BAR = 20  # ширина ASCII-кривой: 20 символов = 100 %

Row = Mapping[str, Any]
Dataset = tuple[str, Sequence[Row]]


def load(path: Path) -> Dataset:
    """Строки прогонов из JSONL стенда (без `summary`) и метка: `label` строк или имя файла."""
    rows: list[Row] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "task" in row and "verified" in row:
            rows.append(row)
    labels = [str(row["label"]) for row in rows if row.get("label")]
    return (labels[0] if labels else path.stem), rows


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % интервал Уилсона для доли k/n; n = 0 — (0, 1)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values: Sequence[float], p: float) -> float:
    """Ближайший ранг, как `bench.percentile`: p95 из 5 значений — максимум."""
    if not values:
        raise ValueError("нет значений")
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p / 100 * len(ordered))) - 1]


def sweep_rows(rows: Iterable[Row]) -> list[Row]:
    return [row for row in rows if row.get("axes")]


def axis_groups(rows: Iterable[Row]) -> dict[tuple[str, str], dict[int, list[Row]]]:
    """(ось, режим) → значение → прогоны; порядок осей — как в файле."""
    groups: dict[tuple[str, str], dict[int, list[Row]]] = {}
    for row in sweep_rows(rows):
        for axis, value in row["axes"]:
            groups.setdefault((str(axis), str(row.get("mode"))), {}).setdefault(int(value), []).append(row)
    return groups


def cell_stats(rows: Sequence[Row]) -> dict[str, Any]:
    n, k = len(rows), sum(bool(row.get("verified")) for row in rows)
    lo, hi = wilson(k, n)
    elapsed = [row["elapsed_ms"] for row in rows]
    wait = [row["timing"]["wait_ms"] for row in rows]
    return {
        "n": n,
        "k": k,
        "lo": lo,
        "hi": hi,
        "elapsed": (statistics.median(elapsed), percentile(elapsed, 95)),
        "wait": (statistics.median(wait), percentile(wait, 95)),
    }


def fixed_params(rows: Iterable[Row], axis: str) -> str:
    """Параметры, постоянные на всей оси (кроме её собственного): «delay=700, sendstatus=1500, net=1»."""
    own = axis.split()[0]
    values: dict[str, set[Any]] = {}
    for row in rows:
        for key, value in (row.get("params") or {}).items():
            values.setdefault(key, set()).add(value)
    return ", ".join(f"{key}={next(iter(v))}" for key, v in values.items() if key != own and len(v) == 1)


def _s(ms: float) -> str:
    return f"{ms / 1000:.1f}"


def _cells(stats: Mapping[str, Any] | None) -> list[str]:
    if stats is None:
        return ["—", "—", "—"]
    return [
        f"{stats['k']}/{stats['n']} ({stats['lo'] * 100:.0f}–{stats['hi'] * 100:.0f} %)",
        f"{_s(stats['elapsed'][0])} / {_s(stats['elapsed'][1])}",
        f"{_s(stats['wait'][0])} / {_s(stats['wait'][1])}",
    ]


def bar(k: int, n: int) -> str:
    filled = round(BAR * k / n) if n else 0
    return "█" * filled + "·" * (BAR - filled)


def format_report(datasets: Sequence[Dataset]) -> str:
    """Markdown: по оси и режиму — таблица (колонки по наборам) и ASCII-кривая доли успехов; в конце — итоги."""
    groups = [axis_groups(rows) for _, rows in datasets]
    keys = list(dict.fromkeys(key for group in groups for key in group))
    names = [label for label, _ in datasets]
    multi = len(datasets) > 1
    width = max(map(len, names)) if multi else 0
    head = " | ".join(f"{label}: {len(sweep_rows(rows))} прогонов" for label, rows in datasets)
    lines = [f"# Развёртка стенда — {head}"]
    if not keys:
        lines.append("")
        lines.append("строк развёртки (`axes`) нет — это не JSONL `eval.py --sweep`")
    for axis, mode in keys:
        own = axis.split()[0]
        on_axis = [row for group in groups for rows in group.get((axis, mode), {}).values() for row in rows]
        fixed = fixed_params(on_axis, axis)
        lines += ["", f"## {axis} · {mode}" + (f" (при {fixed})" if fixed else "")]
        columns = ["успех (95 %)", "elapsed мед / p95, с", "wait мед / p95, с"]
        titles = [f"{label}: {c}" if multi else c for label in names for c in columns]
        lines.append(f"| {own} | " + " | ".join(titles) + " |")
        lines.append("|---:|" + "---|" * len(titles))
        values = sorted({value for group in groups for value in group.get((axis, mode), {})})
        curve: list[str] = []
        for value in values:
            per_set = [group.get((axis, mode), {}).get(value) for group in groups]
            stats = [cell_stats(rows) if rows else None for rows in per_set]
            lines.append(f"| {value} | " + " | ".join(cell for s in stats for cell in _cells(s)) + " |")
            for index, s in enumerate(stats):
                lead = f"{value:>6}" if index == 0 else " " * 6
                tag = f" {names[index]:<{width}}" if multi else ""
                if s is None:
                    curve.append(f"{lead}{tag} {'нет прогонов':<{BAR}}")
                else:
                    curve.append(
                        f"{lead}{tag} {bar(s['k'], s['n'])} {s['k']}/{s['n']}  медиана {_s(s['elapsed'][0])} с"
                    )
        lines += ["", "```", f"доля успехов по {own} (█ — 5 %)", *curve, "```"]
    lines.append("")
    for label, rows in datasets:
        runs = sweep_rows(rows)
        costs = [row["cost"] for row in runs if row.get("cost") is not None]
        cells = {row.get("cell") for row in runs}
        modes = sorted({str(row.get("mode")) for row in runs})
        verified = sum(bool(row.get("verified")) for row in runs)
        lines.append(
            f"{label}: ячеек {len(cells)}, режимы {', '.join(modes) or '—'}, verified {verified}/{len(runs)}, "
            f"cost ${sum(costs):.4f}"
        )
    return "\n".join(lines)


# --- --fuse ----------------------------------------------------------------------------------------------------------


def fuse_samples(rows: Iterable[Row]) -> tuple[list[float], int]:
    """Мс от решения Jev с действием до последнего отчёта страницы раньше следующего решения (последнее действие
    прогона — до последнего отчёта вообще) и сколько действий страницу не изменили."""
    samples: list[float] = []
    quiet = 0
    for row in rows:
        decisions = [d for d in row.get("decisions") or () if isinstance(d.get("t_ms"), (int, float))]
        changes = sorted(r["t_ms"] for r in row.get("reports") or () if isinstance(r.get("t_ms"), (int, float)))
        for index, decision in enumerate(decisions):
            if decision.get("op") not in ACTIONS:
                continue
            start = decision["t_ms"]
            end = decisions[index + 1]["t_ms"] if index + 1 < len(decisions) else math.inf
            inside = [t for t in changes if start < t <= end]
            if inside:
                samples.append(inside[-1] - start)
            else:
                quiet += 1
    return samples, quiet


def wa_samples(paths: Iterable[Path]) -> list[float]:
    """`mutations.last_significant_ms` замеров WhatsApp (§8, `traces/wa-observe-*.json`): мс от клика до последней
    значимой мутации."""
    found: list[float] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = (data.get("mutations") or {}).get("last_significant_ms") if isinstance(data, dict) else None
        if isinstance(value, (int, float)):
            found.append(float(value))
    return found


def current_fuse() -> float | None:
    try:
        from browser_hands.config import Thresholds
    except ImportError:
        return None
    return getattr(Thresholds(), "wait_fuse_s", None)


def format_fuse(rows: Iterable[Row], wa: Sequence[float]) -> str:
    samples, quiet = fuse_samples(rows)
    lines = ["fuse: действие → последнее изменение страницы (§7.4)"]
    p99s = []
    if samples:
        p50, p90, p99 = (percentile(samples, p) for p in (50, 90, 99))
        p99s.append(p99)
        lines.append(
            f"  стенд: n={len(samples)} (без изменений после действия: {quiet}); "
            f"p50 {p50:.0f} мс, p90 {p90:.0f} мс, p99 {p99:.0f} мс"
        )
    else:
        lines.append(f"  стенд: действий с изменением страницы нет (без изменений: {quiet}; нужны `reports` и `t_ms`)")
    if wa:
        p99s.append(percentile(wa, 99))
        shown = ", ".join(f"{v:.0f}" for v in sorted(wa)[:10])
        lines.append(f"  WhatsApp (§8): n={len(wa)}; last_significant_ms: {shown}{' …' if len(wa) > 10 else ''}")
    else:
        lines.append("  WhatsApp (§8): замеров не передано (--wa)")
    if p99s:
        worst = max(p99s)
        fuse = math.ceil((worst + FRAME_MS) / 1000 / FUSE_STEP_S - 1e-9) * FUSE_STEP_S
        now = current_fuse()
        lines.append(
            f"  рекомендуемое wait_fuse_s = {fuse:.2f} с (p99 {worst:.0f} + кадр {FRAME_MS} мс, вверх до "
            f"{FUSE_STEP_S:g} с)" + (f"; сейчас {now:g}" if now is not None else "")
        )
    return "\n".join(lines)


# --- вход ------------------------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Отчёт развёртки стенда browser-hands: оси, режимы, было/стало.")
    parser.add_argument("files", nargs="+", type=Path, help="JSONL eval.py --sweep; два и больше — колонки по файлам")
    parser.add_argument("--out", type=Path, help="записать отчёт и в файл (markdown), например traces/sweep.md")
    parser.add_argument("--fuse", action="store_true", help="распределение «действие → последнее изменение» (§7.4)")
    parser.add_argument("--wa", type=Path, nargs="*", default=[], help="замеры WhatsApp §8 (wa-observe-*.json)")
    args = parser.parse_args(argv)
    missing = [str(path) for path in [*args.files, *args.wa] if not path.is_file()]
    if missing:
        print(f"sweep_report: нет файлов: {', '.join(missing)}", file=sys.stderr)
        return 2
    datasets = [load(path) for path in args.files]
    parts = [format_report(datasets)]
    if args.fuse:
        parts.append(format_fuse([row for _, rows in datasets for row in rows], wa_samples(args.wa)))
    text = "\n\n".join(parts)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"отчёт: {args.out}")
    return 0 if any(sweep_rows(rows) for _, rows in datasets) or args.fuse else 1


if __name__ == "__main__":
    sys.exit(main())
