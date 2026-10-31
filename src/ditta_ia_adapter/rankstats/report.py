"""Turn rank counters into the text `ditta-ia-adapter rank-report` prints.

One section per pool size, since counters from different pool sizes mean
different things. Each section has:

- a histogram: served position groups against the source's rank bands, as
  shares of each row;
- coverage: how deep in the source's order the top 10 came from;
- the tail: how often a top-10 result came from the last fifth of a full
  pool, the sign that the pool is too small;
- the ladder steps the served results came from.
"""

import math
from collections import defaultdict
from datetime import datetime, timezone

from .store import BAND_WIDTH, RankStats, SearchCount, ServedCount

# Served position groups for the histogram: (first, last) 0-based, inclusive.
POSITION_GROUPS = [(0, 0), (1, 2), (3, 9), (10, 49)]
# Coverage and the tail look at results served in this many top places.
TOP = 10
COVERAGE_LEVELS = (0.90, 0.95, 0.99)
# The tail is this share of the pool, from the end.
TAIL_SHARE = 0.2
STEP_ORDER = ["phrase", "and", "all_but_one", "or"]


def build_report(stats: RankStats, days: int) -> str:
    since = stats.today() - (days - 1) * 86400
    span = stats.day_range(since)
    if span is None:
        return f"No searches recorded in the last {days} days."
    return format_report(stats.search_counts(since), stats.served_counts(since), span)


def format_report(searches: list[SearchCount], served: list[ServedCount], span: tuple[int, int]) -> str:
    sections = []
    for pool_size in sorted({s.pool_size for s in searches}):
        sections.append(
            _section(
                pool_size,
                [s for s in searches if s.pool_size == pool_size],
                [s for s in served if s.pool_size == pool_size],
                span,
            )
        )
    return "\n\n".join(sections)


def _section(pool_size: int, searches: list[SearchCount], served: list[ServedCount], span: tuple[int, int]) -> str:
    total = sum(s.count for s in searches)
    full = sum(s.count for s in searches if s.pool_full)
    first, last = (_date(day) for day in span)
    lines = [f"Rank stats, {first} to {last}: {total:,} searches ({full:,} with a full pool), pool {pool_size}", ""]
    lines += _histogram(pool_size, served)
    lines += ["", _coverage_line(served), _tail_line(pool_size, served), _steps_line(served)]
    return "\n".join(lines)


def _histogram(pool_size: int, served: list[ServedCount]) -> list[str]:
    bands = math.ceil(pool_size / BAND_WIDTH)
    labels = [f"{b * BAND_WIDTH + 1}-{min((b + 1) * BAND_WIDTH, pool_size)}" for b in range(bands)]
    width = max(6, *(len(label) + 1 for label in labels))
    lines = [f"{'Served position':<16} Source rank:" + "".join(f"{label:>{width}}" for label in labels)]
    for low, high in POSITION_GROUPS:
        counts = defaultdict(int)
        for s in served:
            if low <= s.position <= high:
                counts[s.band] += s.count
        row_total = sum(counts.values())
        if not row_total:
            continue
        name = str(low + 1) if low == high else f"{low + 1}-{high + 1}"
        cells = "".join(f"{counts[b] / row_total:>{width}.0%}" for b in range(bands))
        lines.append(f"{name:<16} {'':12}{cells}")
    return lines


def _coverage_line(served: list[ServedCount]) -> str:
    top = [s for s in served if s.position < TOP]
    total = sum(s.count for s in top)
    if not total:
        return f"Top {TOP}: nothing served yet."
    by_band = defaultdict(int)
    for s in top:
        by_band[s.band] += s.count
    depths, cumulative, band = [], 0, 0
    for level in COVERAGE_LEVELS:
        while cumulative / total < level:
            cumulative += by_band[band]
            band += 1
        depths.append(band * BAND_WIDTH)
    (first_level, *more_levels), (first_depth, *more_depths) = COVERAGE_LEVELS, depths
    parts = [f"{first_level:.0%} came from the source's top {first_depth}"]
    parts += [f"{level:.0%} from its top {depth}" for level, depth in zip(more_levels, more_depths)]
    return f"Top {TOP}: " + ", ".join(parts) + "."


def _tail_line(pool_size: int, served: list[ServedCount]) -> str:
    tail_band = (pool_size - math.ceil(pool_size * TAIL_SHARE)) // BAND_WIDTH
    top_full = [s for s in served if s.position < TOP and s.pool_full]
    total = sum(s.count for s in top_full)
    if not total:
        return "Tail: no full pools yet, so no tail signal."
    in_tail = sum(s.count for s in top_full if s.band >= tail_band)
    return (
        f"Tail: {in_tail / total:.1%} of top-{TOP} results from full pools came from "
        f"source ranks {tail_band * BAND_WIDTH + 1}-{pool_size}."
    )


def _steps_line(served: list[ServedCount]) -> str:
    counts = defaultdict(int)
    for s in served:
        counts[s.step] += s.count
    total = sum(counts.values())
    if not total:
        return "By ladder step: nothing served yet."
    steps = [step for step in STEP_ORDER if counts[step]] + sorted(set(counts) - set(STEP_ORDER))
    return "By ladder step: " + ", ".join(f"{step} {counts[step] / total:.0%}" for step in steps) + "."


def _date(day: int) -> str:
    return datetime.fromtimestamp(day, tz=timezone.utc).strftime("%Y-%m-%d")
