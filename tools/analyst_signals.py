"""Sell-side analyst contrarian signals — beyond the raw Buy/Hold/Sell level.

The consensus recommendation LEVEL is optimism-biased (few sells) and weakly predictive; the
research shows the edge is in dispersion, revisions, and target-implied returns:

  - forecast_dispersion : cross-analyst EPS-forecast dispersion. High dispersion (differences of
                          opinion under short-sale constraints) predicts LOWER future returns —
                          the naive read "disagreement = opportunity" is backwards. Diether, Malloy
                          & Scherbina (2002, JF 57(5)).
  - recommendation_revision : the CHANGE in consensus, with rare downgrades weighted more than
                          upgrades — the revision predicts, the biased level does not; sells are
                          rare and informative. Jegadeesh, Kim, Krische & Lee (2004, JF 59(3));
                          Womack (1996, JF 51(1)).
  - target_implied_return : mean-target-implied return with a DISPERSION-conditional flip — high
                          implied upside is bullish only when analysts AGREE; high upside with wide
                          target dispersion flags unreliable optimism (a contrarian caution/fade).
                          Da & Warachka (2011, JFE); Palley, Steffen & Zhang (2025, Management Science).

Contrarian framing: a fresh wave of (rare) downgrades AFTER a large decline can be capitulation —
the signal gives direction; the agent judges whether it is fresh deterioration or exhaustion.

CLI: python analyst_signals.py NVDA
"""
import sys
import numpy as np
import yfinance as yf
from common import jprint, rnd, main_guard
from market_data import _analyst_distribution

_W = {"strongBuy": 1.0, "buy": 2.0, "hold": 3.0, "sell": 4.0, "strongSell": 5.0}


def _rec_mean(d):
    if not d:
        return None
    n = sum(d.values())
    return sum(_W[k] * d.get(k, 0) for k in _W) / n if n else None


def recommendation_signal(t, ticker):
    dist = _analyst_distribution(t)
    cur = dist.get("current")
    prev = dist.get("prior_3m") or dist.get("prior_1m")
    if not cur:
        return {"error": "no analyst recommendation distribution available"}
    n = sum(cur.values())
    rec_now, rec_prev = _rec_mean(cur), _rec_mean(prev)
    pct_sell = 100 * ((cur.get("sell", 0) + cur.get("strongSell", 0)) / n) if n else None
    pct_buy = 100 * ((cur.get("strongBuy", 0) + cur.get("buy", 0)) / n) if n else None
    change = (rec_now - rec_prev) if (rec_now is not None and rec_prev is not None) else None
    revision_score = None
    if cur and prev:
        d_sell = (cur.get("sell", 0) + cur.get("strongSell", 0)) - (prev.get("sell", 0) + prev.get("strongSell", 0))
        d_buy = (cur.get("strongBuy", 0) + cur.get("buy", 0)) - (prev.get("strongBuy", 0) + prev.get("buy", 0))
        revision_score = rnd(3.0 * d_sell - 1.0 * d_buy, 1)   # >0 = net (rare-weighted) downgrades
    reads = []
    if pct_buy is not None:
        reads.append(f"{pct_buy:.0f}% Buy / {pct_sell:.0f}% Sell of {n} — lopsided-bullish (crowded long, few sells left to flip)"
                     if pct_buy > 80 else f"{pct_buy:.0f}% Buy / {pct_sell:.0f}% Sell of {n}")
    if change is not None:
        reads.append("net DOWNGRADE trend (consensus deteriorating — fresh pessimism or late-cycle capitulation)" if change > 0.15
                     else "net UPGRADE trend (consensus improving)" if change < -0.15 else "consensus stable")
    return {
        "distribution": cur, "n_analysts": n,
        "rec_mean_now": rnd(rec_now, 2), "rec_mean_3m_ago": rnd(rec_prev, 2),
        "rec_change": rnd(change, 2), "pct_buy": rnd(pct_buy), "pct_sell": rnd(pct_sell),
        "revision_score_rare_downgrade_wtd": revision_score,
        "read": "; ".join(reads),
        "note": "level is optimism-biased; the rec_change/revision_score carry the signal (JKKL 2004; Womack 1996). "
                "A downgrade wave after a big drawdown can be capitulation — read WITH price/positioning.",
    }


def dispersion_signal(t, ticker):
    """Cross-analyst FY+1 EPS forecast dispersion (Diether-Malloy-Scherbina 2002): high => lower
    future returns. Uses yfinance earnings_estimate; degrades gracefully."""
    try:
        ee = t.earnings_estimate
    except Exception as e:
        return {"error": f"earnings_estimate unavailable: {e}"}
    if ee is None or getattr(ee, "empty", True):
        return {"error": "no earnings estimate data"}
    row = None
    for key in ("+1y", "0y"):
        if key in ee.index:
            row = ee.loc[key]; period = key; break
    if row is None:
        return {"error": "no annual EPS estimate row"}
    avg = row.get("avg"); lo = row.get("low"); hi = row.get("high"); na = row.get("numberOfAnalysts")
    disp = None
    if avg not in (None, 0) and lo is not None and hi is not None:
        disp = abs(hi - lo) / abs(avg)
    read = "dispersion unavailable"
    if disp is not None:
        read = ("HIGH forecast dispersion — differences of opinion elevated; Diether-Malloy-Scherbina says "
                "this predicts LOWER returns (fade the disagreement, do not buy it)" if disp > 0.5
                else "low forecast dispersion — analysts agree" if disp < 0.15 else "moderate forecast dispersion")
    return {"period": period, "eps_avg": rnd(avg, 2), "eps_low": rnd(lo, 2), "eps_high": rnd(hi, 2),
            "n_analysts": int(na) if na is not None else None,
            "eps_dispersion_ratio": rnd(disp, 2), "read": read,
            "note": "dispersion = (high-low)/|avg| FY+1 EPS estimate (Diether-Malloy-Scherbina 2002)."}


def target_signal(ticker, i):
    """Mean-target-implied return with a dispersion-conditional flip (Da-Warachka; Palley et al)."""
    price = i.get("currentPrice") or i.get("regularMarketPrice")
    tm, th, tl = i.get("targetMeanPrice"), i.get("targetHighPrice"), i.get("targetLowPrice")
    implied = 100 * (tm / price - 1) if (tm and price) else None
    disp = 100 * ((th - tl) / tm) if (th and tl and tm) else None
    read = "insufficient target data"
    if implied is not None and disp is not None:
        if implied > 25 and disp > 60:
            read = ("high implied upside BUT wide target dispersion — optimism is unreliable / driven by a few "
                    "bulls; contrarian caution, the consensus target is not a floor")
        elif implied > 25 and disp <= 60:
            read = "high implied upside with tight target dispersion — corroborated, analysts agree on the upside"
        elif implied < 0:
            read = "mean target BELOW price — sell-side already bearish (few sells usually, so this is notable)"
        else:
            read = "moderate implied upside"
    return {"price": rnd(price), "target_mean": tm, "target_high": th, "target_low": tl,
            "implied_return_pct": rnd(implied), "target_dispersion_pct": rnd(disp), "read": read}


def analyst_read(ticker):
    t = yf.Ticker(ticker)
    i = t.info or {}
    return {
        "ticker": ticker,
        "recommendation": recommendation_signal(t, ticker),
        "forecast_dispersion": dispersion_signal(t, ticker),
        "target_implied_return": target_signal(ticker, i),
        "note": "contrarian analyst lens: fade crowded lopsided-bullish consensus + high dispersion; "
                "weight rare downgrades; treat post-drawdown downgrade waves as possible capitulation.",
    }


def main():
    jprint(analyst_read(sys.argv[1]))


if __name__ == "__main__":
    main_guard(main)
