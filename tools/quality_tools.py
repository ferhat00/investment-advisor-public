"""Quality screens — the wonderful-business assembly point for the quality investor.

Everything a quality investor needs to score a moat is scattered across the library
(forensic.py, credit_tools.py, quant_factors.py, valuation.py) but nothing turns it into a
single computed verdict for the SUBJECT company, and ROIC durability is assumed via a moat
label rather than MEASURED. This tool closes those gaps by REUSING the existing engines:

  python quality_tools.py panel   NVDA          # QMJ four-pillar + Fundsmith thresholds -> PASS/BORDERLINE/FAIL
  python quality_tools.py qmj     NVDA AMD,AVGO  # peer-relative QMJ z-composite (needs peers)
  python quality_tools.py roic_persistence NVDA --wacc 0.09 --moat wide   # AR(1) fade / half-life / empirical CAP
  python quality_tools.py profitability NVDA     # GP/A, cash-based OP, GMAR, cash conversion (absolute, thresholded)
  python quality_tools.py stability NVDA         # margin / ROIC coefficient-of-variation (inverse-moat fingerprint)
  python quality_tools.py markup  NVDA --wacc 0.09   # production-approach price/marginal-cost markup (pricing power)
  python quality_tools.py payout  NVDA           # net payout yield, FCF coverage, buyback accretion
  python quality_tools.py all     NVDA AMD,AVGO  # ensemble + cheap-safe-quality signature + one-line read

References: Asness-Frazzini-Pedersen (2019, Rev. Acc. Studies 24(1), QMJ); Fama-French (2000, J.
Business 73(2), profitability mean-reversion); Ball-Gerakos-Linnainmaa-Nikolaev (2016, JFE 121(1),
cash-based OP); Novy-Marx (2013, JFE 108(1), GP/A); Dichev-Tang (2009, JAE 47(1-2), earnings-vol);
De Loecker-Eeckhout-Unger (2020, QJE 135(2), markups); Boudoukh-Michaely-Richardson-Roberts (2007,
JF 62(2), payout yield); Frazzini-Kabiller-Pedersen (2018, FAJ 74(4), Buffett's Alpha). Fundsmith
absolute thresholds from the Fundsmith Equity Fund Owner's Manual.

DATA CAVEAT: yfinance annual statements usually return only ~4 fiscal years, so the durability
metrics (roic_persistence AR(1), stability CV, markup trend) are LOW statistical power. Every such
block reports `years_available` and flags a low-power caveat when n < 6 — treat as directional.
"""
import sys, math, argparse
import numpy as np
import yfinance as yf
from common import jprint, rnd, main_guard
import forensic, credit_tools, valuation


# --------------------------------- helpers ---------------------------------

def _safe(a, b):
    try:
        return a / b if (a is not None and b not in (None, 0)) else None
    except (TypeError, ZeroDivisionError):
        return None


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return float(np.mean(xs)) if xs else None


# Year-parameterized statement pulls layered on forensic.Filings (which already gives revenue,
# cogs, gross_profit, sga, net_income, total_assets, current_assets/liab, receivables, inventory,
# cash, ltd, cfo, fcf, shares, depreciation, gross/net PPE with alias tolerance).
def _ebit(f, y=0):      return f.I(["EBIT", "Operating Income"], y)
def _equity(f, y=0):    return f.B(["Common Stock Equity", "Stockholders Equity"], y)
def _interest(f, y=0):  return f.I(["Interest Expense"], y)
def _pretax(f, y=0):    return f.I(["Pretax Income"], y)
def _taxprov(f, y=0):   return f.I(["Tax Provision"], y)
def _capex(f, y=0):     return f.C(["Capital Expenditure"], y)
def _payables(f, y=0):  return f.B(["Accounts Payable", "Payables"], y)
def _dividends(f, y=0): return f.C(["Cash Dividends Paid", "Common Stock Dividend Paid"], y)
def _buybacks(f, y=0):  return f.C(["Repurchase Of Capital Stock"], y)
def _issuance(f, y=0):  return f.C(["Issuance Of Capital Stock", "Net Common Stock Issuance"], y)
def _sbc(f, y=0):       return f.C(["Stock Based Compensation"], y)


def _total_debt(f, y=0):
    d = f.B(["Total Debt"], y)
    if d is not None:
        return d
    ltd = f.ltd(y) or 0
    std = f.B(["Current Debt", "Current Debt And Capital Lease Obligation"], y) or 0
    return (ltd + std) if (ltd or std) else None


def _eff_tax(f, y=0):
    """Effective tax for year y, mirroring valuation._eff_tax (Tax Rate For Calcs, else provision/pretax
    clipped [0,0.35], else 0.21) so NOPAT/ROIC reconcile with valuation.py."""
    tr = f.I(["Tax Rate For Calcs"], y)
    if tr is not None and 0 <= tr <= 0.6:
        return tr
    tp, pt = _taxprov(f, y), _pretax(f, y)
    if tp is not None and pt:
        return min(max(tp / pt, 0.0), 0.35)
    return 0.21


def _nopat(f, y=0):
    e = _ebit(f, y)
    return None if e is None else e * (1 - _eff_tax(f, y))


def _invested_capital(f, y=0):
    """Mirror valuation._invested_capital: the explicit 'Invested Capital' line when present, else
    total debt + equity - cash. Keeps ROIC consistent with valuation.py normalize."""
    ic = f.B(["Invested Capital"], y)
    if ic:
        return ic
    debt, eq, cash = _total_debt(f, y) or 0, _equity(f, y) or 0, f.cash(y) or 0
    ic = debt + eq - cash
    return ic if ic else None


def _roic(f, y=0):
    return _safe(_nopat(f, y), _invested_capital(f, y))


def _roce(f, y=0):
    """EBIT / capital employed (total assets - current liabilities). Terry Smith's headline ratio."""
    ta, cl = f.total_assets(y), f.current_liab(y)
    ce = (ta - cl) if (ta is not None and cl is not None) else ta
    return _safe(_ebit(f, y), ce)


def _grade(v, strong, borderline, higher_better=True):
    """Map a value to strong/borderline/fail vs two thresholds. None -> None."""
    if not isinstance(v, (int, float)):
        return None
    if higher_better:
        return "strong" if v >= strong else "borderline" if v >= borderline else "fail"
    return "strong" if v <= strong else "borderline" if v <= borderline else "fail"


_GRADE_PTS = {"strong": 2, "borderline": 1, "fail": 0}


def _roll_up(grades):
    """Roll a list of strong/borderline/fail grades into an average score and a verdict."""
    pts = [_GRADE_PTS[g] for g in grades if g in _GRADE_PTS]
    if not pts:
        return None, None
    avg = float(np.mean(pts))
    verdict = "PASS" if avg >= 1.5 else "BORDERLINE" if avg >= 0.8 else "FAIL"
    return rnd(avg, 2), verdict


# ------------------------------- ROIC series -------------------------------

def _roic_roce_series(f):
    """Oldest-first per-year ROIC and ROCE (%), plus fiscal-year labels."""
    n = f.n_years
    years, roic, roce = [], [], []
    for y in range(n - 1, -1, -1):  # oldest -> newest
        try:
            years.append(str(f.inc.columns[y].date()) if hasattr(f.inc.columns[y], "date") else str(f.inc.columns[y]))
        except Exception:
            years.append(f"y-{y}")
        r = _roic(f, y)
        c = _roce(f, y)
        roic.append(rnd(100 * r, 1) if r is not None else None)
        roce.append(rnd(100 * c, 1) if c is not None else None)
    return years, roic, roce


def roic_persistence(f, wacc=0.09, moat=None):
    """AR(1) fade of the ROIC-WACC economic-profit spread -> persistence rho, half-life, empirical
    competitive-advantage period. rho is invariant to the WACC level (a constant shift is absorbed
    by the AR(1) intercept), so CAP is the only WACC-sensitive output. Cross-checks the empirical
    fade against the moat-label reversion valuation.py hard-codes."""
    years, roic, roce = _roic_roce_series(f)
    roic_dec = [r / 100 for r in roic if r is not None]
    n = len(roic_dec)
    out = {
        "years_available": n,
        "wacc_used": wacc,
        "roic_series_pct": roic,
        "roce_series_pct": roce,
        "roic_minus_wacc_pp": [rnd(r - 100 * wacc, 1) for r in roic if r is not None] or None,
    }
    if n < 3:
        out["persistence"] = None
        out["caveat"] = f"only {n} usable ROIC years — cannot fit a fade; supply a longer-history feed"
        return out

    spread = np.array([r - wacc for r in roic_dec])          # oldest -> newest
    x, ynext = spread[:-1], spread[1:]                        # AR(1) pairs
    A = np.column_stack([np.ones(len(x)), x])
    alpha, rho = np.linalg.lstsq(A, ynext, rcond=None)[0]
    s_last = float(spread[-1])
    lam = 1 - rho                                            # annual fade rate
    half_life = (math.log(0.5) / math.log(rho)) if 0 < rho < 1 else None
    cap = None
    if 0 < rho < 1 and abs(s_last) > 0.01:
        cap = math.log(0.01 / abs(s_last)) / math.log(rho)

    # moat-label cross-check: what fade the DCF would assume for each moat width
    moat_rev = getattr(valuation, "MOAT_REVERSION", {})
    ff_rev = getattr(valuation, "FF_REVERSION", 0.38)
    assumed = {m: moat_rev.get(m, ff_rev) for m in ("none", "narrow", "wide")} if moat_rev else {"default": ff_rev}
    label_fade = moat_rev.get(moat, ff_rev) if moat else None

    read = "no clean mean-reversion estimate"
    if 0 < rho < 1:
        durability = ("durable — spread fades slowly" if lam < 0.20 else
                      "moderate fade" if lam < 0.40 else "fast fade — weak persistence")
        read = f"empirical fade ~{lam:.0%}/yr ({durability}); half-life ~{half_life:.1f}y"
        if label_fade is not None:
            gap = "SLOWER than" if lam < label_fade - 0.05 else "FASTER than" if lam > label_fade + 0.05 else "in line with"
            read += f"; history is {gap} the '{moat}' moat label the DCF assumes ({label_fade:.0%}/yr)"
    elif rho >= 1:
        read = "spread not mean-reverting over this window (rho>=1) — either a widening moat or too few points"

    out["persistence"] = {
        "current_spread_pp": rnd(100 * s_last, 1),
        "ar1_rho": rnd(float(rho), 3),
        "annual_fade_pct": rnd(100 * lam, 1),
        "half_life_yrs": rnd(half_life, 1) if half_life else None,
        "empirical_cap_yrs": rnd(cap, 1) if cap else None,
        "n_pairs": int(len(x)),
        "moat_label_fade_assumed": label_fade,
        "dcf_moat_reversion_table": {k: rnd(v, 3) for k, v in assumed.items()},
        "read": read,
    }
    if n < 6:
        out["caveat"] = (f"only {n} ROIC years from yfinance — AR(1) fade is LOW power; treat rho/half-life "
                         "as directional, not a point estimate")
    return out


# ------------------------------ profitability ------------------------------

def _cash_based_op(f):
    """Ball-Gerakos-Linnainmaa-Nikolaev (2016) cash-based operating profitability / assets.
    op = gross profit - SG&A (yf SG&A excludes R&D); subtract WC accrual build (Delta rec + inv - pay)."""
    gp, ta = f.gross_profit(0), f.total_assets(0)
    if gp is None or not ta:
        return None
    sga = f.sga(0) or 0
    op = gp - sga
    d_rec = (f.receivables(0) or 0) - (f.receivables(1) or 0)
    d_inv = (f.inventory(0) or 0) - (f.inventory(1) or 0)
    d_pay = (_payables(f, 0) or 0) - (_payables(f, 1) or 0)
    cash_op = op - d_rec - d_inv + d_pay
    return _safe(cash_op, ta)


def profitability(f):
    """Absolute single-name profitability core against Fundsmith bands + a short margin trend.
    No peer set required (the peer-relative cut lives in quant_factors.profitability_discipline_legs)."""
    gp, ta, rev = f.gross_profit(0), f.total_assets(0), f.revenue(0)
    ni = f.net_income(0)
    gpoa = _safe(gp, ta)
    gmar = _safe(gp, rev)
    roa = _safe(ni, ta)
    roe = _safe(ni, _equity(f, 0))
    op_margin = _safe(_ebit(f, 0), rev)
    cash_op = _cash_based_op(f)
    acc = forensic.accruals(f)  # reuse: CFO/NI, FCF/NI, Sloan accrual ratio
    cfo_ni, fcf_ni = acc.get("cfo_to_ni"), acc.get("fcf_to_ni")
    roce = _roce(f, 0)

    # short gross/operating-margin trend (newest vs oldest available)
    gms = [_safe(f.gross_profit(y), f.revenue(y)) for y in range(f.n_years)]
    gms = [g for g in gms if g is not None]
    gm_trend = ("expanding" if len(gms) >= 2 and gms[0] > gms[-1] * 1.02 else
                "compressing" if len(gms) >= 2 and gms[0] < gms[-1] * 0.98 else
                "stable" if len(gms) >= 2 else None)

    grades = {
        "gross_margin": _grade(100 * gmar if gmar is not None else None, 60, 45),
        "operating_margin": _grade(100 * op_margin if op_margin is not None else None, 20, 12),
        "roce": _grade(100 * roce if roce is not None else None, 25, 15),
        "gp_to_assets": _grade(gpoa, 0.33, 0.20),
        "fcf_conversion": _grade(fcf_ni, 0.90, 0.70),
    }
    avg, verdict = _roll_up([g for g in grades.values() if g])
    return {
        "levels": {
            "gp_to_assets": rnd(gpoa, 3), "cash_based_op_to_assets": rnd(cash_op, 3),
            "gross_margin_pct": rnd(100 * gmar, 1) if gmar is not None else None,
            "operating_margin_pct": rnd(100 * op_margin, 1) if op_margin is not None else None,
            "roa_pct": rnd(100 * roa, 1) if roa is not None else None,
            "roe_pct": rnd(100 * roe, 1) if roe is not None else None,
            "roce_pct": rnd(100 * roce, 1) if roce is not None else None,
            "cfo_to_ni": cfo_ni, "fcf_to_ni": fcf_ni,
        },
        "gross_margin_trend": gm_trend,
        "grades_vs_fundsmith": grades,
        "score": avg, "verdict": verdict,
        "note": "bands: gross margin >=60/45; operating margin >=20/12; ROCE >=25/15; GP/A >=0.33/0.20; "
                "FCF/NI cash conversion >=0.90/0.70 (Fundsmith owner's-manual / Novy-Marx).",
    }


# -------------------------------- stability --------------------------------

def _cv(series):
    xs = [x for x in series if isinstance(x, (int, float))]
    if len(xs) < 3:
        return None, None, None
    mu, sd = float(np.mean(xs)), float(np.std(xs))
    cv = abs(sd / mu) if mu else None
    return rnd(mu, 2), rnd(sd, 2), rnd(cv, 3)


def stability(f):
    """Coefficient of variation of gross/operating margin and ROIC across the cycle — the inverse-moat
    fingerprint (Dichev-Tang 2009: low earnings volatility -> high persistence). Cross-check smoothness
    against accruals so accrual-smoothed earnings are not mistaken for a franchise."""
    gm = [_safe(f.gross_profit(y), f.revenue(y)) for y in range(f.n_years)]
    om = [_safe(_ebit(f, y), f.revenue(y)) for y in range(f.n_years)]
    roic = [_roic(f, y) for y in range(f.n_years)]
    gm = [100 * x for x in gm if x is not None]
    om = [100 * x for x in om if x is not None]
    roic = [100 * x for x in roic if x is not None]

    blocks = {}
    for name, ser in (("gross_margin_pct", gm), ("operating_margin_pct", om), ("roic_pct", roic)):
        mu, sd, cv = _cv(ser)
        band = None
        if cv is not None:
            band = "very stable (moat-like)" if cv < 0.10 else "stable" if cv < 0.25 else "volatile (cyclical/commodity)"
        blocks[name] = {"mean": mu, "stdev": sd, "coef_of_variation": cv, "read": band}

    acc = forensic.accruals(f)
    smoothing_flag = None
    gm_cv = blocks["gross_margin_pct"]["coef_of_variation"]
    if gm_cv is not None and gm_cv < 0.10 and acc.get("flag"):
        smoothing_flag = ("margins look very smooth BUT accruals/cash-conversion are weak — the stability may "
                          "be accrual-managed, not a genuine moat (check forensic.accruals)")
    return {
        "years_available": f.n_years,
        "stability": blocks,
        "accrual_cross_check": {"cfo_to_ni": acc.get("cfo_to_ni"), "accrual_flag": acc.get("flag"),
                                "smoothing_warning": smoothing_flag},
        "caveat": (f"only {f.n_years} years — CV is low power" if f.n_years < 6 else None),
        "note": "CV bands: <0.10 very stable / <0.25 stable / else volatile. Low CV + high mean margin + "
                "slow ROIC fade is the durable-franchise signature.",
    }


# --------------------------------- markup ----------------------------------

def markup(f, wacc=0.09):
    """Production-approach price/marginal-cost markup (De Loecker-Eeckhout-Unger 2020): mu = theta * SALE/COGS,
    theta = COGS cost share = COGS / (COGS + user cost of capital), user cost = (wacc + depr rate) * gross PPE.
    Level is production-function-sensitive — trust the WITHIN-FIRM TREND and peer rank, not the level."""
    def mu_year(y):
        rev, cogs = f.revenue(y), f.cogs(y)
        if not rev or not cogs:
            return None
        gppe = f.gross_ppe(y) or f.net_ppe(y) or 0
        dep = f.depreciation(y) or 0
        dep_rate = _safe(dep, gppe) or 0.10
        user_cost = (wacc + dep_rate) * gppe
        theta = _safe(cogs, cogs + user_cost)
        if theta is None:
            return None
        return theta * (rev / cogs)

    series = [mu_year(y) for y in range(f.n_years)]          # newest-first
    vals = [m for m in series if m is not None]
    trend = None
    if len(vals) >= 2:
        trend = ("rising (widening pricing power)" if vals[0] > vals[-1] * 1.03 else
                 "eroding (narrowing moat)" if vals[0] < vals[-1] * 0.97 else "stable")
    return {
        "years_available": f.n_years,
        "markup_now": rnd(vals[0], 3) if vals else None,
        "markup_series_newest_first": [rnd(m, 3) for m in series],
        "trend": trend,
        "gross_margin_now_pct": rnd(100 * _safe(f.gross_profit(0), f.revenue(0)), 1)
                                 if _safe(f.gross_profit(0), f.revenue(0)) is not None else None,
        "maturity": "promising",
        "caveat": ("LEVEL is model-sensitive (Doraszelski-Jaumandreu) — read the trend/peer-rank, not the "
                   "absolute mu; " + (f"only {f.n_years} yrs" if f.n_years < 6 else "")),
        "note": "mu = theta*(SALE/COGS), theta=COGS/(COGS+user cost of capital), user cost=(wacc+dep rate)*gross PPE.",
    }


# --------------------------------- payout ----------------------------------

def payout(f, info):
    """Net payout yield + FCF coverage + buyback accretion (Boudoukh-Michaely-Richardson-Roberts 2007).
    yfinance cash-flow signs: dividends/buybacks are negative outflows, issuance positive inflow."""
    mc = (info or {}).get("marketCap")
    div = abs(_dividends(f, 0) or 0)
    bb = abs(_buybacks(f, 0) or 0)
    iss = _issuance(f, 0) or 0
    iss_pos = max(iss, 0) if iss and iss > 0 else 0     # only count net issuance (dilution)
    fcf = f.fcf(0)
    total_payout = div + bb
    net_payout = total_payout - iss_pos

    sh0, sh1 = f.shares(0), f.shares(1)
    sh_change = (_safe(sh0, sh1) - 1) if _safe(sh0, sh1) is not None else None
    sbc = abs(_sbc(f, 0) or 0)

    fcf_yield = _safe(fcf, mc)
    accretion = None
    if fcf_yield is not None:
        accretion = ("accretive — buybacks at FCF yield %.1f%% above cost of capital" % (100 * fcf_yield)
                     if bb > 0 and fcf_yield > 0.09 else
                     "dilutive-risk — buybacks while FCF yield %.1f%% is below ~cost of capital (paying up)" % (100 * fcf_yield)
                     if bb > 0 else "no material buyback")
    return {
        "payout_yield_pct": rnd(100 * _safe(total_payout, mc), 2) if _safe(total_payout, mc) is not None else None,
        "net_payout_yield_pct": rnd(100 * _safe(net_payout, mc), 2) if _safe(net_payout, mc) is not None else None,
        "payout_to_fcf": rnd(_safe(total_payout, fcf), 2),
        "fcf_coverage_ok": (_safe(total_payout, fcf) is not None and _safe(total_payout, fcf) < 1.0),
        "net_share_change_pct": rnd(100 * sh_change, 2) if sh_change is not None else None,
        "buybacks": rnd(bb, 0), "sbc": rnd(sbc, 0),
        "buyback_net_of_sbc": rnd(bb - sbc, 0),
        "fcf_yield_pct": rnd(100 * fcf_yield, 2) if fcf_yield is not None else None,
        "accretion_read": accretion,
        "note": "net payout = (dividends + buybacks - net issuance)/mcap; want payout/FCF < 1 (funded from FCF); "
                "share count should FALL net of SBC; buybacks accretive only when FCF yield > cost of capital.",
    }


# ------------------------- QMJ panel (single name) -------------------------

def qmj_panel(f, info, ticker):
    """QMJ four-pillar read for the SUBJECT, scored against Fundsmith absolute bands -> one verdict.
    Reuses credit_tools (Safety), forensic.accruals (Payout/quality), and the profitability core above."""
    prof = profitability(f)
    lv = prof["levels"]

    # --- Profitability pillar ---
    prof_grades = [g for g in prof["grades_vs_fundsmith"].values() if g]
    prof_avg, prof_verdict = _roll_up(prof_grades)

    # --- Growth pillar: change in ROIC / gross margin over the window (oldest->newest) ---
    _, roic_ser, _ = _roic_roce_series(f)
    roic_ser = [r for r in roic_ser if r is not None]
    d_roic = (roic_ser[-1] - roic_ser[0]) if len(roic_ser) >= 2 else None
    gm_now = _safe(f.gross_profit(0), f.revenue(0))
    gm_old = _safe(f.gross_profit(f.n_years - 1), f.revenue(f.n_years - 1))
    d_gm = (100 * (gm_now - gm_old)) if (gm_now is not None and gm_old is not None) else None
    growth_grade = _grade(d_roic, 0.0, -3.0) if d_roic is not None else None  # improving/flat/deteriorating
    growth_avg, growth_verdict = _roll_up([g for g in (growth_grade,) if g])

    # --- Safety pillar: reuse credit_tools distress + leverage + ROE volatility ---
    try:
        cp = credit_tools.credit_profile(ticker)
    except Exception as e:
        cp = {"error": str(e)}
    roe_ser = [_roic(f, y) for y in range(f.n_years)]  # ROIC-vol proxy for earnings-vol safety
    roe_ser = [x for x in roe_ser if x is not None]
    roe_vol = float(np.std([100 * x for x in roe_ser])) if len(roe_ser) >= 3 else None
    ohlson = (cp.get("ohlson_o") or {}) if isinstance(cp, dict) else {}
    safety_grades = [
        _grade(cp.get("net_debt_ebitda"), 1.0, 2.5, higher_better=False) if isinstance(cp, dict) else None,
        _grade(ohlson.get("distress_probability"), 0.10, 0.30, higher_better=False),
        _grade(cp.get("interest_coverage_ebit"), 15, 6) if isinstance(cp, dict) else None,
        _grade(roe_vol, 3.0, 8.0, higher_better=False) if roe_vol is not None else None,
    ]
    safety_avg, safety_verdict = _roll_up([g for g in safety_grades if g])

    # --- Payout pillar ---
    pay = payout(f, info)
    payout_grades = [
        _grade(pay.get("net_payout_yield_pct"), 3.0, 0.5),
        _grade(pay.get("net_share_change_pct"), 0.0, 2.0, higher_better=False),
    ]
    payout_avg, payout_verdict = _roll_up([g for g in payout_grades if g])

    pillars = {
        "profitability": {"score": prof_avg, "verdict": prof_verdict, "grades": prof["grades_vs_fundsmith"]},
        "growth": {"score": growth_avg, "verdict": growth_verdict,
                   "delta_roic_pp": rnd(d_roic, 1), "delta_gross_margin_pp": rnd(d_gm, 1)},
        "safety": {"score": safety_avg, "verdict": safety_verdict,
                   "net_debt_ebitda": cp.get("net_debt_ebitda") if isinstance(cp, dict) else None,
                   "altman_z": cp.get("altman_z") if isinstance(cp, dict) else None,
                   "ohlson_distress_prob": ohlson.get("distress_probability"),
                   "interest_coverage": cp.get("interest_coverage_ebit") if isinstance(cp, dict) else None,
                   "roic_stdev_pp": rnd(roe_vol, 1)},
        "payout": {"score": payout_avg, "verdict": payout_verdict,
                   "net_payout_yield_pct": pay.get("net_payout_yield_pct"),
                   "net_share_change_pct": pay.get("net_share_change_pct")},
    }
    overall_avg, overall_verdict = _roll_up(
        [p["verdict"].lower() if p["verdict"] in ("PASS",) else None for p in pillars.values()])
    # verdict roll-up on the pillar scores directly (more informative than nested strings)
    pillar_scores = [p["score"] for p in pillars.values() if isinstance(p["score"], (int, float))]
    mean_score = float(np.mean(pillar_scores)) if pillar_scores else None
    verdict = None
    if mean_score is not None:
        verdict = "PASS (wonderful business)" if mean_score >= 1.5 else \
                  "BORDERLINE" if mean_score >= 0.9 else "FAIL (not a quality compounder)"
    return {
        "ticker": ticker,
        "quality_verdict": verdict,
        "composite_score_0_2": rnd(mean_score, 2),
        "pillars": pillars,
        "levels": lv,
        "years_available": f.n_years,
        "note": "QMJ pillars (Asness-Frazzini-Pedersen 2019) scored on Fundsmith absolute bands; a single "
                "subject-level verdict, not a peer z-score. Use `qmj TICKER PEERS` for the cross-sectional cut.",
    }


# ------------------------- QMJ peer z-composite ----------------------------

def _profit_legs(ticker):
    """Raw single-name legs for the peer z-composite (higher = better quality)."""
    try:
        f = forensic.Filings(ticker)
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}
    ni = f.net_income(0)
    return {
        "ticker": ticker,
        "gp_to_assets": _safe(f.gross_profit(0), f.total_assets(0)),
        "cash_op_to_assets": _cash_based_op(f),
        "roe": _safe(ni, _equity(f, 0)),
        "roce": _roce(f, 0),
        "neg_leverage": -(_safe(_total_debt(f, 0), f.total_assets(0)) or 0),
        "neg_net_issuance": -((_safe(f.shares(0), f.shares(1)) - 1) if _safe(f.shares(0), f.shares(1)) is not None else 0),
    }


def qmj_peers(ticker, peers):
    """Peer-relative QMJ z-composite over the profitability/safety/payout legs."""
    names = [ticker] + peers
    legs = {t: _profit_legs(t) for t in names}
    keys = ["gp_to_assets", "cash_op_to_assets", "roe", "roce", "neg_leverage", "neg_net_issuance"]
    z = {t: {} for t in names}
    for k in keys:
        vals = {t: legs[t].get(k) for t in names if isinstance(legs[t].get(k), (int, float))}
        if len(vals) >= 3:
            arr = np.array(list(vals.values()), dtype=float)
            mu, sd = arr.mean(), arr.std() or 1
            for t, v in vals.items():
                z[t][k + "_z"] = rnd((v - mu) / sd, 2)
    for t in names:
        comp = [v for v in z[t].values() if isinstance(v, (int, float))]
        z[t]["quality_composite_z"] = rnd(float(np.mean(comp)), 2) if comp else None
    n = len(names)
    return {
        "ticker": ticker,
        "n_names": n,
        "small_sample_caveat": ("peer set < 5 — z-scores coarse, directional only" if n < 5 else None),
        "legs": legs,
        "zscores_vs_peers": z,
        "subject_quality_z": z.get(ticker, {}).get("quality_composite_z"),
        "note": "composite = mean z of GP/A, cash-based OP, ROE, ROCE, -leverage, -net issuance (QMJ direction).",
    }


# ---------------------------------- all ------------------------------------

def all_quality(ticker, peers, wacc=0.09, moat=None):
    f = forensic.Filings(ticker)
    info = yf.Ticker(ticker).info or {}
    out = {"ticker": ticker, "years_available": f.n_years}
    def _try(fn, label):
        try:
            return fn()
        except Exception as e:
            return {"error": f"{label}: {e}"}
    panel = _try(lambda: qmj_panel(f, info, ticker), "panel")
    prof = _try(lambda: profitability(f), "profitability")
    persist = _try(lambda: roic_persistence(f, wacc, moat), "roic_persistence")
    out["panel"] = panel
    out["profitability"] = prof
    out["roic_persistence"] = persist
    out["stability"] = _try(lambda: stability(f), "stability")
    out["markup"] = _try(lambda: markup(f, wacc), "markup")
    out["payout"] = _try(lambda: payout(f, info), "payout")
    if peers:
        out["qmj_peers"] = _try(lambda: qmj_peers(ticker, peers), "qmj_peers")

    # cheap-safe-quality signature (Buffett's Alpha single-name shortcut): quality + safety + cheapness
    q_verdict = panel.get("quality_verdict") if isinstance(panel, dict) else None
    beta = info.get("beta")
    fcf_yield = _safe(info.get("freeCashflow"), info.get("marketCap"))
    safety = (panel.get("pillars", {}).get("safety", {}) if isinstance(panel, dict) else {})
    out["cheap_safe_quality_signature"] = {
        "quality": q_verdict,
        "safety_low_beta": rnd(beta, 2),
        "safety_leverage_net_debt_ebitda": safety.get("net_debt_ebitda"),
        "cheapness_fcf_yield_pct": rnd(100 * fcf_yield, 2) if fcf_yield is not None else None,
        "read": "Buffett's-Alpha shortcut: wants PASS quality + beta<=1 + low leverage + a real FCF yield; "
                "full QMJ/BAB factor regression needs external factor series (deferred).",
    }
    # one-line read
    persist_read = persist.get("persistence", {}).get("read") if isinstance(persist, dict) and persist.get("persistence") else None
    out["one_line"] = f"{ticker}: quality={q_verdict}; {persist_read or 'ROIC fade n/a'}"
    return out


_CMDS = {"panel", "qmj", "roic_persistence", "profitability", "stability", "markup", "payout", "all"}

def main():
    p = argparse.ArgumentParser(description="Quality screens — QMJ/Fundsmith composite, ROIC persistence, "
                                            "profitability, stability, markup, payout")
    p.add_argument("cmd", choices=sorted(_CMDS))
    p.add_argument("ticker")
    p.add_argument("peers", nargs="?", default="", help="comma-separated peers (for qmj / all)")
    p.add_argument("--wacc", type=float, default=0.09, help="WACC for ROIC spread / markup user-cost (default 0.09)")
    p.add_argument("--moat", choices=["none", "narrow", "wide"], default=None,
                   help="moat label to cross-check empirical ROIC fade against the DCF assumption")
    a = p.parse_args()
    peers = [x.strip().upper() for x in a.peers.split(",") if x.strip()]

    if a.cmd == "all":
        jprint(all_quality(a.ticker, peers, a.wacc, a.moat)); sys.exit(0)
    if a.cmd == "qmj":
        if not peers:
            jprint({"error": "qmj needs a peer list, e.g. quality_tools.py qmj NVDA AMD,AVGO,INTC"}); sys.exit(1)
        jprint(qmj_peers(a.ticker, peers)); sys.exit(0)

    try:
        f = forensic.Filings(a.ticker)
    except Exception as e:
        jprint({"error": f"could not load statements for {a.ticker}: {e}"}); sys.exit(1)
    if f.n_years < 2:
        jprint({"error": f"need >=2 years of statements; got {f.n_years}"}); sys.exit(1)

    if a.cmd == "panel":
        jprint(qmj_panel(f, yf.Ticker(a.ticker).info or {}, a.ticker))
    elif a.cmd == "roic_persistence":
        jprint(roic_persistence(f, a.wacc, a.moat))
    elif a.cmd == "profitability":
        jprint({"ticker": a.ticker, "profitability": profitability(f)})
    elif a.cmd == "stability":
        jprint({"ticker": a.ticker, "stability": stability(f)})
    elif a.cmd == "markup":
        jprint({"ticker": a.ticker, "markup": markup(f, a.wacc)})
    elif a.cmd == "payout":
        jprint({"ticker": a.ticker, "payout": payout(f, yf.Ticker(a.ticker).info or {})})


if __name__ == "__main__":
    main_guard(main)
