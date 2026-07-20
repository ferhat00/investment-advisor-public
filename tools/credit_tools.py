"""Credit metrics: leverage, coverage, Altman Z, Ohlson O, synthetic rating, liquidity.
CLI: python credit_tools.py NVDA

Altman Z and Ohlson O are the distress *models* the committee's credit view owns; the fundamental
analyst consumes the resulting distress read and the synthetic-rating cost of debt as a WACC input
(rather than reimplementing them inside valuation.py).
"""
import sys, math
import yfinance as yf
from common import jprint, rnd, stmt_val, main_guard


def _row(df, names):
    for n in names:
        if n in df.index:
            v = df.loc[n].iloc[0]
            return None if v != v else float(v)
    return None


# Damodaran synthetic-rating table (large non-financial): interest coverage -> rating -> default spread
_RATING_TABLE = [
    (8.50, "Aaa/AAA", 0.0069), (6.50, "Aa2/AA", 0.0085), (5.50, "A1/A+", 0.0107),
    (4.25, "A2/A", 0.0118), (3.00, "A3/A-", 0.0133), (2.50, "Baa2/BBB", 0.0171),
    (2.25, "Ba1/BB+", 0.0215), (2.00, "Ba2/BB", 0.0261), (1.75, "B1/B+", 0.0329),
    (1.50, "B2/B", 0.0424), (1.25, "B3/B-", 0.0516), (0.80, "Caa/CCC", 0.0820),
    (0.65, "Ca2/CC", 0.0864), (0.20, "C2/C", 0.1134), (-1e9, "D2/D", 0.1512),
]


def synthetic_rating(coverage, risk_free=None, tax=0.21):
    """Damodaran synthetic rating from interest coverage -> default spread -> cost of debt."""
    if coverage is None:
        return None
    rating, spread = next((r, s) for thr, r, s in _RATING_TABLE if coverage >= thr)
    if risk_free is None:
        try:
            from common import fred
            risk_free = float(fred("DGS10").iloc[-1]) / 100
        except Exception:
            risk_free = 0.04
    pretax_kd = risk_free + spread
    return {
        "interest_coverage": rnd(coverage, 1),
        "synthetic_rating": rating,
        "default_spread_pct": rnd(100 * spread, 2),
        "risk_free_pct": rnd(100 * risk_free, 2),
        "pretax_cost_of_debt_pct": rnd(100 * pretax_kd, 2),
        "after_tax_cost_of_debt_pct": rnd(100 * pretax_kd * (1 - tax), 2),
        "note": "feed after_tax_cost_of_debt into the WACC instead of a stale book rate",
    }


def ohlson_o(ta, tl, wc, cl, ca, ni, ni_prev, cfo):
    """Ohlson (1980) O-Score -> probability of financial distress. Independent of Altman Z.
    Size term uses ln(total assets in $mm) as a scale proxy for the original GNP-deflated term."""
    if not (ta and tl is not None and ni is not None):
        return None
    x_negbook = 1.0 if tl > ta else 0.0
    y_recentloss = 1.0 if (ni < 0 and (ni_prev is not None and ni_prev < 0)) else 0.0
    chin = ((ni - ni_prev) / (abs(ni) + abs(ni_prev))) if (ni_prev is not None and (abs(ni) + abs(ni_prev)) != 0) else 0.0
    o = (-1.32
         - 0.407 * math.log(ta / 1e6)
         + 6.03 * (tl / ta)
         - 1.43 * ((wc / ta) if wc is not None else 0.0)
         + 0.0757 * ((cl / ca) if (cl is not None and ca) else 0.0)
         - 1.72 * x_negbook
         - 2.37 * (ni / ta)
         - 1.83 * ((cfo / tl) if (cfo is not None and tl) else 0.0)
         + 0.285 * y_recentloss
         - 0.521 * chin)
    prob = math.exp(o) / (1 + math.exp(o))
    return {
        "o_score": rnd(o, 2),
        "distress_probability": rnd(prob, 3),
        "zone": "high distress risk (P>0.5)" if prob > 0.5 else "elevated (P>0.038)" if prob > 0.038 else "low",
        "flag": prob > 0.5,
        "note": "size term approximated as ln(total assets $mm); >0.038 is Ohlson's misclassification-min cutoff",
    }


def credit_profile(ticker):
    t = yf.Ticker(ticker)
    i = t.info or {}
    inc, bal, cf = t.income_stmt, t.balance_sheet, t.cashflow
    ebit = _row(inc, ["EBIT", "Operating Income"])
    interest = _row(inc, ["Interest Expense"])
    revenue = _row(inc, ["Total Revenue"])
    ta = _row(bal, ["Total Assets"])
    tl = _row(bal, ["Total Liabilities Net Minority Interest"])
    wc = _row(bal, ["Working Capital"])
    re = _row(bal, ["Retained Earnings"])
    ca = _row(bal, ["Current Assets"])
    cl = _row(bal, ["Current Liabilities"])
    ni = _row(inc, ["Net Income", "Net Income Common Stockholders"])
    ni_prev = stmt_val(inc, ["Net Income", "Net Income Common Stockholders"], 1)
    cfo = _row(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"])
    debt = i.get("totalDebt") or _row(bal, ["Total Debt"]) or 0
    cash = i.get("totalCash") or _row(bal, ["Cash And Cash Equivalents"]) or 0
    ebitda = i.get("ebitda")
    mve = i.get("marketCap")
    out = {
        "ticker": ticker,
        "total_debt": debt, "cash": cash, "net_debt": debt - cash,
        "ebitda_ttm": ebitda,
        "net_debt_ebitda": rnd((debt - cash) / ebitda, 2) if ebitda else None,
        "gross_debt_ebitda": rnd(debt / ebitda, 2) if ebitda else None,
        "interest_coverage_ebit": rnd(ebit / abs(interest), 1) if (ebit and interest) else None,
        "debt_to_equity_pct": rnd(i.get("debtToEquity")),
        "current_ratio": rnd(i.get("currentRatio")),
        "quick_ratio": rnd(i.get("quickRatio")),
        "fcf_ttm": i.get("freeCashflow"),
        "fcf_to_debt_pct": rnd(100 * (i.get("freeCashflow") or 0) / debt) if debt else None,
    }
    if all(v is not None for v in [wc, re, ebit, ta, tl, mve, revenue]):
        z = (1.2 * wc / ta + 1.4 * re / ta + 3.3 * ebit / ta + 0.6 * mve / tl + 1.0 * revenue / ta)
        out["altman_z"] = rnd(z)
        out["altman_zone"] = "safe (>2.99)" if z > 2.99 else "grey (1.81-2.99)" if z > 1.81 else "distress (<1.81)"
    else:
        out["altman_z"] = None

    out["ohlson_o"] = ohlson_o(ta, tl, wc, cl, ca, ni, ni_prev, cfo)
    tax = i.get("effectiveTaxRate") if isinstance(i.get("effectiveTaxRate"), (int, float)) else 0.21
    out["synthetic_rating"] = synthetic_rating(out["interest_coverage_ebit"], tax=tax if 0 <= tax <= 0.5 else 0.21)

    ndeb = out["net_debt_ebitda"]; cov = out["interest_coverage_ebit"]
    if ndeb is not None:
        grade = ("net cash / minimal" if ndeb <= 0 else "IG-like (<2x)" if ndeb < 2
                 else "crossover (2-4x)" if ndeb < 4 else "HY-like (>4x)")
        out["leverage_read"] = grade
    if cov is not None:
        out["coverage_read"] = "strong (>8x)" if cov > 8 else "adequate (3-8x)" if cov > 3 else "weak (<3x)"
    return out


def main():
    jprint(credit_profile(sys.argv[1]))


if __name__ == "__main__":
    main_guard(main)
