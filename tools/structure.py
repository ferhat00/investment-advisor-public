"""Structure from volume, not just price. Gives the tape-reader the levels that
have actual transacted volume (and psychology) behind them, rather than only
swing highs/lows.

  - Volume profile: point-of-control (POC), value area, high-volume nodes as S/R.
  - Anchored VWAP from the 52-week high and low as dynamic S/R.
  - Round-number grid (Osler 2000/2003: reversals cluster at round numbers,
    breakouts accelerate just beyond them).

CLI:  python structure.py NVDA [period]   ->  structure.json
"""
import sys, math
import numpy as np
from common import yf_history, jprint, rnd, main_guard


def volume_profile(df, bins=50, lookback=252):
    d = df.tail(lookback)
    tp = ((d.High + d.Low + d.Close) / 3).to_numpy()
    vol = d.Volume.to_numpy()
    edges = np.linspace(tp.min(), tp.max(), bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    idx = np.clip(np.digitize(tp, edges) - 1, 0, bins - 1)
    prof = np.zeros(bins)
    np.add.at(prof, idx, vol)
    poc = centers[prof.argmax()]
    order = prof.argsort()[::-1]                       # value area = 70% of volume
    cum = np.cumsum(prof[order])
    sel = order[:np.searchsorted(cum, 0.70 * prof.sum()) + 1]
    hvn = sorted(round(float(x), 2) for x in centers[order[:3]])
    return {"poc": rnd(poc), "value_area_high": rnd(centers[sel].max()),
            "value_area_low": rnd(centers[sel].min()), "hvn_levels": hvn}


def avwap(df, anchor_pos):
    seg = df.iloc[anchor_pos:]
    tp = (seg.High + seg.Low + seg.Close) / 3
    return float((tp * seg.Volume).cumsum().iloc[-1] / seg.Volume.cumsum().iloc[-1])


def round_grid(price):
    mag = 10 ** math.floor(math.log10(price))
    out = {}
    for name, step in (("major", mag), ("minor", mag / 2), ("fine", mag / 10)):
        below = math.floor(price / step) * step
        above = below + step
        near = min(price - below, above - price) / price < 0.005
        out[name] = {"step": rnd(step), "below": rnd(below), "above": rnd(above), "near": near}
    return out


def analyze(df):
    last = float(df.Close.iloc[-1])
    win = df.tail(252)
    lo_pos = int(np.argmin(win.Low.to_numpy())) + (len(df) - len(win))
    hi_pos = int(np.argmax(win.High.to_numpy())) + (len(df) - len(win))
    vp = volume_profile(df)
    av_lo, av_hi = avwap(df, lo_pos), avwap(df, hi_pos)
    grid = round_grid(last)

    # merge candidate levels into support (below) / resistance (above), nearest first
    cand = [vp["poc"], vp["value_area_high"], vp["value_area_low"], *vp["hvn_levels"],
            rnd(av_lo), rnd(av_hi), rnd(float(win.High.max())), rnd(float(win.Low.min())),
            grid["major"]["below"], grid["major"]["above"],
            grid["minor"]["below"], grid["minor"]["above"]]
    cand = sorted({x for x in cand if x is not None})
    sup, res = [], []
    for x in cand:
        (sup if x < last else res).append(x)
    # dedupe levels within 0.5% of each other, keep nearest to price
    def thin(levels, reverse):
        levels = sorted(levels, reverse=reverse)
        out = []
        for x in levels:
            if not out or abs(x - out[-1]) / last > 0.005:
                out.append(x)
        return out[:4]

    return {
        "ticker": None, "last": rnd(last),
        "volume_profile": vp,
        "anchored_vwap": {
            "from_52w_low": rnd(av_lo), "from_52w_high": rnd(av_hi),
            "price_vs_52wlow_avwap_pct": rnd(100 * (last / av_lo - 1)),
            "price_vs_52whigh_avwap_pct": rnd(100 * (last / av_hi - 1))},
        "round_numbers": grid,
        "nearest_round_note": _round_note(last, grid),
        "support_levels": thin(sup, reverse=True),
        "resistance_levels": thin(res, reverse=False),
        "read": _read(last, vp, grid),
    }


def _round_note(last, grid):
    if any(grid[k]["near"] for k in grid):
        return (f"price {last:.2f} is sitting on a round number — Osler: expect a "
                f"reversal tendency here, but a clean break tends to accelerate")
    return (f"nearest round levels: {grid['fine']['below']:.2f} / "
            f"{grid['fine']['above']:.2f} (fine), {grid['major']['above']:.2f} (major)")


def _read(last, vp, grid):
    loc = ("above value area (extended)" if last > vp["value_area_high"] else
           "below value area (discounted)" if last < vp["value_area_low"] else
           "inside value area (balanced)")
    return f"price {loc}; POC (max-volume magnet) at {vp['poc']:.2f}"


def main():
    ticker = sys.argv[1]
    period = sys.argv[2] if len(sys.argv) > 2 else "2y"
    out = analyze(yf_history(ticker, period=period))
    out["ticker"], out["period"] = ticker, period
    jprint(out)

if __name__ == "__main__":
    main_guard(main)
