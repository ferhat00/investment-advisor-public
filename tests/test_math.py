"""Golden tests for the pure-numpy kernels — the functions where a silent regression
produces confidently wrong committee inputs. Fully offline; no network, no yfinance.

Run: python tests/test_math.py   (exit 0 = all pass; prints one line per test)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import common  # noqa: E402
import decision_scoring  # noqa: E402
import risk_sizing  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"{status:4s}  {name}  {detail}")
    if not cond:
        FAILURES.append(name)


# --- common.py statistics -----------------------------------------------------

def test_norm_cdf_pdf():
    check("norm_cdf(0)=0.5", abs(common._norm_cdf(0.0) - 0.5) < 1e-12)
    check("norm_cdf(1.96)~0.975", abs(common._norm_cdf(1.96) - 0.9750021) < 1e-4)
    check("norm_pdf(0)~0.3989", abs(common._norm_pdf(0.0) - 0.3989423) < 1e-6)


def test_zscore_last():
    s = [1.0] * 30 + [1.0]
    check("zscore constant->None", common.zscore_last(s) is None)
    s = list(range(100))  # last obs vs trailing window
    z = common.zscore_last(s)
    check("zscore monotone>1.5", z is not None and z > 1.5, f"z={z:.3f}")


def test_realized_vol_ann():
    rng = np.random.default_rng(7)
    import pandas as pd
    r = pd.Series(rng.normal(0, 0.01, 5000))
    v = common.realized_vol_ann(r)
    check("realized_vol ~ 0.01*sqrt(252)", abs(v - 0.01 * np.sqrt(252)) < 0.01, f"v={v:.4f}")


def test_probit_fit():
    rng = np.random.default_rng(42)
    n = 4000
    x = rng.normal(0, 1, n)
    true_b0, true_b1 = -0.3, 1.2
    p = np.array([common._norm_cdf(true_b0 + true_b1 * xi) for xi in x])
    y = (rng.uniform(0, 1, n) < p).astype(float)
    b = common.probit_fit(y, x)
    check("probit b0", abs(b[0] - true_b0) < 0.15, f"b0={b[0]:.3f} vs {true_b0}")
    check("probit b1", abs(b[1] - true_b1) < 0.15, f"b1={b[1]:.3f} vs {true_b1}")


def test_quantile_reg():
    rng = np.random.default_rng(43)
    n = 3000
    x = rng.normal(0, 1, n)
    y = 2.0 + 3.0 * x + rng.normal(0, 0.5, n)
    b = common.quantile_reg(y, x, tau=0.5)
    check("quantreg intercept", abs(b[0] - 2.0) < 0.1, f"b0={b[0]:.3f}")
    check("quantreg slope", abs(b[1] - 3.0) < 0.1, f"b1={b[1]:.3f}")


def test_parse_list():
    check("parse_list basic", common.parse_list(" a, b,,c ") == ["a", "b", "c"])
    check("parse_list none", common.parse_list(None) == [])


# --- risk_sizing tail kernels ---------------------------------------------------

def test_gpd_pwm():
    rng = np.random.default_rng(11)
    xi_true, beta_true = 0.3, 1.0
    u = rng.uniform(0, 1, 4000)
    sample = beta_true / xi_true * ((1 - u) ** (-xi_true) - 1)  # GPD inverse-CDF
    xi, beta = risk_sizing._gpd_pwm(sample)
    check("gpd xi", xi is not None and abs(xi - xi_true) < 0.08, f"xi={xi:.3f} vs {xi_true}")
    check("gpd beta", beta is not None and abs(beta - beta_true) < 0.15, f"beta={beta:.3f}")


def test_hill():
    rng = np.random.default_rng(12)
    alpha_true = 3.0  # Pareto tail alpha -> xi = 1/3
    sample = (1 - rng.uniform(0, 1, 20000)) ** (-1 / alpha_true)
    xi = risk_sizing._hill(sample, k=1000)
    check("hill xi~1/3", xi is not None and abs(xi - 1 / 3) < 0.05, f"xi={xi:.3f}")


def test_pot_var_es():
    # xi=0 branch: VaR = u - beta*ln((n/nu)(1-p)); ES defined and > VaR
    var99, es99 = risk_sizing._pot_var_es(u=2.0, xi=1e-9, beta=1.0, n=1000, nu=50, p=0.99)
    var995, _ = risk_sizing._pot_var_es(u=2.0, xi=1e-9, beta=1.0, n=1000, nu=50, p=0.995)
    check("pot var>u", var99 is not None and var99 > 2.0, f"var99={var99:.3f}")
    check("pot monotone in p", var995 > var99)
    check("pot es>var", es99 is not None and es99 > var99, f"es={es99:.3f}")


# --- decision_scoring parsers ----------------------------------------------------

def test_parse_ref_price():
    cases = [
        ("$215.62 USD (~$216)", 215.62, "USD"),
        ("1399p GBX (RR.L pence-quoted)", 1399.0, "GBX"),
        ("$1,769 USD / €1,548", 1769.0, "USD"),
        ("161.28 JPY per USD (USD/JPY spot)", 161.28, "JPY"),
        ("NAV £102.3k", 102300.0, "GBP"),
    ]
    for raw, val, units in cases:
        v, u_ = decision_scoring.parse_ref_price(raw)
        check(f"ref_price '{raw[:24]}'", v == val and u_ == units, f"-> {v} {u_}")


def test_parse_log_invariants():
    if not os.path.isfile(decision_scoring._default_log()):
        print("skip  log invariants  (DECISION_LOG.md not present in this checkout)")
        return
    entries = decision_scoring.parse_log(decision_scoring._default_log())
    ids = [int(e["id"].split("-")[1]) for e in entries]
    check("log >=15 entries", len(entries) >= 15, f"n={len(entries)}")
    check("log strictly descending", ids == sorted(ids, reverse=True))
    market = [e for e in entries if e.get("type") == "thesis"]
    missing = [e["id"] for e in market if not e.get("tickers") or not e.get("ref_price_raw")]
    check("thesis entries have machine lines", not missing, f"missing={missing}")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(FAILURES)} failure(s)")
    sys.exit(1 if FAILURES else 0)
