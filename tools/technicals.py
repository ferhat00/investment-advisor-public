"""Technical analysis. CLI: python technicals.py NVDA [period]"""
import sys
import numpy as np
import pandas as pd
from common import yf_history, jprint, rnd, main_guard
from regime import hurst, variance_ratio, regime_label

def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)

def macd(close, fast=12, slow=26, sig=9):
    m = close.ewm(span=fast).mean() - close.ewm(span=slow).mean()
    s = m.ewm(span=sig).mean()
    return m, s, m - s

def atr(df, n=14):
    tr = pd.concat([df.High - df.Low, (df.High - df.Close.shift()).abs(),
                    (df.Low - df.Close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def adx(df, n=14):
    up, dn = df.High.diff(), -df.Low.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    trur = atr(df, n)
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / trur
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / trur
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, mdi

def _pivots(v, w=5):
    """Positional indices of local highs/lows (w bars on each side)."""
    highs, lows = [], []
    for i in range(w, len(v) - w):
        seg = v[i - w:i + w + 1]
        if np.argmax(seg) == w: highs.append(i)
        if np.argmin(seg) == w: lows.append(i)
    return highs, lows

def divergence(price, osc, w=5, lookback=120):
    """Automated price/oscillator divergence on the last two swing pivots.
    bearish = price higher-high while osc lower-high; bullish = mirror."""
    p = price.tail(lookback).to_numpy(); o = osc.tail(lookback).to_numpy()
    ph, pl = _pivots(p, w)
    res = "none"
    if len(ph) >= 2 and p[ph[-1]] > p[ph[-2]] and o[ph[-1]] < o[ph[-2]]:
        res = "bearish"
    if len(pl) >= 2 and p[pl[-1]] < p[pl[-2]] and o[pl[-1]] > o[pl[-2]]:
        if res == "none" or pl[-1] > ph[-1]:
            res = "bullish"
    return res

def analyze(df):
    c = df.Close
    e20, e50, e200 = c.ewm(span=20).mean(), c.ewm(span=50).mean(), c.ewm(span=200).mean()
    r = rsi(c); m, ms, mh = macd(c); a = atr(df); ax, pdi, mdi = adx(df)
    bb_mid = c.rolling(20).mean(); bb_sd = c.rolling(20).std()
    last = c.iloc[-1]
    vol20 = df.Volume.rolling(20).mean()
    out = {
        "last": rnd(last),
        "ema20": rnd(e20.iloc[-1]), "ema50": rnd(e50.iloc[-1]), "ema200": rnd(e200.iloc[-1]),
        "above_ema200": bool(last > e200.iloc[-1]),
        "ema50_vs_200": "golden" if e50.iloc[-1] > e200.iloc[-1] else "death",
        "rsi14": rnd(r.iloc[-1]),
        "macd_hist": rnd(mh.iloc[-1], 3), "macd_cross": "bull" if m.iloc[-1] > ms.iloc[-1] else "bear",
        "atr14": rnd(a.iloc[-1]), "atr_pct": rnd(100 * a.iloc[-1] / last),
        "adx14": rnd(ax.iloc[-1]), "di_bias": "bull" if pdi.iloc[-1] > mdi.iloc[-1] else "bear",
        "bb_pos": rnd((last - bb_mid.iloc[-1]) / (2 * bb_sd.iloc[-1]), 2),
        "pct_off_52w_high": rnd(100 * (last / c.tail(252).max() - 1)),
        "pct_above_52w_low": rnd(100 * (last / c.tail(252).min() - 1)),
        "ret_1m_pct": rnd(100 * (last / c.iloc[-21] - 1)),
        "ret_3m_pct": rnd(100 * (last / c.iloc[-63] - 1)),
        "ret_6m_pct": rnd(100 * (last / c.iloc[-126] - 1)) if len(c) > 126 else None,
        "ret_12m_pct": rnd(100 * (last / c.iloc[-252] - 1)) if len(c) > 252 else None,
        "vol_vs_20d_avg": rnd(df.Volume.iloc[-1] / vol20.iloc[-1], 2),
        "dist_to_ema50_atr": rnd((last - e50.iloc[-1]) / a.iloc[-1], 1),
        "support_zone": rnd(min(e50.iloc[-1], bb_mid.iloc[-1] - 2 * bb_sd.iloc[-1])),
        "resistance_52w_high": rnd(c.tail(252).max()),
    }

    # --- regime gate (Hurst + Lo-MacKinlay variance ratio) -----------------
    lr = np.log(c / c.shift()).dropna()
    h = hurst(c); vr20, z20 = variance_ratio(lr, 20)
    label, conf = regime_label(h, vr20, z20)
    out["hurst"] = rnd(h, 3)
    out["variance_ratio_20"] = rnd(vr20, 2)
    out["variance_ratio_20_z"] = rnd(z20, 2)
    out["regime"] = label
    out["regime_confidence"] = conf

    # --- systematic trend signals that survive testing ---------------------
    # Trend factor: signed agreement across MA lookbacks (Han-Zhou-Zhu 2016), -1..+1
    lbs = [l for l in (5, 10, 20, 50, 100, 200) if len(c) > l]
    frac_above = np.mean([bool(last > c.rolling(l).mean().iloc[-1]) for l in lbs])
    out["trend_factor"] = rnd(2 * frac_above - 1, 2)
    # Time-series momentum, 12m skip-most-recent-month (Moskowitz-Ooi-Pedersen 2012)
    if len(c) > 252:
        tsmom = 100 * (c.iloc[-21] / c.iloc[-252] - 1)
        out["tsmom_12_1_pct"] = rnd(tsmom)
        out["tsmom_signal"] = "long" if tsmom > 0 else "short"
    else:
        out["tsmom_12_1_pct"] = None; out["tsmom_signal"] = "n/a"
    # Moving-average distance, 21d vs 200d (Avramov-Kaplanski-Subrahmanyam 2021)
    if len(c) > 200:
        mad = c.rolling(21).mean().iloc[-1] / c.rolling(200).mean().iloc[-1]
        out["mad_ratio"] = rnd(mad, 3)
        out["mad_pct"] = rnd(100 * (mad - 1))
    else:
        out["mad_ratio"] = None; out["mad_pct"] = None
    # 52-week-high proximity as an anchoring/continuation signal (George-Hwang 2004)
    prox = last / c.tail(252).max()
    out["high52_proximity"] = rnd(prox, 3)
    out["high52_read"] = ("at/near high — anchoring continuation zone" if prox >= 0.95
                          else "mid-range" if prox >= 0.80 else "far below high")
    # Automated momentum divergences (objective, not eyeballed)
    out["divergence_rsi"] = divergence(c, r)
    out["divergence_macd"] = divergence(c, mh)

    score = sum([out["above_ema200"], out["ema50_vs_200"] == "golden",
                 out["macd_cross"] == "bull", out["di_bias"] == "bull",
                 45 <= (out["rsi14"] or 0) <= 70])
    out["trend_score_0_5"] = score
    out["read"] = ("strong uptrend" if score >= 4 else "uptrend" if score == 3
                   else "mixed" if score == 2 else "downtrend")
    return out

def main():
    ticker = sys.argv[1]
    period = sys.argv[2] if len(sys.argv) > 2 else "2y"
    jprint(analyze(yf_history(ticker, period=period)))

if __name__ == "__main__":
    main_guard(main)
