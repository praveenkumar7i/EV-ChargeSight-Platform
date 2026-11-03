"""Small statistics helpers (no SciPy dependency)."""
from __future__ import annotations

import math
from statistics import mean, stdev

# Two-sided 95% Student-t critical values.
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
    9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074,
    23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045,
    30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980,
}


def t_critical_95(df: int) -> float:
    if df <= 0:
        return float("nan")
    if df in _T95:
        return _T95[df]
    for k in (40, 60, 120):
        if df < k:
            return _T95[k]
    return 1.960


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolation percentile (same as numpy 'linear'). p in [0, 100]."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def paired_summary(a: list[float], b: list[float]) -> dict:
    """Summary of the paired difference d = b - a (b = priority, a = FIFO)."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    n = len(pairs)
    if n == 0:
        return {"n": 0, "mean_a": None, "mean_b": None, "mean_diff": None,
                "ci95_low": None, "ci95_high": None, "relative_diff": None, "significant": None}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    d = [y - x for x, y in pairs]
    md = mean(d)
    if n > 1:
        sd = stdev(d)
        half = t_critical_95(n - 1) * sd / math.sqrt(n)
        lo, hi = md - half, md + half
    else:
        sd, lo, hi = 0.0, None, None
    ma = mean(xs)
    rel = (md / ma) if ma not in (0, 0.0) else None
    sig = None if lo is None else (lo > 0 or hi < 0)
    return {
        "n": n,
        "mean_a": ma,
        "mean_b": mean(ys),
        "mean_diff": md,
        "sd_diff": sd,
        "ci95_low": lo,
        "ci95_high": hi,
        "relative_diff": rel,
        "significant": sig,
    }
