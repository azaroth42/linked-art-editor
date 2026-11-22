"""Spike S1, step 7: extrapolate the measured index build times to 1B triples.

Method: least-squares fit of ``log(seconds) = log(a) + b * log(triples)`` (a power law) over the
measured builds, evaluated at 1e9; reported next to plain linear scaling of the largest build.
Peak RSS is reported as measured (it is capped by ``STXXL_MEMORY`` in ``deploy/Qleverfile``).

    uv run --project backend python scripts/spikes/s1_qlever/extrapolate.py \
        1e6=/tmp/lae-i6/results/homebrew-1m/rebuild-index.txt \
        1e7=/tmp/lae-i6/results/homebrew/rebuild-index.txt ...
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

TARGET = 1e9


def read(path: Path) -> dict[str, float]:
    """``wall_clock_s`` and ``peak_rss_sampled_mib`` from a peak_rss.sh log."""
    values = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition(": ")
        if key in ("wall_clock_s", "peak_rss_sampled_mib", "max_rss_children_mib"):
            values[key] = float(value)
    return values


def main(argv: list[str]) -> int:
    """Print the measured points, the fitted exponent, and the 1B-triple estimates as JSON."""
    points = []
    for arg in argv:
        size, _, path = arg.partition("=")
        values = read(Path(path))
        points.append({"triples": float(size), **values})
    points.sort(key=lambda p: p["triples"])
    xs = [math.log(p["triples"]) for p in points]
    ys = [math.log(p["wall_clock_s"]) for p in points]
    n = len(points)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    b = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sum(
        (x - mean_x) ** 2 for x in xs
    )
    a = mean_y - b * mean_x
    largest = points[-1]
    result = {
        "points": points,
        "fit": {"exponent_b": round(b, 3), "coefficient_a": round(math.exp(a), 6)},
        "estimate_1e9_power_law_s": round(math.exp(a + b * math.log(TARGET))),
        "estimate_1e9_linear_from_largest_s": round(
            largest["wall_clock_s"] * TARGET / largest["triples"]
        ),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
