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
import fx_data  # noqa: E402
import positioning  # noqa: E402
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


def test_size_currency_bridge_and_leverage_cap():
    # LJPY-2026-08-02 committee run: FXY/LJPY-like low-vol FX name sized against a GBP NAV,
    # quoted in USD -- 3% risk / a ~1.12% 2xATR stop reproduced 268% NAV before this fix
    # (cro/mean-reversion/position-trader seats all independently flagged it).
    core = risk_sizing._size_core
    last, atr = 65.0, 0.364                       # 2*atr/last = 1.12% (matches the bug report)
    low_vol = core("FXY", last, atr, nav=100000.0, risk_pct=3.0, stop_atr_mult=2.0,
                   vol_target=0.20, unit=1.0, fx=1.35, quote_ccy="USD", nav_ccy="GBP", vol_ann=0.08)
    check("low-vol stop distance flagged as floored", low_vol["stop_distance_floored"] is True)
    check("low-vol leverage capped near 100% (was 268%)",
          95.0 <= low_vol["position_pct_nav"] <= 100.01, f"got {low_vol['position_pct_nav']}")
    check("low-vol shares positive", low_vol["shares"] > 0, f"shares={low_vol['shares']}")

    # Normal (non-pathological) same-currency case: stop is wide enough that the floor never
    # engages -- must reproduce the pre-fix formula exactly (no regression for typical names).
    same_ccy = core("NVDA", 100.0, 5.0, nav=100000.0, risk_pct=3.0, stop_atr_mult=2.0,
                    vol_target=0.20, unit=1.0, fx=1.0, quote_ccy="USD", nav_ccy="USD", vol_ann=0.30)
    check("unfloored same-ccy shares match pre-fix formula", same_ccy["shares"] == 300,
          f"shares={same_ccy['shares']}")
    check("unfloored same-ccy position_pct_nav matches pre-fix formula",
          same_ccy["position_pct_nav"] == 30.0, f"pct={same_ccy['position_pct_nav']}")
    check("floor not applied when stop is wide", same_ccy["stop_distance_floored"] is False)

    # Same setup, cross-currency (GBP NAV, USD quote, fx=1.35): shares/position_value must
    # scale with the FX rate -- previously risk_amt (GBP) was divided by stop_dist (USD) with
    # NO conversion at all. position_pct_nav is FX-invariant by construction (a pure
    # risk_pct/stop_distance_pct ratio), so it must still match the same-currency case exactly.
    cross_ccy = core("NVDA", 100.0, 5.0, nav=100000.0, risk_pct=3.0, stop_atr_mult=2.0,
                     vol_target=0.20, unit=1.0, fx=1.35, quote_ccy="USD", nav_ccy="GBP", vol_ann=0.30)
    check("cross-ccy shares scale by the fx rate (was mixed with no conversion)",
          cross_ccy["shares"] == 405, f"shares={cross_ccy['shares']}")
    check("cross-ccy position_pct_nav still matches same-ccy (fx-invariant ratio)",
          cross_ccy["position_pct_nav"] == same_ccy["position_pct_nav"],
          f"{cross_ccy['position_pct_nav']} vs {same_ccy['position_pct_nav']}")
    check("cross-ccy note explains the FX bridge", "FX-converted" in cross_ccy["note_currency"])
    check("cross-ccy position_value_nav_ccy back-converted",
          abs(cross_ccy["position_value_nav_ccy"] - same_ccy["position_value"]) < 1.0,
          f"{cross_ccy['position_value_nav_ccy']} vs {same_ccy['position_value']}")

    # _fx_rate offline fast paths (same currency / missing currency -- no network attempted)
    check("fx_rate same currency -> 1.0", risk_sizing._fx_rate("GBP", "GBP") == 1.0)
    check("fx_rate missing currency -> 1.0", risk_sizing._fx_rate(None, "USD") == 1.0)


# --- positioning.py COT direction -------------------------------------------------

def test_positioning_direction_4w():
    d = positioning._direction_4w
    # LJPY-2026-08-02 committee run: net short grew -155,092 -> -163,412; the old
    # `last > prev` comparison read this as "reducing" (backwards for a short).
    check("growing short -> building", d(-163412, -155092) == "building")
    # USDJPY-2026-07-14 redteam-bear report: net short covered -145,818 -> -123,778 (specs
    # bought back ~22k contracts); the old comparison read this as "building" (also backwards).
    check("covering short -> reducing", d(-123778, -145818) == "reducing")
    # Net longs: unchanged behavior, sanity check against the same helper.
    check("growing long -> building", d(150, 100) == "building")
    check("reducing long -> reducing", d(100, 150) == "reducing")
    check("no change -> unchanged", d(100, 100) == "unchanged")
    check("no 4w-ago data -> None", d(100, None) is None)


# --- fx_data.py rate/spot link ------------------------------------------------------

def test_fx_corr_window_computed():
    # fx_pack.json's rate_spot_link.corr_since_2018 was a hardcoded label even though the
    # actual join window tracks yfinance's 5y spot lookback (rarely reaches back to 2018) --
    # reproduced as an actual 2021-08 -> 2026-06 window mislabeled "since_2018". The window
    # must now be read from the data, not asserted in a field name.
    import pandas as pd
    idx = pd.date_range("2021-08-02", periods=200, freq="D")
    rng = np.random.default_rng(5)
    diff = pd.Series(rng.normal(2.0, 0.3, len(idx)), index=idx)
    usdjpy = 130 + 5 * diff + rng.normal(0, 1, len(idx))
    joined = pd.DataFrame({"usdjpy": usdjpy, "us10_jp10_diff": diff.values}, index=idx)
    link = fx_data._rate_spot_link(joined, current_diff=2.0)
    check("corr_window_start reflects actual data start", link["corr_window_start"] == "2021-08-02",
          f"got {link['corr_window_start']}")
    check("corr_window_end reflects actual data end", link["corr_window_end"] == str(idx[-1].date()),
          f"got {link['corr_window_end']}")
    check("window not silently mislabeled 2018", not link["corr_window_start"].startswith("2018"))
    check("no stale corr_since_2018 key", "corr_since_2018" not in link)
    check("corr field present", link.get("corr") is not None)
    check("short window (<=60 obs) returns None", fx_data._rate_spot_link(joined.iloc[:10], 2.0) is None)


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


def test_minor_unit_scale():
    # "GBp" (pence) and "GBP" (pounds) differ only by case — an .upper() in the lookup
    # would divide every pound-quoted name by 100.
    check("GBp is pence", common.minor_unit_scale({"currency": "GBp"}) == 0.01)
    check("GBX is pence", common.minor_unit_scale({"currency": "GBX"}) == 0.01)
    check("GBP is NOT scaled", common.minor_unit_scale({"currency": "GBP"}) == 1.0)
    check("USD is not scaled", common.minor_unit_scale({"currency": "USD"}) == 1.0)
    check("missing currency is safe", common.minor_unit_scale({}) == 1.0)
    check("None info is safe", common.minor_unit_scale(None) == 1.0)


def test_quote_price():
    # An LSE bank at 671.82p against a book value in pounds must price as 6.7182.
    px = common.quote_price({"currency": "GBp", "currentPrice": 671.82})
    check("GBp quote -> pounds", abs(px - 6.7182) < 1e-9, f"got {px}")
    check("USD quote unchanged",
          common.quote_price({"currency": "USD", "currentPrice": 127.13}) == 127.13)
    # Cross-currency: quotes pence, reports USD -> pence fix AND an explicit FX factor.
    px = common.quote_price({"currency": "GBp", "currentPrice": 1574.6}, price_scale=1.35)
    check("GBp quote x FX", abs(px - 21.2571) < 1e-6, f"got {px}")
    check("no price -> None", common.quote_price({"currency": "GBp"}) is None)
    check("regularMarketPrice fallback",
          common.quote_price({"currency": "USD", "regularMarketPrice": 10.0}) == 10.0)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(FAILURES)} failure(s)")
    sys.exit(1 if FAILURES else 0)
