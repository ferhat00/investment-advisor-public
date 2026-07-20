"""Commodity term-structure / carry signals for macro commodity expressions.

The three legs of the commodity risk premium a macro trader trades (Gorton-Rouwenhorst
2006; Szymanowska-de Roon-Nijman-van den Goorbergh 2014; Fuertes-Miffre-Rallis 2010):

  - ROLL YIELD / curve shape (backwardation = positive carry; contango = negative carry;
    theory of storage: low inventory ⇒ backwardation, Brennan 1958). Needs a second point
    on the futures curve; Yahoo does not serve deferred contracts reliably, so roll yield
    is computed when a deferred symbol is wired (COMMODITY_CURVE_* / DEFERRED map) and
    degrades to an n/a pointer otherwise — the same feed-seam pattern as the premium legs
    in common.py.
  - MOMENTUM (12-1 time-series momentum — Miffre-Rallis 2007; Moskowitz-Ooi-Pedersen 2012):
    always computed from the front future.
  - HEDGING PRESSURE (De Roon-Nijman-Veld 2000): speculator net positioning from
    positioning.cot() — high speculative crowding = a mean-reversion warning.

The Fuertes-Miffre-Rallis double sort (long backwardated winners / short contangoed losers)
is surfaced as basis_momentum_signal when roll yield is available, else momentum-only.

CLI:
  python commodity_carry.py all
  python commodity_carry.py wti|gold|copper|natgas|corn ...
"""
import os
import sys
import numpy as np
import pandas as pd
from common import yf_history, jprint, rnd, safe, main_guard

COMMODITIES = {
    "wti": "CL=F", "brent": "BZ=F", "natgas": "NG=F", "gasoline": "RB=F",
    "gold": "GC=F", "silver": "SI=F", "copper": "HG=F",
    "corn": "ZC=F", "wheat": "ZW=F", "soybean": "ZS=F",
}
# map to positioning.py COT market keys for hedging pressure
_COT_MAP = {"wti": "crude", "natgas": "natgas", "gold": "gold", "silver": "silver",
            "copper": "copper", "corn": "corn", "wheat": "wheat"}
# Deferred-contract yfinance symbols for the second point on the curve. Empty by default —
# Yahoo does not reliably serve deferred contracts, so roll yield degrades until wired
# (env: COMMODITY_CURVE_<KEY>, e.g. COMMODITY_CURVE_WTI="CLQ25.NYM"). Honest feed seam.
_DEFERRED = {}


def _trend(px):
    """12-1 TSMOM + multi-lookback trend agreement (−1..+1) from a price series."""
    out = {}
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
        out["trend_agreement"] = rnd(2 * frac - 1, 2)
    return out


def _roll_yield(key, front):
    """Annualized roll yield between front and (wired) deferred contract. Positive =
    backwardation. Returns (roll_pct, curve_state) or (None, note) when no deferred feed."""
    defsym = os.environ.get(f"COMMODITY_CURVE_{key.upper()}") or _DEFERRED.get(key)
    if not defsym:
        return None, "n/a (manual — wire deferred contract via COMMODITY_CURVE_%s)" % key.upper()
    deferred = yf_history(defsym, period="1mo")["Close"]
    f, d = float(front.iloc[-1]), float(deferred.iloc[-1])
    if d <= 0:
        return None, "n/a (bad deferred price)"
    # months to deferred unknown here; report simple front/second % gap annualized ~ /(1 month)
    gap = (f - d) / d
    roll_ann = 100.0 * gap * 12.0
    return rnd(roll_ann, 1), ("backwardation" if gap > 0 else "contango" if gap < 0 else "flat")


def analyze(key):
    key = key.lower()
    if key not in COMMODITIES:
        return {"commodity": key, "error": f"unknown; known: {', '.join(COMMODITIES)}"}
    errs = []
    sym = COMMODITIES[key]
    out = {"commodity": key, "yf_symbol": sym}
    px = safe(lambda: yf_history(sym, period="2y")["Close"].dropna(), f"px_{key}", errs)
    if px is not None and len(px):
        out["last"] = rnd(float(px.iloc[-1]))
        out.update(_trend(px))
    roll = safe(lambda: _roll_yield(key, px), f"roll_{key}", errs) if px is not None else None
    if roll is not None:
        out["roll_yield_ann_pct"], out["curve_state"] = roll
    else:
        out["roll_yield_ann_pct"], out["curve_state"] = None, "n/a"
    # hedging pressure (speculator crowding) from CFTC COT
    cot_key = _COT_MAP.get(key)
    if cot_key:
        try:
            import positioning
            c = safe(lambda: positioning.cot(cot_key), f"cot_{key}", errs)
            if isinstance(c, dict):
                out["hedging_pressure"] = {
                    "speculator_net_pctile": c.get("net_position_pctile"),
                    "speculator_net_z_5y": c.get("net_position_z_5y"),
                    "extreme_flag": c.get("extreme_flag"), "read": c.get("read"),
                }
        except Exception as e:
            errs.append(f"cot_import_{key}: {e}")
    out["inventory_pctile"] = ("n/a (manual — EIA petroleum/natgas stocks or USDA WASDE; "
                               "low inventory ⇒ backwardation, Brennan 1958)")
    # Fuertes-Miffre-Rallis double-sort read (needs both curve + momentum)
    ry, ts = out.get("roll_yield_ann_pct"), out.get("tsmom_12_1_pct")
    if isinstance(ry, (int, float)) and isinstance(ts, (int, float)):
        if ry > 0 and ts > 0:
            out["basis_momentum_signal"] = "long (backwardated winner — carry+momentum aligned)"
        elif ry < 0 and ts < 0:
            out["basis_momentum_signal"] = "short (contangoed loser — carry+momentum aligned)"
        else:
            out["basis_momentum_signal"] = "mixed (carry and momentum disagree)"
    elif isinstance(ts, (int, float)):
        out["basis_momentum_signal"] = f"momentum-only ({out.get('tsmom_signal')}); curve feed not wired"
    out["refs"] = ("Gorton-Rouwenhorst 2006; Szymanowska et al 2014; Fuertes-Miffre-Rallis 2010; "
                   "De Roon-Nijman-Veld 2000; Brennan 1958.")
    out["errors"] = errs
    return out


def all_commodities():
    errs = []
    res = {}
    for key in COMMODITIES:
        r = safe(lambda key=key: analyze(key), f"analyze_{key}", errs)
        if r is not None:
            res[key] = r
    return {"as_of": str(pd.Timestamp.now().date()), "commodities": res, "build_errors": errs}


def main():
    args = sys.argv[1:] or ["all"]
    if args[0] == "all":
        jprint(all_commodities())
    elif len(args) == 1:
        jprint(analyze(args[0]))
    else:
        jprint({k: analyze(k) for k in args})


if __name__ == "__main__":
    main_guard(main)
