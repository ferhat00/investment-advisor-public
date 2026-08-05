"""Valuation engine for the fundamental analyst: DCF, reverse/expectations solve, comps,
residual income (RIM), Monte-Carlo, and implied cost of capital (ICC).

CLI:
  python valuation.py dcf NVDA --growth5 0.25 --fade 0.10 --tg 0.03 --wacc 0.10
                              [--fade-model ff|const] [--moat none|narrow|wide] [--normalize]
  python valuation.py reverse NVDA --wacc 0.10 --tg 0.03 [--solve growth|margin|cap]
  python valuation.py rim NVDA --coe 0.10 [--roe-fade-to 0.12] [--years 10]
  python valuation.py mc  NVDA --draws 10000 --growth5 0.20 --wacc 0.10
  python valuation.py scenario NVDA --bull 0.30 --base 0.18 --bear 0.05 --probs 0.25,0.50,0.25
  python valuation.py icc NVDA [--method hvz|gls|ct|ojn|peg]
  python valuation.py comps NVDA AMD,AVGO,INTC [--warranted]
  python valuation.py epv NVDA --wacc 0.09          # Greenwald-Kahn EPV + franchise value (growth-free floor)
  python valuation.py netnet NVDA                   # Graham NCAV / net-net / liquidation floor

Design notes:
- `dcf_value()` is a pure FCFF function: it discounts the flow it is given at WACC and then
  subtracts net debt. It is unchanged and still reproduces the original two-stage-with-fade
  arithmetic for any given input.
- CASH-FLOW BASIS FIX (2026-07-28): `_base_inputs()` used to hand `dcf_value()` yfinance's
  `freeCashflow`, which is LEVERED (OCF is struck after the `Interest Paid Cfo` line). Being
  discounted at WACC and then netted against debt, the debt was charged twice and every fair
  value came out biased LOW. `_base_inputs()` now returns FCFF (= levered FCF + after-tax
  interest, see `_unlever_fcf`) and reports the reconciliation as `fcff_reconciliation` in the
  `dcf` and `reverse` output. This moves `dcf`/`reverse`/`mc`/`scenario`; `epv`/`netnet` never
  used the FCF value. **DCF/reverse figures in theses/ runs dated before 2026-07-28 were
  produced by the broken basis and are not comparable to later ones.**
- `--fade-model ff` replaces the hand-picked fade with a Fama-French ~38%/yr reversion of
  growth toward terminal over a moat-scaled competitive-advantage period (Morningstar mapping).
- Every intrinsic number is meant as a range: `mc` reports P10/median/P90 and P(value>price);
  `rim` is a book-anchored second model least correlated with the FCFF DCF.
"""
import argparse
import sys

import numpy as np
import yfinance as yf
from common import jprint, rnd, yf_statements, stmt_val, main_guard, parse_list, quote_price

FF_REVERSION = 0.38          # Fama-French (2000) ~38%/yr profitability mean-reversion (no-moat baseline)
FF_HORIZON = 18              # explicit fade horizon before perpetuity (long enough for wide moats to converge)
# Moat governs PERSISTENCE (reversion speed), not just tail length. Rates are calibrated so the
# excess growth decays to ~terminal over the Morningstar fade horizon: none ~4-5y, narrow ~12y, wide ~20y.
MOAT_REVERSION = {"none": 0.44, "narrow": 0.18, "wide": 0.11}


def _unlever_fcf(fcf_levered, inc, cf):
    """yfinance `freeCashflow` = Operating Cash Flow - Capex, and OCF is struck AFTER cash
    interest (the `Interest Paid Cfo` line). It is therefore a LEVERED flow. Discounting it at
    WACC and then subtracting net debt charges the debt twice — the defect this reverses.

    FCFF = levered FCF + after-tax NET interest (interest paid - interest received). The
    add-back must be NET, not gross, because the bridge subtracts `net_debt = total debt -
    total CASH`: crediting the whole cash balance while leaving the interest that cash earns
    inside the flow would count the cash twice. For a net-cash issuer the term is therefore
    small — and can be negative, REDUCING the flow. Getting this wrong flatters exactly the
    firms the raw defect already flatters.

    We prefer the cash lines (`Interest Paid Cfo` / `Interest Received Cfo`) over the accrual
    income-statement lines, so the adjustment reverses exactly what OCF deducted.
    Returns (fcff, reconciliation)."""
    if fcf_levered is None:
        return None, {"applied": False, "reason": "no levered FCF from yfinance"}
    paid, received = stmt_val(cf, ["Interest Paid Cfo"]), stmt_val(cf, ["Interest Received Cfo"])
    basis = "Interest Paid/Received Cfo (cash)"
    if paid is None:
        paid = stmt_val(inc, ["Interest Expense", "Interest Expense Non Operating"])
        received = stmt_val(inc, ["Interest Income", "Interest Income Non Operating"])
        basis = "Interest Expense/Income (accrual — cash interest lines unavailable)"
    if paid is None:
        return fcf_levered, {
            "applied": False, "fcf_levered": rnd(fcf_levered, 0), "fcff": rnd(fcf_levered, 0),
            "note": "No interest line found — FCFF could NOT be derived, so the LEVERED flow is "
                    "discounted at WACC against net debt. This DOUBLE-COUNTS the debt and biases "
                    "fair value LOW. Treat the DCF as a lower bound.",
        }
    tax = _eff_tax(inc)
    net_interest = abs(paid) - abs(received or 0)
    adj = net_interest * (1 - tax)
    return fcf_levered + adj, {
        "applied": True,
        "fcf_levered": rnd(fcf_levered, 0),
        "interest_basis": basis,
        "interest_paid": rnd(abs(paid), 0),
        "interest_received": rnd(abs(received or 0), 0),
        "net_interest_pretax": rnd(net_interest, 0),
        "eff_tax_rate": rnd(tax, 3),
        "interest_addback_after_tax": rnd(adj, 0),
        "fcff": rnd(fcf_levered + adj, 0),
        "note": "FCFF = levered FCF + after-tax NET interest (paid less received), so the WACC "
                "discount and the net-debt bridge each charge debt — and credit cash — exactly "
                "once. A negative add-back is correct for a net-cash issuer.",
    }


def _base_inputs(ticker, price_scale=1.0):
    """Returns (FCFF, shares, net_debt, price, info, fcff_reconciliation).

    The first element is UNLEVERED (FCFF) — see `_unlever_fcf`. Callers discount it at WACC and
    subtract net debt, which is only consistent for a firm-level flow."""
    i = yf.Ticker(ticker).info or {}
    fcf = i.get("freeCashflow")
    shares = i.get("sharesOutstanding")
    net_debt = (i.get("totalDebt") or 0) - (i.get("totalCash") or 0)
    # GBp-quoted LSE names would otherwise be 100x out vs book. price_scale is the SEPARATE
    # cross-CURRENCY factor for names that quote and report in different currencies (SHEL.L
    # and HSBA.L quote in pence but report in USD). Until 2026-07-30 this call ignored it, so
    # --price-scale reached rim/icc only and every DCF-family command silently compared a
    # sterling price against dollar cash flows. On SHEL.L that reported epv_covers_price true
    # at +15.9% when the corrected figure is 0.86x, -13.9% -- a sign flip on the verdict.
    price = quote_price(i, price_scale)
    try:
        inc, _bal, cf = yf_statements(ticker)
        fcff, recon = _unlever_fcf(fcf, inc, cf)
    except Exception as e:
        fcff, recon = fcf, {"applied": False, "error": str(e),
                            "note": "Statement fetch failed — levered FCF used unadjusted; the "
                                    "DCF double-counts debt and is a LOWER BOUND."}
    return fcff, shares, net_debt, price, i, recon


# --------------------------- statement-derived helpers ---------------------------
def _eff_tax(inc):
    tr = stmt_val(inc, ["Tax Rate For Calcs"])
    if tr is not None and 0 <= tr <= 0.6:
        return tr
    tp, pt = stmt_val(inc, ["Tax Provision"]), stmt_val(inc, ["Pretax Income"])
    if tp is not None and pt:
        return min(max(tp / pt, 0.0), 0.35)
    return 0.21


def _nopat(inc):
    ebit = stmt_val(inc, ["EBIT", "Operating Income"])
    return None if ebit is None else ebit * (1 - _eff_tax(inc))


def _invested_capital(bal):
    ic = stmt_val(bal, ["Invested Capital"])
    if ic:
        return ic
    debt = stmt_val(bal, ["Total Debt"]) or 0
    eq = stmt_val(bal, ["Common Stock Equity", "Stockholders Equity"]) or 0
    cash = stmt_val(bal, ["Cash And Cash Equivalents"]) or 0
    return debt + eq - cash


# Industry-specific intangible-capitalization parameters. `rd_life` = knowledge-capital useful
# life in years (≈ 1/δ_R&D — short where R&D obsoletes fast, e.g. software; long where it is
# patent-protected, e.g. pharma); `sga_org_frac` = the share of SG&A that is organization-capital
# investment (γ — high for brand/marketing-heavy consumer names, low for capital-intensive utilities).
# Representative encoding of Ewens-Peters-Wang (2024, Management Science) and Peters-Taylor (2017,
# JFE); keyed on the yfinance `sector`. Unmapped sectors fall back to the Peters-Taylor flat
# convention (rd_life=5y, γ=0.30) so behaviour is unchanged where we have no industry prior.
INTANGIBLE_PARAMS = {
    "Technology":             {"rd_life": 3, "sga_org_frac": 0.35},
    "Communication Services": {"rd_life": 4, "sga_org_frac": 0.40},
    "Healthcare":             {"rd_life": 8, "sga_org_frac": 0.25},
    "Consumer Cyclical":      {"rd_life": 5, "sga_org_frac": 0.45},
    "Consumer Defensive":     {"rd_life": 6, "sga_org_frac": 0.45},
    "Industrials":            {"rd_life": 6, "sga_org_frac": 0.20},
    "Basic Materials":        {"rd_life": 6, "sga_org_frac": 0.15},
    "Energy":                 {"rd_life": 6, "sga_org_frac": 0.15},
    "Financial Services":     {"rd_life": 4, "sga_org_frac": 0.30},
    "Utilities":              {"rd_life": 7, "sga_org_frac": 0.15},
    "Real Estate":            {"rd_life": 5, "sga_org_frac": 0.20},
}
INTANGIBLE_PARAMS_SOURCE = ("Representative encoding of Ewens-Peters-Wang (2024, Management Science) and "
                            "Peters-Taylor (2017, JFE) industry intangible-capitalization parameters; "
                            "fallback = Peters-Taylor flat rd_life=5y, SG&A org-capital share=0.30.")


def _intangible_params(sector, rd_life=None, sga_org_frac=None, org_life=None):
    """Resolve (rd_life, sga_org_frac, org_life, basis-label). Explicit args override the industry
    table; unmapped sectors fall back to the Peters-Taylor flat convention."""
    base = INTANGIBLE_PARAMS.get(sector or "", {})
    rl = rd_life if rd_life is not None else base.get("rd_life", 5)
    gf = sga_org_frac if sga_org_frac is not None else base.get("sga_org_frac", 0.30)
    ol = org_life if org_life is not None else base.get("org_life", 5)
    basis = (f"{sector} (Ewens-Peters-Wang / Peters-Taylor industry params)"
             if sector in INTANGIBLE_PARAMS else "flat Peters-Taylor default (sector unmapped)")
    return rl, gf, ol, basis


def normalize(ticker, rd_life=None, sga_org_frac=None, org_life=None):
    """A6: correct FCF and invested capital for SBC and R&D so ROIC/RIM/CAP cross-checks
    are trustworthy. yfinance FCF adds SBC back (treats it as free) and expenses R&D — both
    flatter the business. We deduct SBC from FCF and capitalize R&D into a research asset.

    Also capitalizes organization capital = `sga_org_frac` of SG&A over `org_life` (Peters-
    Taylor 2017, JFE: a share of SG&A is intangible investment in the organization). Emitted as
    `organization_capital`, and combined with the research asset as `intangible_capital` — the
    quant-factor screen uses these for intangible-adjusted book value (do NOT re-derive there).

    The R&D life and SG&A org-capital share are INDUSTRY-SPECIFIC (Ewens-Peters-Wang 2024) via
    `_intangible_params`, keyed on the firm's sector, with the flat Peters-Taylor convention as
    fallback; the parameters used are reported in `intangible_params`."""
    inc, bal, cf = yf_statements(ticker)
    sector = (yf.Ticker(ticker).info or {}).get("sector")
    rd_life, sga_org_frac, org_life, params_basis = _intangible_params(sector, rd_life, sga_org_frac, org_life)
    fcf = stmt_val(cf, ["Free Cash Flow"])
    sbc = stmt_val(cf, ["Stock Based Compensation"]) or 0
    norm_fcf = (fcf - sbc) if fcf is not None else None
    rds = [stmt_val(inc, ["Research And Development"], y) for y in range(rd_life)]
    rds = [r for r in rds if r is not None]
    research_asset = sum(r * ((rd_life - i) / rd_life) for i, r in enumerate(rds)) if rds else None
    amort = sum(r / rd_life for r in rds) if rds else None
    cur_rd = rds[0] if rds else None
    # organization capital: capitalize sga_org_frac of SG&A over org_life (Peters-Taylor)
    sgas = [stmt_val(inc, ["Selling General And Administration",
                           "Selling General And Administrative Expense"], y) for y in range(org_life)]
    sgas = [s for s in sgas if s is not None]
    org_capital = (sga_org_frac * sum(s * ((org_life - i) / org_life) for i, s in enumerate(sgas))
                   if sgas else None)
    intangible_capital = (research_asset or 0) + (org_capital or 0) if (research_asset or org_capital) else None
    tax, nopat, ic = _eff_tax(inc), _nopat(inc), _invested_capital(bal)
    nopat_norm = ni_addback = None
    if nopat is not None and cur_rd is not None and amort is not None:
        ni_addback = (cur_rd - amort) * (1 - tax)
        nopat_norm = nopat + ni_addback
    ic_norm = (ic + research_asset) if (ic is not None and research_asset is not None) else ic
    leases = stmt_val(bal, ["Capital Lease Obligations"]) or 0
    return {
        "ticker": ticker,
        "reported_fcf": rnd(fcf, 0), "sbc": rnd(sbc, 0), "normalized_fcf": rnd(norm_fcf, 0),
        "sbc_pct_of_fcf": rnd(100 * sbc / fcf, 1) if fcf else None,
        "rd_expense": rnd(cur_rd, 0), "capitalized_research_asset": rnd(research_asset, 0),
        "organization_capital": rnd(org_capital, 0), "intangible_capital": rnd(intangible_capital, 0),
        "rd_amortization": rnd(amort, 0), "ni_rd_addback": rnd(ni_addback, 0),
        "nopat_reported": rnd(nopat, 0), "nopat_normalized": rnd(nopat_norm, 0),
        "invested_capital": rnd(ic, 0), "invested_capital_normalized": rnd(ic_norm, 0),
        "roic_reported_pct": rnd(100 * nopat / ic, 1) if (nopat is not None and ic) else None,
        "roic_normalized_pct": rnd(100 * nopat_norm / ic_norm, 1) if (nopat_norm is not None and ic_norm) else None,
        "capital_lease_obligations": rnd(leases, 0),
        "intangible_params": {"sector": sector, "rd_life_yrs": rd_life,
                              "sga_org_frac": sga_org_frac, "org_life_yrs": org_life, "basis": params_basis},
        "intangible_params_source": INTANGIBLE_PARAMS_SOURCE,
        "note": "normalized_fcf deducts SBC; ROIC_normalized capitalizes R&D over "
                f"{rd_life}y and {sga_org_frac:.0%} of SG&A as organization capital (industry-tuned). "
                "Leases shown separately (yfinance Total Debt already includes capital leases).",
    }


# ------------------------------- core DCF math -------------------------------
def dcf_value(fcf0, growth5, fade_g, terminal_g, wacc, net_debt, shares, years_stage1=5, years_fade=5):
    """Two-stage-with-fade FCFF DCF -> per-share equity value. (Unchanged: backward-compatible.)"""
    flows, f = [], fcf0
    for _ in range(years_stage1):
        f *= (1 + growth5); flows.append(f)
    for k in range(1, years_fade + 1):
        g = growth5 + (fade_g - growth5) * k / years_fade
        f *= (1 + g); flows.append(f)
    disc = [(fl / (1 + wacc) ** (t + 1)) for t, fl in enumerate(flows)]
    tv = flows[-1] * (1 + terminal_g) / (wacc - terminal_g)
    tv_pv = tv / (1 + wacc) ** len(flows)
    ev = sum(disc) + tv_pv
    return (ev - net_debt) / shares, {"ev": ev, "tv_pv_share_of_ev": rnd(tv_pv / ev, 2)}


def _ff_growth_path(growth5, terminal_g, moat):
    """Fama-French geometric reversion of growth toward terminal, with the reversion SPEED set
    by moat quality (wider moat -> slower fade -> growth persists longer)."""
    rev = MOAT_REVERSION.get(moat, FF_REVERSION)
    return [terminal_g + (growth5 - terminal_g) * (1 - rev) ** (t + 1) for t in range(FF_HORIZON)]


def _effective_cap(path, terminal_g, tol=0.01):
    """Years until growth is within `tol` of terminal — the implied competitive-advantage period."""
    for t, g in enumerate(path):
        if abs(g - terminal_g) < tol:
            return t + 1
    return len(path)


def dcf_from_path(fcf0, growth_path, terminal_g, wacc, net_debt, shares):
    flows, f = [], fcf0
    for g in growth_path:
        f *= (1 + g); flows.append(f)
    disc = [(fl / (1 + wacc) ** (t + 1)) for t, fl in enumerate(flows)]
    tv = flows[-1] * (1 + terminal_g) / (wacc - terminal_g)
    tv_pv = tv / (1 + wacc) ** len(flows)
    ev = sum(disc) + tv_pv
    return (ev - net_debt) / shares, {"ev": ev, "tv_pv_share_of_ev": rnd(tv_pv / ev, 2), "years": len(flows)}


def dcf(ticker, growth5, fade_g, terminal_g, wacc, fade_model="const", moat=None, normalize_fcf=False, price_scale=1.0):
    fcf0, shares, net_debt, price, _, fcff_recon = _base_inputs(ticker, price_scale)
    if not fcf0 or not shares:
        return {"error": "missing FCF or share count from yfinance; supply manually"}
    norm = None
    if normalize_fcf:
        norm = normalize(ticker)
        if norm.get("normalized_fcf"):
            # normalize() also works off the LEVERED `Free Cash Flow` line, so re-apply the
            # same after-tax interest add-back to keep the flow firm-level.
            fcf0 = norm["normalized_fcf"] + (fcff_recon.get("interest_addback_after_tax") or 0)

    if fade_model == "ff":
        path = _ff_growth_path(growth5, terminal_g, moat)
        fv, meta = dcf_from_path(fcf0, path, terminal_g, wacc, net_debt, shares)
        meta["growth_path"] = [rnd(g, 3) for g in path]
        meta["reversion_rate"] = MOAT_REVERSION.get(moat, FF_REVERSION)
        meta["competitive_advantage_period_yrs"] = _effective_cap(path, terminal_g)
    else:
        fv, meta = dcf_value(fcf0, growth5, fade_g, terminal_g, wacc, net_debt, shares)

    out = {"ticker": ticker, "fcf0_used": rnd(fcf0, 0), "fcf0_basis": "FCFF (unlevered)",
           "normalized": normalize_fcf, "fcff_reconciliation": fcff_recon,
           "assumptions": {"growth_y1_5": growth5, "fade_to": fade_g, "terminal_g": terminal_g,
                           "wacc": wacc, "fade_model": fade_model, "moat": moat},
           "fair_value": rnd(fv), "price": price,
           "upside_pct": rnd(100 * (fv / price - 1)) if price else None, **meta}

    # value-driver / CAP decomposition: steady-state (NOPAT/WACC) vs value-of-growth
    try:
        inc, bal, _cf = yf_statements(ticker)
        nopat = (norm or {}).get("nopat_normalized") or _nopat(inc)
        ic = _invested_capital(bal)
        if nopat and wacc:
            steady_ev = nopat / wacc
            steady_ps = (steady_ev - net_debt) / shares
            vog = meta["ev"] - steady_ev
            out["value_drivers"] = {
                "steady_state_value_ps": rnd(steady_ps),
                "steady_state_pct_of_ev": rnd(100 * steady_ev / meta["ev"], 1),
                "value_of_growth_pct_of_ev": rnd(100 * vog / meta["ev"], 1),
                "implied_roic_pct": rnd(100 * nopat / ic, 1) if ic else None,
                "roic_minus_wacc_pp": rnd(100 * (nopat / ic - wacc), 1) if ic else None,
                "note": "steady_state = NOPAT/WACC (no value creation); the rest is value-of-growth, "
                        "which only exists while ROIC>WACC over the CAP.",
            }
    except Exception as e:
        out["value_drivers"] = {"error": str(e)}

    sens = {}
    for w in [wacc - 0.01, wacc, wacc + 0.01]:
        for g in [growth5 - 0.05, growth5, growth5 + 0.05]:
            if fade_model == "ff":
                v, _m = dcf_from_path(fcf0, _ff_growth_path(g, terminal_g, moat), terminal_g, w, net_debt, shares)
            else:
                v, _m = dcf_value(fcf0, g, fade_g, terminal_g, w, net_debt, shares)
            sens[f"wacc={w:.0%},g={g:.0%}"] = rnd(v)
    out["sensitivity"] = sens
    if norm:
        out["normalization"] = {k: norm[k] for k in ("sbc_pct_of_fcf", "roic_reported_pct", "roic_normalized_pct")}
    return out


# ---------------------- reverse / expectations-investing solve ----------------------
def _solve_growth(fcf0, shares, net_debt, price, wacc, terminal_g):
    lo, hi = -0.5, 1.5
    for _ in range(80):
        mid = (lo + hi) / 2
        v, _m = dcf_value(fcf0, mid, mid / 3, terminal_g, wacc, net_debt, shares)
        lo, hi = (mid, hi) if v < price else (lo, mid)
    return mid


def reverse_dcf(ticker, wacc, terminal_g, solve="growth", price_scale=1.0):
    """Expectations investing: back out the operating driver the market is pricing in, and
    report which driver the valuation is most sensitive to (the 'turbo trigger')."""
    fcf0, shares, net_debt, price, info, fcff_recon = _base_inputs(ticker, price_scale)
    if not (fcf0 and shares and price):
        return {"error": "missing inputs from yfinance"}
    out = {"ticker": ticker, "price": price, "wacc": wacc, "terminal_g": terminal_g, "solve": solve,
           "fcf0_basis": "FCFF (unlevered)", "fcff_reconciliation": fcff_recon}

    g_impl = _solve_growth(fcf0, shares, net_debt, price, wacc, terminal_g)
    out["implied_fcf_growth_y1_5"] = rnd(g_impl, 3)

    if solve == "margin":
        # DCF is linear in fcf0: implied FCF0 that justifies price at a REFERENCE growth,
        # expressed as an implied steady-state FCF margin on current revenue.
        ref_g = 0.10
        v_ref, meta = dcf_value(fcf0, ref_g, ref_g / 3, terminal_g, wacc, net_debt, shares)
        ev_per_fcf0 = meta["ev"] / fcf0
        fcf0_implied = (price * shares + net_debt) / ev_per_fcf0
        rev = info.get("totalRevenue")
        out["reference_growth"] = ref_g
        out["implied_fcf0"] = rnd(fcf0_implied, 0)
        out["implied_fcf_margin_pct"] = rnd(100 * fcf0_implied / rev, 1) if rev else None
        out["current_fcf_margin_pct"] = rnd(100 * fcf0 / rev, 1) if rev else None

    if solve == "cap":
        # implied competitive-advantage period: years of ~15% growth (ff-fade) the price needs
        base_g = min(max(g_impl, 0.08), 0.30)
        best, gap = None, 1e18
        for cap in range(1, 31):
            path = [terminal_g + (base_g - terminal_g) * (1 - FF_REVERSION) ** (t + 1) for t in range(cap)]
            v, _m = dcf_from_path(fcf0, path, terminal_g, wacc, net_debt, shares)
            if abs(v - price) < gap:
                gap, best = abs(v - price), cap
        out["reference_growth"] = rnd(base_g, 3)
        out["implied_competitive_advantage_period_yrs"] = best

    # local sensitivity: which driver moves fair value most (turbo trigger)
    base_v, _ = dcf_value(fcf0, g_impl, g_impl / 3, terminal_g, wacc, net_debt, shares)
    dg, _ = dcf_value(fcf0, g_impl + 0.01, (g_impl + 0.01) / 3, terminal_g, wacc, net_debt, shares)
    dw, _ = dcf_value(fcf0, g_impl, g_impl / 3, terminal_g, wacc + 0.01, net_debt, shares)
    dt, _ = dcf_value(fcf0, g_impl, g_impl / 3, terminal_g + 0.005, wacc, net_debt, shares)
    sens = {"growth_+1pp": rnd(100 * (dg / base_v - 1), 1), "wacc_+1pp": rnd(100 * (dw / base_v - 1), 1),
            "terminal_g_+0.5pp": rnd(100 * (dt / base_v - 1), 1)}
    out["value_sensitivity_pct"] = sens
    out["turbo_trigger"] = max(sens, key=lambda k: abs(sens[k]))
    out["note"] = "growth the market is pricing in; judge each implied driver vs your independent view + base rates"
    return out


# ------------------------------- residual income -------------------------------
def rim(ticker, coe, roe_fade_to=None, years=10, raw_book=False, price_scale=1.0):
    """B2: book-anchored second value. V0 = B0 + Σ PV((ROE_t - coe)·B_{t-1}); ROE fades from
    current to `roe_fade_to` (default = coe, i.e. excess returns compete away -> conservative,
    terminal-value-light). Emits value-to-price (V/P).

    By default the equity base is grossed up by the capitalized R&D research asset (and NI by the
    R&D add-back) so the model isn't fooled by the tiny reported book of asset-light, R&D-heavy
    compounders — the classic RIM blind spot. Pass raw_book=True for statutory book value."""
    inc, bal, cf = yf_statements(ticker)
    info = yf.Ticker(ticker).info or {}
    b0 = stmt_val(bal, ["Common Stock Equity", "Stockholders Equity"])
    ni = stmt_val(inc, ["Net Income", "Net Income Common Stockholders"])
    shares = info.get("sharesOutstanding")
    price = quote_price(info, price_scale)
    if not (b0 and ni and shares):
        return {"error": "missing book value / net income / shares"}
    intangible_adj = False
    if not raw_book:
        nrm = normalize(ticker)
        ra, add = nrm.get("capitalized_research_asset"), nrm.get("ni_rd_addback")
        if ra:
            b0 += ra; intangible_adj = True
            if add:
                ni += add
    roe0 = ni / b0
    div = abs(stmt_val(cf, ["Cash Dividends Paid", "Common Stock Dividend Paid"]) or 0)
    payout = min(max(div / ni, 0.0), 1.0) if ni else 0.0
    fade_to = coe if roe_fade_to is None else roe_fade_to

    book, pv_ri = b0, 0.0
    schedule = []
    for t in range(1, years + 1):
        roe_t = fade_to + (roe0 - fade_to) * (1 - FF_REVERSION) ** t
        ri = (roe_t - coe) * book
        disc = ri / (1 + coe) ** t
        pv_ri += disc
        book = book * (1 + roe_t * (1 - payout))
        schedule.append({"yr": t, "roe": rnd(roe_t, 3), "residual_income": rnd(ri, 0)})
    # terminal residual income (perpetuity at final year's RI, no growth -> conservative)
    roe_T = fade_to + (roe0 - fade_to) * (1 - FF_REVERSION) ** years
    ri_T = (roe_T - coe) * book
    tv_ri = (ri_T / coe) / (1 + coe) ** years if roe_T > coe else 0.0
    value = b0 + pv_ri + tv_ri
    vps = value / shares
    return {
        "ticker": ticker, "price": price, "cost_of_equity": coe,
        "intangible_adjusted_book": intangible_adj,
        "book_value_ps": rnd(b0 / shares), "current_roe_pct": rnd(100 * roe0, 1),
        "roe_fade_to_pct": rnd(100 * fade_to, 1), "payout_ratio": rnd(payout, 2),
        "rim_value_ps": rnd(vps),
        "pv_of_residual_income_ps": rnd((pv_ri + tv_ri) / shares),
        "value_to_price": rnd(vps / price, 2) if price else None,
        "upside_pct": rnd(100 * (vps / price - 1)) if price else None,
        "read": ("cheap on RIM (V/P>1.1)" if price and vps / price > 1.1
                 else "expensive on RIM (V/P<0.9)" if price and vps / price < 0.9 else "fair on RIM"),
        "note": "Independent of the FCFF DCF; most value sits in audited book, so terminal value "
                "carries less weight. Low V/P on a low-multiple name is a value-trap tell.",
    }


# ------------------------------- Monte-Carlo / scenario -------------------------------
def montecarlo(ticker, draws, growth5, fade_g, terminal_g, wacc, price_scale=1.0):
    """B6: distribution of intrinsic value from assumption uncertainty -> P10/median/P90 and
    P(value>price). Directly counters precision-illusion. Input dispersion is anchored to the
    firm's own historical revenue-growth volatility where available."""
    fcf0, shares, net_debt, price, _, _recon = _base_inputs(ticker, price_scale)
    if not (fcf0 and shares and price):
        return {"error": "missing inputs from yfinance"}
    # growth dispersion from historical revenue growth stdev, floored/capped for sanity
    g_sd = 0.05
    try:
        inc, _b, _c = yf_statements(ticker)
        rev = [stmt_val(inc, ["Total Revenue"], y) for y in range(5)]
        rev = [r for r in rev if r]
        if len(rev) >= 3:
            grs = [rev[i] / rev[i + 1] - 1 for i in range(len(rev) - 1)]
            g_sd = float(min(max(np.std(grs), 0.03), 0.20))
    except Exception:
        pass
    rng = np.random.default_rng(42)   # deterministic: reproducible thesis
    gs = rng.normal(growth5, g_sd, draws)
    ws = rng.normal(wacc, 0.01, draws)
    ts = rng.normal(terminal_g, 0.005, draws)
    vals = []
    for g, w, t in zip(gs, ws, ts):
        w = max(w, t + 0.02)
        v, _m = dcf_value(fcf0, float(g), fade_g, float(t), w, net_debt, shares)
        vals.append(v)
    vals = np.array(vals)
    return {
        "ticker": ticker, "price": price, "draws": draws,
        "growth_mean": growth5, "growth_stdev_used": rnd(g_sd, 3),
        "value_p10": rnd(float(np.percentile(vals, 10))),
        "value_median": rnd(float(np.percentile(vals, 50))),
        "value_p90": rnd(float(np.percentile(vals, 90))),
        "prob_value_gt_price": rnd(float((vals > price).mean()), 2),
        "median_upside_pct": rnd(100 * (float(np.percentile(vals, 50)) / price - 1)),
        "read": "report fair value as this range, not a point; P(value>price) is the honest margin-of-safety odds",
    }


def scenario(ticker, bull, base, bear, probs, fade_g, terminal_g, wacc, price_scale=1.0):
    fcf0, shares, net_debt, price, _, _recon = _base_inputs(ticker, price_scale)
    if not (fcf0 and shares and price):
        return {"error": "missing inputs from yfinance"}
    labels = {"bull": bull, "base": base, "bear": bear}
    pv = dict(zip(["bull", "base", "bear"], probs))
    rows, ev_weighted = {}, 0.0
    for name, g in labels.items():
        v, _m = dcf_value(fcf0, g, fade_g, terminal_g, wacc, net_debt, shares)
        rows[name] = {"growth": g, "value_ps": rnd(v), "prob": pv[name],
                      "upside_pct": rnd(100 * (v / price - 1))}
        ev_weighted += v * pv[name]
    return {"ticker": ticker, "price": price, "scenarios": rows,
            "prob_weighted_value_ps": rnd(ev_weighted),
            "prob_weighted_upside_pct": rnd(100 * (ev_weighted / price - 1)),
            "note": "probabilities must sum to ~1; probability-weighted EV vs price is the decision number"}


# ------------------------------- implied cost of capital -------------------------------
def icc(ticker, method="hvz", sustainable_roe=0.12, price_scale=1.0):
    """B7: back the discount rate out of price instead of assuming a WACC. Default HvZ-style
    is filings-only (no analyst forecasts): solve for the cost of equity that equates a
    residual-income valuation to the current price, with ROE fading toward a sustainable
    long-run level (GLS-style, fade to an industry-ish ROE rather than to r) plus a terminal
    residual-income perpetuity."""
    if method in ("gls", "ct", "ojn", "peg"):
        return {"ticker": ticker, "method": method,
                "status": "needs_external_data",
                "reason": f"{method.upper()} requires a consensus forward-EPS feed, which this system does not pull. "
                          "Use --method hvz (filings-only) as the default implied cost of equity."}
    inc, bal, cf = yf_statements(ticker)
    info = yf.Ticker(ticker).info or {}
    b0 = stmt_val(bal, ["Common Stock Equity", "Stockholders Equity"])
    ni = stmt_val(inc, ["Net Income", "Net Income Common Stockholders"])
    shares = info.get("sharesOutstanding")
    price = quote_price(info, price_scale)
    if not (b0 and ni and shares and price):
        return {"error": "missing book / earnings / price"}
    roe0 = ni / b0
    div = abs(stmt_val(cf, ["Cash Dividends Paid", "Common Stock Dividend Paid"]) or 0)
    payout = min(max(div / ni, 0.0), 1.0) if ni else 0.0
    mktcap = price * shares
    T = 12

    def rim_value(r):
        book, pv = b0, 0.0
        roe_t = roe0
        for t in range(1, T + 1):
            roe_t = sustainable_roe + (roe0 - sustainable_roe) * (1 - FF_REVERSION) ** t  # fade to sustainable ROE
            pv += (roe_t - r) * book / (1 + r) ** t
            book *= (1 + roe_t * (1 - payout))
        ri_T = (roe_t - r) * book                       # terminal residual income perpetuity (no growth)
        tv = (ri_T / r) / (1 + r) ** T if roe_t > r else 0.0
        return b0 + pv + tv

    lo0, hi0 = 0.03, 0.30
    lo, hi = lo0, hi0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if rim_value(mid) > mktcap else (lo, mid)   # higher r -> lower value
    hit = "floor" if mid < lo0 + 0.005 else "ceiling" if mid > hi0 - 0.005 else None
    out = {
        "ticker": ticker, "method": "hvz (filings-only RIM-implied cost of equity)",
        "implied_cost_of_equity_pct": rnd(100 * mid, 2),
        "current_roe_pct": rnd(100 * roe0, 1), "sustainable_roe_pct": rnd(100 * sustainable_roe, 1),
        "read": "use as an independent check on your CAPM WACC; a high ICC means the market already "
                "demands a rich return (little priced-in optimism), a low ICC the opposite",
    }
    if hit:
        out["bound_hit"] = hit
        out["warning"] = (
            "price cannot be reconciled to a book-anchored RIM at any cost of equity in [3%,30%] - "
            "ICC pinned at the " + hit + ". This method is only informative for mature, earnings-anchored "
            "names; for high-growth firms whose value is mostly long-duration growth, use reverse-DCF instead.")
    return out


# ------------------------------- comps (+ warranted) -------------------------------
def _peer_row(tk):
    i = yf.Ticker(tk).info or {}
    return {"pe_fwd": rnd(i.get("forwardPE")), "ps": rnd(i.get("priceToSalesTrailing12Months")),
            "ev_ebitda": rnd(i.get("enterpriseToEbitda")), "pb": rnd(i.get("priceToBook")),
            "gross_margin": rnd((i.get("grossMargins") or 0) * 100),
            "op_margin": rnd((i.get("operatingMargins") or 0) * 100),
            "rev_growth": rnd((i.get("revenueGrowth") or 0) * 100),
            "roe": rnd((i.get("returnOnEquity") or 0) * 100),
            "mkt_cap_bn": rnd((i.get("marketCap") or 0) / 1e9, 1)}


def comps(ticker, peers, warranted=False):
    rows = {}
    for tk in [ticker] + peers:
        try:
            rows[tk] = _peer_row(tk)
        except Exception as e:
            rows[tk] = {"error": str(e)}
    peer_vals = [r["ev_ebitda"] for k, r in rows.items() if k != ticker and r.get("ev_ebitda")]
    if peer_vals and rows[ticker].get("ev_ebitda"):
        med = float(np.median(peer_vals))
        rows["_summary"] = {"peer_median_ev_ebitda": rnd(med),
                            "subject_premium_pct": rnd(100 * (rows[ticker]["ev_ebitda"] / med - 1))}

    if warranted:
        # B8: fair EV/EBITDA from a fundamentals regression (multiple ~ growth + margin + roe)
        # instead of a raw peer average. Actual - warranted = the structural relative-value signal.
        X, y, names = [], [], []
        for k, r in rows.items():
            if k in (ticker, "_summary") or "error" in r:
                continue
            if all(r.get(f) is not None for f in ("ev_ebitda", "rev_growth", "op_margin", "roe")):
                X.append([1.0, r["rev_growth"], r["op_margin"], r["roe"]]); y.append(r["ev_ebitda"]); names.append(k)
        subj = rows[ticker]
        if len(X) >= 4 and all(subj.get(f) is not None for f in ("rev_growth", "op_margin", "roe", "ev_ebitda")):
            A, b = np.array(X), np.array(y)
            coef, *_ = np.linalg.lstsq(A, b, rcond=None)
            warr = float(coef @ np.array([1.0, subj["rev_growth"], subj["op_margin"], subj["roe"]]))
            rows["_warranted"] = {
                "model": "EV/EBITDA ~ 1 + rev_growth + op_margin + roe (OLS on peers)",
                "peers_used": names,
                "warranted_ev_ebitda": rnd(warr),
                "actual_ev_ebitda": subj["ev_ebitda"],
                "residual": rnd(subj["ev_ebitda"] - warr),
                "read": ("cheap vs fundamentals-warranted" if subj["ev_ebitda"] < warr
                         else "rich vs fundamentals-warranted"),
            }
        else:
            rows["_warranted"] = {"status": "need >=4 peers with growth/margin/roe/EV-EBITDA for the regression"}
        # intangible-adjusted P/B (book + capitalized R&D)
        try:
            nrm = normalize(ticker)
            inc, bal, _cf = yf_statements(ticker)
            eq = stmt_val(bal, ["Common Stock Equity", "Stockholders Equity"])
            ra = nrm.get("capitalized_research_asset")
            info = yf.Ticker(ticker).info or {}
            mktcap = info.get("marketCap")
            if eq and ra and mktcap:
                rows["_intangible_adj_pb"] = {"reported_pb": rnd(mktcap / eq, 2),
                                              "intangible_adj_pb": rnd(mktcap / (eq + ra), 2),
                                              "note": "book grossed up by capitalized R&D research asset"}
        except Exception:
            pass
    return rows


# ---------------------- Earnings Power Value (Greenwald-Kahn) ----------------------
def _reported_intangibles(bal):
    """Reported goodwill + acquired intangibles, so tangible equity can strip them out before we
    add back an internally-estimated reproduction of the intangible base (avoids double-counting
    acquired goodwill with the capitalized-R&D/SG&A reproduction)."""
    combined = stmt_val(bal, ["Goodwill And Other Intangible Assets"])
    if combined is not None:
        return combined
    gw = stmt_val(bal, ["Goodwill"]) or 0
    oi = stmt_val(bal, ["Other Intangible Assets"]) or 0
    return (gw + oi) if (gw or oi) else 0


def epv(ticker, wacc, price_scale=1.0):
    """Greenwald-Kahn Earnings Power Value — the most conservative, growth-FREE intrinsic anchor.
    EPV = normalized NOPAT / WACC (no value creation assumed) is a Graham floor. Compared with a
    GOING-CONCERN reproduction value (tangible equity + an internally-estimated reproduction of the
    intangible base), the difference is franchise value — what a moat, not the assets, is worth. If
    EPV alone already covers the price, the buyer pays nothing for growth (the strongest margin of
    safety). Reproduction here is GOING-CONCERN, explicitly NOT a liquidation floor (that is the
    deep-value-distressed agent's lane; see `netnet`)."""
    _fcf, shares, net_debt, price, _info, _recon = _base_inputs(ticker, price_scale)
    inc, bal, _cf = yf_statements(ticker)
    if not shares:
        return {"error": "missing share count from yfinance"}
    norm = normalize(ticker)
    nopat = norm.get("nopat_normalized") or _nopat(inc)
    if nopat is None:
        return {"error": "missing NOPAT (EBIT) from yfinance statements"}
    book_equity = stmt_val(bal, ["Common Stock Equity", "Stockholders Equity"])
    if book_equity is None:
        return {"error": "missing book equity from yfinance balance sheet"}
    intangible_cap = norm.get("intangible_capital") or 0
    reported_intang = _reported_intangibles(bal)
    tangible_equity = book_equity - reported_intang
    # Reproduction value: reported book equity already capitalizes ACQUIRED intangibles/goodwill,
    # so gross it up by the INTERNALLY-generated intangibles GAAP expensed (capitalized R&D + org
    # capital) rather than stripping the acquired base (which would understate reproduction cost for
    # acquisitive, asset-heavy firms). tangible_book_ps is reported separately for the Graham reader.
    reproduction_equity = book_equity + intangible_cap

    epv_ev = nopat / wacc
    epv_ps = (epv_ev - net_debt) / shares
    repro_ps = reproduction_equity / shares
    franchise_ps = epv_ps - repro_ps
    return {
        "ticker": ticker, "price": price, "wacc": wacc,
        "nopat_normalized": rnd(nopat, 0),
        "epv_value_ps": rnd(epv_ps), "epv_ev": rnd(epv_ev, 0),
        "reproduction_value_ps": rnd(repro_ps),
        "book_equity": rnd(book_equity, 0),
        "tangible_book_ps": rnd(tangible_equity / shares),
        "reported_goodwill_intangibles": rnd(reported_intang, 0),
        "intangible_capital_added": rnd(intangible_cap, 0),
        "franchise_value_ps": rnd(franchise_ps),
        "franchise_pct_of_epv": rnd(100 * franchise_ps / epv_ps, 1) if epv_ps else None,
        "epv_covers_price": bool(price and epv_ps >= price),
        "upside_to_epv_pct": rnd(100 * (epv_ps / price - 1)) if price else None,
        "read": ("EPV alone covers the price - growth is free (strongest margin of safety)"
                 if price and epv_ps >= price else
                 "price exceeds EPV - the gap is what you pay for growth/franchise; judge if the moat earns it"
                 if price else "no price available"),
        "note": "EPV = normalized NOPAT/WACC (growth-free, Graham floor). reproduction_value = reported "
                "book equity (which already capitalizes ACQUIRED intangibles/goodwill) grossed up by the "
                "INTERNALLY-generated intangibles GAAP expensed (capitalized R&D + org capital); "
                "tangible_book_ps strips all intangibles for the Graham-conservative reader. This is a "
                "GOING-CONCERN reproduction, NOT a liquidation floor (deep-value-distressed's lane; see "
                "`netnet`). franchise_value = EPV - reproduction.",
        "data_caveats": "~4y annual history, so NOPAT is a single-/few-year proxy for maintenance "
                        "earnings power (Greenwald-Kahn ideally averages a full cycle); no maintenance-"
                        "vs-growth capex split; reproduction uses book + capitalized intangibles, not "
                        "appraised replacement cost.",
    }


# ---------------------- Graham net-net / liquidation floor ----------------------
# Graham (Security Analysis) approximate liquidation haircuts by asset class.
LIQUIDATION_HAIRCUTS = {"cash": 1.00, "receivables": 0.80, "inventory": 0.60,
                        "other_current": 0.50, "net_ppe": 0.20}


def netnet(ticker, price_scale=1.0):
    """Benjamin Graham's net current asset value (NCAV) and liquidation floor — the cigar-butt
    deep-value asset floor. NCAV = current assets - TOTAL liabilities (fixed assets get no
    going-concern credit). Also net-net working capital (NNWC = cash + haircut receivables/
    inventory - total liabilities) and an approximate liquidation value using Graham's asset-class
    haircuts. A price below two-thirds of NCAV is the classic Graham deep bargain. This is a
    LIQUIDATION floor (deep-value-distressed's lane), distinct from the going-concern reproduction
    value in `epv` (value agent's lane)."""
    _fcf, shares, _nd, price, _info, _recon = _base_inputs(ticker, price_scale)
    _inc, bal, _cf = yf_statements(ticker)
    ca = stmt_val(bal, ["Current Assets", "Total Current Assets"])
    tl = stmt_val(bal, ["Total Liabilities Net Minority Interest", "Total Liabilities"])
    if not (ca and tl and shares):
        return {"error": "missing current assets / total liabilities / shares from yfinance"}
    cash = stmt_val(bal, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]) or 0
    recv = stmt_val(bal, ["Receivables", "Accounts Receivable"]) or 0
    inv = stmt_val(bal, ["Inventory"]) or 0
    net_ppe = stmt_val(bal, ["Net PPE"]) or 0
    other_ca = max(ca - cash - recv - inv, 0)
    h = LIQUIDATION_HAIRCUTS

    ncav = ca - tl
    nnwc = cash + h["receivables"] * recv + h["inventory"] * inv - tl
    liquidation = (h["cash"] * cash + h["receivables"] * recv + h["inventory"] * inv
                   + h["other_current"] * other_ca + h["net_ppe"] * net_ppe - tl)
    ncav_ps, nnwc_ps, liq_ps = ncav / shares, nnwc / shares, liquidation / shares
    book_eq = stmt_val(bal, ["Common Stock Equity", "Stockholders Equity"]) or 0
    return {
        "ticker": ticker, "price": price,
        "ncav_ps": rnd(ncav_ps), "nnwc_ps": rnd(nnwc_ps), "liquidation_value_ps": rnd(liq_ps),
        "book_value_ps": rnd(book_eq / shares),
        "price_to_ncav": rnd(price / ncav_ps, 2) if (price and ncav_ps > 0) else None,
        "margin_of_safety_to_ncav_pct": rnd(100 * (ncav_ps / price - 1)) if (price and ncav_ps) else None,
        "below_ncav": bool(price and ncav_ps > 0 and price < ncav_ps),
        "below_two_thirds_ncav": bool(price and ncav_ps > 0 and price < (2 / 3) * ncav_ps),
        "is_net_net": bool(price and nnwc_ps > 0 and price < nnwc_ps),
        "haircuts_used": h,
        "read": ("classic Graham net-net: price below net-net working capital (deep bargain)"
                 if price and nnwc_ps > 0 and price < nnwc_ps else
                 "below NCAV but above NNWC: cheap on current assets, not a strict net-net"
                 if price and ncav_ps > 0 and price < ncav_ps else
                 "trades above NCAV: no Graham asset-floor margin of safety"),
        "note": "NCAV = current assets - TOTAL liabilities (fixed assets get no going-concern credit). "
                "Liquidation value applies Graham asset-class haircuts. This is a LIQUIDATION floor "
                "(deep-value-distressed's lane), NOT the going-concern reproduction value in `epv`.",
        "data_caveats": "yfinance current-asset components (cash/receivables/inventory) can be partial; "
                        "other_current is a residual. Haircuts are Graham approximations, not appraised.",
    }


# --------------------------------- CLI ---------------------------------
def main():
    p = argparse.ArgumentParser(description="Valuation engine")
    p.add_argument("cmd", choices=["dcf", "reverse", "rim", "mc", "scenario", "icc", "comps", "normalize", "epv", "netnet"])
    p.add_argument("ticker")
    p.add_argument("peers", nargs="?", default="")
    # 0.15 is a DELIBERATELY optimistic placeholder kept for reproducibility of prior runs,
    # but a bare `dcf`/`mc`/`scenario` call silently embeds a 15% five-year growth rate and
    # every P(value>price) computed that way reads as a company estimate when it is not.
    # Found by `fundamental` 2026-07-30, which struck four of its own prior figures on it.
    # The default stands; what changes is that the output now says where the number came from.
    p.add_argument("--growth5", type=float, default=0.15)
    p.add_argument("--fade", type=float, default=0.05)
    p.add_argument("--tg", type=float, default=0.025)
    p.add_argument("--wacc", type=float, default=0.09)
    p.add_argument("--fade-model", dest="fade_model", choices=["const", "ff"], default="const")
    p.add_argument("--moat", choices=["none", "narrow", "wide"], default=None)
    p.add_argument("--normalize", action="store_true")
    p.add_argument("--solve", choices=["growth", "margin", "cap"], default="growth")
    p.add_argument("--coe", type=float, default=0.10)
    p.add_argument("--roe-fade-to", dest="roe_fade_to", type=float, default=None)
    p.add_argument("--raw-book", dest="raw_book", action="store_true", help="RIM: use statutory book (skip R&D capitalization)")
    p.add_argument("--price-scale", dest="price_scale", type=float, default=1.0,
                   help="FX factor applied to the quote when it is in a different "
                        "CURRENCY from the statements (e.g. GBPUSD spot for HSBA.L, which "
                        "quotes in pence but reports in USD). GBp->GBP pence conversion is "
                        "automatic and must NOT be included here.")
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--draws", type=int, default=10000)
    p.add_argument("--method", choices=["hvz", "gls", "ct", "ojn", "peg"], default="hvz")
    p.add_argument("--sustainable-roe", dest="sustainable_roe", type=float, default=0.12)
    p.add_argument("--warranted", action="store_true")
    p.add_argument("--bull", type=float, default=0.30)
    p.add_argument("--base", type=float, default=0.15)
    p.add_argument("--bear", type=float, default=0.02)
    p.add_argument("--probs", default="0.25,0.50,0.25")
    a = p.parse_args()
    _g5_defaulted = not any(x == "--growth5" or x.startswith("--growth5=") for x in sys.argv)
    _g5_note = {"growth5_used": a.growth5,
                "growth5_source": ("DEFAULT -- a 15% placeholder, NOT a company or consensus "
                                   "estimate; pass --growth5 to set it"
                                   if _g5_defaulted else "USER")}

    def jprint_g(d):
        """jprint, with the growth assumption stamped on any command that consumes it."""
        if isinstance(d, dict):
            d = dict(d, **{"_assumptions": _g5_note})
        jprint(d)


    if a.cmd == "dcf":
        jprint_g(dcf(a.ticker, a.growth5, a.fade, a.tg, a.wacc, a.fade_model, a.moat, a.normalize,
                   a.price_scale))
    elif a.cmd == "reverse":
        jprint(reverse_dcf(a.ticker, a.wacc, a.tg, a.solve, a.price_scale))
    elif a.cmd == "rim":
        jprint(rim(a.ticker, a.coe, a.roe_fade_to, a.years, a.raw_book, a.price_scale))
    elif a.cmd == "mc":
        jprint_g(montecarlo(a.ticker, a.draws, a.growth5, a.fade, a.tg, a.wacc, a.price_scale))
    elif a.cmd == "scenario":
        probs = [float(x) for x in a.probs.split(",")]
        jprint_g(scenario(a.ticker, a.bull, a.base, a.bear, probs, a.fade, a.tg, a.wacc,
                        a.price_scale))
    elif a.cmd == "icc":
        jprint(icc(a.ticker, a.method, a.sustainable_roe, a.price_scale))
    elif a.cmd == "normalize":
        jprint(normalize(a.ticker))
    elif a.cmd == "comps":
        jprint(comps(a.ticker, parse_list(a.peers), a.warranted))
    elif a.cmd == "epv":
        jprint(epv(a.ticker, a.wacc, a.price_scale))
    elif a.cmd == "netnet":
        jprint(netnet(a.ticker, a.price_scale))


if __name__ == "__main__":
    main_guard(main)
