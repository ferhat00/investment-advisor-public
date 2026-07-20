"""Forensic accounting / earnings-quality screens — the fundamental analyst's value-trap gate.

Runs BEFORE the DCF is trusted: a cheap valuation built on manipulated or low-quality
earnings is the classic value trap. Every score is computable from the three statements.

CLI:
  python forensic.py mscore NVDA       # Beneish M-Score (manipulation);   flag M > -1.78
  python forensic.py fscore NVDA       # Dechow-Ge-Larson-Sloan F-Score (misstatement)
  python forensic.py piotroski NVDA    # Piotroski F-Score (0-9 fundamental strength)
  python forensic.py accruals NVDA     # Sloan accruals + CFO/NI + FCF/NI conversion
  python forensic.py cscore NVDA       # Montier C-Score (0-6 manipulation flags)
  python forensic.py all NVDA          # ensemble + single PASS / FLAG / FAIL gate

References: Beneish (1999, FAJ); Dechow-Ge-Larson-Sloan (2011, CAR); Piotroski (2000, JAR);
Sloan (1996, TAR); Montier (2008, "Cooking the Books"). yfinance row labels vary, so all
line-item pulls use alias lists via common.stmt_val (same fallback pattern as market_data).
"""
import sys, math, argparse
from common import jprint, rnd, yf_statements, stmt_val, main_guard


def _safe_div(a, b):
    try:
        if a is None or b is None or b == 0:
            return None
        return a / b
    except (TypeError, ZeroDivisionError):
        return None


class Filings:
    """Thin accessor over (income, balance, cashflow); V(names, year) -> float|None.
    year 0 = most recent fiscal year, 1 = prior, 2 = two years back."""

    def __init__(self, ticker):
        self.ticker = ticker
        self.inc, self.bal, self.cf = yf_statements(ticker)
        self.n_years = min(
            self.inc.shape[1] if self.inc is not None else 0,
            self.bal.shape[1] if self.bal is not None else 0,
            self.cf.shape[1] if self.cf is not None else 0,
        )

    def I(self, names, y=0): return stmt_val(self.inc, names, y)
    def B(self, names, y=0): return stmt_val(self.bal, names, y)
    def C(self, names, y=0): return stmt_val(self.cf, names, y)

    # --- shared line items (alias lists tuned to the yfinance schema) ---
    def revenue(self, y=0): return self.I(["Total Revenue", "Operating Revenue"], y)
    def cogs(self, y=0): return self.I(["Cost Of Revenue", "Reconciled Cost Of Revenue"], y)
    def gross_profit(self, y=0):
        gp = self.I(["Gross Profit"], y)
        if gp is not None:
            return gp
        rev, c = self.revenue(y), self.cogs(y)
        return (rev - c) if (rev is not None and c is not None) else None
    def sga(self, y=0): return self.I(["Selling General And Administration", "Selling General And Administrative Expense"], y)
    def rnd_exp(self, y=0): return self.I(["Research And Development"], y)
    def net_income(self, y=0): return self.I(["Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"], y)
    def depreciation(self, y=0):
        return (self.C(["Depreciation", "Depreciation Amortization Depletion", "Depreciation And Amortization"], y)
                or self.I(["Reconciled Depreciation"], y))
    def total_assets(self, y=0): return self.B(["Total Assets"], y)
    def net_ppe(self, y=0): return self.B(["Net PPE"], y)
    def gross_ppe(self, y=0): return self.B(["Gross PPE"], y)
    def current_assets(self, y=0): return self.B(["Current Assets"], y)
    def current_liab(self, y=0): return self.B(["Current Liabilities"], y)
    def receivables(self, y=0): return self.B(["Receivables", "Accounts Receivable"], y)
    def inventory(self, y=0): return self.B(["Inventory"], y)
    def cash(self, y=0): return self.B(["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"], y)
    def ltd(self, y=0): return self.B(["Long Term Debt", "Long Term Debt And Capital Lease Obligation"], y)
    def cfo(self, y=0): return self.C(["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"], y)
    def fcf(self, y=0): return self.C(["Free Cash Flow"], y)
    def shares(self, y=0): return self.B(["Ordinary Shares Number", "Share Issued"], y) or self.I(["Diluted Average Shares"], y)


# ----------------------------- Beneish M-Score -----------------------------
def mscore(f):
    """8-variable Beneish M-Score. M > -1.78 flags likely earnings manipulation."""
    rev0, rev1 = f.revenue(0), f.revenue(1)
    ar0, ar1 = f.receivables(0), f.receivables(1)
    gp0, gp1 = f.gross_profit(0), f.gross_profit(1)
    ca0, ca1 = f.current_assets(0), f.current_assets(1)
    ppe0, ppe1 = f.net_ppe(0), f.net_ppe(1)
    ta0, ta1 = f.total_assets(0), f.total_assets(1)
    dep0, dep1 = f.depreciation(0), f.depreciation(1)
    sga0, sga1 = f.sga(0), f.sga(1)
    ltd0, ltd1 = f.ltd(0) or 0, f.ltd(1) or 0
    cl0, cl1 = f.current_liab(0) or 0, f.current_liab(1) or 0
    ni0, cfo0 = f.net_income(0), f.cfo(0)

    dsri = _safe_div(_safe_div(ar0, rev0), _safe_div(ar1, rev1))
    gmi = _safe_div(_safe_div(gp1, rev1), _safe_div(gp0, rev0))
    aqi = _safe_div(
        (1 - _safe_div((ca0 or 0) + (ppe0 or 0), ta0)) if ta0 else None,
        (1 - _safe_div((ca1 or 0) + (ppe1 or 0), ta1)) if ta1 else None,
    )
    sgi = _safe_div(rev0, rev1)
    depi = _safe_div(
        _safe_div(dep1, (dep1 or 0) + (ppe1 or 0)) if dep1 is not None else None,
        _safe_div(dep0, (dep0 or 0) + (ppe0 or 0)) if dep0 is not None else None,
    )
    sgai = _safe_div(_safe_div(sga0, rev0), _safe_div(sga1, rev1))
    lvgi = _safe_div(_safe_div(ltd0 + cl0, ta0), _safe_div(ltd1 + cl1, ta1))
    tata = _safe_div((ni0 - cfo0) if (ni0 is not None and cfo0 is not None) else None, ta0)

    comps = {"DSRI": dsri, "GMI": gmi, "AQI": aqi, "SGI": sgi,
             "DEPI": depi, "SGAI": sgai, "LVGI": lvgi, "TATA": tata}
    # neutral (=1) fill for index terms, 0 for accrual term, so a missing input doesn't nuke the score
    d = {k: (v if v is not None else (0.0 if k == "TATA" else 1.0)) for k, v in comps.items()}
    m = (-4.84 + 0.920 * d["DSRI"] + 0.528 * d["GMI"] + 0.404 * d["AQI"] + 0.892 * d["SGI"]
         + 0.115 * d["DEPI"] - 0.172 * d["SGAI"] + 4.679 * d["TATA"] - 0.327 * d["LVGI"])
    missing = [k for k, v in comps.items() if v is None]
    # Beneish M is known to false-positive on hyper-growth firms: high SGI/AQI/TATA
    # from a rapid revenue+receivable+inventory build mechanically push M up even with
    # no manipulation. Flag that caveat so the gate doesn't treat growth as fraud.
    growth_distorted = (sgi is not None and sgi > 1.40)
    return {
        "m_score": rnd(m, 2),
        "threshold": -1.78,
        "flag": m > -1.78,
        "growth_distorted": bool(growth_distorted),
        "read": ("M > -1.78, but SGI is high - likely a growth artifact, needs corroboration"
                 if (m > -1.78 and growth_distorted)
                 else "likely manipulator (M > -1.78)" if m > -1.78 else "no manipulation signal"),
        "components": {k: rnd(v, 3) for k, v in comps.items()},
        "missing_inputs": missing or None,
    }


# ------------------- Dechow-Ge-Larson-Sloan F-Score (Model 1) -------------------
def fscore(f):
    """DGLS misstatement F-Score. >1 = above-normal risk, >2.45 = substantial.
    RSST accruals approximated as (NI-CFO)/avgTA (documented simplification of the
    WC+NCO+FIN construction, which the three-statement pull cannot fully reproduce)."""
    ta0, ta1 = f.total_assets(0), f.total_assets(1)
    if not (ta0 and ta1):
        return {"error": "missing total assets for 2 years"}
    avg_ta = (ta0 + ta1) / 2
    ni0, cfo0 = f.net_income(0), f.cfo(0)
    ar0, ar1, ar2 = f.receivables(0), f.receivables(1), f.receivables(2)
    inv0, inv1 = f.inventory(0) or 0, f.inventory(1) or 0
    ppe0, cash0 = f.net_ppe(0) or 0, f.cash(0) or 0
    rev0, rev1 = f.revenue(0), f.revenue(1)
    ni1, ta_prev = f.net_income(1), f.total_assets(1)

    rsst = _safe_div((ni0 - cfo0) if (ni0 is not None and cfo0 is not None) else None, avg_ta)
    ch_rec = _safe_div((ar0 - ar1) if (ar0 is not None and ar1 is not None) else None, avg_ta)
    ch_inv = _safe_div(inv0 - inv1, avg_ta)
    soft = _safe_div(ta0 - ppe0 - cash0, ta0)
    # change in cash sales, % — cash sales = revenue - Δreceivables
    cs0 = (rev0 - (ar0 - ar1)) if (rev0 is not None and ar0 is not None and ar1 is not None) else None
    cs1 = (rev1 - (ar1 - ar2)) if (rev1 is not None and ar1 is not None and ar2 is not None) else None
    ch_cs = _safe_div((cs0 - cs1) if (cs0 is not None and cs1 is not None) else None, cs1)
    if ch_cs is None:  # fallback to plain sales growth when a 3rd year of AR is absent
        ch_cs = _safe_div((rev0 - rev1) if (rev0 is not None and rev1 is not None) else None, rev1)
    roa0 = _safe_div(ni0, ta0)
    roa1 = _safe_div(ni1, ta_prev)
    ch_roa = (roa0 - roa1) if (roa0 is not None and roa1 is not None) else None
    issued = 1.0 if ((f.C(["Issuance Of Debt", "Long Term Debt Issuance"], 0) or 0) > 0
                     or (f.C(["Net Common Stock Issuance"], 0) or 0) > 0) else 0.0

    terms = {"rsst": rsst, "ch_rec": ch_rec, "ch_inv": ch_inv, "soft_assets": soft,
             "ch_cash_sales": ch_cs, "ch_roa": ch_roa, "issuance": issued}
    d = {k: (v if v is not None else 0.0) for k, v in terms.items()}
    pred = (-7.893 + 0.790 * d["rsst"] + 2.518 * d["ch_rec"] + 1.191 * d["ch_inv"]
            + 1.979 * d["soft_assets"] + 0.171 * d["ch_cash_sales"] - 0.932 * d["ch_roa"]
            + 1.029 * d["issuance"])
    prob = math.exp(pred) / (1 + math.exp(pred))
    fsc = prob / 0.0037  # scale by unconditional misstatement probability
    level = "high (>2.45)" if fsc > 2.45 else "above normal (>1)" if fsc > 1 else "normal"
    return {
        "f_score": rnd(fsc, 2), "probability": rnd(prob, 4),
        "risk": level, "flag": fsc > 1, "gate_fail": fsc > 2.45,
        "components": {k: rnd(v, 4) for k, v in terms.items()},
        "note": "RSST accruals approximated as (NI-CFO)/avgTA",
    }


# ----------------------------- Piotroski F-Score -----------------------------
def piotroski(f):
    """9-point fundamental-strength score. >=7 strong, <=2 weak. Low score on a
    statistically cheap name is the value-trap tell."""
    ta0, ta1 = f.total_assets(0), f.total_assets(1)
    ni0, ni1 = f.net_income(0), f.net_income(1)
    cfo0 = f.cfo(0)
    ltd0, ltd1 = f.ltd(0), f.ltd(1)
    ca0, ca1 = f.current_assets(0), f.current_assets(1)
    cl0, cl1 = f.current_liab(0), f.current_liab(1)
    sh0, sh1 = f.shares(0), f.shares(1)
    gp0, gp1 = f.gross_profit(0), f.gross_profit(1)
    rev0, rev1 = f.revenue(0), f.revenue(1)

    roa0, roa1 = _safe_div(ni0, ta0), _safe_div(ni1, ta1)
    tests = {}
    tests["roa_positive"] = (roa0 is not None and roa0 > 0)
    tests["cfo_positive"] = (cfo0 is not None and cfo0 > 0)
    tests["roa_rising"] = (roa0 is not None and roa1 is not None and roa0 > roa1)
    tests["accrual_quality"] = (_safe_div(cfo0, ta0) is not None and roa0 is not None and _safe_div(cfo0, ta0) > roa0)
    tests["leverage_falling"] = (_safe_div(ltd0, ta0) is not None and _safe_div(ltd1, ta1) is not None
                                 and _safe_div(ltd0, ta0) < _safe_div(ltd1, ta1))
    tests["current_ratio_rising"] = (_safe_div(ca0, cl0) is not None and _safe_div(ca1, cl1) is not None
                                     and _safe_div(ca0, cl0) > _safe_div(ca1, cl1))
    tests["no_dilution"] = (sh0 is not None and sh1 is not None and sh0 <= sh1 * 1.001)
    tests["gross_margin_rising"] = (_safe_div(gp0, rev0) is not None and _safe_div(gp1, rev1) is not None
                                    and _safe_div(gp0, rev0) > _safe_div(gp1, rev1))
    tests["asset_turnover_rising"] = (_safe_div(rev0, ta0) is not None and _safe_div(rev1, ta1) is not None
                                      and _safe_div(rev0, ta0) > _safe_div(rev1, ta1))
    score = sum(1 for v in tests.values() if v)
    return {
        "piotroski_f": score, "max": 9,
        "read": "strong (>=7)" if score >= 7 else "weak (<=2)" if score <= 2 else "middling",
        "tests": {k: bool(v) for k, v in tests.items()},
    }


# ------------------------------- Sloan accruals -------------------------------
def accruals(f):
    """Sloan accrual ratio + cash-conversion. High accruals / low CFO-to-NI = low-quality
    earnings that tend to reverse (Sloan 1996)."""
    ta0, ta1 = f.total_assets(0), f.total_assets(1)
    ni0, cfo0, fcf0 = f.net_income(0), f.cfo(0), f.fcf(0)
    avg_ta = ((ta0 + ta1) / 2) if (ta0 and ta1) else ta0
    accrual = (ni0 - cfo0) if (ni0 is not None and cfo0 is not None) else None
    ratio = _safe_div(accrual, avg_ta)
    cfo_ni = _safe_div(cfo0, ni0)
    fcf_ni = _safe_div(fcf0, ni0)
    flags = []
    if ratio is not None and ratio > 0.10:
        flags.append("accrual ratio > 10% (earnings running ahead of cash)")
    if cfo_ni is not None and cfo_ni < 0.8:
        flags.append("CFO/NI < 0.8 (weak cash backing of earnings)")
    return {
        "sloan_accrual_ratio": rnd(ratio, 3),
        "cfo_to_ni": rnd(cfo_ni, 2),
        "fcf_to_ni": rnd(fcf_ni, 2),
        "flag": bool(flags), "flags": flags or None,
        "read": "low earnings quality" if flags else "earnings cash-backed",
    }


# ------------------------------- Montier C-Score -------------------------------
def cscore(f):
    """Montier 6-flag manipulation score (0 best, 6 worst)."""
    rev0, rev1 = f.revenue(0), f.revenue(1)
    ni0, ni1 = f.net_income(0), f.net_income(1)
    cfo0, cfo1 = f.cfo(0), f.cfo(1)
    ar0, ar1 = f.receivables(0), f.receivables(1)
    inv0, inv1 = f.inventory(0), f.inventory(1)
    cogs0, cogs1 = f.cogs(0), f.cogs(1)
    ca0, ca1 = f.current_assets(0), f.current_assets(1)
    cash0, cash1 = f.cash(0), f.cash(1)
    dep0, dep1 = f.depreciation(0), f.depreciation(1)
    gppe0, gppe1 = f.gross_ppe(0), f.gross_ppe(1)
    ta0, ta1 = f.total_assets(0), f.total_assets(1)

    flags = {}
    flags["ni_cfo_divergence"] = (ni0 is not None and ni1 is not None and cfo0 is not None and cfo1 is not None
                                  and ni0 > ni1 and cfo0 < cfo1)
    flags["dso_rising"] = (_safe_div(ar0, rev0) is not None and _safe_div(ar1, rev1) is not None
                           and _safe_div(ar0, rev0) > _safe_div(ar1, rev1))
    flags["dsi_rising"] = (_safe_div(inv0, cogs0) is not None and _safe_div(inv1, cogs1) is not None
                           and _safe_div(inv0, cogs0) > _safe_div(inv1, cogs1))
    oca0 = (ca0 - (ar0 or 0) - (inv0 or 0) - (cash0 or 0)) if ca0 is not None else None
    oca1 = (ca1 - (ar1 or 0) - (inv1 or 0) - (cash1 or 0)) if ca1 is not None else None
    flags["other_ca_rising"] = (_safe_div(oca0, rev0) is not None and _safe_div(oca1, rev1) is not None
                                and _safe_div(oca0, rev0) > _safe_div(oca1, rev1))
    flags["depreciation_slowing"] = (_safe_div(dep0, gppe0) is not None and _safe_div(dep1, gppe1) is not None
                                     and _safe_div(dep0, gppe0) < _safe_div(dep1, gppe1))
    flags["asset_growth_high"] = (_safe_div(ta0, ta1) is not None and (_safe_div(ta0, ta1) - 1) > 0.20)
    score = sum(1 for v in flags.values() if v)
    return {
        "c_score": score, "max": 6,
        "read": "high manipulation risk (>=4)" if score >= 4 else "elevated (2-3)" if score >= 2 else "clean",
        "flags": {k: bool(v) for k, v in flags.items()},
    }


# --------------------------------- gate / all ---------------------------------
def all_scores(f):
    m = mscore(f); dg = fscore(f); pi = piotroski(f); ac = accruals(f); cs = cscore(f)
    reasons_fail, reasons_flag = [], []
    cfo_ni = ac.get("cfo_to_ni")
    weak_cash = (cfo_ni is not None and cfo_ni < 0.8)
    roa_falling = (pi.get("tests", {}).get("roa_rising") is False)

    # Beneish M triggers a FAIL only when corroborated by cash/deterioration evidence, so a
    # growth-driven M false-positive (healthy cash conversion) becomes a FLAG, not a block.
    if m.get("flag"):
        corroborated = weak_cash and roa_falling
        if corroborated and not m.get("growth_distorted"):
            reasons_fail.append(f"Beneish M={m['m_score']} > -1.78, corroborated by weak cash conversion + falling ROA")
        elif corroborated and m.get("growth_distorted"):
            reasons_fail.append(f"Beneish M={m['m_score']} > -1.78 with weak cash + falling ROA despite high growth")
        else:
            note = " (likely growth artifact - cash conversion intact)" if m.get("growth_distorted") else ""
            reasons_flag.append(f"Beneish M={m['m_score']} > -1.78{note}")
    if dg.get("gate_fail"):
        reasons_fail.append(f"DGLS F={dg.get('f_score')} > 2.45 (high misstatement risk)")
    if cfo_ni is not None and cfo_ni < 0.5:
        reasons_fail.append(f"CFO/NI={cfo_ni} < 0.5 (earnings not cash-backed)")

    if pi.get("piotroski_f") is not None and pi["piotroski_f"] <= 3:
        reasons_flag.append(f"Piotroski={pi['piotroski_f']}/9 (weak fundamentals)")
    if cs.get("c_score", 0) >= 4:
        reasons_flag.append(f"Montier C={cs['c_score']}/6 (multiple manipulation flags)")
    if dg.get("flag") and not dg.get("gate_fail"):
        reasons_flag.append(f"DGLS F={dg.get('f_score')} > 1 (above-normal misstatement risk)")
    if ac.get("flag"):
        reasons_flag.append("Sloan accruals / cash-conversion weak")

    verdict = "FAIL" if reasons_fail else ("FLAG" if reasons_flag else "PASS")
    action = {
        "FAIL": "Do NOT stamp 'cheap'. Normalize earnings and re-run the DCF, or return AVOID citing the forensic gate.",
        "FLAG": "Proceed with caution: haircut the FCF base / widen the margin of safety and name the flag in the thesis.",
        "PASS": "Earnings-quality gate clear; the DCF base can be trusted subject to normal assumptions.",
    }[verdict]
    return {
        "ticker": f.ticker,
        "gate": {"verdict": verdict, "action": action,
                 "fail_reasons": reasons_fail or None, "flag_reasons": reasons_flag or None},
        "beneish_m": m, "dgls_f": dg, "piotroski": pi, "accruals": ac, "montier_c": cs,
        "years_available": f.n_years,
    }


_CMDS = {"mscore": mscore, "fscore": fscore, "piotroski": piotroski,
         "accruals": accruals, "cscore": cscore, "all": all_scores}

def main():
    p = argparse.ArgumentParser(description="Forensic earnings-quality screens")
    p.add_argument("cmd", choices=list(_CMDS))
    p.add_argument("ticker")
    a = p.parse_args()
    try:
        f = Filings(a.ticker)
    except Exception as e:
        jprint({"error": f"could not load statements for {a.ticker}: {e}"}); sys.exit(1)
    if f.n_years < 2:
        jprint({"error": f"need >=2 years of statements; got {f.n_years}"}); sys.exit(1)
    out = _CMDS[a.cmd](f)
    if a.cmd != "all":
        out = {"ticker": a.ticker, a.cmd: out}
    jprint(out)


if __name__ == "__main__":
    main_guard(main)
