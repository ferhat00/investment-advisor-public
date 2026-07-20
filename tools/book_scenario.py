"""Correlated factor-shock scenario engine on the live book (CRO / tail-risk / global-macro).

Promotes the hand-rolled qr_scenario.py pattern from portfolio runs into a tool. Each
holding's daily returns are regressed on factor-proxy ETF returns (multifactor OLS via
numpy lstsq); scenarios are applied as a mean shift to a multivariate-normal factor draw
calibrated on the historical factor covariance (scenario-conditioned Monte Carlo, the
standard stress-testing construction, cf. Kupiec 1998 "Stress Testing in a VaR Framework"),
plus per-name idiosyncratic residual draws -> full NAV P&L distribution per scenario.

Factor proxies: SPY (market), QQQ-minus-SPY (AI / duration-growth spread), TLT (rates,
long duration), UUP (USD), GLD (gold), USO (oil).

Scenario shock table (total factor move over the horizon):
  riskoff : SPY -15%, QQQ-SPY -8%, TLT +5%, UUP +4%, GLD +3%
  rates   : TLT -8%,  SPY -5%
  usd     : UUP +6%,  SPY -3%
  ai      : QQQ-SPY -20%, SPY -8%
  oil     : USO +25%, SPY -4%

CLI:
  python book_scenario.py --book <portfolio_context.json> [--period 2y] [--sims 5000]
                          [--scenario all|riskoff|rates|usd|ai|oil] [--horizon 21]

Book schema (defensive): accepts portfolio_context.json ({nav_gbp, cash_gbp, positions:
[{ticker, gbp, yahoo?, cluster?}]}) or book_exposures.json ({nav, cash, rows:[...]}).
Names with no resolvable price history degrade to an `unresolved` list, never a crash.
"""
import argparse
import numpy as np
import pandas as pd
from common import yf_history, jprint, rnd, safe, main_guard

FACTORS = ["SPY", "QQQSPY", "TLT", "UUP", "GLD", "USO"]  # QQQSPY = QQQ ret minus SPY ret

# scenario -> total factor move over the horizon (decimal); unlisted factors shock 0
SCENARIOS = {
    "riskoff": {"SPY": -0.15, "QQQSPY": -0.08, "TLT": 0.05, "UUP": 0.04, "GLD": 0.03},
    "rates":   {"TLT": -0.08, "SPY": -0.05},
    "usd":     {"UUP": 0.06, "SPY": -0.03},
    "ai":      {"QQQSPY": -0.20, "SPY": -0.08},
    "oil":     {"USO": 0.25, "SPY": -0.04},
}

# Non-identity Yahoo symbols seen in past books that ship without a `yahoo` field
# (same map qr_scenario.py hand-rolled). A row's own `yahoo` field always wins.
FALLBACK_YAHOO = {"RR": "RR.L", "ENR": "ENR.DE", "RHM": "RHM.DE", "SU": "SU.PA",
                  "PLS": "PLS.AX", "HY9H": "HY9H.SG"}

MIN_OBS = 120  # minimum overlapping daily obs for a usable beta regression


def parse_book(path):
    """Defensive book parse -> (nav, cash, [{ticker, yahoo, gbp, cluster}])."""
    import json
    with open(path) as f:
        book = json.load(f)
    nav = book.get("nav") or book.get("nav_gbp")
    cash = book.get("cash") or book.get("cash_gbp") or 0.0
    rows = book.get("positions") or book.get("rows") or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        tk = r.get("ticker") or r.get("symbol")
        gbp = r.get("gbp") or r.get("mv_gbp") or r.get("value_gbp")
        if not tk or gbp is None:
            continue
        out.append({"ticker": tk,
                    "yahoo": r.get("yahoo") or FALLBACK_YAHOO.get(tk, tk),
                    "gbp": float(gbp),
                    "cluster": r.get("cluster")})
    if nav is None:  # last resort: reconstruct NAV from cash + positions
        nav = float(cash) + sum(r["gbp"] for r in out)
    if not out:
        raise ValueError(f"no positions parsed from {path}")
    return float(nav), float(cash), out


def factor_panel(period):
    """Daily factor-return DataFrame with columns FACTORS (QQQSPY spread built in)."""
    rets = {}
    for tk in ["SPY", "QQQ", "TLT", "UUP", "GLD", "USO"]:
        rets[tk] = yf_history(tk, period=period)["Close"].pct_change()
    df = pd.DataFrame(rets).dropna()
    df["QQQSPY"] = df["QQQ"] - df["SPY"]
    return df[FACTORS]


def regress(name_ret, F):
    """OLS of a name's daily returns on the factor panel. Returns (betas, resid_vol_daily,
    r2, n_obs) or None if overlap is too short."""
    panel = pd.concat([name_ret.rename("y"), F], axis=1).dropna()
    if len(panel) < MIN_OBS:
        return None
    y = panel["y"].values
    X = np.column_stack([np.ones(len(panel)), panel[FACTORS].values])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    return beta[1:], float(resid.std()), r2, len(panel)


def simulate(scenario_shocks, B, resid_vols, weights, F, sims, horizon, rng):
    """Scenario-conditioned Monte Carlo. Factor draws ~ N(shock, hist_daily_cov*horizon)
    via cholesky; idiosyncratic draws ~ N(0, resid_vol^2*horizon) per name.
    Returns (nav_pnl_pct array (sims,), per-name mean pnl %NAV (n,))."""
    k = len(FACTORS)
    mu = np.array([scenario_shocks.get(f, 0.0) for f in FACTORS])
    cov = F.cov().values * horizon
    L = np.linalg.cholesky(cov + 1e-12 * np.eye(k))
    f_draw = mu + rng.standard_normal((sims, k)) @ L.T                 # (sims, k)
    eps = rng.standard_normal((sims, len(weights))) * (resid_vols * np.sqrt(horizon))
    name_ret = f_draw @ B.T + eps                                      # (sims, n)
    contrib = name_ret * weights                                       # % of NAV per name
    return contrib.sum(axis=1), contrib.mean(axis=0)


class _Parser(argparse.ArgumentParser):
    """Raise instead of printing usage+exit(2), so main_guard emits one JSON error."""
    def error(self, message):
        raise ValueError(f"{message} | {self.format_usage().strip()}")


def main():
    ap = _Parser(description="Correlated factor-shock scenarios on the book")
    ap.add_argument("--book", required=True, help="portfolio_context.json / book_exposures.json")
    ap.add_argument("--period", default="2y", help="history window for betas/cov (default 2y)")
    ap.add_argument("--sims", type=int, default=5000)
    ap.add_argument("--scenario", default="all", choices=["all"] + list(SCENARIOS))
    ap.add_argument("--horizon", type=int, default=21, help="trading days over which the shock plays out")
    a = ap.parse_args()

    nav, cash, rows = parse_book(a.book)
    F = factor_panel(a.period)
    errs = []

    resolved, unresolved = [], []
    for r in rows:
        px = safe(lambda: yf_history(r["yahoo"], period=a.period)["Close"], r["ticker"], errs)
        fit = regress(px.pct_change().dropna(), F) if px is not None else None
        if fit is None:
            unresolved.append({"ticker": r["ticker"], "yahoo": r["yahoo"],
                               "weight_pct_nav": rnd(100 * r["gbp"] / nav),
                               "reason": "no price data" if px is None else f"<{MIN_OBS} overlapping obs"})
            continue
        betas, rv, r2, n = fit
        resolved.append({**r, "betas": betas, "resid_vol_d": rv, "r2": r2, "n_obs": n})

    if not resolved:
        jprint({"error": "no holdings resolved to price history", "unresolved": unresolved,
                "fetch_errors": errs})
        return

    B = np.array([h["betas"] for h in resolved])                       # (n, k)
    rv = np.array([h["resid_vol_d"] for h in resolved])
    w = np.array([h["gbp"] / nav for h in resolved])                   # NAV weights (cash implicit)
    rng = np.random.default_rng(42)

    scen_names = list(SCENARIOS) if a.scenario == "all" else [a.scenario]
    scen_out = {}
    for s in scen_names:
        pnl, mean_contrib = simulate(SCENARIOS[s], B, rv, w, F, a.sims, a.horizon, rng)
        order = np.argsort(mean_contrib)[:5]
        scen_out[s] = {
            "shocks_pct": {f: rnd(100 * v) for f, v in SCENARIOS[s].items()},
            "navImpactPct": {"mean": rnd(100 * pnl.mean()),
                             "p5": rnd(100 * np.percentile(pnl, 5)),
                             "p50": rnd(100 * np.percentile(pnl, 50)),
                             "p95": rnd(100 * np.percentile(pnl, 95))},
            "worstNames": [{"ticker": resolved[i]["ticker"],
                            "cluster": resolved[i]["cluster"],
                            "weight_pct_nav": rnd(100 * w[i]),
                            "mean_pnl_pct_nav": rnd(100 * mean_contrib[i])}
                           for i in order],
        }

    beta_summary = [{"ticker": h["ticker"], "cluster": h["cluster"],
                     "weight_pct_nav": rnd(100 * h["gbp"] / nav),
                     **{f"beta_{f.lower()}": rnd(b, 2) for f, b in zip(FACTORS, h["betas"])},
                     "resid_vol_ann_pct": rnd(100 * h["resid_vol_d"] * np.sqrt(252)),
                     "r2": rnd(h["r2"], 2), "n_obs": h["n_obs"]}
                    for h in sorted(resolved, key=lambda x: -x["gbp"])]

    # book-level factor betas ($-weighted, % of NAV per 100% factor move)
    book_beta = {f"beta_{f.lower()}": rnd(float((w * B[:, i]).sum()), 2)
                 for i, f in enumerate(FACTORS)}

    jprint({
        "book": a.book, "period": a.period, "sims": a.sims, "horizon_days": a.horizon,
        "nav": rnd(nav, 0), "cash_pct_nav": rnd(100 * cash / nav),
        "resolved_names": len(resolved),
        "resolved_weight_pct_nav": rnd(100 * w.sum()),
        "unresolved": unresolved,
        "factors": {"SPY": "market", "QQQSPY": "QQQ-minus-SPY (AI/duration-growth)",
                    "TLT": "rates (long duration)", "UUP": "USD", "GLD": "gold", "USO": "oil"},
        "book_betas": book_beta,
        "scenarios": scen_out,
        "betaSummary": beta_summary,
        "fetch_errors": errs or None,
        "note": "navImpactPct is P&L as % of NAV over the horizon; cash held flat; "
                "unresolved names contribute zero (their weight is a stated gap).",
    })


if __name__ == "__main__":
    main_guard(main)
