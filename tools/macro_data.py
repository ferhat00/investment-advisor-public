"""Macro dashboard + regime classification from FRED (keyless), plus a literature-backed
multi-block macro pack (nowcast, curve decomposition, financial conditions + Growth-at-Risk,
liquidity/plumbing, inflation attribution, global/cross-asset overlay, fiscal, valuation).

CLI:
  python macro_data.py dashboard   # 15-series snapshot (unchanged)
  python macro_data.py regime      # {dashboard, regime} quadrant (unchanged)
  python macro_data.py nowcast|curve|conditions|liquidity|inflation|policy|global|fiscal|valuation
  python macro_data.py full        # assemble the extended macro.json superset

Design: dashboard()/regime() are preserved byte-for-byte (ORCHESTRATOR contract). New pillars
are additive block builders that degrade to null/"n/a" on failure and never abort the run.
Sources are free FRED series where possible; a few are published CSVs (Excess Bond Premium);
r*/shadow rate/CAPE/COT/policy-surprise series are consume-via-web ("n/a (manual — see Tools)").
"""
import sys
import numpy as np
import pandas as pd
from common import (fred, jprint, rnd, fetch_csv, safe, zscore_last,
                    probit_fit, quantile_reg, _norm_cdf, yf_history, main_guard)

SERIES = {
    "fed_funds": "DFF", "ust_2y": "DGS2", "ust_10y": "DGS10", "ust_30y": "DGS30",
    "cpi": "CPIAUCSL", "core_pce": "PCEPILFE", "unemployment": "UNRATE",
    "hy_oas": "BAMLH0A0HYM2", "ig_oas": "BAMLC0A0CM", "dxy_broad": "DTWEXBGS",
    "wti": "DCOILWTICO", "breakeven_10y": "T10YIE", "real_10y": "DFII10",
    "sahm": "SAHMREALTIME", "nfci": "NFCI",
}

def dashboard():
    out = {}
    for name, sid in SERIES.items():
        try:
            s = fred(sid, start="2015-01-01")
            last = s.iloc[-1]
            if name in ("cpi", "core_pce"):
                yoy = 100 * (s.iloc[-1] / s.iloc[-13] - 1)
                out[name + "_yoy_pct"] = rnd(yoy)
                out[name + "_yoy_3m_ago_pct"] = rnd(100 * (s.iloc[-4] / s.iloc[-16] - 1))
            else:
                out[name] = rnd(last)
                out[name + "_6m_ago"] = rnd(s[s.index <= s.index[-1] - pd_offset(126)].iloc[-1]) if len(s) > 130 else None
            out[name + "_date"] = str(s.index[-1].date())
        except Exception as e:
            out[name] = f"error: {e}"
    if isinstance(out.get("ust_10y"), float) and isinstance(out.get("ust_2y"), float):
        out["curve_2s10s_bp"] = rnd(100 * (out["ust_10y"] - out["ust_2y"]), 0)
    return out

def pd_offset(days):
    import pandas as pd
    return pd.Timedelta(days=days * 1.45)

def regime(d=None):
    """Crude 4-quadrant read: growth (unemployment/Sahm/NFCI) x inflation (core PCE trend)."""
    d = d or dashboard()
    notes, growth, inflation = [], 0, 0
    sahm = d.get("sahm"); nfci = d.get("nfci")
    if isinstance(sahm, float):
        growth -= 1 if sahm >= 0.5 else 0
        notes.append(f"Sahm {sahm} ({'recession signal' if sahm >= 0.5 else 'no recession signal'})")
    if isinstance(nfci, float):
        growth += 1 if nfci < 0 else -1
        notes.append(f"NFCI {nfci} ({'loose' if nfci < 0 else 'tight'} financial conditions)")
    hy = d.get("hy_oas")
    if isinstance(hy, float):
        growth += 1 if hy < 4.0 else (-1 if hy > 5.5 else 0)
        notes.append(f"HY OAS {hy}% ({'calm' if hy < 4 else 'stressed' if hy > 5.5 else 'neutral'} credit)")
    pce_now, pce_prev = d.get("core_pce_yoy_pct"), d.get("core_pce_yoy_3m_ago_pct")
    if isinstance(pce_now, float):
        inflation += 1 if pce_now > 2.5 else -1
        if isinstance(pce_prev, float):
            inflation += 1 if pce_now > pce_prev else -1
        notes.append(f"Core PCE {pce_now}% yoy vs {pce_prev}% 3m ago")
    quad = ("reflation/goldilocks" if growth > 0 and inflation <= 0 else
            "overheat (growth+, inflation+)" if growth > 0 else
            "stagflation risk (growth-, inflation+)" if inflation > 0 else
            "disinflationary slowdown")
    curve = d.get("curve_2s10s_bp")
    if curve is not None:
        notes.append(f"2s10s {curve}bp ({'steep' if curve > 50 else 'inverted' if curve < 0 else 'flat'})")
    return {"quadrant": quad, "growth_score": growth, "inflation_score": inflation, "notes": notes}

# ============================================================================
# Extended macro pack — literature-backed pillar blocks (additive; each degrades
# to null/"n/a" on failure and never aborts the run). dashboard()/regime() above
# are preserved byte-for-byte for the ORCHESTRATOR contract.
# ============================================================================

def _last(sid, errs, start="2015-01-01", nd=2):
    """Latest value + date of a FRED series as {'value','date'}; degrades to Nones."""
    s = safe(lambda: fred(sid, start=start), sid, errs)
    if s is None or len(s) == 0:
        return {"value": None, "date": None}
    return {"value": rnd(float(s.iloc[-1]), nd), "date": str(s.index[-1].date())}

def _isnum(x):
    """True for real numbers (int/float), excluding bool — for defensive score checks."""
    return isinstance(x, (int, float)) and not isinstance(x, bool)

def _yoy(sid, errs, periods=12, start="2010-01-01", nd=2):
    """Year-over-year % change of a FRED series (`periods` observations back)."""
    s = safe(lambda: fred(sid, start=start), sid, errs)
    if s is None or len(s) <= periods:
        return {"value": None, "date": None}
    v = 100.0 * (float(s.iloc[-1]) / float(s.iloc[-1 - periods]) - 1.0)
    return {"value": rnd(v, nd), "date": str(s.index[-1].date())}

def _hlw_rstar():
    """Best-effort NY Fed Holston-Laubach-Williams r* (US, one-sided). Distributed as xlsx;
    pd.read_excel needs an engine (openpyxl) that may be absent — any failure raises and the
    caller degrades to the n/a note. Returns the latest US r* estimate as a float."""
    url = ("https://www.newyorkfed.org/medialibrary/media/research/economists/williams/data/"
           "Holston_Laubach_Williams_current_estimates.xlsx")
    df = pd.read_excel(url, sheet_name=0)
    col = next(c for c in df.columns if "rstar" in str(c).lower() or "r-star" in str(c).lower())
    return float(pd.to_numeric(df[col], errors="coerce").dropna().iloc[-1])


def _wuxia_shadow():
    """Best-effort Atlanta Fed Wu-Xia shadow federal funds rate (xlsx). Degrades on failure."""
    url = ("https://www.atlantafed.org/-/media/documents/datafiles/cqer/research/"
           "wu-xia-shadow-federal-funds-rate/wu-xia-shadow-federal-funds-rate.xlsx")
    df = pd.read_excel(url, sheet_name=-1)
    ser = pd.to_numeric(df.iloc[:, -1], errors="coerce").dropna()
    return float(ser.iloc[-1])


def _frbsf_mp_surprise():
    """Best-effort FRBSF orthogonalized monetary-policy surprise series (Bauer-Swanson).
    No stable free CSV endpoint → typically degrades to the n/a note."""
    df = fetch_csv("https://www.frbsf.org/research-and-insights/data-and-indicators/"
                   "monetary-policy-surprises/mps.csv")
    col = next(c for c in df.columns if "surprise" in str(c).lower() or "mps" in str(c).lower())
    return float(pd.to_numeric(df[col], errors="coerce").dropna().iloc[-1])


def inflation():
    """Robust underlying-inflation cores + market expectations. Source attribution
    (Shapiro supply/demand, CIE, UIG) and the Kilian oil-shock split are consume-via-web."""
    e = []
    out = {
        "trimmed_mean_pce_yoy": _last("PCETRIM12M159SFRBDAL", e),
        "median_cpi_yoy": _last("MEDCPIM158SFRBCLE", e),
        "trimmed_cpi_16pct_yoy": _last("TRMMEANCPIM158SFRBCLE", e),
        "sticky_cpi_yoy": _last("CORESTICKM159SFRBATL", e),
        "breakeven_5y": _last("T5YIE", e),
        "breakeven_10y": _last("T10YIE", e),
        "fwd_5y5y_breakeven": _last("T5YIFR", e),
    }
    tm = safe(lambda: fred("PCETRIM12M159SFRBDAL"), "trim_dir", e)
    if tm is not None and len(tm) > 4:
        out["trimmed_mean_pce_3m_ago"] = rnd(float(tm.iloc[-4]), 2)
        out["trimmed_mean_pce_dir"] = "rising" if tm.iloc[-1] > tm.iloc[-4] else "falling"
    out["shapiro_supply_demand_pce"] = "n/a (manual — FRBSF supply- vs demand-driven PCE)"
    out["common_infl_expectations"] = "n/a (manual — Fed Board CIE / Ahn-Fulton)"
    out["nyfed_uig"] = "n/a (manual — NY Fed Underlying Inflation Gauge)"
    out["oil_shock_note"] = ("Kilian supply/demand/precautionary split — read WTI "
                             "(dashboard.wti) + copper/gold vs global activity; see Tools")
    tmv, stv = out["trimmed_mean_pce_yoy"]["value"], out["sticky_cpi_yoy"]["value"]
    if isinstance(tmv, float):
        out["inflation_read"] = (
            f"Trimmed-mean PCE {tmv}% ({out.get('trimmed_mean_pce_dir', '?')}), sticky CPI "
            f"{stv}% — {'above' if tmv > 2.0 else 'near/below'} 2% target on the low-noise core.")
    out["errors"] = e
    return out

def curve_decomp():
    """Term-premium vs expectations (ACM), near-term forward spread (Engstrom-Sharpe proxy),
    Estrella-Mishkin 10y-3m recession probit, and the real/breakeven split."""
    e = []
    out = {}
    tp = safe(lambda: fred("THREEFYTP10", start="1990-01-01"), "acm_tp", e)
    ust10 = safe(lambda: fred("DGS10", start="1990-01-01"), "dgs10", e)
    if tp is not None and len(tp):
        out["term_premium_acm_10y"] = {"value": rnd(float(tp.iloc[-1]), 2), "date": str(tp.index[-1].date())}
        if ust10 is not None:
            j = pd.concat([ust10.rename("y"), tp.rename("tp")], axis=1).dropna()
            if len(j):
                out["expectations_10y"] = rnd(float(j["y"].iloc[-1] - j["tp"].iloc[-1]), 2)
    gs1 = safe(lambda: fred("GS1", start="2000-01-01"), "gs1", e)
    gs2 = safe(lambda: fred("GS2", start="2000-01-01"), "gs2", e)
    dgs3mo = safe(lambda: fred("DGS3MO", start="2000-01-01"), "dgs3mo", e)
    if all(x is not None and len(x) for x in (gs1, gs2, dgs3mo)):
        fwd_18m = 2.0 * float(gs2.iloc[-1]) - float(gs1.iloc[-1])  # ~1y-fwd-1y ≈ 6q-ahead avg short rate
        out["near_term_fwd_spread_proxy"] = rnd(fwd_18m - float(dgs3mo.iloc[-1]), 2)
        out["near_term_fwd_spread_note"] = ("Engstrom-Sharpe near-term forward spread (proxy: "
            "(2*GS2-GS1)-DGS3MO). Negative = market prices near-term cuts = elevated recession risk.")
    def _em_probit():
        sp = fred("T10Y3M", start="1982-01-01").resample("MS").mean().dropna()
        rec = fred("USREC", start="1982-01-01").resample("MS").last().dropna()
        df = pd.concat([sp.rename("spread"), rec.rename("rec")], axis=1).dropna()
        df["rec_fwd"] = df["rec"].shift(-12)
        d2 = df.dropna(subset=["rec_fwd"])
        beta = probit_fit(d2["rec_fwd"].values, d2["spread"].values)
        cur = float(sp.iloc[-1])
        return rnd(100.0 * _norm_cdf(beta[0] + beta[1] * cur), 1), rnd(cur, 2)
    r = safe(_em_probit, "em_probit", e)
    if r is not None:
        out["recession_prob_probit_12m_pct"] = r[0]
        out["curve_10y3m_bp"] = rnd(r[1] * 100, 0)
    out["real_10y"] = _last("DFII10", e)
    out["breakeven_10y"] = _last("T10YIE", e)
    out["fwd_5y5y_breakeven"] = _last("T5YIFR", e)
    out["errors"] = e
    return out

def conditions():
    """Composite FCI + subindices, default-purged Excess Bond Premium (Gilchrist-Zakrajsek),
    and Growth-at-Risk (quantile regression of real GDP growth on the NFCI)."""
    e = []
    out = {
        "nfci": _last("NFCI", e), "anfci": _last("ANFCI", e),
        "nfci_credit": _last("NFCICREDIT", e), "nfci_risk": _last("NFCIRISK", e),
        "nfci_leverage": _last("NFCILEVERAGE", e), "stlfsi4": _last("STLFSI4", e),
    }
    def _ebp():
        df = fetch_csv("https://www.federalreserve.gov/econresdata/notes/feds-notes/2016/files/ebp_csv.csv")
        df = df.dropna(subset=["ebp"])
        row = df.iloc[-1]
        prev = df.iloc[-7] if len(df) > 7 else df.iloc[0]
        return {
            "ebp": rnd(float(row["ebp"]), 3), "gz_spread": rnd(float(row["gz_spread"]), 3),
            "gz_recession_prob": rnd(float(row["est_prob"]), 3), "date": str(row["date"]),
            "ebp_6m_ago": rnd(float(prev["ebp"]), 3),
            "ebp_dir": "widening" if row["ebp"] > prev["ebp"] else "compressing",
        }
    ebp = safe(_ebp, "excess_bond_premium", e)
    out["excess_bond_premium"] = ebp if ebp is not None else "n/a (fetch failed — see errors)"
    def _gar():
        gdp = fred("GDPC1", start="1975-01-01")
        g = (400.0 * np.log(gdp / gdp.shift(1))).dropna().resample("QS").last()
        nfci = fred("NFCI", start="1975-01-01").resample("QS").mean()
        df = pd.concat([g.rename("g"), nfci.rename("nfci")], axis=1).dropna()
        cur = float(fred("NFCI").iloc[-1])
        y, x = df["g"].values, df["nfci"].values
        b05, b50 = quantile_reg(y, x, 0.05), quantile_reg(y, x, 0.50)
        gar5, med = b05[0] + b05[1] * cur, b50[0] + b50[1] * cur
        taus = np.linspace(0.05, 0.95, 19)
        preds = np.array([(lambda b: b[0] + b[1] * cur)(quantile_reg(y, x, t)) for t in taus])
        if preds[0] > 0:
            p_neg = 0.0
        elif preds[-1] < 0:
            p_neg = 1.0
        else:
            p_neg = float(np.interp(0.0, preds, taus))
        return {"gar_5pct_growth": rnd(gar5, 1), "median_growth": rnd(med, 1),
                "p_negative_growth": rnd(p_neg, 2), "current_nfci": rnd(cur, 2),
                "method": "numpy IRLS quantile reg: annualized real GDP growth ~ NFCI, since 1975"}
    gar = safe(_gar, "growth_at_risk", e)
    out["growth_at_risk"] = gar if gar is not None else "n/a (compute failed — see errors)"
    out["gar_caveat"] = "GaR is a conditioning gauge, not a recession-timing oracle (OOS caveat: Plagborg-Moller)."
    out["errors"] = e
    return out

def liquidity():
    """Fed net liquidity + funding-stress plumbing. Policy stance vs neutral (r*, shadow rate,
    proxy rate) is consume-via-web. CAVEAT: net-liquidity↔risk-asset link is unstable."""
    e = []
    out = {}
    walcl = safe(lambda: fred("WALCL", start="2015-01-01"), "walcl", e)
    tga = safe(lambda: fred("WTREGEN", start="2015-01-01"), "tga", e)
    rrp = safe(lambda: fred("RRPONTSYD", start="2015-01-01"), "rrp", e)
    res = safe(lambda: fred("WRESBAL", start="2015-01-01"), "wresbal", e)
    if walcl is not None and len(walcl):
        idx = walcl.index
        tga_a = tga.reindex(idx, method="ffill") if tga is not None else 0.0
        rrp_a = rrp.reindex(idx, method="ffill") if rrp is not None else 0.0
        # WALCL, WTREGEN in $millions; RRPONTSYD in $billions -> net in $billions
        net = ((walcl / 1000.0) - (tga_a / 1000.0) - rrp_a).dropna()
        out["net_liquidity_bn"] = {"value": rnd(float(net.iloc[-1]), 0), "date": str(net.index[-1].date())}
        if len(net) > 13:
            out["net_liquidity_chg_13w_bn"] = rnd(float(net.iloc[-1] - net.iloc[-14]), 0)
            out["net_liquidity_dir"] = "adding" if net.iloc[-1] > net.iloc[-14] else "draining"
        out["components_bn"] = {
            "fed_balance_sheet": rnd(float(walcl.iloc[-1] / 1000.0), 0),
            "tga": rnd(float(tga.iloc[-1] / 1000.0), 0) if tga is not None and len(tga) else None,
            "rrp": rnd(float(rrp.iloc[-1]), 0) if rrp is not None and len(rrp) else None,
            "reserves": rnd(float(res.iloc[-1] / 1000.0), 0) if res is not None and len(res) else None,
        }
    sofr = safe(lambda: fred("SOFR", start="2019-01-01"), "sofr", e)
    iorb = safe(lambda: fred("IORB", start="2019-01-01"), "iorb", e)
    sofr99 = safe(lambda: fred("SOFR99", start="2019-01-01"), "sofr99", e)
    fs = {}
    if sofr is not None and iorb is not None:
        j = pd.concat([sofr.rename("s"), iorb.rename("i")], axis=1).dropna()
        if len(j):
            fs["sofr"] = rnd(float(j["s"].iloc[-1]), 2)
            fs["iorb"] = rnd(float(j["i"].iloc[-1]), 2)
            fs["sofr_iorb_spread_bp"] = rnd(100.0 * (j["s"].iloc[-1] - j["i"].iloc[-1]), 0)
    if sofr99 is not None and sofr is not None:
        j = pd.concat([sofr.rename("s"), sofr99.rename("p99")], axis=1).dropna()
        if len(j):
            fs["sofr99_minus_sofr_bp"] = rnd(100.0 * (j["p99"].iloc[-1] - j["s"].iloc[-1]), 0)
    out["funding_stress"] = fs
    # --- policy stance vs neutral: real policy rate (always free) + r*/shadow-rate ingest ---
    # real fed funds = nominal DFF − trimmed-mean PCE inflation; benchmark it against its own
    # long-run average as a transparent neutral proxy, and against HLW r* when a feed is wired.
    dff = safe(lambda: fred("DFF", start="2000-01-01"), "dff_stance", e)
    tm = safe(lambda: fred("PCETRIM12M159SFRBDAL", start="2000-01-01"), "trim_pce_stance", e)
    rfr = None
    if dff is not None and tm is not None and len(dff) and len(tm):
        rfr = float(dff.iloc[-1]) - float(tm.iloc[-1])
        out["real_fed_funds_pct"] = rnd(rfr, 2)
        realhist = (dff - tm.reindex(dff.index, method="ffill")).dropna()
        if len(realhist) > 100:
            lr = float(realhist.mean())
            out["real_fed_funds_lr_avg_pct"] = rnd(lr, 2)
            out["policy_stance_vs_lr_avg_pp"] = rnd(rfr - lr, 2)
            out["policy_stance_read"] = ("restrictive vs historical average" if rfr - lr > 0.5 else
                                         "accommodative vs historical average" if rfr - lr < -0.5 else
                                         "near the historical-average real policy rate")
    rstar = safe(_hlw_rstar, "hlw_rstar", e)
    out["r_star_hlw"] = rstar if _isnum(rstar) else "n/a (manual — NY Fed Holston-Laubach-Williams r*; xlsx feed)"
    if _isnum(rstar) and rfr is not None:
        out["policy_stance_gap_vs_rstar_pp"] = rnd(rfr - float(rstar), 2)
    shadow = safe(_wuxia_shadow, "wuxia_shadow", e)
    out["shadow_rate_wu_xia"] = shadow if _isnum(shadow) else "n/a (manual — Atlanta Fed Wu-Xia shadow rate; xlsx feed)"
    out["proxy_funds_rate"] = "n/a (manual — SF Fed Proxy Funds Rate)"
    nl = out.get("net_liquidity_chg_13w_bn")
    if isinstance(nl, float):
        out["liquidity_read"] = (f"Net liquidity {out.get('net_liquidity_dir', '?')} "
            f"(13w chg {nl:+.0f}bn). CAVEAT: net-liq↔risk-asset link unstable — conditioning only.")
    out["errors"] = e
    return out

def nowcast():
    """Real-time growth engine: model recession probability, GDP nowcast, weekly activity,
    profits, housing leads, and the labor/Beveridge bloc."""
    e = []
    out = {
        "recession_prob_chauvet_piger_pct": _last("RECPROUSM156N", e),
        "gdpnow_saar_pct": _last("GDPNOW", e),
        "sahm_realtime": _last("SAHMREALTIME", e),
    }
    wei = safe(lambda: fred("WEI", start="2015-01-01"), "wei", e)
    if wei is not None and len(wei) > 8:
        out["weekly_econ_index"] = {"value": rnd(float(wei.iloc[-1]), 2), "date": str(wei.index[-1].date()),
                                    "chg_8w": rnd(float(wei.iloc[-1] - wei.iloc[-9]), 2)}
    out["corporate_profits_yoy_pct"] = _yoy("CP", e, periods=4)
    out["housing"] = {
        "building_permits_k": _last("PERMIT", e), "housing_starts_k": _last("HOUST", e),
        "mortgage_30y_pct": _last("MORTGAGE30US", e), "permits_yoy_pct": _yoy("PERMIT", e, periods=12),
    }
    jolts = safe(lambda: fred("JTSJOL", start="2010-01-01"), "jolts", e)
    unemp = safe(lambda: fred("UNEMPLOY", start="2010-01-01"), "unemploy", e)
    vu = None
    if jolts is not None and unemp is not None:
        j = pd.concat([jolts.rename("v"), unemp.rename("u")], axis=1).dropna()
        if len(j):
            vu = rnd(float(j["v"].iloc[-1] / j["u"].iloc[-1]), 2)
    out["labor"] = {
        "job_openings_k": _last("JTSJOL", e), "quits_rate_pct": _last("JTSQUR", e),
        "initial_claims": _last("ICSA", e), "avg_weekly_hours_mfg": _last("AWHMAN", e),
        "unemployment_pct": _last("UNRATE", e), "vacancy_unemployment_ratio": vu,
    }
    out["lei_diffusion"] = "n/a (manual — Conference Board LEI is licensed; rebuild from components)"
    out["ads_index"] = "n/a (manual — Philadelphia Fed ADS Business Conditions Index)"
    out["nyfed_staff_nowcast"] = "n/a (manual — NY Fed Staff Nowcast, weekly)"
    rp = out["recession_prob_chauvet_piger_pct"]["value"]
    gn = out["gdpnow_saar_pct"]["value"]
    if isinstance(gn, float):
        out["growth_engine_read"] = (f"GDPNow {gn}% SAAR; model recession prob {rp}%; "
            f"V/U {vu}. Real-time growth vector for the quadrant.")
    out["errors"] = e
    return out

def global_overlay():
    """Global Financial Cycle proxy, stock-bond correlation regime, uncertainty complex,
    and a light cross-asset trend read. COT / GPR / MOVE are consume-via-web."""
    e = []
    out = {}
    comps, zs = {}, []
    for key, sid in [("vix", "VIXCLS"), ("broad_dollar", "DTWEXBGS"),
                     ("hy_oas", "BAMLH0A0HYM2"), ("nfci", "NFCI")]:
        s = safe(lambda sid=sid: fred(sid, start="2010-01-01"), sid, e)
        if s is not None and len(s) > 260:
            z = zscore_last(s, lookback=1250)
            if z is not None:
                comps[key] = rnd(z, 2)
                zs.append(z)
    if zs:
        m = float(np.mean(zs))
        out["gfc_proxy"] = {"composite_z": rnd(m, 2), "components_z": comps,
            "read": ("risk-off" if m > 0.5 else "risk-on" if m < -0.5 else "neutral")}
    def _sbcorr():
        spy = yf_history("SPY", period="2y")["Close"].pct_change().dropna()
        tlt = yf_history("TLT", period="2y")["Close"].pct_change().dropna()
        j = pd.concat([spy.rename("s"), tlt.rename("t")], axis=1).dropna()
        c = float(j["s"].tail(63).corr(j["t"].tail(63)))
        return {"corr_63d": rnd(c, 2), "read": ("bonds hedging equities" if c < -0.1 else
                "positive stock-bond corr — diversification broken" if c > 0.2 else "decoupled")}
    sb = safe(_sbcorr, "stock_bond_corr", e)
    if sb is not None:
        out["stock_bond_corr"] = sb
    out["uncertainty"] = {"vix": _last("VIXCLS", e), "epu": _last("USEPUINDXD", e),
        "move": "n/a (manual — ICE BofA MOVE index)", "gpr": "n/a (manual — Caldara-Iacoviello GPR)"}
    def _tsmom():
        res = {}
        for etf in ["SPY", "TLT", "GLD", "DBC", "UUP"]:
            px = yf_history(etf, period="2y")["Close"]
            if len(px) > 252:
                rr = float(px.iloc[-21] / px.iloc[-252] - 1.0)  # 12-1 month momentum
                res[etf] = {"mom_12_1_pct": rnd(100 * rr, 1), "trend": "up" if rr > 0 else "down"}
        return res
    ts = safe(_tsmom, "tsmom", e)
    if ts:
        out["cross_asset_trend"] = ts
    out["cot_positioning"] = "n/a (manual — CFTC Commitments of Traders / OFR Hedge Fund Monitor)"
    out["errors"] = e
    return out

def fiscal():
    """Fiscal impulse (deficit swing + output gap) and r-g debt dynamics. Hutchins FIM and
    Treasury supply / preferred-habitat term premium are consume-via-web."""
    e = []
    out = {}
    fb = safe(lambda: fred("FYFSGDA188S", start="2000-01-01"), "fiscal_balance", e)
    if fb is not None and len(fb) > 1:
        out["fiscal_balance_pct_gdp"] = {"value": rnd(float(fb.iloc[-1]), 2), "date": str(fb.index[-1].date())}
        out["fiscal_impulse_pp"] = rnd(float(fb.iloc[-1] - fb.iloc[-2]), 2)
        out["fiscal_impulse_dir"] = "expansionary" if fb.iloc[-1] < fb.iloc[-2] else "contractionary"
    gdp = safe(lambda: fred("GDP", start="2000-01-01"), "gdp_nom", e)
    pot = safe(lambda: fred("NGDPPOT", start="2000-01-01"), "ngdppot", e)
    if gdp is not None and pot is not None and len(gdp):
        pj = pot.reindex(gdp.index, method="ffill")
        j = pd.concat([gdp.rename("g"), pj.rename("p")], axis=1).dropna()
        if len(j):
            out["output_gap_pct"] = rnd(100.0 * (j["g"].iloc[-1] / j["p"].iloc[-1] - 1.0), 2)
    def _rg():
        debt_ratio = fred("GFDEGDQ188S", start="2000-01-01")
        gdp_n = fred("GDP", start="2000-01-01")
        interest = fred("A091RC1Q027SBEA", start="2000-01-01")  # interest outlays, $B SAAR
        j = pd.concat([debt_ratio.rename("dr"), gdp_n.rename("g"), interest.rename("i")], axis=1).dropna()
        row = j.iloc[-1]
        debt_level = row["dr"] / 100.0 * row["g"]  # $B
        avg_int_rate = 100.0 * row["i"] / debt_level
        nom_g = 100.0 * (j["g"].iloc[-1] / j["g"].iloc[-5] - 1.0) if len(j) > 5 else None
        return {"avg_interest_rate_pct": rnd(avg_int_rate, 2),
                "nominal_gdp_growth_pct": rnd(nom_g, 2) if nom_g is not None else None,
                "r_minus_g_pp": rnd(avg_int_rate - nom_g, 2) if nom_g is not None else None,
                "debt_pct_gdp": rnd(float(row["dr"]), 1)}
    rg = safe(_rg, "r_minus_g", e)
    if rg is not None:
        out["debt_dynamics"] = rg
    out["treasury_supply_note"] = ("n/a (manual — Treasury issuance duration vs SOMA free-float; "
                                   "preferred-habitat term premium, Vayanos-Vila)")
    out["errors"] = e
    return out

def _shiller_cape():
    # Best-effort; most public CAPE endpoints block scrapers -> typically degrades to n/a.
    df = fetch_csv("https://data.nasdaq.com/api/v3/datasets/MULTPL/SHILLER_PE_RATIO_MONTH.csv")
    return float(df.iloc[0, 1])

def valuation_erp():
    """Equity valuation starting point: real-yield discount anchor (reliable) + best-effort
    Shiller CAPE -> cyclically-adjusted earnings yield -> ERP. Welch-Goyal OOS humility."""
    e = []
    out = {}
    real10 = safe(lambda: fred("DFII10", start="2010-01-01"), "dfii10", e)
    ry = float(real10.iloc[-1]) if real10 is not None and len(real10) else None
    out["real_10y_pct"] = rnd(ry, 2) if ry is not None else None
    cape = safe(_shiller_cape, "shiller_cape", e)
    if cape is not None:
        out["shiller_cape"] = rnd(cape, 1)
        ey = 100.0 / cape  # cyclically-adjusted earnings yield
        out["cape_earnings_yield_pct"] = rnd(ey, 2)
        if ry is not None:
            out["equity_risk_premium_pct"] = rnd(ey - ry, 2)
    else:
        out["shiller_cape"] = "n/a (manual — Shiller CAPE / multpl.com; ERP = 100/CAPE - real 10y)"
    out["valuation_caveat"] = ("Welch-Goyal (2008): valuation ratios have weak out-of-sample return "
                               "predictability — regime context, not a timing signal.")
    out["errors"] = e
    return out

def policy_surprises():
    """Market-implied policy path (free, from 30-day fed funds futures) + monetary-policy-surprise
    ingest. High-frequency-identified surprises (Kuttner 2001; Swanson 2021; Bauer-Swanson 2023)
    are not a live FRED series → the FRBSF orthogonalized series is attempted and degrades; the
    agent also scores Fed communication itself. Keys are additive over the prior thin block."""
    e = []
    out = {}
    # market-implied fed funds from 30-day fed funds futures (ZQ): implied = 100 − price
    implied = safe(lambda: 100.0 - float(yf_history("ZQ=F", period="6mo")["Close"].dropna().iloc[-1]),
                   "zq_futures", e)
    dff = safe(lambda: fred("DFF"), "dff_policy", e)
    cur = float(dff.iloc[-1]) if dff is not None and len(dff) else None
    if _isnum(implied):
        out["fed_funds_futures_implied_pct"] = rnd(implied, 3)
        out["current_effective_ffr_pct"] = rnd(cur, 2) if cur is not None else None
        if cur is not None:
            out["priced_change_vs_current_bp"] = rnd(100.0 * (implied - cur), 0)
            out["market_path_read"] = ("market pricing cuts" if implied < cur - 0.05 else
                                       "market pricing hikes" if implied > cur + 0.05 else
                                       "market pricing hold")
    fs = safe(_frbsf_mp_surprise, "frbsf_mp", e)
    out["bauer_swanson_mp_surprise"] = fs if _isnum(fs) else "n/a (manual — FRBSF orthogonalized MP surprises)"
    out["jarocinski_karadi"] = "n/a (manual — sign-restricted MP vs information shocks; needs intraday rate-equity co-move)"
    out["fed_text_hawkishness"] = "n/a (agent scores latest FOMC statement/minutes via LLM + Shapiro-Wilson dictionary)"
    out["note"] = ("Read the surprise vs what the futures path already priced "
                   "(fed_funds_futures_implied) and its information-effect sign (Jarocinski-Karadi 2020): "
                   "a growth-driven 'hawkish' surprise is not risk-negative. "
                   "Refs: Kuttner 2001; Swanson 2021; Bauer-Swanson 2023.")
    out["errors"] = e
    return out

def regime_plus(d, blocks):
    """Extend regime() with literature-backed scoring: a growth-signal-agreement CONFIDENCE,
    a conditions score (NFCI/EBP/GaR) and a liquidity score. Preserves all existing keys."""
    base = regime(d)
    notes = []
    growth = blocks.get("growth") or {}
    cond = blocks.get("conditions") or {}
    liq = blocks.get("liquidity") or {}
    infl = blocks.get("inflation") or {}
    # growth-signal agreement (recession-leaning vote share -> confidence)
    votes = []
    sahm = d.get("sahm")
    if _isnum(sahm):
        votes.append(1 if sahm >= 0.5 else 0)
    rp = (growth.get("recession_prob_chauvet_piger_pct") or {}).get("value")
    if _isnum(rp):
        votes.append(1 if rp >= 20 else 0)
    wei = growth.get("weekly_econ_index") or {}
    if _isnum(wei.get("value")):
        votes.append(1 if wei["value"] < 1.0 else 0)  # WEI < ~1 = below-trend growth
    ebp = cond.get("excess_bond_premium")
    if isinstance(ebp, dict) and _isnum(ebp.get("ebp")):
        votes.append(1 if ebp["ebp"] > 0 else 0)
    if votes:
        share = sum(votes) / len(votes)
        conf = round(2.0 * abs(share - 0.5), 2)  # 1 = unanimous (agree), 0 = 50/50 split
        base["confidence"] = conf
        base["growth_signal_recession_share"] = round(share, 2)
        notes.append(f"{sum(votes)}/{len(votes)} growth signals recession-leaning (confidence {conf}).")
    # conditions score
    cs = 0
    nfci_v = (cond.get("nfci") or {}).get("value")
    if _isnum(nfci_v):
        cs += -1 if nfci_v > 0 else 1
    if isinstance(ebp, dict) and _isnum(ebp.get("ebp")):
        cs += -1 if ebp["ebp"] > 0 else 1
    gar = cond.get("growth_at_risk")
    if isinstance(gar, dict) and _isnum(gar.get("p_negative_growth")):
        cs += -1 if gar["p_negative_growth"] > 0.35 else 0
        notes.append(f"GaR 5th-pctile growth {gar.get('gar_5pct_growth')}%, P(neg) {gar.get('p_negative_growth')}.")
    base["conditions_score"] = cs
    # liquidity score
    ls = 0
    nl = liq.get("net_liquidity_dir")
    ls += 1 if nl == "adding" else (-1 if nl == "draining" else 0)
    fs = (liq.get("funding_stress") or {}).get("sofr_iorb_spread_bp")
    if _isnum(fs):
        ls += -1 if fs > 5 else 0
    base["liquidity_score"] = ls
    tmv = (infl.get("trimmed_mean_pce_yoy") or {}).get("value")
    if _isnum(tmv):
        notes.append(f"Trimmed-mean PCE {tmv}% corroborates the inflation axis.")
    base["growth_score_extended"] = base["growth_score"] + cs + ls
    base["extended_notes"] = notes
    return base

def full():
    """Assemble the extended macro.json superset: dashboard + regime_plus + all pillar blocks.
    Each block is wrapped so one failure degrades to an error entry without aborting the pack."""
    errs = []
    d = dashboard()
    blocks = {
        "growth": safe(nowcast, "growth_block", errs),
        "curve": safe(curve_decomp, "curve_block", errs),
        "conditions": safe(conditions, "conditions_block", errs),
        "liquidity": safe(liquidity, "liquidity_block", errs),
        "inflation": safe(inflation, "inflation_block", errs),
        "policy_surprises": safe(policy_surprises, "policy_block", errs),
        "global": safe(global_overlay, "global_block", errs),
        "fiscal": safe(fiscal, "fiscal_block", errs),
        "valuation": safe(valuation_erp, "valuation_block", errs),
    }
    reg = regime_plus(d, blocks)
    out = {"as_of": str(pd.Timestamp.now().date()), "dashboard": d, "regime": reg}
    for k, v in blocks.items():
        out[k] = v if v is not None else {"error": "block failed — see build_errors"}
    out["build_errors"] = errs
    return out

_BLOCKS = {"nowcast": nowcast, "curve": curve_decomp, "conditions": conditions,
           "liquidity": liquidity, "inflation": inflation, "policy": policy_surprises,
           "global": global_overlay, "fiscal": fiscal, "valuation": valuation_erp}

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dashboard"
    if cmd == "dashboard":
        jprint(dashboard())
    elif cmd == "regime":
        d = dashboard()
        jprint({"dashboard": d, "regime": regime(d)})
    elif cmd == "full":
        jprint(full())
    elif cmd in _BLOCKS:
        jprint(_BLOCKS[cmd]())
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main_guard(main)
