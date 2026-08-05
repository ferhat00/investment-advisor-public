"""FX data-pack builder + generalized FX-factor panel for currency theses.

`pack` builds the USD/JPY data pack (spot, rate differentials, dollar index, PPP anchor,
technicals). The panel subcommands add the systematic FX-factor lenses a macro trader uses
to pick and size currency expressions (all vs USD, free FRED + spot data):

  carry   — rate-differential carry (foreign 3m − US 3m), vol-scaled (Lustig-Roussanov-
            Verdelhan 2011; Menkhoff-Sarno-Schmeling-Schrimpf 2012). High-yielders earn
            carry but crash in vol spikes → the carry/vol and trend columns are the guardrail.
  value   — PPP/REER over/under-valuation (z of the BIS real effective exchange rate vs its
            10y mean; Froot-Rogoff PPP-reversion). Degrades to n/a per currency if the REER
            series is unavailable.
  dollar  — the dollar factor (Verdelhan 2018): broad-USD level/trend + each currency's beta
            to the broad dollar.
  basis   — CIP deviation / cross-currency basis (Du-Tepper-Verdelhan 2018) as a dollar-
            funding-stress gauge. Needs XCCY basis swaps (paid) → degrades to an n/a pointer.

CLI:
  python fx_data.py pack USDJPY <outdir>
  python fx_data.py carry | value | dollar | basis
"""
import sys, os, json
import numpy as np
import pandas as pd
from common import fred, yf_history, jprint, rnd, _ser, safe, zscore_last, realized_vol_ann, main_guard


def _rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn
    return 100 - 100 / (1 + rs)


def _safe(fn, label, errs):
    try:
        return fn()
    except Exception as e:
        errs.append(f"{label}: {e}")
        return None


def _rate_spot_link(joined, current_diff):
    """Correlation + linear fit of USD/JPY spot vs the US-JP 10y differential over whatever
    window `joined` actually covers (>=60 obs). The window is reported from the data, not
    asserted in the field name: yfinance's 5y spot history (preferred over FRED for
    freshness) rarely reaches back to the ">= 2018-01-01" filter applied upstream, so a
    hardcoded "since_2018" label silently drifts from the true fit window (e.g. an
    actual 2021-08 -> 2026-06 window mislabeled as 2018-)."""
    if joined is None or len(joined) <= 60:
        return None
    corr = float(joined["usdjpy"].corr(joined["us10_jp10_diff"]))
    # simple beta: yen per dollar per 1pp of differential
    b = np.polyfit(joined["us10_jp10_diff"], joined["usdjpy"], 1)
    return {
        "corr_window_start": str(joined.index[0].date()),
        "corr_window_end": str(joined.index[-1].date()),
        "corr": rnd(corr, 2),
        "beta_usdjpy_per_1pp_diff": rnd(float(b[0]), 1),
        "model_fit_intercept": rnd(float(b[1]), 1),
        "fitted_fair_usdjpy_at_current_diff": rnd(float(b[0] * current_diff + b[1]), 1) if current_diff else None,
    }


def build_usdjpy(outdir):
    os.makedirs(outdir, exist_ok=True)
    errs = []
    out = {"as_of": str(pd.Timestamp.now().date()), "pair": "USDJPY (yen per USD)"}

    # ---- Spot: FRED DEXJPUS (daily, long history) + yfinance recent ----
    fx = _safe(lambda: fred("DEXJPUS", start="1990-01-01"), "fred_DEXJPUS", errs)
    yfx = _safe(lambda: yf_history("JPY=X", period="5y")["Close"], "yf_JPYX", errs)
    # prefer yfinance for the freshest print, fall back to FRED
    spot_series = None
    if yfx is not None and len(yfx) > 0:
        spot_series = yfx.copy()
    elif fx is not None:
        spot_series = fx.copy()

    if spot_series is not None:
        spot_series = spot_series.dropna()
        spot_series.to_csv(os.path.join(outdir, "usdjpy_prices.csv"))
        px = spot_series
        last = float(px.iloc[-1])
        def ret(n):
            return rnd(100 * (px.iloc[-1] / px.iloc[-n] - 1)) if len(px) > n else None
        ytd = None
        try:
            yr_start = px[px.index.year == px.index[-1].year].iloc[0]
            ytd = rnd(100 * (last / yr_start - 1))
        except Exception as e:
            errs.append(f"ytd: {e}")
        win = px.tail(252)
        out["spot"] = {
            "last": rnd(last, 2),
            "asof_date": str(px.index[-1].date()),
            "asof_confirmed_session": bool(px.index[-1].weekday() < 5),
            "ret_1m_pct": ret(21), "ret_3m_pct": ret(63),
            "ret_6m_pct": ret(126), "ret_1y_pct": ret(252), "ytd_pct": ytd,
            "high_52w": rnd(float(win.max()), 2), "low_52w": rnd(float(win.min()), 2),
            "off_52w_high_pct": rnd(100 * (last / float(win.max()) - 1)),
            "vol_1y_ann_pct": rnd(100 * px.pct_change().tail(252).std() * (252 ** 0.5)),
            "sma50": rnd(float(px.tail(50).mean()), 2),
            "sma200": rnd(float(px.tail(200).mean()), 2),
            "rsi14": rnd(float(_rsi(px).iloc[-1]), 1),
        }
        out["spot"]["above_sma50"] = last > out["spot"]["sma50"]
        out["spot"]["above_sma200"] = last > out["spot"]["sma200"]

    # ---- Rate differentials (the core USD/JPY driver) ----
    us10 = _safe(lambda: fred("DGS10", start="2010-01-01"), "fred_DGS10", errs)
    us2 = _safe(lambda: fred("DGS2", start="2010-01-01"), "fred_DGS2", errs)
    us30 = _safe(lambda: fred("DGS30", start="2010-01-01"), "fred_DGS30", errs)
    dff = _safe(lambda: fred("DFF", start="2010-01-01"), "fred_DFF", errs)
    usr10 = _safe(lambda: fred("DFII10", start="2010-01-01"), "fred_DFII10", errs)
    be10 = _safe(lambda: fred("T10YIE", start="2010-01-01"), "fred_T10YIE", errs)
    jp10 = _safe(lambda: fred("IRLTLT01JPM156N", start="2010-01-01"), "fred_JP10Y", errs)  # monthly
    dxy = _safe(lambda: fred("DTWEXBGS", start="2010-01-01"), "fred_DXY_broad", errs)

    def last_of(s):
        return rnd(float(s.dropna().iloc[-1]), 2) if s is not None and len(s.dropna()) else None
    def ago(s, months=12):
        if s is None or len(s.dropna()) == 0:
            return None
        ss = s.dropna()
        cut = ss.index[-1] - pd.DateOffset(months=months)
        sub = ss[ss.index <= cut]
        return rnd(float(sub.iloc[-1]), 2) if len(sub) else None

    us10_l, jp10_l = last_of(us10), last_of(jp10)
    out["rates"] = {
        "us_policy_DFF": last_of(dff),
        "us_2y": last_of(us2), "us_10y": us10_l, "us_30y": last_of(us30),
        "us_real_10y": last_of(usr10), "us_breakeven_10y": last_of(be10),
        "jp_10y": jp10_l, "jp_10y_date": str(jp10.dropna().index[-1].date()) if jp10 is not None and len(jp10.dropna()) else None,
        "us10_jp10_diff": rnd(us10_l - jp10_l, 2) if (us10_l and jp10_l) else None,
        "us10_jp10_diff_1y_ago": rnd((ago(us10) or 0) - (ago(jp10) or 0), 2) if (ago(us10) and ago(jp10)) else None,
        "us2_10y_diff_2y_ago": None,
    }

    # ---- Differential time series + correlation with spot ----
    if all(x is not None for x in [us10, jp10, spot_series]):
        j = jp10.resample("D").ffill()
        u = us10.resample("D").ffill()
        diff = (u - j).dropna()
        diff.name = "us10_jp10_diff"
        sp = spot_series.resample("D").ffill()
        joined = pd.concat([sp.rename("usdjpy"), diff], axis=1).dropna()
        joined = joined[joined.index >= "2018-01-01"]
        joined.to_csv(os.path.join(outdir, "usdjpy_diff.csv"))
        link = _rate_spot_link(joined, out["rates"]["us10_jp10_diff"])
        if link:
            out["rate_spot_link"] = link

    # ---- Dollar index context ----
    if dxy is not None:
        d = dxy.dropna()
        out["dollar_index_broad"] = {
            "last": last_of(dxy), "date": str(d.index[-1].date()),
            "chg_1y_pct": rnd(100 * (d.iloc[-1] / d[d.index <= d.index[-1] - pd.DateOffset(months=12)].iloc[-1] - 1)) if len(d[d.index <= d.index[-1] - pd.DateOffset(months=12)]) else None,
        }

    # ---- PPP / valuation anchor (OECD PPP for Japan ~ mid-90s yen/USD historically) ----
    out["valuation_anchor"] = {
        "note": "OECD PPP for USD/JPY sits ~ 90-100; BigMac/REER show yen deeply undervalued when spot >> 100. Confirm live PPP via web.",
    }

    out["errors"] = errs
    path = os.path.join(outdir, "fx_pack.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=_ser)
    jprint({"wrote": [path]})
    jprint(out)
    return out


# ============================================================================
# Generalized FX-factor panel (additive; build_usdjpy/`pack` preserved above).
# All majors expressed as USD-per-foreign so "up = foreign appreciates vs USD".
# ============================================================================

CURRENCIES = {
    "EUR": {"yf": "EURUSD=X", "usd_per_foreign": True,  "rate3m": "IR3TIB01EZM156N", "reer": "RBXMBIS"},
    "JPY": {"yf": "JPY=X",    "usd_per_foreign": False, "rate3m": "IR3TIB01JPM156N", "reer": "RBJPBIS"},
    "GBP": {"yf": "GBPUSD=X", "usd_per_foreign": True,  "rate3m": "IR3TIB01GBM156N", "reer": "RBGBBIS"},
    "AUD": {"yf": "AUDUSD=X", "usd_per_foreign": True,  "rate3m": "IR3TIB01AUM156N", "reer": "RBAUBIS"},
    "CAD": {"yf": "CAD=X",    "usd_per_foreign": False, "rate3m": "IR3TIB01CAM156N", "reer": "RBCABIS"},
    "CHF": {"yf": "CHF=X",    "usd_per_foreign": False, "rate3m": "IR3TIB01CHM156N", "reer": "RBCHBIS"},
}


def _last_fred(sid, errs, start="2010-01-01"):
    s = safe(lambda: fred(sid, start=start), sid, errs)
    return (float(s.iloc[-1]) if s is not None and len(s) else None)


def _foreign_in_usd(spec, errs, period="5y"):
    px = safe(lambda: yf_history(spec["yf"], period=period)["Close"].dropna(), spec["yf"], errs)
    if px is None or len(px) == 0:
        return None
    return px if spec["usd_per_foreign"] else (1.0 / px)


def fx_carry():
    """Rate-differential carry vs USD, vol-scaled, with the 12-1 spot trend as the guardrail."""
    errs = []
    us3 = _last_fred("DGS3MO", errs, start="2015-01-01")
    rows = {}
    for cc, spec in CURRENCIES.items():
        r3 = _last_fred(spec["rate3m"], errs)
        px = _foreign_in_usd(spec, errs)
        row = {"short_rate_pct": rnd(r3, 2), "us_short_rate_pct": rnd(us3, 2)}
        if r3 is not None and us3 is not None:
            row["carry_pct"] = rnd(r3 - us3, 2)
        if px is not None and len(px) > 252:
            ret = px.pct_change().dropna()
            vol = 100 * realized_vol_ann(ret, 63)
            row["spot_vol_ann_pct"] = rnd(vol)
            row["spot_ret_12_1_pct"] = rnd(100 * (float(px.iloc[-21]) / float(px.iloc[-252]) - 1.0), 1)
            if row.get("carry_pct") is not None and vol > 0:
                row["carry_to_vol"] = rnd(row["carry_pct"] / vol, 2)
            row["trend_signal"] = "long" if row["spot_ret_12_1_pct"] and row["spot_ret_12_1_pct"] > 0 else "short"
            if row.get("carry_pct") is not None:
                row["carry_trend_agree"] = bool((row["carry_pct"] > 0) == (row["spot_ret_12_1_pct"] > 0))
        rows[cc] = row
    ranked = sorted([c for c in rows if rows[c].get("carry_pct") is not None],
                    key=lambda c: rows[c]["carry_pct"], reverse=True)
    out = {"as_of": str(pd.Timestamp.now().date()), "currencies": rows,
           "high_yielders": ranked[:2], "low_yielders": ranked[-2:] if len(ranked) > 2 else [],
           "note": "Long high-yielders / short low-yielders is the FX carry trade — but it "
                   "crashes in vol spikes (funding unwind). Size by carry_to_vol and cut when "
                   "trend disagrees or global vol jumps.",
           "refs": "Lustig-Roussanov-Verdelhan 2011; Menkhoff-Sarno-Schmeling-Schrimpf 2012.",
           "errors": errs}
    return out


def fx_value():
    """PPP/REER over- vs under-valuation: z of the BIS real effective exchange rate vs 10y mean."""
    errs = []
    rows = {}
    for cc, spec in CURRENCIES.items():
        s = safe(lambda spec=spec: fred(spec["reer"], start="2005-01-01"), spec["reer"], errs)
        if s is None or len(s.dropna()) < 60:
            rows[cc] = {"reer_z_10y": None, "read": "n/a (BIS REER unavailable)"}
            continue
        z = zscore_last(s, lookback=120)
        rows[cc] = {"reer_last": rnd(float(s.iloc[-1]), 1), "reer_z_10y": rnd(z, 2),
                    "read": ("overvalued (rich vs PPP — value-short lean)" if z and z > 1 else
                             "undervalued (cheap vs PPP — value-long lean)" if z and z < -1 else
                             "near fair value")}
    return {"as_of": str(pd.Timestamp.now().date()), "currencies": rows,
            "note": "REER mean-reverts on a multi-year horizon (~4y half-life); a value tilt, "
                    "not a timing signal. Pair with carry + trend.",
            "refs": "Froot-Rogoff 1995 (PPP reversion); BIS real effective exchange rates.",
            "errors": errs}


def fx_dollar():
    """Dollar factor (Verdelhan 2018): broad-USD level/trend + each currency's beta to it."""
    errs = []
    dxy = safe(lambda: fred("DTWEXBGS", start="2010-01-01"), "DTWEXBGS", errs)
    out = {"as_of": str(pd.Timestamp.now().date())}
    if dxy is not None and len(dxy) > 260:
        d = dxy.dropna()
        prior = d[d.index <= d.index[-1] - pd.DateOffset(months=12)]
        out["broad_dollar"] = {
            "last": rnd(float(d.iloc[-1]), 2), "date": str(d.index[-1].date()),
            "chg_1y_pct": rnd(100 * (d.iloc[-1] / prior.iloc[-1] - 1)) if len(prior) else None,
            "z_5y": rnd(zscore_last(d, lookback=1250), 2),
            "trend": "USD strengthening" if len(prior) and d.iloc[-1] > prior.iloc[-1] else "USD weakening",
        }
        dret = d.pct_change().dropna()
        betas = {}
        for cc, spec in CURRENCIES.items():
            px = _foreign_in_usd(spec, errs)
            if px is None:
                continue
            fret = px.pct_change().dropna()
            j = pd.concat([fret.rename("f"), dret.rename("d")], axis=1).dropna()
            if len(j) > 120:
                b = np.polyfit(j["d"], j["f"], 1)[0]
                betas[cc] = rnd(float(b), 2)
        out["currency_beta_to_broad_dollar"] = betas
        out["note"] = ("The dollar factor explains a large share of bilateral FX variation; a "
                       "strong-dollar regime pressures every high-beta currency at once "
                       "(correlations converge). Read broad-USD trend before any single pair.")
    else:
        out["broad_dollar"] = "n/a (DTWEXBGS unavailable)"
    out["refs"] = "Verdelhan 2018 (dollar factor); Lustig-Roussanov-Verdelhan 2011."
    out["errors"] = errs
    return out


def fx_basis():
    """CIP deviation / cross-currency basis — dollar-funding-stress gauge. Needs XCCY basis
    swaps (paid) → degrades to an n/a pointer with the SOFR-OIS complement it pairs with."""
    return {
        "cross_currency_basis": "n/a (manual — 3m EUR/JPY/GBP cross-currency basis swaps; "
                                "negative basis = dollar scarcity, Du-Tepper-Verdelhan 2018)",
        "complement": "Pair with macro_data.py liquidity funding_stress (SOFR-IORB) and the "
                      "broad-dollar trend; a widening negative basis + SOFR-IORB spike = "
                      "USD funding stress → cut carry / reduce EM & risk.",
        "refs": "Du-Tepper-Verdelhan 2018 (Deviations from Covered Interest Rate Parity, JF).",
    }


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pack"
    if cmd == "carry":
        jprint(fx_carry())
    elif cmd == "value":
        jprint(fx_value())
    elif cmd == "dollar":
        jprint(fx_dollar())
    elif cmd == "basis":
        jprint(fx_basis())
    else:  # "pack"
        # The default outdir used to be theses/<pair>, so a bare `fx_data.py pack` from any
        # run silently OVERWROTE theses/USDJPY/ -- a dated run folder belonging to a different
        # thesis. The 2026-07-30 portfolio run clobbered that run's 2026-07-03 fx_pack.json
        # exactly this way. Dated run folders are the corpus's memory; a data-pack builder must
        # never write into one it was not handed. The outdir is now required for that reason.
        pair = sys.argv[2] if len(sys.argv) > 2 else "USDJPY"
        if len(sys.argv) > 3:
            outdir = sys.argv[3]
        else:
            raise SystemExit(
                json.dumps({"error": "pack requires an explicit outdir",
                            "usage": f"python fx_data.py pack {pair} <outdir>",
                            "why": "defaulting to theses/<pair> overwrites a dated run folder"}))
        build_usdjpy(outdir)


if __name__ == "__main__":
    main_guard(main)
