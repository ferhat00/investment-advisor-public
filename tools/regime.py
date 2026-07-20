"""Regime detection + statistical-significance harness (price/volume only).

Answers two questions the tape-reader must not skip:
  1. Am I in a trend, a range, or noise?  (Hurst exponent + Lo-MacKinlay variance ratio)
  2. Is a signal I like real, or did I snoop it out of noise?  (block-bootstrap /
     White Reality Check across a rule family)

Refs: Lo & MacKinlay (1988) variance ratio; Hurst rescaled-range / structure function;
Sullivan, Timmermann & White (1999) data-snooping; White (2000) Reality Check.

CLI:  python regime.py NVDA [period]   ->  regime.json
Also exports hurst(), variance_ratio(), regime_label(), timing_pvalue() for import
by technicals.py so the trend/momentum read and the tape-reader share one regime call.
"""
import sys, math
import numpy as np
import pandas as pd
from common import yf_history, jprint, rnd, main_guard


# ----- regime measures ---------------------------------------------------------

def hurst(prices, min_lag=2, max_lag=60):
    """Generalized Hurst via the order-2 structure function on log prices.
    H > 0.55 persistent/trending, ~0.5 random walk, < 0.45 mean-reverting.
    Returns nan if too little data."""
    p = np.log(np.asarray(prices, dtype=float))
    p = p[np.isfinite(p)]
    n = len(p)
    if n < 4 * min_lag + 4:
        return float("nan")
    max_lag = int(min(max_lag, n // 4))
    if max_lag <= min_lag:
        return float("nan")
    lags, tau = [], []
    for lag in range(min_lag, max_lag + 1):
        s = np.std(p[lag:] - p[:-lag])
        if s > 0:
            lags.append(lag); tau.append(s)
    if len(lags) < 3:
        return float("nan")
    return float(np.polyfit(np.log(lags), np.log(tau), 1)[0])


def variance_ratio(logret, q):
    """Lo-MacKinlay overlapping variance ratio at horizon q with a
    heteroskedasticity-robust z-stat. VR > 1 trending, < 1 mean-reverting;
    |z| > 1.96 => reject random walk at 5%."""
    r = np.asarray(logret, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < q + 2 or q < 2:
        return float("nan"), float("nan")
    mu = r.mean()
    dev = r - mu
    var1 = np.sum(dev ** 2) / n
    if var1 == 0:
        return float("nan"), float("nan")
    qsum = np.convolve(r, np.ones(q), mode="valid")           # overlapping q-period returns
    m = q * (n - q + 1) * (1 - q / n)
    varq = np.sum((qsum - q * mu) ** 2) / m
    vr = varq / var1
    dev2 = dev ** 2
    theta = 0.0                                                # heteroskedasticity-robust variance
    denom = np.sum(dev2) ** 2
    for j in range(1, q):
        delta = np.sum(dev2[j:] * dev2[:-j]) / denom
        theta += ((2 * (q - j) / q) ** 2) * delta
    z = (vr - 1) / math.sqrt(theta) if theta > 0 else float("nan")
    return float(vr), float(z)


def regime_label(h, vr, z):
    """Fuse Hurst + variance ratio into trending / mean_reverting / random_walk
    plus a confidence. Both agreeing and VR significant => high confidence."""
    if h is None or (isinstance(h, float) and math.isnan(h)):
        return "unknown", "low"
    trend = (h > 0.55) + (vr is not None and not math.isnan(vr) and vr > 1.10)
    mr = (h < 0.45) + (vr is not None and not math.isnan(vr) and vr < 0.90)
    sig = vr is not None and not math.isnan(z) and abs(z) > 1.96
    if trend > mr:
        return "trending", ("high" if trend == 2 and sig else "medium" if trend == 2 else "low")
    if mr > trend:
        return "mean_reverting", ("high" if mr == 2 and sig else "medium" if mr == 2 else "low")
    return "random_walk", ("medium" if sig else "low")


# ----- significance harness (anti-apophenia) -----------------------------------

def _circular_block_idx(n, block, rng):
    reps = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=reps)
    return ((starts[:, None] + np.arange(block)[None, :]) % n).reshape(-1)[:n]


def reality_check(ret, positions, n_boot=1000, block=10, seed=7):
    """White (2000) Reality Check over a family of timing rules, testing skill
    against buy-and-hold. `ret` is the 1-period return series; `positions` is a
    (T x K) matrix of prior-bar positions (long=1 / flat=0) aligned to `ret`.
    Statistic per rule = Cov(position, return) (timing skill, exposure-neutral);
    the max over K is block-bootstrapped and centred so the p-value already
    pays the data-snooping tax for screening K rules.
    Returns (best_rule_index, best_stat, snoop_adjusted_p, standalone_p_of_best)."""
    rng = np.random.default_rng(seed)
    ret = np.asarray(ret, dtype=float)
    positions = np.asarray(positions, dtype=float)
    T, K = positions.shape
    g = (positions - positions.mean(axis=0, keepdims=True)) * ret[:, None]   # col mean = Cov_k
    f = g.mean(axis=0)
    best = int(np.argmax(f))
    v_max = math.sqrt(T) * f.max()
    v_best = math.sqrt(T) * f[best]
    snoop_hits, solo_hits = 0, 0
    gb_best = g[:, best]
    for _ in range(n_boot):
        idx = _circular_block_idx(T, block, rng)
        fb = g[idx].mean(axis=0)
        if math.sqrt(T) * (fb - f).max() >= v_max:
            snoop_hits += 1
        if math.sqrt(T) * (gb_best[idx].mean() - f[best]) >= v_best:
            solo_hits += 1
    return best, float(f[best]), (snoop_hits + 1) / (n_boot + 1), (solo_hits + 1) / (n_boot + 1)


def timing_pvalue(ret, position, n_boot=1000, block=10, seed=7):
    """Standalone block-bootstrap p-value that a single position series times `ret`."""
    _, stat, _, p = reality_check(ret, np.asarray(position).reshape(-1, 1),
                                  n_boot=n_boot, block=block, seed=seed)
    return stat, p


# ----- CLI ---------------------------------------------------------------------

_RULES = [(10, 50), (20, 50), (20, 100), (50, 100), (50, 200), (20, 200)]

def analyze(df, n_boot=1000):
    c = df.Close
    logret = np.log(c / c.shift()).dropna()
    ret = c.pct_change()
    h_full = hurst(c)
    h_recent = hurst(c.tail(150))
    vr5, z5 = variance_ratio(logret, 5)
    vr20, z20 = variance_ratio(logret, 20)
    label, conf = regime_label(h_recent, vr20, z20)

    # build EMA-cross long/flat rule family, position decided at t, earns return t+1
    cols, names = [], []
    for f_, s_ in _RULES:
        pos = (c.ewm(span=f_).mean() > c.ewm(span=s_).mean()).astype(float).shift(1)
        cols.append(pos); names.append(f"EMA{f_}>EMA{s_}")
    pos_df = pd.concat(cols, axis=1)
    aligned = pd.concat([ret.rename("ret"), pos_df], axis=1).dropna()
    best, stat, p_snoop, p_solo = reality_check(
        aligned["ret"].values, aligned.iloc[:, 1:].values, n_boot=n_boot)

    return {
        "ticker": None, "n_obs": int(len(c)),
        "hurst": rnd(h_recent, 3), "hurst_full": rnd(h_full, 3),
        "hurst_read": ("trending" if (h_recent or 0.5) > 0.55 else
                       "mean-reverting" if (h_recent or 0.5) < 0.45 else "random-walk"),
        "vr_5": rnd(vr5, 2), "vr_5_z": rnd(z5, 2),
        "vr_20": rnd(vr20, 2), "vr_20_z": rnd(z20, 2),
        "vr_read": ("momentum/trending" if (vr20 or 1) > 1.1 else
                    "mean-reverting" if (vr20 or 1) < 0.9 else "random-walk"),
        "regime": label, "regime_confidence": conf,
        "n_rules_screened": len(_RULES), "best_trend_rule": names[best],
        "best_rule_timing_cov_bps": rnd(1e4 * stat, 2),
        "best_rule_pvalue_standalone": rnd(p_solo, 3),
        "best_rule_pvalue_snoop_adj": rnd(p_snoop, 3),
        "significant_after_snooping": bool(p_snoop < 0.05),
        "note": ("Trend rules survive the data-snooping correction on this name."
                 if p_snoop < 0.05 else
                 "No trend rule beats buy-and-hold once the snooping tax is paid — "
                 "treat directional setups here as low-confidence."),
    }

def main():
    ticker = sys.argv[1]
    period = sys.argv[2] if len(sys.argv) > 2 else "3y"
    out = analyze(yf_history(ticker, period=period))
    out["ticker"], out["period"] = ticker, period
    jprint(out)

if __name__ == "__main__":
    main_guard(main)
