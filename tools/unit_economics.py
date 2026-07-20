"""Unit economics — bottom-up cross-check on the top-down DCF, where the business model warrants it.

Rule-of-40 and the S&M efficiency ("magic number") proxy are computable straight from the three
statements. True LTV/CAC, CLV, and customer-based corporate valuation (CBCV) need KPI disclosures
(customer counts, churn, ARPU) that the statements don't carry — those accept manual inputs and
otherwise return a clearly-labelled stub rather than a fake number.

CLI:
  python unit_economics.py rule40 NVDA
  python unit_economics.py magic  CRM                     # S&M efficiency proxy from SG&A
  python unit_economics.py ltv_cac X --arpu 1200 --gross-margin 0.80 --churn 0.10 --cac 2500
  python unit_economics.py clv    X --arpu 1200 --gross-margin 0.80 --retention 0.90 --wacc 0.10
  python unit_economics.py cbcv   X                       # methodology + required inputs (no flags)
  python unit_economics.py cbcv   X --acq 2e6 --retention 0.9 --arpu 150 --gross-margin 0.75 --cac 120
  python unit_economics.py cbcv   X --acq 2e6 --sbg-alpha 1.2 --sbg-beta 3 --arpu 150 --gross-margin 0.75

References: McKinsey Rule of 40; SaaS magic number (Bessemer); Gupta-Lehmann-Stuart (2004, JMR)
CLV; McCarthy-Fader-Hardie (2017, J. Marketing) & McCarthy-Fader (2018, JMR) CBCV; Fader-Hardie sBG retention.
"""
import argparse
from common import jprint, rnd, yf_statements, stmt_val, main_guard
import yfinance as yf


def _rev_growth_and_margins(ticker):
    inc, _bal, cf = yf_statements(ticker)
    info = yf.Ticker(ticker).info or {}
    rev0 = stmt_val(inc, ["Total Revenue", "Operating Revenue"], 0)
    rev1 = stmt_val(inc, ["Total Revenue", "Operating Revenue"], 1)
    fcf0 = stmt_val(cf, ["Free Cash Flow"], 0)
    ebitda = info.get("ebitda") or stmt_val(inc, ["EBITDA", "Normalized EBITDA"], 0)
    op_inc = stmt_val(inc, ["Operating Income", "EBIT"], 0)
    growth = (rev0 / rev1 - 1) if (rev0 and rev1) else None
    return {
        "revenue": rev0, "revenue_growth": growth,
        "fcf_margin": (fcf0 / rev0) if (fcf0 is not None and rev0) else None,
        "ebitda_margin": (ebitda / rev0) if (ebitda is not None and rev0) else None,
        "op_margin": (op_inc / rev0) if (op_inc is not None and rev0) else None,
    }


def rule40(ticker):
    """Rule of 40: revenue growth% + profitability margin% >= 40. The canonical health check for
    scaling/subscription businesses — trades growth against margin on one axis."""
    m = _rev_growth_and_margins(ticker)
    if m["revenue_growth"] is None:
        return {"ticker": ticker, "error": "insufficient revenue history"}
    g = 100 * m["revenue_growth"]
    out = {"ticker": ticker, "revenue_growth_pct": rnd(g, 1)}
    for basis, key in [("fcf", "fcf_margin"), ("ebitda", "ebitda_margin"), ("operating", "op_margin")]:
        if m[key] is not None:
            score = g + 100 * m[key]
            out[f"rule40_{basis}"] = {"margin_pct": rnd(100 * m[key], 1), "score": rnd(score, 1),
                                      "pass": score >= 40}
    primary = out.get("rule40_fcf") or out.get("rule40_ebitda") or out.get("rule40_operating")
    if primary:
        out["verdict"] = "PASS (>=40)" if primary["pass"] else "FAIL (<40)"
        out["note"] = "growth+margin balance; a subscription/SaaS name below 40 is over-spending for its growth"
    return out


def magic_number(ticker):
    """SaaS 'magic number' proxy: incremental revenue / prior-year S&M. yfinance only reports
    combined SG&A, so this is a proxy (true magic number uses S&M alone and new ARR)."""
    inc, _bal, _cf = yf_statements(ticker)
    rev0 = stmt_val(inc, ["Total Revenue"], 0)
    rev1 = stmt_val(inc, ["Total Revenue"], 1)
    sga1 = stmt_val(inc, ["Selling General And Administration"], 1)
    if not (rev0 and rev1 and sga1):
        return {"ticker": ticker, "error": "need revenue (2y) and prior SG&A"}
    mn = (rev0 - rev1) / sga1
    return {
        "ticker": ticker, "delta_revenue": rnd(rev0 - rev1, 0), "prior_sga": rnd(sga1, 0),
        "magic_number_proxy": rnd(mn, 2),
        "read": "efficient (>0.75)" if mn > 0.75 else "moderate (0.5-0.75)" if mn > 0.5 else "inefficient (<0.5)",
        "note": "proxy: uses combined SG&A, not S&M alone; directional only",
    }


def ltv_cac(ticker, arpu, gross_margin, churn, cac):
    if None in (arpu, gross_margin, churn, cac) or churn <= 0 or cac <= 0:
        return {"ticker": ticker, "status": "needs_inputs",
                "required": "--arpu (annual revenue/customer), --gross-margin (0-1), --churn (annual, 0-1), --cac",
                "note": "these are KPI disclosures, not in the three statements"}
    ltv = (arpu * gross_margin) / churn                 # gross-margin LTV, geometric-retention
    ratio = ltv / cac
    payback_months = cac / (arpu * gross_margin / 12)
    return {
        "ticker": ticker, "ltv": rnd(ltv, 0), "cac": rnd(cac, 0), "ltv_cac": rnd(ratio, 1),
        "payback_months": rnd(payback_months, 1),
        "verdict": "healthy (LTV/CAC>=3 & payback<=18m)" if (ratio >= 3 and payback_months <= 18)
                   else "marginal" if ratio >= 1 else "unprofitable acquisition",
        "note": "LTV = ARPU*gross_margin/churn (perpetual geometric retention)",
    }


def clv(ticker, arpu, gross_margin, retention, wacc):
    """Discounted customer lifetime value (Gupta-Lehmann-Stuart closed form):
    CLV = margin * r / (1 + d - r), with r = retention, d = discount rate."""
    if None in (arpu, gross_margin, retention, wacc):
        return {"ticker": ticker, "status": "needs_inputs",
                "required": "--arpu, --gross-margin, --retention (annual, 0-1), --wacc"}
    margin = arpu * gross_margin
    clv_val = margin * retention / (1 + wacc - retention)
    return {"ticker": ticker, "annual_gross_profit_per_customer": rnd(margin, 0),
            "retention": retention, "discount_rate": wacc, "clv": rnd(clv_val, 0),
            "note": "Gupta-Lehmann-Stuart: CLV = margin*r/(1+d-r); multiply by customer count for a "
                    "customer-based equity value, then compare to market cap"}


def _sbg_survival(k, alpha, beta):
    """Shifted-Beta-Geometric survivor S(k) = P(still active after k renewals), Fader-Hardie (2007).
    S(0)=1; S(k) = Π_{i=1..k} (beta+i-1)/(alpha+beta+i-1). Cohort-level retention RISES with tenure
    (heterogeneity: the loyal survive), which geometric-churn models miss."""
    s = 1.0
    for i in range(1, k + 1):
        s *= (beta + i - 1) / (alpha + beta + i - 1)
    return s


def _current_ev(ticker):
    i = yf.Ticker(ticker).info or {}
    mc = i.get("marketCap")
    if not mc:
        return None, {}
    ev = mc + (i.get("totalDebt") or 0) - (i.get("totalCash") or 0)
    return ev, {"market_cap": mc, "total_debt": i.get("totalDebt") or 0, "total_cash": i.get("totalCash") or 0}


def cbcv(ticker, acq=None, acq_growth=0.0, retention=None, arpu=None, gross_margin=None,
         cac=None, wacc=0.10, horizon=10, customers=None, sbg_alpha=None, sbg_beta=None):
    """Customer-based corporate valuation (McCarthy-Fader-Hardie 2017; McCarthy-Fader 2018): value the
    firm bottom-up as the discounted contribution of its existing + future customer cohorts, instead of
    from aggregate revenue. Contractual/subscription version — retention as constant annual rate, or an
    sBG survivor curve (pass --sbg-alpha/--sbg-beta) so cohort retention rises with tenure.

    Customer KPIs aren't in the three statements, so acquisitions/retention/ARPU are manual inputs
    (from investor decks / 10-K KPI tables / transcripts). Output is an implied enterprise value to
    cross-check against the aggregate-revenue DCF and the current EV — a durable-growth vs. borrowed-
    growth tell: a rich EV that the cohort economics can't rebuild is priced on optimism, not customers."""
    have_retention = retention is not None or (sbg_alpha is not None and sbg_beta is not None)
    if arpu is None or gross_margin is None or not have_retention or (acq is None and customers is None):
        return {
            "ticker": ticker, "status": "needs_kpi_disclosures",
            "method": "CBCV (McCarthy-Fader-Hardie 2017): acquisition model x retention (sBG) x spend per "
                      "active customer -> cohort contribution cash flows -> DCF -> implied EV",
            "required_inputs": {
                "--acq": "customers acquired per year (or --customers for the current active base)",
                "--retention": "annual retention rate 0-1  (OR --sbg-alpha/--sbg-beta for a tenure-rising curve)",
                "--arpu": "annual revenue per active customer", "--gross-margin": "0-1",
                "--cac": "acquisition cost per customer (optional)", "--acq-growth": "annual growth in acquisitions (optional)",
                "--wacc": "discount rate", "--horizon": "explicit years (default 10)",
            },
            "how_to_use": "pull the KPIs from disclosures, then compare the CBCV implied EV to the DCF and "
                          "current EV. `clv` is a first-order per-customer approximation.",
        }
    surv = ((lambda k: _sbg_survival(k, sbg_alpha, sbg_beta)) if (sbg_alpha and sbg_beta)
            else (lambda k: retention ** k))
    retention_model = (f"sBG(alpha={sbg_alpha}, beta={sbg_beta})" if (sbg_alpha and sbg_beta)
                       else f"constant annual retention {retention}")
    # expected customer lifetime L = Σ_{k>=0} S(k) (periods); LTV per customer = ARPU*margin*L − CAC
    L = sum(surv(k) for k in range(200))
    unit_margin = arpu * gross_margin
    ltv_gross = unit_margin * L
    ltv_net = ltv_gross - (cac or 0)

    base = acq if acq is not None else customers
    acqs = [base * (1 + acq_growth) ** p for p in range(horizon)]   # acquired at start of periods 1..H
    c0 = customers if (customers is not None and acq is not None) else 0   # existing base as a tenure-0 cohort
    pv_explicit, rows = 0.0, []
    for p in range(1, horizon + 1):
        active = c0 * surv(p) + sum(acqs[q - 1] * surv(p - q) for q in range(1, p + 1))
        contribution = unit_margin * active
        acq_spend = (cac or 0) * acqs[p - 1]
        cf = contribution - acq_spend
        pv_explicit += cf / (1 + wacc) ** p
        rows.append({"yr": p, "active_customers": rnd(active, 0), "net_cash_flow": rnd(cf, 0)})
    # steady-state terminal: perpetuity on the final-year acquisition level's net contribution
    a_last = acqs[-1]
    steady_net = a_last * ltv_net                     # each period's cohort is worth its net LTV in steady state
    tv = (steady_net / wacc) / (1 + wacc) ** horizon if wacc > 0 else 0.0
    cbcv_ev = pv_explicit + tv
    cur_ev, ev_parts = _current_ev(ticker)
    out = {
        "ticker": ticker, "retention_model": retention_model,
        "expected_customer_lifetime_yrs": rnd(L, 1),
        "ltv_per_customer_gross": rnd(ltv_gross, 0), "ltv_per_customer_net_of_cac": rnd(ltv_net, 0),
        "ltv_cac": rnd(ltv_gross / cac, 1) if cac else None,
        "pv_explicit_horizon": rnd(pv_explicit, 0), "pv_terminal": rnd(tv, 0),
        "cbcv_implied_ev": rnd(cbcv_ev, 0), "current_ev": rnd(cur_ev, 0) if cur_ev else None,
        "cbcv_ev_to_current_ev": rnd(cbcv_ev / cur_ev, 2) if cur_ev else None,
        "assumptions": {"acq_per_yr": base, "acq_growth": acq_growth, "arpu": arpu,
                        "gross_margin": gross_margin, "cac": cac, "wacc": wacc, "horizon": horizon,
                        "existing_customer_base": customers},
        "schedule": rows,
        "read": ("customer economics support the EV (CBCV/EV >= 0.9)" if (cur_ev and cbcv_ev / cur_ev >= 0.9)
                 else "EV runs ahead of the customer economics (CBCV/EV < 0.9) — growth may be borrowed/priced-in"
                 if cur_ev else "no current EV from yfinance to compare"),
        "note": "contribution-margin CBCV (contractual/subscription); nets CAC but not corporate overhead/taxes, "
                "so treat as a cohort cross-check on the DCF, not a full equity value. sBG retention rises with "
                "tenure (Fader-Hardie 2007).",
    }
    if ev_parts:
        out["current_ev_parts"] = ev_parts
    return out


def main():
    p = argparse.ArgumentParser(description="Unit economics")
    p.add_argument("cmd", choices=["rule40", "magic", "ltv_cac", "clv", "cbcv"])
    p.add_argument("ticker")
    p.add_argument("--arpu", type=float, default=None)
    p.add_argument("--gross-margin", dest="gross_margin", type=float, default=None)
    p.add_argument("--churn", type=float, default=None)
    p.add_argument("--retention", type=float, default=None)
    p.add_argument("--cac", type=float, default=None)
    p.add_argument("--wacc", type=float, default=0.10)
    p.add_argument("--acq", type=float, default=None, help="cbcv: customers acquired per year")
    p.add_argument("--acq-growth", dest="acq_growth", type=float, default=0.0, help="cbcv: annual acquisition growth")
    p.add_argument("--customers", type=float, default=None, help="cbcv: current active customer base")
    p.add_argument("--horizon", type=int, default=10, help="cbcv: explicit projection years")
    p.add_argument("--sbg-alpha", dest="sbg_alpha", type=float, default=None, help="cbcv: sBG retention alpha")
    p.add_argument("--sbg-beta", dest="sbg_beta", type=float, default=None, help="cbcv: sBG retention beta")
    a = p.parse_args()
    if a.cmd == "rule40":
        jprint(rule40(a.ticker))
    elif a.cmd == "magic":
        jprint(magic_number(a.ticker))
    elif a.cmd == "ltv_cac":
        jprint(ltv_cac(a.ticker, a.arpu, a.gross_margin, a.churn, a.cac))
    elif a.cmd == "clv":
        jprint(clv(a.ticker, a.arpu, a.gross_margin, a.retention, a.wacc))
    elif a.cmd == "cbcv":
        jprint(cbcv(a.ticker, a.acq, a.acq_growth, a.retention, a.arpu, a.gross_margin, a.cac,
                    a.wacc, a.horizon, a.customers, a.sbg_alpha, a.sbg_beta))


if __name__ == "__main__":
    main_guard(main)
