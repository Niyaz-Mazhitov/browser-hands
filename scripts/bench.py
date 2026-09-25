"""Бенчмарк: N прогонов одной задачи на одном Chrome и одних ModelClients (меряем тёплое соединение).

    uv run --env-file .env python scripts/bench.py --runs 5 --headless --fresh-profile \\
        --url https://en.wikipedia.org/wiki/Main_Page \\
        --goal "Find and open the Wikipedia article about Gödel's incompleteness theorems."

Платно (Jev и текстовая модель на каждом шаге); в pytest не запускается. Итог — таблица по прогонам, медиана и p95
по elapsed/model/text/browser/wait, сумма cost; построчно — JSONL в traces/bench-<ts>.jsonl (последняя строка — сводка).
"""

import argparse
import contextlib
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from browser_hands.cli import result_to_json
from browser_hands.config import ConfigError, Settings, apply_overrides
from browser_hands.server import BrowseService
from browser_hands.types import RunResult

ROOT = Path(__file__).resolve().parent.parent
METRICS = ("elapsed_ms", "model_ms", "text_ms", "browser_ms", "wait_ms")


def metrics(result: RunResult) -> dict[str, int]:
    t = result.timing
    return {
        "elapsed_ms": result.elapsed_ms,
        "model_ms": t.model_ms,
        "text_ms": t.text_ms,
        "browser_ms": t.browser_ms,
        "wait_ms": t.wait_ms,
    }


def percentile(values: Sequence[float], p: float) -> float:
    """Перцентиль методом ближайшего ранга: p95 из 5 значений — максимум, из 20 — 19-е по возрастанию."""
    if not values:
        raise ValueError("нет значений")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(results: Sequence[RunResult]) -> dict[str, Any]:
    rows = [metrics(r) for r in results]
    stats = {
        name: {"median": statistics.median(r[name] for r in rows), "p95": percentile([r[name] for r in rows], 95)}
        for name in METRICS
    }
    costs = [r.cost for r in results if r.cost is not None]
    return {
        "runs": len(results),
        "done": sum(r.status == "done" for r in results),
        "stats": stats,
        "cost_total": round(sum(costs), 6) if costs else None,
        "cost_runs": len(costs),  # сколько прогонов вернули cost
    }


def format_row(index: int, result: RunResult) -> str:
    m = metrics(result)
    cost = f"{result.cost:.4f}" if result.cost is not None else "n/a"
    cells = [f"{m[name]:>8}" for name in METRICS]
    return f"{index:>3}  {result.status:<10} {len(result.steps):>5}  {'  '.join(cells)}  {cost:>8}"


def table_header() -> str:
    return f"{'#':>3}  {'status':<10} {'steps':>5}  " + "  ".join(f"{n[:-3]:>8}" for n in METRICS) + f"  {'cost $':>8}"


def format_summary(summary: dict[str, Any]) -> str:
    lines = []
    for label in ("median", "p95"):
        cells = [f"{summary['stats'][name][label]:>8.0f}" for name in METRICS]
        lines.append(f"{label:<23}{'  '.join(cells)}")  # 23 = ширина колонок «#, status, steps» в строке прогона
    cost = f"${summary['cost_total']:.4f}" if summary["cost_total"] is not None else "n/a"
    lines.append(
        f"done {summary['done']}/{summary['runs']}; cost всего {cost} (прогонов с cost: {summary['cost_runs']})"
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Бенчмарк browser-hands на тёплом Chrome и HTTP-клиентах (платно).")
    parser.add_argument("--runs", type=int, default=5, metavar="N")
    parser.add_argument("--url", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--mode", choices=["attach", "launch"])
    parser.add_argument("--headless", action="store_true", default=None)
    parser.add_argument("--fresh-profile", action="store_true", help="launch: временный профиль, удаляется по выходу")
    parser.add_argument("--max-steps", type=int, metavar="N")
    parser.add_argument("--timeout", type=float, metavar="S")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "traces", help="куда писать bench-<ts>.jsonl")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs: ожидается ≥ 1")
    if args.fresh_profile and args.mode == "attach":
        parser.error("--fresh-profile — только для launch")
    return args


@contextlib.contextmanager
def fresh_profile(enabled: bool) -> Iterator[str | None]:
    if not enabled:
        yield None
        return
    profile = tempfile.mkdtemp(prefix="browser-hands-bench-")
    try:
        yield profile
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    factories: Mapping[str, Any] | None = None,
) -> int:
    args = parse_args(argv)
    with fresh_profile(args.fresh_profile) as profile:
        try:
            settings = apply_overrides(
                Settings.from_env(os.environ if env is None else env),
                mode="launch" if args.fresh_profile else args.mode,  # временный профиль бывает только у launch
                headless=args.headless,
                data_dir=profile,
                max_steps=args.max_steps,
                timeout_s=args.timeout,
            )
            settings.validate()
        except ConfigError as exc:
            print(f"bench: {exc}", file=sys.stderr)
            return 2

        args.out_dir.mkdir(parents=True, exist_ok=True)
        out = args.out_dir / f"bench-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        service = BrowseService(settings, **(factories or {}))  # один Chrome и одни клиенты на все прогоны
        results: list[RunResult] = []
        try:
            with out.open("w", encoding="utf-8") as trace:
                print(table_header())
                for index in range(1, args.runs + 1):
                    result = service.browse(args.url, args.goal)
                    results.append(result)
                    trace.write(json.dumps({"run": index, **result_to_json(result)}, ensure_ascii=False) + "\n")
                    trace.flush()
                    print(format_row(index, result), flush=True)
                    if result.error:
                        print(f"     error: {result.error}", flush=True)
                summary = summarize(results)
                trace.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
        finally:
            service.close()

    print(format_summary(summary))
    print(f"trace: {out}")
    return 0 if summary["done"] == summary["runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
