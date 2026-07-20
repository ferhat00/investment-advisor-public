"""Factor exposures & scores vs peer group — the quantamental analyst's systematic screen.

Backward-compatible: `stock_factors()` (single-name, 12 keys) and `zscore_vs_peers()`
(original equal-weight composite) are UNCHANGED, so `factor` and `quant-researcher` agents
that consume factors.json keep working. When peers are supplied the CLI ADDS an enriched
block that turns the naive screen into a modern, reconciliation-ready one:

  - value_detail        : composite value (B/M, S/P, EBIT/EV, CF/P, earnings-yield) + sub-ratio
                          DISPERSION, plus an intangible-adjusted book leg (reuses valuation.py's
                          capitalized R&D + organization capital) and the raw->adjusted FLIP.
                          Refs: Asness-Frazzini-Israel-Moskowitz (2015, JPM 42(1)); Israel-
                          Laursen-Richardson (2021, JPM 47(2)); Arnott-Harvey-Kalesnik-Linnainmaa
                          (2021, FAJ 77(1)); Peters-Taylor (2017, JFE 123(2)); Asness-Frazzini
                          "Devil in HML's Details" (2013, JPM 39(4)).
  - profitability       : gross profitability GP/Assets (Novy-Marx 2013, JFE 108(1)) + a cash-
                          based operating-profitability proxy (Ball-Gerakos-Linnainmaa-Nikolaev
                          2016, JFE 121(1)) — accrual-clean quality above the manipulable lines.
  - discipline          : continuous signed asset growth (Cooper-Gulen-Schill 2008, JF 63(4)) and
                          net share issuance (Pontiff-Woodgate 2008, JF 63(2); FF5 CMA 2015).
  - earnings_momentum   : SUE-like earnings-momentum z + operating-momentum acceleration and its
                          CONCORDANCE with price momentum — the explicit "narrative not yet in the
                          numbers vs stale price" signal (Bernard-Thomas 1989, JAR 27; Novy-Marx
                          2015, NBER WP 20984).
  - residual_momentum   : factor-neutralized momentum from factor_attribution.py residuals
                          (Blitz-Huij-Martens 2011, JEmpFin 18(3)).
  - robust_screen       : MAD-based robust z (winsorized +/-3) + missing-data shrink + a per-name
                          factor CONFLICT score (Ghayur-Heaney-Platt 2018, FAJ 74(3); Fitzgibbons-
                          Friedman-Pomorski-Serban 2017, J. of Investing 26(4)).
  - mispricing          : Stambaugh-Yuan two-cluster aggregate mispricing composite, clusters kept
                          SEPARATE as the diagnostic (Stambaugh-Yuan 2017, RFS 30(4)). Reuses
                          forensic.accruals + credit_tools.ohlson_o.
  - expectation_error   : Piotroski-So value/glamour x fundamental-strength 2x2 (Piotroski-So 2012,
                          RFS 25(9); Piotroski 2000, JAR 38). Reuses forensic.piotroski.

Peer-relative percentiles/z-scores on 3-8-name peer sets are coarse — every cross-sectional
block reports `n` and flags small samples. Nothing here re-implements a forensic/credit/DCF
metric; it reuses their outputs and wires them into the screen.

CLI: python quant_factors.py NVDA AMD,AVGO,INTC,QCOM
"""
import sys
import numpy as np
import yfinance as yf
from common import yf_history, jprint, rnd, main_guard, parse_list
import forensic, credit_tools, valuation, factor_attribution


def stock_factors(ticker, bench="SPY"):
    px = yf_history(ticker, period="2y")["Close"]
    b = yf_history(bench, period="2y")["Close"]
    r, rb = px.pct_change().dropna(), b.pct_change().dropna()
    j = r.index.intersection(rb.index); r, rb = r[j], rb[j]
    i = yf.Ticker(ticker).info or {}
    mom_12_1 = 100 * (px.iloc[-21] / px.iloc[-252] - 1) if len(px) > 252 else None
    beta = float(np.cov(r.tail(252), rb.tail(252))[0, 1] / np.var(rb.tail(252)))
    fcf, mc = i.get("freeCashflow"), i.get("marketCap")
    return {
        "momentum_12_1_pct": rnd(mom_12_1),
        "momentum_6m_pct": rnd(100 * (px.iloc[-1] / px.iloc[-126] - 1)) if len(px) > 126 else None,
        "vol_ann_pct": rnd(100 * r.tail(252).std() * np.sqrt(252)),
        "downside_vol_pct": rnd(100 * r.tail(252)[r < 0].std() * np.sqrt(252)),
        "beta": rnd(beta),
        "value__earnings_yield_pct": rnd(100 / i["forwardPE"], 2) if i.get("forwardPE") else None,
        "value__fcf_yield_pct": rnd(100 * fcf / mc, 2) if (fcf and mc) else None,
        "quality__roe_pct": rnd((i.get("returnOnEquity") or 0) * 100),
        "quality__gross_margin_pct": rnd((i.get("grossMargins") or 0) * 100),
        "quality__debt_equity_pct": rnd(i.get("debtToEquity")),
        "growth__rev_pct": rnd((i.get("revenueGrowth") or 0) * 100),
        "size__mkt_cap_bn": rnd((i.get("marketCap") or 0) / 1e9, 1),
    }


def zscore_vs_peers(ticker, peers, data=None):
    """Original equal-weight z-composite (UNCHANGED output). `data` may be a precomputed
    {ticker: stock_factors} dict to avoid refetching; if None it is built here."""
    all_t = [ticker] + peers
    if data is None:
        data = {}
        for tk in all_t:
            try: data[tk] = stock_factors(tk)
            except Exception as e: data[tk] = {"error": str(e)}
    keys = ["momentum_12_1_pct", "value__fcf_yield_pct", "quality__roe_pct", "growth__rev_pct", "vol_ann_pct"]
    z = {}
    for k in keys:
        vals = {tk: d.get(k) for tk, d in data.items() if isinstance(d.get(k), (int, float))}
        if len(vals) >= 3:
            arr = np.array(list(vals.values()), dtype=float)
            mu, sd = arr.mean(), arr.std() or 1
            for tk, v in vals.items():
                z.setdefault(tk, {})[k + "_z"] = rnd((v - mu) / sd, 2)
    for tk, zz in z.items():
        comps = [zz.get("momentum_12_1_pct_z", 0), zz.get("value__fcf_yield_pct_z", 0),
                 zz.get("quality__roe_pct_z", 0), zz.get("growth__rev_pct_z", 0),
                 -zz.get("vol_ann_pct_z", 0)]
        zz["composite"] = rnd(float(np.mean(comps)), 2)
    return {"factors": data, "zscores_vs_peers": z,
            "note": "composite = mean(momentum, fcf-yield, roe, growth, -vol) z-scores"}


# =============================== enrichment layer ===============================

def _safe(a, b):
    try:
        return a / b if (a is not None and b not in (None, 0)) else None
    except (TypeError, ZeroDivisionError):
        return None


def _robust_z(values):
    """MAD-based robust z, winsorized to +/-3. `values` is {ticker: value|None}.
    Robust to the outlier peer / tiny sample that would swing an ordinary z."""
    arr = np.array([v for v in values.values() if isinstance(v, (int, float))], dtype=float)
    out = {tk: None for tk in values}
    if len(arr) < 3:
        return out
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    scale = 1.4826 * mad if mad > 0 else (float(arr.std()) or 1.0)
    for tk, v in values.items():
        if isinstance(v, (int, float)):
            out[tk] = rnd(float(np.clip((v - med) / scale, -3, 3)), 2)
    return out


def _pct_rank(values, high_is_overpriced=True):
    """Peer percentile [0,100] where 100 = most OVERPRICED (lowest expected return).
    high_is_overpriced flips whether a high or low raw value maps to overpricing."""
    items = [(tk, v) for tk, v in values.items() if isinstance(v, (int, float))]
    out = {tk: None for tk in values}
    if len(items) < 3:
        return out
    vals = np.array([v for _, v in items], dtype=float)
    order = vals.argsort()
    ranks = np.empty(len(vals)); ranks[order] = np.arange(len(vals))
    pct = 100 * ranks / (len(vals) - 1)
    if not high_is_overpriced:
        pct = 100 - pct
    for (tk, _), p in zip(items, pct):
        out[tk] = rnd(float(p), 0)
    return out


def _legs(ticker, sf):
    """Raw factor legs for one name (statements + info fetched once). `sf` is the
    already-computed stock_factors dict (reused for momentum / earnings-yield)."""
    info = yf.Ticker(ticker).info or {}
    try:
        f = forensic.Filings(ticker)
    except Exception:
        f = None
    mc = info.get("marketCap")
    ev = info.get("enterpriseValue")
    L = {"ticker": ticker, "momentum_12_1": (sf or {}).get("momentum_12_1_pct"),
         "vol_ann": (sf or {}).get("vol_ann_pct"), "growth_rev": (sf or {}).get("growth__rev_pct")}

    # --- value ratios (higher = cheaper) ---
    pb = info.get("priceToBook"); ps = info.get("priceToSalesTrailing12Months")
    L["bm"] = _safe(1.0, pb)                                   # book/market
    L["sp"] = _safe(1.0, ps)                                   # sales/price
    L["ey"] = (sf or {}).get("value__earnings_yield_pct")      # %
    if f is not None:
        ebit = f.I(["EBIT", "Operating Income"])
        cfo = f.cfo(0)
        L["ebit_ev"] = _safe(ebit, ev)
        L["cfp"] = _safe(cfo, mc)
        # --- profitability (accrual-clean) ---
        gp = f.gross_profit(0); ta = f.total_assets(0)
        L["gp_to_assets"] = _safe(gp, ta)                      # Novy-Marx
        sga = f.sga(0) or 0
        op = (gp - sga) if gp is not None else None            # operating profit (SG&A excl R&D in yf)
        d_rec = _safe((f.receivables(0) or 0) - (f.receivables(1) or 0), 1)
        d_inv = (f.inventory(0) or 0) - (f.inventory(1) or 0)
        pay0 = f.B(["Accounts Payable", "Payables"], 0) or 0
        pay1 = f.B(["Accounts Payable", "Payables"], 1) or 0
        d_pay = pay0 - pay1
        cash_op = (op - ((f.receivables(0) or 0) - (f.receivables(1) or 0)) - d_inv + d_pay) if op is not None else None
        L["cash_op_to_assets"] = _safe(cash_op, ta)            # BGL cash-based OP proxy
        ni = f.net_income(0)
        L["roa"] = _safe(ni, ta)
        # --- capital discipline ---
        ta1 = f.total_assets(1)
        L["asset_growth"] = (_safe(ta, ta1) - 1) if _safe(ta, ta1) is not None else None
        sh0, sh1 = f.shares(0), f.shares(1)
        L["net_issuance"] = (_safe(sh0, sh1) - 1) if _safe(sh0, sh1) is not None else None
        capex = abs(f.C(["Capital Expenditure"], 0) or 0)
        L["investment_to_assets"] = _safe(capex, ta1)
        # NOA/TA (Hirshleifer): (operating assets) - (operating liabilities), scaled by lagged TA
        cash = f.cash(0) or 0
        tl = f.B(["Total Liabilities Net Minority Interest"], 0)
        debt = info.get("totalDebt") or ((f.ltd(0) or 0))
        noa = (ta - cash) - ((tl - debt) if tl is not None else 0) if ta is not None else None
        L["noa_to_ta"] = _safe(noa, ta1)
        # --- distress (reuse credit_tools.ohlson_o) ---
        wc = f.B(["Working Capital"], 0)
        ca, cl = f.current_assets(0), f.current_liab(0)
        try:
            oh = credit_tools.ohlson_o(ta, tl, wc, cl, ca, ni, f.net_income(1), cfo)
            L["distress_prob"] = (oh or {}).get("distress_probability")
        except Exception:
            L["distress_prob"] = None
        # --- accruals (reuse forensic) ---
        try:
            L["accrual_ratio"] = forensic.accruals(f).get("sloan_accrual_ratio")
        except Exception:
            L["accrual_ratio"] = None
        # --- fundamental strength (reuse forensic) ---
        try:
            L["fscore"] = forensic.piotroski(f).get("piotroski_f")
        except Exception:
            L["fscore"] = None
        # --- earnings momentum (annual SUE-like) ---
        L.update(_earnings_momentum(f))
    return L


def _earnings_momentum(f):
    """Annual SUE-like earnings-momentum z + operating-momentum acceleration. yfinance
    carries only ~4 annual points, so this is a low-frequency proxy for a true seasonal
    SUE (flagged); analyst-revision SUE needs a consensus feed we don't have."""
    eps = []
    for y in range(4):
        e = f.I(["Diluted EPS", "Basic EPS"], y)
        if e is None:
            ni, sh = f.net_income(y), f.shares(y)
            e = _safe(ni, sh)
        eps.append(e)
    eps = [e for e in eps if e is not None]
    out = {"eps_growth_pct": None, "earnings_mom_z": None, "operating_mom_accel_pct": None}
    if len(eps) >= 2 and eps[1]:
        out["eps_growth_pct"] = rnd(100 * (eps[0] / eps[1] - 1)) if eps[1] != 0 else None
    if len(eps) >= 3:
        gr = [(_safe(eps[i], eps[i + 1]) - 1) for i in range(len(eps) - 1)
              if _safe(eps[i], eps[i + 1]) is not None]
        if len(gr) >= 2:
            sd = float(np.std(gr)) or None
            out["earnings_mom_z"] = rnd(gr[0] / sd, 2) if sd else None      # latest growth vs its own vol
            out["operating_mom_accel_pct"] = rnd(100 * (gr[0] - gr[1]))     # 2nd difference
    return out


def _value_detail(legs, ticker):
    """Composite value z (5 ratios) + dispersion, and subject intangible-adjusted book flip."""
    ratios = ["bm", "sp", "ebit_ev", "cfp", "ey"]
    zsub = {k: _robust_z({t: legs[t].get(k) for t in legs}) for k in ratios}
    detail = {}
    for t in legs:
        zs = [zsub[k][t] for k in ratios if zsub[k][t] is not None]
        detail[t] = {"value_composite_z": rnd(float(np.mean(zs)), 2) if zs else None,
                     "value_dispersion": rnd(float(np.std(zs)), 2) if len(zs) >= 2 else None,
                     "n_ratios": len(zs),
                     "sub_z": {k: zsub[k][t] for k in ratios}}
    # subject intangible-adjusted book flip (reuse valuation.normalize intangibles)
    try:
        info = yf.Ticker(ticker).info or {}
        mc = info.get("marketCap")
        nrm = valuation.normalize(ticker)
        f = forensic.Filings(ticker)
        eq = f.B(["Common Stock Equity", "Stockholders Equity"], 0)
        intan = nrm.get("intangible_capital") or 0
        if mc and eq:
            raw_bm = eq / mc
            adj_bm = (eq + intan) / mc
            material = adj_bm > raw_bm * 1.15
            detail["_intangible_adjustment"] = {
                "raw_book_to_market": rnd(raw_bm, 3),
                "intangible_adj_book_to_market": rnd(adj_bm, 3),
                "intangible_capital": rnd(intan, 0),
                "book_uplift_pct": rnd(100 * (adj_bm / raw_bm - 1)) if raw_bm else None,
                "flip": ("material intangible gap: capitalizing R&D + organization capital raises book "
                         "notably — if the intangibles are economically real (R&D-heavy moat), the raw-book "
                         "screen understates value; for an SG&A-heavy asset-light-in-name-only business the "
                         "org-capital add-back can overstate it, so judge the source before crediting the flip"
                         if material else "little intangible gap — GAAP book is a fair value proxy here"),
                "note": "adj book = equity + capitalized R&D + organization capital (valuation.normalize); "
                        "org capital = 30% of SG&A capitalized, a blanket rate that overstates for asset-heavy firms",
            }
    except Exception as e:
        detail["_intangible_adjustment"] = {"error": str(e)}
    return detail


def _robust_screen(legs):
    """Robust-z composite over value/profitability/momentum/growth/low-vol with a missing-data
    shrink, plus a per-name factor CONFLICT score (dispersion across the leg z's)."""
    fields = {  # name: (leg_key, higher_is_better)
        "value": ("bm", True), "profitability": ("gp_to_assets", True),
        "momentum": ("momentum_12_1", True), "growth": ("growth_rev", True),
        "low_vol": ("vol_ann", False)}
    zmap = {}
    for nm, (key, hib) in fields.items():
        z = _robust_z({t: legs[t].get(key) for t in legs})
        if not hib:
            z = {t: (-v if v is not None else None) for t, v in z.items()}
        zmap[nm] = z
    out = {}
    for t in legs:
        zs = {nm: zmap[nm][t] for nm in fields if zmap[nm][t] is not None}
        vals = list(zs.values())
        out[t] = {"leg_z": zs,
                  "composite_robust": rnd(float(np.mean(vals)), 2) if vals else None,
                  "conflict_score": rnd(float(np.std(vals)), 2) if len(vals) >= 2 else None,
                  "n_used": len(vals)}
    return out


_CLUSTER_A = {"net_issuance": True, "accrual_ratio": True, "noa_to_ta": True,
              "asset_growth": True, "investment_to_assets": True}      # financing/investment
_CLUSTER_B = {"momentum_12_1": False, "gp_to_assets": False, "roa": False,
              "distress_prob": True}                                    # performance/distress


def _mispricing(legs):
    """Stambaugh-Yuan two-cluster aggregate mispricing (peer-relative percentiles; 100 = most
    overpriced). Clusters kept SEPARATE — their disagreement localizes the mispricing story."""
    def cluster_pct(spec):
        pr = {leg: _pct_rank({t: legs[t].get(leg) for t in legs}, high_is_overpriced=hib)
              for leg, hib in spec.items()}
        res = {}
        for t in legs:
            vals = [pr[leg][t] for leg in spec if pr[leg][t] is not None]
            res[t] = (rnd(float(np.mean(vals)), 0) if vals else None, len(vals))
        return res
    A, B = cluster_pct(_CLUSTER_A), cluster_pct(_CLUSTER_B)
    out = {}
    for t in legs:
        a, na = A[t]; b, nb = B[t]
        comp = rnd(float(np.mean([x for x in (a, b) if x is not None])), 0) if (a is not None or b is not None) else None
        disagree = rnd(abs(a - b), 0) if (a is not None and b is not None) else None
        read = None
        if a is not None and b is not None:
            if disagree is not None and disagree >= 30:
                hi, lo = ("financing/investment", "performance/distress") if a > b else ("performance/distress", "financing/investment")
                read = (f"clusters DISAGREE ({disagree:.0f}pts): {hi} flags overpricing while {lo} does not — "
                        "localize the gap here rather than average it")
            elif comp is not None and comp >= 70:
                read = "both clusters lean overpriced — systematic short-side signal for the fundamental read to confirm/refute"
            elif comp is not None and comp <= 30:
                read = "both clusters lean underpriced — systematic long-side signal"
            else:
                read = "mispricing signal muted / mid-pack"
        out[t] = {"cluster_A_financing_investment_pct": a, "cluster_B_performance_distress_pct": b,
                  "mispricing_composite_pct": comp, "cluster_disagreement_pts": disagree,
                  "legs_used": {"A": na, "B": nb}, "read": read}
    return out


def _expectation_error(legs):
    """Piotroski-So value/glamour x fundamental-strength 2x2. Premium concentrates in the
    INCONGRUENT cells (cheap+strong = underpriced; expensive+weak = overpriced)."""
    bm_pct = _pct_rank({t: legs[t].get("bm") for t in legs}, high_is_overpriced=True)  # high B/M = cheap = "value"
    out = {}
    for t in legs:
        p = bm_pct[t]; fs = legs[t].get("fscore")
        if p is None or fs is None:
            out[t] = {"value_bucket": None, "fscore": fs, "cell": None,
                      "read": "insufficient peers or F-score"}
            continue
        bucket = "value(cheap)" if p >= 66 else "glamour(expensive)" if p <= 33 else "neutral"
        strength = "strong" if fs >= 7 else "weak" if fs <= 3 else "mid"
        cell, read = f"{bucket} x {strength}", None
        if bucket.startswith("value") and strength == "strong":
            read = "INCONGRUENT: cheap + strong fundamentals — market too pessimistic, underpriced (the long)"
        elif bucket.startswith("glamour") and strength == "weak":
            read = "INCONGRUENT: expensive + weak fundamentals — market too optimistic, overpriced (the short)"
        elif bucket.startswith("value") and strength == "weak":
            read = "CONGRUENT: cheap + weak — value-trap candidate (cheap for a reason)"
        elif bucket.startswith("glamour") and strength == "strong":
            read = "CONGRUENT: expensive + strong — glamour priced for quality"
        else:
            read = "mid-bucket — expectation error not sharply signalled"
        out[t] = {"value_bucket": bucket, "fscore": fs, "cell": cell, "read": read}
    return out


def full_screen(ticker, peers):
    """Original composite + the enriched reconciliation blocks. Small peer sets => coarse
    cross-sectional stats; every block reports n."""
    all_t = [ticker] + peers
    sf_data = {}
    for tk in all_t:
        try: sf_data[tk] = stock_factors(tk)
        except Exception as e: sf_data[tk] = {"error": str(e)}
    out = zscore_vs_peers(ticker, peers, data=sf_data)                 # original keys preserved

    legs = {}
    for tk in all_t:
        try: legs[tk] = _legs(tk, sf_data.get(tk))
        except Exception as e: legs[tk] = {"ticker": tk, "error": str(e)}
    legs = {tk: L for tk, L in legs.items() if "error" not in L} or legs

    n = len([t for t in legs if "error" not in legs.get(t, {"error": 1})])
    small = n < 5
    out["reconciliation"] = {
        "n_names": n,
        "small_sample_caveat": ("peer set < 5 — cross-sectional percentiles/z-scores are coarse; "
                                "treat as directional, lean on the fundamental read" if small else None),
        "value_detail": _value_detail(legs, ticker),
        "profitability_discipline_legs": {t: {k: rnd(legs[t].get(k), 4) for k in
                                              ("gp_to_assets", "cash_op_to_assets", "roa",
                                               "asset_growth", "net_issuance", "investment_to_assets", "noa_to_ta")}
                                          for t in legs},
        "earnings_momentum": {t: {k: legs[t].get(k) for k in
                                  ("eps_growth_pct", "earnings_mom_z", "operating_mom_accel_pct")}
                              for t in legs},
        "robust_screen": _robust_screen(legs),
        "mispricing": _mispricing(legs),
        "expectation_error": _expectation_error(legs),
    }

    # subject-level: price-vs-earnings momentum concordance + residual (factor-neutral) momentum
    subj = legs.get(ticker, {})
    pm = subj.get("momentum_12_1"); em = subj.get("earnings_mom_z")
    concord = None
    if pm is not None and em is not None:
        if pm > 0 and em > 0:
            concord = "CONCORDANT up: price and earnings momentum agree — corroborated under-reaction drift"
        elif pm > 0 and em <= 0:
            concord = "DIVERGENT: price up while earnings momentum flat/negative — narrative running ahead of the numbers (stale-narrative, crash-prone)"
        elif pm <= 0 and em > 0:
            concord = "DIVERGENT: earnings up while price lags — under-reaction / catalyst not yet priced (the investable case)"
        else:
            concord = "CONCORDANT down: price and earnings momentum both weak"
    out["reconciliation"]["momentum_concordance"] = {
        "price_momentum_12_1_pct": pm, "earnings_mom_z": em, "read": concord}
    try:
        out["reconciliation"]["residual_momentum"] = factor_attribution.residual_momentum(ticker)
    except Exception as e:
        out["reconciliation"]["residual_momentum"] = {"error": str(e)}
    try:
        out["reconciliation"]["crowding"] = crowding_factors(ticker)
    except Exception as e:
        out["reconciliation"]["crowding"] = {"error": str(e)}
    out["reconciliation"]["reads_note"] = (
        "systematic screen only — pair the mispricing composite / expectation-error cell / momentum "
        "concordance with the independent bottom-up read; DIAGNOSE disagreements, do not average them.")
    return out


# =============================== crowding / positioning ===============================
# Contrarian-lens descriptors of how CROWDED/extreme positioning already is (NOT a factor
# return forecast — the factor agent owns factor construction). Reuses factor_attribution
# residuals so the "extreme is in the nonfundamental part" test needs no new plumbing.

def _residual_reversal(ticker, lookback=21):
    """Factor-neutral short-term reversal (Da-Liu-Schaumburg 2014, Mgmt Sci; Blitz-Huij-
    Lansdorp-Verbeek 2013, J.Fin.Markets). Strips market + style-factor moves via
    factor_attribution.residual_returns, then measures the trailing ~1-month residual. The
    extreme in the NONFUNDAMENTAL part of the move mean-reverts far more reliably than raw price."""
    try:
        rs = factor_attribution.residual_returns(ticker, period="2y")
    except Exception as e:
        return {"error": str(e)}
    if rs is None or len(rs) < lookback + 5:
        return {"error": "insufficient residual history"}
    win = rs.iloc[-lookback:]
    resid_cum = float(win.sum())
    sd = float(rs.std())
    z = resid_cum / (sd * np.sqrt(lookback)) if sd > 0 else None
    raw = None
    try:
        px = yf_history(ticker, period="6mo")["Close"]
        raw = 100 * (px.iloc[-1] / px.iloc[-lookback - 1] - 1) if len(px) > lookback else None
    except Exception:
        pass
    read = "nonfundamental move mild — no reversal edge"
    if z is not None:
        if z <= -1.0:
            read = ("residual sharply NEGATIVE — the selloff is mostly stock-specific/nonfundamental; "
                    "history says this part mean-reverts UP (contrarian-long candidate, if no fresh fundamental cause)")
        elif z >= 1.0:
            read = ("residual sharply POSITIVE — the run-up is mostly stock-specific/nonfundamental; "
                    "prone to mean-revert DOWN (fade candidate)")
    return {"resid_reversal_1m_pct": rnd(100 * resid_cum), "resid_reversal_z": rnd(z, 2),
            "raw_return_1m_pct": rnd(raw), "window_days": lookback, "read": read,
            "note": "residual = daily return minus market+momentum+value+quality+low-vol+size factor moves "
                    "(factor_attribution); isolates the mean-reverting overreaction from justified repricing"}


def _ownership_breadth(ticker, i):
    """Chen-Hong-Stein (2002, JFE) breadth of ownership. The predictive signal is the CHANGE in
    the number of funds holding — a fall in breadth at a high price is a textbook fade. That
    change needs a 13F holdings feed; free data gives only levels, so change degrades to null."""
    out = {"held_pct_institutions": rnd((i.get("heldPercentInstitutions") or 0) * 100, 1) or None,
           "held_pct_insiders": rnd((i.get("heldPercentInsiders") or 0) * 100, 1) or None,
           "breadth_change_qoq": None,
           "note": "Chen-Hong-Stein change-in-breadth (funds holding, QoQ) requires a 13F holdings feed "
                   "(S&P Global / Refinitiv / WhaleWisdom). Not available free => null; levels only."}
    try:
        ih = yf.Ticker(ticker).institutional_holders
        if ih is not None and not ih.empty:
            out["n_reported_institutional_holders"] = int(len(ih))
    except Exception:
        pass
    return out


def _crash_gate(ticker, mom_etf="MTUM", mkt="SPY"):
    """Momentum-crash fragility gate (Daniel-Moskowitz 2016, JFE; Barroso-Santa-Clara 2015, JFE):
    the loser leg reverses violently in a bear market with elevated momentum-factor volatility.
    Tells the contrarian WHEN an extreme is most likely to snap back. Market-state overlay from
    ETFs — a positioning-fragility read, not a macro-regime forecast (that is the macro agent's lane)."""
    try:
        m = yf_history(mom_etf, period="3y")["Close"].pct_change().dropna()
        spx = yf_history(mkt, period="3y")["Close"]
    except Exception as e:
        return {"error": str(e)}
    rv = (m.rolling(126).std() * np.sqrt(252)).dropna()
    if rv.empty:
        return {"error": "insufficient momentum-factor history"}
    wml_vol, wml_med = float(rv.iloc[-1]), float(rv.median())
    hi_vol = bool(wml_vol > 1.3 * wml_med)
    spx_ret_12m = float(spx.iloc[-1] / spx.iloc[-252] - 1) if len(spx) > 252 else None
    bear = bool((spx_ret_12m is not None and spx_ret_12m < 0) or (spx.iloc[-1] < spx.tail(252).mean()))
    try:
        px = yf_history(ticker, period="2y")["Close"]
        name_mom = 100 * (px.iloc[-21] / px.iloc[-252] - 1) if len(px) > 252 else None
    except Exception:
        name_mom = None
    fragile = bool(hi_vol and bear)
    read = ("MOMENTUM-CRASH regime (bear market + elevated momentum-factor vol): extreme momentum is most "
            "likely to reverse violently and the loser leg snaps back hardest — the highest-payoff window to "
            "FADE an extreme, but size for whipsaw" if fragile else
            "no momentum-crash regime — extreme momentum more likely to persist; fading is lower-odds here")
    return {"wml_proxy_vol_ann_pct": rnd(100 * wml_vol), "wml_vol_vs_median": rnd(wml_vol / wml_med, 2),
            "high_vol_state": hi_vol, "market_12m_ret_pct": rnd(100 * spx_ret_12m) if spx_ret_12m is not None else None,
            "bear_state": bear, "name_momentum_12_1_pct": rnd(name_mom), "momentum_crash_regime": fragile,
            "read": read, "note": "WML proxied by MTUM ETF realized vol; overlay for WHEN to fade momentum."}


def crowding_factors(ticker):
    """Positioning/crowding descriptors for the contrarian lens: days-to-cover, factor-neutral
    residual short-term reversal, ownership breadth (best-effort), and a momentum-crash fragility
    gate. Free/existing data; premium legs (breadth change) degrade to null with a note.
    Refs: Hong-Li-Ni-Scheinkman-Yan (2015) days-to-cover; Da-Liu-Schaumburg (2014) residual
    reversal; Chen-Hong-Stein (2002) breadth; Daniel-Moskowitz (2016) crashes."""
    i = yf.Ticker(ticker).info or {}
    out = {"ticker": ticker}
    ss = i.get("sharesShort")
    adv = (i.get("averageDailyVolume10Day") or i.get("averageVolume10days")
           or i.get("averageDailyVolume3Month") or i.get("averageVolume"))
    dtc = _safe(ss, adv)
    short = {
        "short_pct_float": rnd((i.get("shortPercentOfFloat") or 0) * 100, 2),
        "shares_short": ss, "avg_daily_volume": adv,
        "days_to_cover": rnd(dtc, 2) if dtc is not None else rnd(i.get("shortRatio"), 2),
        "days_to_cover_source": "sharesShort/ADV" if dtc is not None else "yahoo shortRatio",
        "shares_short_prior_month": i.get("sharesShortPriorMonth"), "short_trend": None,
    }
    spm = i.get("sharesShortPriorMonth")
    if ss and spm:
        short["short_trend"] = ("rising" if ss > spm * 1.05 else "falling" if ss < spm * 0.95 else "flat")
        short["short_delta_pct"] = rnd(100 * (ss / spm - 1))
    dv, sf = short["days_to_cover"], short["short_pct_float"]
    reads = []
    if dv is not None:
        reads.append("high days-to-cover — crowded/trapped short: hard to exit, squeeze fuel on good news" if dv > 5
                     else "low days-to-cover — shorts can exit easily, little squeeze potential" if dv < 1 else "moderate days-to-cover")
    if sf is not None and sf > 10:
        reads.append(f"elevated short interest ({sf:.1f}% float)")
    short["read"] = "; ".join(reads) or "short positioning unremarkable"
    out["short"] = short
    out["residual_reversal"] = _residual_reversal(ticker)
    out["ownership"] = _ownership_breadth(ticker, i)
    out["momentum_crash_gate"] = _crash_gate(ticker)
    out["note"] = ("crowding/positioning descriptors for the contrarian lens — how one-sided and how "
                   "fragile the positioning already is; NOT a factor-return forecast (factor agent's lane).")
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "crowding":
        jprint(crowding_factors(sys.argv[2]))
    else:
        ticker = sys.argv[1]
        peers = parse_list(sys.argv[2] if len(sys.argv) > 2 else "")
        jprint(full_screen(ticker, peers) if peers else stock_factors(ticker))


if __name__ == "__main__":
    main_guard(main)
