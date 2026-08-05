"""Market-wide investor-sentiment regime + per-stock sentiment sensitivity — the conditioning
variable the contrarian lacked. A single-name extreme is far more likely to mean-revert when
(a) market-wide sentiment is itself at an extreme, and (b) the name is the KIND of stock whose
price is driven by sentiment (hard to value, hard to arbitrage).

  sentiment_regime()      : composite market sentiment percentile from free proxies (HY credit
                            spread, VIX, financial conditions, consumer sentiment), or the OFFICIAL
                            Baker-Wurgler index if its CSV is supplied. Baker & Wurgler (2006, JF
                            61(4); 2007, JEP 21(2)).
  sentiment_sensitivity() : per-stock score on the Baker-Wurgler characteristics — small, volatile,
                            non-dividend-paying, unprofitable, extreme-growth names load most on
                            sentiment. Stambaugh, Yu & Yuan (2012, JFE 104(2)) — anomalies concentrate
                            in sentiment-prone, hard-to-arbitrage stocks.

Conditioning rule for the contrarian: take the highest-conviction fades only when market sentiment
is extreme AND the name is sentiment-sensitive; for low-sensitivity names (large, profitable,
dividend), lean on name-specific crowding instead. This is a market-STATE conditioner, not a macro
regime forecast (that is the macro agent's lane).

Optional PLS-refined sentiment upgrade: Huang, Jiang, Tu & Zhou (2015, RFS 28(3), DOI 10.1093/rfs/hhu080).

CLI:
  python sentiment_regime.py market
  python sentiment_regime.py NVDA
"""
import os
import sys
import math
import numpy as np
from common import jprint, rnd, fred, safe, main_guard


def _pct_current(series, high_is_high_sentiment):
    """Percentile of the latest value within its own history, oriented so 100 = high sentiment."""
    s = series.dropna()
    if len(s) < 24:
        return None, None
    cur = float(s.iloc[-1])
    p = 100.0 * float((s < cur).mean())
    return (p if high_is_high_sentiment else 100.0 - p), cur


def _official_bw():
    """Prefer the official Baker-Wurgler sentiment index if the CSV is provided (env BW_SENT_PATH
    or tools/data/baker_wurgler_sentiment.csv with columns date,SENT). Returns dict or None."""
    path = os.environ.get("BW_SENT_PATH") or os.path.join(
        os.path.dirname(__file__), "data", "baker_wurgler_sentiment.csv")
    if not os.path.exists(path):
        return None
    try:
        import pandas as pd
        df = pd.read_csv(path)
        col = [c for c in df.columns if c.lower() in ("sent", "sentiment", "bw")][0]
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        cur = float(s.iloc[-1])
        pct = 100.0 * float((s < cur).mean())
        return {"source": "official Baker-Wurgler index", "bw_level": rnd(cur, 3),
                "sentiment_percentile": rnd(pct, 0)}
    except Exception:
        return None


def sentiment_regime():
    """Composite market-sentiment percentile (0 = max fear/pessimism, 100 = max greed/optimism)."""
    official = _official_bw()
    errs = []
    legs = {}
    # (fred_id, high_value_is_high_sentiment)
    specs = {
        "hy_credit_spread": ("BAMLH0A0HYM2", False),   # tight spread = risk-on = high sentiment
        "vix": ("VIXCLS", False),                       # low vol = complacent = high sentiment
        "financial_conditions_nfci": ("NFCI", False),   # loose (low) = high sentiment
        "consumer_sentiment_umcsent": ("UMCSENT", True),
    }
    pcts = []
    for name, (sid, hi) in specs.items():
        s = safe(lambda: fred(sid), name, errs)
        if s is None or len(s) < 24:
            legs[name] = None
            continue
        p, cur = _pct_current(s, hi)
        legs[name] = {"latest": rnd(cur, 2), "sentiment_percentile": rnd(p, 0) if p is not None else None}
        if p is not None:
            pcts.append(p)
    comp = float(np.mean(pcts)) if pcts else None
    state = None
    if comp is not None:
        state = ("elevated/greed" if comp >= 70 else "depressed/fear" if comp <= 30 else "neutral")
    out = {
        "composite_sentiment_percentile": rnd(comp, 0),
        "state": state,
        "legs": legs,
        "official_baker_wurgler": official,
        "read": (None if comp is None else
                 ("market sentiment ELEVATED (greed) — euphoric single-name extremes have a market-wide tailwind to fade; "
                  "contrarian SHORTS of sentiment-prone names are better odds here" if comp >= 70 else
                  "market sentiment DEPRESSED (fear) — panic-driven single-name extremes more likely to mean-revert UP; "
                  "contrarian LONGS into capitulation are better odds here" if comp <= 30 else
                  "market sentiment NEUTRAL — no market-wide tailwind; rely on name-specific crowding")),
        "note": "free composite proxy (HY spread, VIX, NFCI, consumer sentiment) unless the official Baker-Wurgler CSV "
                "is supplied; a market-STATE conditioner, not a macro forecast. Refs: Baker-Wurgler 2006/2007; Stambaugh-Yu-Yuan 2012.",
    }
    if errs:
        out["fetch_errors"] = errs
    return out


def sentiment_sensitivity(ticker):
    """Per-stock Baker-Wurgler sentiment-sensitivity score in [0,1] (higher = more sentiment-driven):
    small, high-beta/volatile, non-dividend, unprofitable, extreme-growth names load most."""
    import yfinance as yf
    i = yf.Ticker(ticker).info or {}
    parts = {}
    mc = i.get("marketCap")
    if mc and mc > 0:
        parts["size"] = float(np.clip((10.5 - math.log10(mc)) / 2.5, 0, 1))     # microcaps ~1, megacaps ~0
    beta = i.get("beta")
    if beta is not None:
        parts["beta"] = float(np.clip((beta - 0.8) / 1.2, 0, 1))
    dy = i.get("dividendYield") or 0
    parts["no_dividend"] = 1.0 if not dy else 0.0
    pm = i.get("profitMargins")
    if pm is not None:
        parts["unprofitable"] = float(np.clip((0.10 - pm) / 0.20, 0, 1))         # loss-making ~1, fat-margin ~0
    rg = i.get("revenueGrowth")
    if rg is not None:
        parts["extreme_growth"] = float(np.clip(rg / 0.5, 0, 1))                  # >50% growth ~1
    score = float(np.mean(list(parts.values()))) if parts else None
    band = None
    if score is not None:
        band = "high" if score >= 0.6 else "low" if score < 0.3 else "moderate"
    return {"sentiment_sensitivity_score": rnd(score, 2), "band": band,
            "components": {k: rnd(v, 2) for k, v in parts.items()},
            "note": "Baker-Wurgler characteristics: small/volatile/non-dividend/unprofitable/high-growth = sentiment-prone "
                    "and hard-to-arbitrage; large profitable dividend-payers are sentiment-insensitive."}


def sentiment_read(ticker):
    reg = sentiment_regime()
    sens = sentiment_sensitivity(ticker)
    comp, band = reg.get("composite_sentiment_percentile"), sens.get("band")
    cond = "market sentiment or sensitivity unavailable"
    if comp is not None and band is not None:
        extreme = comp >= 70 or comp <= 30
        if extreme and band == "high":
            cond = ("CONDITION MET: market sentiment is at an extreme AND this name is highly sentiment-prone — "
                    "highest-conviction contrarian window; fade euphoria / buy capitulation per the regime direction")
        elif extreme and band == "moderate":
            cond = "partial: market sentiment extreme but the name is only moderately sentiment-driven — size accordingly"
        elif band == "low":
            cond = ("name is NOT sentiment-driven (large/profitable/dividend) — market-wide sentiment is a weak conditioner; "
                    "rely on name-specific crowding, not the regime")
        else:
            cond = "market sentiment neutral — no market-wide tailwind; lean on name-specific crowding"
    return {"ticker": ticker, "market": reg, "name_sensitivity": sens, "conditioning": cond}


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "market"
    if arg.lower() == "market":
        jprint(sentiment_regime())
    else:
        jprint(sentiment_read(arg))


if __name__ == "__main__":
    main_guard(main)
