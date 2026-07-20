"""Base-rate / reference-class check on DCF growth assumptions — the outside view.

The single most abused DCF input is the multi-year revenue growth rate, and analysts systematically
forecast a tighter, more optimistic distribution than firms actually deliver (Mauboussin "Base Rate
Book", 2016; Chan-Karceski-Lakonishok 2003 show near-zero persistence in long-run growth). This tool
maps a company to its starting-size cohort and reports where a proposed growth assumption sits in the
empirical distribution of realised forward sales CAGR — so a number can be checked against the outside
view, not just defended from the inside.

Bundled reference table = a representative encoding of the published Base Rate Book / CKL sales-growth
distributions by starting-revenue bucket. It ships with the tool (no network needed for the table).

CLI:
  python base_rates.py check NVDA --growth 0.25 --horizon 3
  python base_rates.py table [--horizon 3|5]
"""
import argparse
from common import jprint, rnd, main_guard
import yfinance as yf

# Starting-revenue buckets (USD). Growth fades with size — even top-decile large caps rarely
# sustain the CAGRs small caps can. Percentiles are forward sales CAGR in %.
BUCKETS = [
    ("<$500M", 0, 0.5e9),
    ("$500M-2.5B", 0.5e9, 2.5e9),
    ("$2.5B-7.5B", 2.5e9, 7.5e9),
    ("$7.5B-25B", 7.5e9, 25e9),
    ("$25B-75B", 25e9, 75e9),
    (">$75B", 75e9, 1e15),
]

# {horizon_years: {bucket_label: [P10, P25, P50, P75, P90]}}  (forward sales CAGR, %)
BASE_RATES = {
    3: {
        "<$500M":     [-8, 0, 8, 18, 32],
        "$500M-2.5B": [-6, 2, 9, 17, 28],
        "$2.5B-7.5B": [-4, 2, 8, 14, 22],
        "$7.5B-25B":  [-3, 2, 7, 12, 18],
        "$25B-75B":   [-2, 1, 5, 9, 14],
        ">$75B":      [-3, 0, 4, 8, 12],
    },
    5: {
        "<$500M":     [-6, 1, 8, 16, 27],
        "$500M-2.5B": [-4, 2, 8, 15, 24],
        "$2.5B-7.5B": [-3, 2, 7, 12, 19],
        "$7.5B-25B":  [-2, 2, 6, 10, 15],
        "$25B-75B":   [-1, 1, 5, 8, 12],
        ">$75B":      [-2, 1, 4, 7, 10],
    },
}
_PCTS = [10, 25, 50, 75, 90]
SOURCE = ("Representative encoding of Mauboussin 'The Base Rate Book' (Credit Suisse, 2016) and "
          "Chan-Karceski-Lakonishok (2003, JF) sales-growth base rates. Directional reference, not a live panel.")


def _bucket(revenue):
    for label, lo, hi in BUCKETS:
        if lo <= revenue < hi:
            return label
    return BUCKETS[-1][0]


def _percentile_of(value_pct, breaks):
    """Piecewise-linear percentile of value_pct against the [P10..P90] breakpoints."""
    if value_pct <= breaks[0]:
        return max(0.0, 10 * value_pct / breaks[0]) if breaks[0] else 5.0
    if value_pct >= breaks[-1]:
        return 95.0
    for i in range(len(breaks) - 1):
        if breaks[i] <= value_pct <= breaks[i + 1]:
            frac = (value_pct - breaks[i]) / (breaks[i + 1] - breaks[i]) if breaks[i + 1] != breaks[i] else 0
            return _PCTS[i] + frac * (_PCTS[i + 1] - _PCTS[i])
    return 50.0


def check(ticker, growth, horizon):
    horizon = 5 if horizon >= 5 else 3
    info = yf.Ticker(ticker).info or {}
    rev = info.get("totalRevenue")
    if not rev:
        return {"ticker": ticker, "error": "no revenue from yfinance to size the cohort"}
    bucket = _bucket(rev)
    breaks = BASE_RATES[horizon][bucket]
    g_pct = 100 * growth
    pctile = _percentile_of(g_pct, breaks)
    if pctile >= 90:
        verdict, flag = "EXTREME - above the 90th percentile of the cohort; almost no firm sustains this", True
    elif pctile >= 75:
        verdict, flag = "AGGRESSIVE - top quartile of the cohort; requires an explicit above-base-rate reason", True
    elif pctile <= 25:
        verdict, flag = "CONSERVATIVE - bottom quartile of the cohort", False
    else:
        verdict, flag = "IN LINE with the cohort's central range", False
    # regress the point toward the reference-class median (shrinkage toward the outside view)
    shrunk = 0.5 * g_pct + 0.5 * breaks[2]
    return {
        "ticker": ticker, "revenue": rev, "size_bucket": bucket, "horizon_years": horizon,
        "assumed_growth_pct": rnd(g_pct, 1),
        "cohort_distribution_pct": dict(zip(["P10", "P25", "P50", "P75", "P90"], breaks)),
        "assumption_percentile": rnd(pctile, 0),
        "verdict": verdict, "flag": flag,
        "reference_class_median_pct": breaks[2],
        "shrunk_toward_outside_view_pct": rnd(shrunk, 1),
        "source": SOURCE,
        "note": "if flagged, either lower the assumption or write down the specific reason this firm beats its base rate",
    }


def table(horizon):
    horizon = 5 if horizon >= 5 else 3
    return {"horizon_years": horizon, "percentiles": _PCTS,
            "forward_sales_cagr_pct_by_size": BASE_RATES[horizon], "source": SOURCE}


def main():
    p = argparse.ArgumentParser(description="Growth base-rate / reference-class check")
    p.add_argument("cmd", choices=["check", "table"])
    p.add_argument("ticker", nargs="?", default="")
    p.add_argument("--growth", type=float, default=0.15, help="assumed forward sales CAGR, decimal (0.20 = 20 pct)")
    p.add_argument("--horizon", type=int, default=3)
    a = p.parse_args()
    if a.cmd == "check":
        if not a.ticker:
            jprint({"error": "check needs a ticker"})
        else:
            jprint(check(a.ticker, a.growth, a.horizon))
    else:
        jprint(table(a.horizon))


if __name__ == "__main__":
    main_guard(main)
