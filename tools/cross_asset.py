"""Cross-asset CARRY + TREND confirmation table for a discretionary macro trader.

Systematic style signals used as a confirmation/timing overlay on a top-down regime view.
Carry (Koijen-Moskowitz-Pedersen-Vrugt 2018 'Carry', JFE) and time-series momentum /
trend (Moskowitz-Ooi-Pedersen 2012, JFE; Hurst-Ooi-Pedersen 2017, JPM; Asness-Moskowitz-
Pedersen 2013 'Value and Momentum Everywhere', JF) span every asset class. The discipline:
do not fight a strong confirmed trend, and size up only when the regime narrative, carry,
and trend agree.

Per instrument we compute, from free FRED + price data:
  carry_pct       — the asset's own carry proxy (bond term spread; credit OAS; equity
                    dividend-yield − rf; gold − rf cost-of-carry; USD short-rate differential)
  carry_z         — z-score of that carry vs its 5y history (where a series exists)
  tsmom_12_1_pct  — 12-month-skip-1 time-series momentum (front/price)
  trend_signal    — multi-lookback trend agreement (−1..+1) + long/short
  carry_to_vol    — carry / annualized vol (a crude risk-adjusted carry)
  carry_trend_agree — do carry and trend point the same way?

Commodity roll-yield carry lives in commodity_carry.py (needs a curve feed); here the broad
commodity leg (DBC) is trend-only with a pointer.

CLI:
  python cross_asset.py all | carry | trend
"""
import sys
import numpy as np
import pandas as pd
from common import fred, yf_history, jprint, rnd, safe, zscore_last, realized_vol_ann, main_guard

RF_SID = "DGS3MO"   # 3-month T-bill, the cash/funding leg (percent)


def _series_carry_bond(errs):
    d10 = safe(lambda: fred("DGS10", start="2010-01-01"), "dgs10", errs)
    d3 = safe(lambda: fred(RF_SID, start="2010-01-01"), "dgs3mo", errs)
    if d10 is None or d3 is None:
        return None
    j = pd.concat([d10.rename("l"), d3.rename("s")], axis=1).dropna()
    return (j["l"] - j["s"]).rename("bond_carry")            # term spread (pp)


def _series_carry_credit(errs):
    return safe(lambda: fred("BAMLH0A0HYM2", start="2010-01-01"), "hy_oas", errs)  # OAS (pp)


def _series_carry_gold(errs):
    d3 = safe(lambda: fred(RF_SID, start="2010-01-01"), "dgs3mo", errs)
    return (-d3).rename("gold_carry") if d3 is not None else None  # −rf (gold ~ zero yield)


def _series_carry_usd(errs):
    d3 = safe(lambda: fred(RF_SID, start="2010-01-01"), "dgs3mo", errs)
    fx3 = []
    for sid in ("IR3TIB01EZM156N", "IR3TIB01JPM156N", "IR3TIB01GBM156N"):  # EUR/JPY/GBP 3m (OECD, monthly)
        s = safe(lambda sid=sid: fred(sid, start="2010-01-01"), sid, errs)
        if s is not None and len(s):
            fx3.append(s)
    if d3 is None or not fx3:
        return None
    foreign = pd.concat(fx3, axis=1).mean(axis=1).resample("D").ffill()
    j = pd.concat([d3.rename("us"), foreign.rename("fx")], axis=1).dropna()
    return (j["us"] - j["fx"]).rename("usd_carry")           # US − G3 short rate (pp)


def _point_equity_carry(sym, errs):
    """Equity carry ≈ trailing-12m dividend yield − rf (the index-futures dividend basis)."""
    d3 = safe(lambda: fred(RF_SID, start="2015-01-01"), "dgs3mo", errs)
    rf = float(d3.iloc[-1]) if d3 is not None and len(d3) else None
    def _dy():
        import yfinance as yf
        divs = yf.Ticker(sym).dividends
        px = yf_history(sym, period="2y")["Close"]
        if divs is None or len(divs) == 0:
            return None
        divs.index = divs.index.tz_localize(None)
        last12 = divs[divs.index >= divs.index[-1] - pd.DateOffset(years=1)].sum()
        return 100.0 * float(last12) / float(px.iloc[-1])
    dy = safe(_dy, f"divyield_{sym}", errs)
    if dy is None or rf is None:
        return None
    return dy - rf


INSTRUMENTS = [
    {"key": "equity_us", "etf": "SPY", "carry": "equity"},
    {"key": "bond_us",   "etf": "IEF", "carry": "bond"},
    {"key": "credit_hy", "etf": "HYG", "carry": "credit"},
    {"key": "gold",      "etf": "GLD", "carry": "gold"},
    {"key": "commodity", "etf": "DBC", "carry": "none"},
    {"key": "usd",       "etf": "UUP", "carry": "usd"},
]


def _trend(px):
    out = {}
    r = px.pct_change().dropna()
    vol = realized_vol_ann(r, 63)
    out["vol_ann_63d_pct"] = rnd(100 * vol)
    if len(px) > 252:
        ts = 100.0 * (float(px.iloc[-21]) / float(px.iloc[-252]) - 1.0)
        out["tsmom_12_1_pct"] = rnd(ts, 1)
        out["tsmom_signal"] = "long" if ts > 0 else "short"
    else:
        out["tsmom_12_1_pct"] = None
        out["tsmom_signal"] = "n/a"
    lbs = [l for l in (20, 50, 100, 200) if len(px) > l]
    if lbs:
        last = float(px.iloc[-1])
        frac = np.mean([last > float(px.rolling(l).mean().iloc[-1]) for l in lbs])
        out["trend_signal"] = rnd(2 * frac - 1, 2)
    return out, vol


def _carry_for(spec, errs):
    kind = spec["carry"]
    if kind == "bond":
        s = _series_carry_bond(errs)
    elif kind == "credit":
        s = _series_carry_credit(errs)
    elif kind == "gold":
        s = _series_carry_gold(errs)
    elif kind == "usd":
        s = _series_carry_usd(errs)
    elif kind == "equity":
        lvl = _point_equity_carry(spec["etf"], errs)
        return lvl, None, ("dividend_yield_minus_rf" if lvl is not None else None)
    else:
        return None, None, "n/a (commodity roll yield → commodity_carry.py; needs curve feed)"
    if s is None or len(s.dropna()) < 30:
        return None, None, None
    s = s.dropna()
    return rnd(float(s.iloc[-1]), 2), zscore_last(s, lookback=1250), kind


def analyze(spec):
    errs = []
    key, etf = spec["key"], spec["etf"]
    out = {"instrument": key, "etf": etf}
    px = safe(lambda: yf_history(etf, period="2y")["Close"].dropna(), f"px_{etf}", errs)
    vol = None
    if px is not None and len(px):
        out["last"] = rnd(float(px.iloc[-1]))
        t, vol = _trend(px)
        out.update(t)
    lvl, z, kind = _carry_for(spec, errs)
    out["carry_pct"] = lvl
    out["carry_z_5y"] = rnd(z, 2) if z is not None else None
    out["carry_basis"] = kind
    if isinstance(lvl, (int, float)) and vol and vol > 0:
        out["carry_to_vol"] = rnd(lvl / (100 * vol), 2)
    ts = out.get("tsmom_12_1_pct")
    if isinstance(lvl, (int, float)) and isinstance(ts, (int, float)):
        out["carry_trend_agree"] = bool((lvl > 0) == (ts > 0))
    out["errors"] = errs
    return out


def table(mode="all"):
    errs = []
    rows = {}
    for spec in INSTRUMENTS:
        r = safe(lambda spec=spec: analyze(spec), f"row_{spec['key']}", errs)
        if r is None:
            continue
        if mode == "carry":
            rows[spec["key"]] = {k: r.get(k) for k in
                                 ("etf", "carry_pct", "carry_z_5y", "carry_basis", "carry_to_vol")}
        elif mode == "trend":
            rows[spec["key"]] = {k: r.get(k) for k in
                                 ("etf", "tsmom_12_1_pct", "tsmom_signal", "trend_signal", "vol_ann_63d_pct")}
        else:
            rows[spec["key"]] = r
    return {"as_of": str(pd.Timestamp.now().date()), "mode": mode, "instruments": rows,
            "refs": "Koijen-Moskowitz-Pedersen-Vrugt 2018; Moskowitz-Ooi-Pedersen 2012; "
                    "Hurst-Ooi-Pedersen 2017; Asness-Moskowitz-Pedersen 2013.",
            "build_errors": errs}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode not in ("all", "carry", "trend"):
        print(f"unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)
    jprint(table(mode))


if __name__ == "__main__":
    main_guard(main)
