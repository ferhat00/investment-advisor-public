"""Dividend quality / income analytics: TTM yield, growth streak, CAGR, payout vs EPS
and vs FCF, FCF coverage, and a 0-4 cut-risk score (payout/coverage/leverage/growth flags,
in the spirit of the S&P dividend-cut screens). Consumers: income, wrapper-tax.

CLI:
  python dividend_analysis.py <TICKER> [--cost <your_cost_basis>]
"""
import sys
import pandas as pd
import yfinance as yf
from common import jprint, rnd, safe, stmt_val, main_guard


# yfinance row-label aliases (same drift-tolerant idiom as credit_tools._row)
_NI = ["Net Income", "Net Income Common Stockholders"]
_OCF = ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"]
_CAPEX = ["Capital Expenditure", "Purchase Of PPE", "Net PPE Purchase And Sale"]
_DIVPAID = ["Cash Dividends Paid", "Common Stock Dividend Paid", "Payment Of Dividends",
            "Dividends Paid"]
_EBITDA_ROW = ["EBITDA", "Normalized EBITDA"]


def _cagr(first, last, years):
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1.0 / years) - 1.0


def _annual_dividends(div):
    """Calendar-year dividend-per-share sums, complete years only (current year excluded)."""
    idx = div.index.tz_localize(None) if div.index.tz is not None else div.index
    d = pd.Series(div.values, index=idx)
    ann = d.groupby(d.index.year).sum()
    cur_year = pd.Timestamp.now().year
    return ann[ann.index < cur_year]


def _streaks(ann):
    """(rising streak, no-cut streak) in consecutive complete calendar years, ending at the
    most recent complete year. Requires year continuity (a skipped year breaks the streak)."""
    if len(ann) < 2:
        return 0, 0
    years, vals = list(ann.index), list(ann.values)
    rising, nocut = 0, 0
    rising_alive, nocut_alive = True, True
    for i in range(len(years) - 1, 0, -1):
        if years[i] - years[i - 1] != 1:
            break
        if rising_alive and vals[i] > vals[i - 1]:
            rising += 1
        else:
            rising_alive = False
        if nocut_alive and vals[i] >= vals[i - 1]:
            nocut += 1
        else:
            nocut_alive = False
        if not nocut_alive:
            break
    return rising, nocut


def analyze(ticker, cost=None):
    t = yf.Ticker(ticker)
    div = t.dividends
    if div is None or len(div) == 0:
        return {"ticker": ticker, "dividend": None, "note": "no dividend history"}

    gaps = []
    info = safe(lambda: t.info or {}, "info", gaps) or {}
    inc = safe(lambda: t.income_stmt, "income_stmt", gaps)
    cf = safe(lambda: t.cashflow, "cashflow", gaps)
    bal = safe(lambda: t.balance_sheet, "balance_sheet", gaps)

    idx = div.index.tz_localize(None) if div.index.tz is not None else div.index
    d = pd.Series(div.values, index=idx)
    now = pd.Timestamp.now()
    ttm_div = float(d[d.index > now - pd.Timedelta(days=365)].sum())

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price is None:
        price = safe(lambda: float(t.history(period="5d")["Close"].iloc[-1]), "price", gaps)
    yield_pct = rnd(100 * ttm_div / price, 2) if (price and ttm_div) else None

    # --- growth: complete calendar years only ---
    ann = _annual_dividends(d)
    rising_streak, nocut_streak = _streaks(ann)
    last_yr = int(ann.index[-1]) if len(ann) else None
    cagr = {}
    for n in (3, 5, 10):
        if len(ann) > n:
            cagr[f"{n}y_pct"] = rnd(100 * _cagr(float(ann.iloc[-1 - n]), float(ann.iloc[-1]), n), 1)
        else:
            cagr[f"{n}y_pct"] = None
    # cut/negative growth inside the last 3 complete years
    recent = ann.tail(4)
    recent_cut = bool(len(recent) >= 2 and any(
        recent.values[i] < recent.values[i - 1] for i in range(1, len(recent))))

    # --- payout ratios ---
    ni = stmt_val(inc, _NI)
    ocf = stmt_val(cf, _OCF)
    capex = stmt_val(cf, _CAPEX)  # yfinance reports capex negative
    div_paid = stmt_val(cf, _DIVPAID)  # negative outflow
    div_paid_abs = abs(div_paid) if div_paid is not None else None
    fcf = (ocf + capex) if (ocf is not None and capex is not None) else None
    if fcf is None:
        gaps.append("fcf: OCF or capex missing")

    eps = info.get("trailingEps")
    dps_annual = float(ann.iloc[-1]) if len(ann) else ttm_div
    payout_eps = None
    if eps and eps > 0:
        payout_eps = rnd(ttm_div / eps, 2)
    elif ni and ni > 0 and div_paid_abs:
        payout_eps = rnd(div_paid_abs / ni, 2)
    else:
        gaps.append("payout_eps: EPS/NI unavailable or non-positive")

    payout_fcf = rnd(div_paid_abs / fcf, 2) if (fcf and fcf > 0 and div_paid_abs) else None
    if payout_fcf is None:
        gaps.append("payout_fcf: FCF non-positive or dividends-paid row missing")
    coverage = rnd(fcf / div_paid_abs, 2) if (fcf is not None and div_paid_abs) else None

    # --- leverage (credit_tools alias approach) ---
    debt = info.get("totalDebt") or stmt_val(bal, ["Total Debt"]) or 0
    cash = info.get("totalCash") or stmt_val(bal, ["Cash And Cash Equivalents"]) or 0
    ebitda = info.get("ebitda") or stmt_val(inc, _EBITDA_ROW)
    nd_ebitda = rnd((debt - cash) / ebitda, 2) if ebitda else None
    if nd_ebitda is None:
        gaps.append("net_debt_ebitda: EBITDA unavailable")

    # --- cut-risk score 0-4 (unknown inputs score 0, stated in gaps) ---
    flags = {
        "payout_fcf_gt_0.8": bool(payout_fcf is not None and payout_fcf > 0.8),
        "fcf_coverage_lt_1.2": bool(coverage is not None and coverage < 1.2),
        "net_debt_ebitda_gt_3": bool(nd_ebitda is not None and nd_ebitda > 3),
        "growth_negative_or_cut_last_3y": bool(recent_cut or (cagr.get("3y_pct") is not None
                                                              and cagr["3y_pct"] < 0)),
    }
    score = sum(flags.values())
    verdict = "SAFE" if score == 0 else "WATCH" if score <= 2 else "AT-RISK"

    out = {
        "ticker": ticker,
        "price": rnd(price, 2),
        "dividend": {
            "ttm_per_share": rnd(ttm_div, 4),
            "yield_pct": yield_pct,
            "last_complete_year": last_yr,
            "annual_dps_last_complete_year": rnd(dps_annual, 4),
            "growth_streak_years": rising_streak,
            "years_without_cut": nocut_streak,
            "cagr": cagr,
        },
        "payout": {
            "payout_vs_eps": payout_eps,
            "payout_vs_fcf": payout_fcf,
            "fcf_coverage_x": coverage,
            "fcf": rnd(fcf, 0),
            "dividends_paid": rnd(div_paid_abs, 0),
        },
        "leverage": {"net_debt_ebitda": nd_ebitda},
        "cut_risk": {"score_0_4": score, "flags": flags, "verdict": verdict},
    }
    if cost is not None:
        out["yield_on_cost_pct"] = rnd(100 * ttm_div / cost, 2) if cost > 0 else None
    if gaps:
        out["gaps"] = gaps
    return out


def main():
    args = sys.argv[1:]
    ticker = args[0]
    cost = None
    if "--cost" in args:
        cost = float(args[args.index("--cost") + 1])
    jprint(analyze(ticker, cost))


if __name__ == "__main__":
    main_guard(main)
