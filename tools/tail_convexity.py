"""Fragility, convexity, and hedge-efficiency tools — the tail-risk agent's Taleb seat.

This is the capability the agent is NAMED for but did not have: a model-free, probability-free
test of whether harm ACCELERATES as a stressor grows (negative convexity = fragile), plus a
concrete cheap-hedge recommender that turns "buy protection" into a specific structure with an
explicit cost-vs-payoff / annualized bleed budget (the antidote to reflexive hedging).

Subcommands:
  fragility TICKER [--stressor vol] [--delta 0.25]   Taleb-Douady (2013) fragility heuristic:
      the 2nd derivative of expected shortfall to a mean-preserving spread of the stressor,
      NORMALIZED against a Gaussian of the same vol so ordinary symmetric vol scores ~1
      (anti-cries-wolf) and only genuine tail-acceleration flags as fragile.
  hedge TICKER [--otm 0.12] [--dte 90] [--notional 100000] [--structure auto]   cheapest convex
      structure (outright put / put-spread / collar, branched on the variance risk premium) with
      premium, breakeven, annualized bleed vs the CBOE PPUT drag, and a size-down-vs-hedge verdict.
  rnd TICKER [dte]   Breeden-Litzenberger (1978) risk-neutral P(down>10/20%) from the option smile.
  quadrant TICKER    Taleb (2009) Fourth-Quadrant gate: when the tail index alpha<=~3 AND the
      payoff is convex/fragile, variance/Sharpe/Kelly sizing is structurally unreliable -> override
      to size-down + convex hedge. Includes the Hill (1975) tail index vs the Gabaix cubic law.

Refs: Taleb & Douady 2013 (Quantitative Finance 13(11)); Taleb 2009 (Int. J. Forecasting 25(4));
Gabaix-Gopikrishnan-Plerou-Stanley 2003 (Nature 423); Hill 1975 (Ann. Statistics 3(5));
Breeden & Litzenberger 1978 (J. Business 51(4)); Israelov 2019 (JAI 21(3)); Whaley 2002 (J.
Derivatives 10(2)). numpy/pandas + free yfinance only (no scipy/statsmodels).
"""
import argparse, math
import numpy as np
import pandas as pd
import yfinance as yf
from common import jprint, rnd, yf_history, fred, _norm_cdf, main_guard


def _ret(ticker, period="3y"):
    px = yf_history(ticker, period=period)["Close"]
    return px.pct_change().dropna()


def _rf():
    try:
        return float(fred("DGS3MO").iloc[-1]) / 100.0
    except Exception:
        return 0.04


# ─────────────────────────────────── FRAGILITY (Taleb-Douady) ───────────────────────────────

def _shortfall(z, sig_h, K):
    """Expected exceedance of a fixed adverse threshold K (a horizon return, K<0): mean of
    (K - R) over the mass below K, where R = z*sig_h has the empirical SHAPE z scaled to
    horizon vol sig_h. Convex in sig_h for a fixed tail threshold — the Taleb-Douady harm fn."""
    R = z * sig_h
    return float(np.mean(np.where(R < K, K - R, 0.0)))


def _d2(vals, h):
    """5-point central second derivative on an evenly spaced grid of step h."""
    return (-vals[0] + 16 * vals[1] - 30 * vals[2] + 16 * vals[3] - vals[4]) / (12 * h ** 2)


def fragility(ticker, stressor="vol", delta=0.25, horizon=21, k_sigma=2.0):
    """Taleb-Douady (2013) fragility heuristic. Harm = expected shortfall below a FIXED adverse
    threshold; perturb the stressor's dispersion by +/-delta and +/-2*delta and take the 2nd
    derivative of harm. A fat/negatively-skewed tail makes harm ACCELERATE with vol faster than
    a Gaussian -> fragility_ratio>1 = FRAGILE; a symmetric name scores ~1 (anti-cries-wolf)."""
    errs = []
    r = _ret(ticker, "3y")
    z = ((r - r.mean()) / r.std()).values            # empirical shape (fat tails, skew)
    sig0 = float(r.std())
    hz = math.sqrt(horizon)
    K = -k_sigma * sig0 * hz                          # fixed horizon-return threshold, held constant
    scales = [1 - 2 * delta, 1 - delta, 1.0, 1 + delta, 1 + 2 * delta]
    # Harm = expected shortfall below the fixed adverse threshold; its convexity in the vol
    # stressor is the Taleb-Douady 'H'. The DISCRIMINATOR (vs ordinary vol) is whether the
    # DOWNSIDE harm accelerates faster than the mirror-image UPSIDE 'windfall': a fat/heavy
    # LEFT tail gives a ratio > 1 = fragile; a symmetric name gives ~1 (anti-cries-wolf), and a
    # right-skewed name gives < 1. This is deterministic (no simulation).
    om_dn = [_shortfall(z, sig0 * s * hz, K) for s in scales]        # left-tail shortfall convexity
    om_up = [_shortfall(-z, sig0 * s * hz, K) for s in scales]       # mirror (right-tail) shortfall
    d2_dn, d2_up = _d2(om_dn, delta), _d2(om_up, delta)
    dvu = d2_dn / d2_up if d2_up > 1e-12 else None
    sk, ek = float(pd.Series(r).skew()), float(pd.Series(r).kurtosis())
    fragile = bool(dvu is not None and dvu > 1.15 and d2_dn > 0)
    return {
        "ticker": ticker, "stressor": stressor, "delta": delta, "horizon_days": horizon,
        "downside_harm_convexity": rnd(d2_dn, 5), "upside_harm_convexity": rnd(d2_up, 5),
        "fragility_ratio": rnd(dvu, 2),
        "skew": rnd(sk, 2), "excess_kurtosis": rnd(ek, 1),
        "fragile": fragile,
        "read": ((f"FRAGILE — downside harm accelerates {rnd(dvu,1)}x faster than the upside as vol rises "
                  f"(heavy/negatively-skewed left tail, excess-kurt {rnd(ek,0)}); size down / add convex hedge") if fragile
                 else f"downside/upside harm convexity ~{rnd(dvu,2)} (symmetric or right-skewed) — high vol here is not fragility"),
        "note": "Downside vs mirror-upside 2nd derivative of expected shortfall to a mean-preserving "
                "vol spread (Taleb-Douady 2013). Symmetric name -> ~1; fat left tail -> >1.",
        "errors": errs,
    }


# ─────────────────────────────── RISK-NEUTRAL DENSITY (Breeden-Litzenberger) ────────────────

def _chain(ticker, target_dte):
    t = yf.Ticker(ticker)
    info = t.info or {}
    spot = info.get("currentPrice") or info.get("regularMarketPrice") or float(yf_history(ticker, "5d")["Close"].iloc[-1])
    exps = t.options
    if not exps:
        return None
    today = pd.Timestamp.now()
    expiry = min(exps, key=lambda e: abs((pd.Timestamp(e) - today).days - target_dte))
    dte = (pd.Timestamp(expiry) - today).days
    ch = t.option_chain(expiry)
    return {"spot": float(spot), "expiry": expiry, "dte": max(dte, 1), "calls": ch.calls, "puts": ch.puts}


def _bs_put(S, K, T, r, sig):
    if sig <= 0 or T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _mid_px(row):
    bid, ask, last = float(row.get("bid") or 0), float(row.get("ask") or 0), float(row.get("lastPrice") or 0)
    return (bid + ask) / 2 if (bid > 0 and ask > 0) else last


def _rn_cdf(ticker, target_dte):
    """Risk-neutral CDF Q(S_T<K) via Breeden-Litzenberger: dP/dK = e^{-rT} Q(S_T<K), so
    Q(S_T<K) = e^{rT} * dP/dK. Builds the put-price curve DIRECTLY from OTM put mid-prices
    (and parity-synthesized puts from OTM calls above spot) rather than from the impliedVolatility
    field — robust when yfinance's IV column is stale/degenerate — enforces the no-arbitrage
    monotonicity of P(K), then takes the stable FIRST derivative."""
    c = _chain(ticker, target_dte)
    if c is None:
        return None
    spot, T, r = c["spot"], c["dte"] / 365.0, _rf()
    disc = math.exp(-r * T)
    pts = {}
    for _, row in c["puts"].iterrows():
        k, px = float(row["strike"]), _mid_px(row)
        if px > 0 and 0.4 * spot < k <= spot * 1.02:
            pts[k] = px
    for _, row in c["calls"].iterrows():                 # parity: P = C - S + K e^{-rT}
        k, px = float(row["strike"]), _mid_px(row)
        if px > 0 and spot * 0.98 <= k < 1.6 * spot:
            pts.setdefault(k, max(px - spot + k * disc, 0.0))
    pts = {k: v for k, v in pts.items() if v > 0}
    if len(pts) < 6:
        return None
    ks = np.array(sorted(pts)); pxs = np.array([pts[k] for k in ks])
    grid = np.linspace(ks.min(), ks.max(), 400)
    pc = np.maximum.accumulate(np.interp(grid, ks, pxs))   # enforce P(K) nondecreasing in K
    cdf = np.clip(math.exp(r * T) * np.gradient(pc, grid), 0.0, 1.0)
    iv_put = iv_call = None
    dfp = c["puts"].dropna(subset=["impliedVolatility"])
    dfc = c["calls"].dropna(subset=["impliedVolatility"])
    if not dfp.empty and (dfp["impliedVolatility"].between(0.01, 5).any()):
        iv_put = float(dfp.iloc[(dfp.strike - spot * 0.9).abs().argsort()].iloc[0].impliedVolatility)
    if not dfc.empty and (dfc["impliedVolatility"].between(0.01, 5).any()):
        iv_call = float(dfc.iloc[(dfc.strike - spot * 1.1).abs().argsort()].iloc[0].impliedVolatility)
    return {"spot": spot, "expiry": c["expiry"], "dte": c["dte"], "grid": grid, "cdf": cdf,
            "iv_put": iv_put, "iv_call": iv_call}


def rnd_density(ticker, dte=30):
    """Breeden-Litzenberger risk-neutral P(down>10/20%) + left/right tail-cost asymmetry."""
    d = _rn_cdf(ticker, dte)
    if d is None:
        return {"ticker": ticker, "error": "insufficient option smile for a risk-neutral density"}
    spot, grid, cdf = d["spot"], d["grid"], d["cdf"]
    p10 = float(np.interp(spot * 0.90, grid, cdf))
    p20 = float(np.interp(spot * 0.80, grid, cdf))
    p_up10 = 1.0 - float(np.interp(spot * 1.10, grid, cdf))     # risk-neutral P(up>=10%)
    tail_asym = (p10 / p_up10) if p_up10 > 1e-4 else None       # downside vs upside 10% move
    return {
        "ticker": ticker, "spot": rnd(spot), "expiry_used": d["expiry"], "dte": d["dte"],
        "prob_down_10pct": rnd(p10, 3), "prob_down_20pct": rnd(p20, 3),
        "prob_up_10pct": rnd(p_up10, 3), "downside_upside_10pct_ratio": rnd(tail_asym, 2),
        "read": (f"the option market prices a {rnd(100*p10,0)}% chance of a >=10% drop and {rnd(100*p20,0)}% "
                 f">=20% by {d['dte']}d"
                 + (f"; a >=10% drop is {rnd(tail_asym,1)}x as likely as a >=10% rally — "
                    + ("priced downside fear (size down)" if tail_asym > 1.3 else "roughly symmetric tails")
                    if tail_asym else "")),
        "note": "Coarse buckets from a smoothed smile via the FIRST derivative of the put curve "
                "(stable). Not the Bollerslev-Todorov HF-EVT estimator. Breeden-Litzenberger 1978.",
    }


# ─────────────────────────────── HEDGE COST-EFFICIENCY & STRUCTURE ──────────────────────────

def _mid(df, strike):
    d = df.iloc[(df["strike"] - strike).abs().argsort()]
    if d.empty:
        return None, None
    row = d.iloc[0]
    bid, ask, last = float(row.get("bid") or 0), float(row.get("ask") or 0), float(row.get("lastPrice") or 0)
    px = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
    return (px if px > 0 else None), float(row["strike"])


def hedge(ticker, otm=0.12, dte=90, notional=100000, structure="auto"):
    """Cheapest convex hedge for the identified tail: outright put (when the variance risk premium
    is compressed — protection cheap), put-spread (when VRP rich — cheapen the carry), or collar
    (when an upside cap is acceptable). Reports premium, breakeven, annualized bleed vs the CBOE
    PPUT drag (~a few %/yr), risk-neutral P(hit), and a size-down-vs-hedge verdict.
    Refs: Israelov 2019 (JAI 21); Whaley 2002 (J. Derivatives 10); Kozhan-Neuberger-Schneider 2013 (RFS 26)."""
    c = _chain(ticker, dte)
    if c is None:
        return {"ticker": ticker, "error": "no listed options"}
    spot = c["spot"]
    kp = spot * (1 - otm)
    put_prem, kp = _mid(c["puts"], kp)
    kp2 = spot * (1 - 2 * otm)
    put2_prem, kp2 = _mid(c["puts"], kp2)
    kc = spot * (1 + otm)
    call_prem, kc = _mid(c["calls"], kc)
    ann = 365.0 / c["dte"]
    structures = {}
    if put_prem:
        structures["protective_put"] = {"legs": f"long put @{rnd(kp)}", "net_premium": rnd(put_prem),
                                         "premium_pct": rnd(100 * put_prem / spot, 2),
                                         "annualized_bleed_pct": rnd(100 * put_prem / spot * ann, 1),
                                         "breakeven": rnd(kp - put_prem), "max_protection_pct": rnd(100 * (spot - kp) / spot, 1)}
    if put_prem and put2_prem:
        net = put_prem - put2_prem
        structures["put_spread"] = {"legs": f"long put @{rnd(kp)} / short put @{rnd(kp2)}", "net_premium": rnd(net),
                                    "premium_pct": rnd(100 * net / spot, 2), "annualized_bleed_pct": rnd(100 * net / spot * ann, 1),
                                    "breakeven": rnd(kp - net), "max_protection_pct": rnd(100 * (kp - kp2) / spot, 1)}
    if put_prem and call_prem:
        net = put_prem - call_prem
        structures["collar"] = {"legs": f"long put @{rnd(kp)} / short call @{rnd(kc)}", "net_premium": rnd(net),
                                "premium_pct": rnd(100 * net / spot, 2), "annualized_bleed_pct": rnd(100 * net / spot * ann, 1),
                                "breakeven": rnd(kp - net), "upside_cap_pct": rnd(100 * (kc - spot) / spot, 1)}
    # branch on the variance risk premium (rich => cheapen via spread/collar; compressed => outright)
    vrp_read, rec = None, "protective_put"
    try:
        import options_analysis
        v = options_analysis.vrp(ticker, dte)
        vrp_pts = v.get("vrp_vol_points")
        vrp_read = v.get("read")
        if vrp_pts is not None:
            if structure != "auto":
                rec = structure
            elif vrp_pts > 6 and "put_spread" in structures:
                rec = "put_spread"
            elif vrp_pts > 3 and "collar" in structures:
                rec = "collar"
            else:
                rec = "protective_put"
    except Exception:
        pass
    rec = rec if rec in structures else (next(iter(structures)) if structures else None)
    rnd_d = rnd_density(ticker, dte)
    p_hit = rnd_d.get("prob_down_10pct") if isinstance(rnd_d, dict) else None
    chosen = structures.get(rec) if rec else None
    bleed = chosen.get("annualized_bleed_pct") if chosen else None
    PPUT_DRAG = 3.5   # CBOE PPUT long-run annual protection toll, literature benchmark
    verdict = None
    if bleed is not None:
        verdict = (f"hedge is cost-efficient (annualized bleed {bleed}% ~ the ~{PPUT_DRAG}%/yr PPUT drag) — carry the {rec}"
                   if bleed <= 1.5 * PPUT_DRAG
                   else f"hedge is expensive (bleed {bleed}%/yr >> ~{PPUT_DRAG}% PPUT drag) — prefer sizing DOWN over paying this carry")
    return {
        "ticker": ticker, "spot": rnd(spot), "expiry_used": c["expiry"], "dte": c["dte"], "otm": otm,
        "structures": structures, "recommended": rec, "vrp_read": vrp_read,
        "rn_prob_down_10pct": p_hit, "annualized_bleed_pct": bleed, "pput_drag_benchmark_pct": PPUT_DRAG,
        "verdict": verdict,
        "read": (f"recommended {rec}: {bleed}%/yr bleed to protect {chosen.get('max_protection_pct') if chosen else 'n/a'}% downside; "
                 + (verdict or "")) if chosen else "no usable option structure priced",
        "note": "Structure branched on the variance risk premium; bleed benchmarked to the CBOE PPUT drag. "
                "Size hedges as insurance vs the specific tail, not as a directional bet. Israelov 2019; Whaley 2002.",
    }


# ─────────────────────────────── FOURTH-QUADRANT GATE ───────────────────────────────────────

def _hill_alpha(losses):
    L = np.sort(np.asarray(losses, float))[::-1]
    L = L[L > 0]
    n = len(L)
    if n < 20:
        return None
    k = max(10, int(0.05 * n))
    k = min(k, n - 1)
    if L[k] <= 0:
        return None
    xi = float(np.mean(np.log(L[:k]) - np.log(L[k])))
    return (1 / xi) if xi > 0 else None


def quadrant(ticker):
    """Taleb (2009) Fourth-Quadrant gate: when the loss tail index alpha<=~3 (Gabaix cubic-law
    frontier) AND the name is fragile (convex harm), the variance/Sharpe/Kelly machinery in
    size()/macro_size()/kelly() is structurally unreliable -> the agent should override toward
    size-down + a convex hedge rather than trust vol-based sizing."""
    r = _ret(ticker, "5y")
    alpha = _hill_alpha(-r.values)
    frag = fragility(ticker)
    fr = frag.get("fragility_ratio")
    fat = bool(alpha is not None and alpha <= 3.0)
    fragile = bool(frag.get("fragile"))
    fourth_quadrant = bool(fat and fragile)
    return {
        "ticker": ticker,
        "tail_alpha_hill": rnd(alpha, 2), "cubic_law_benchmark": 3.0,
        "fat_tailed": fat, "fragile": fragile, "fragility_ratio": fr,
        "fourth_quadrant": fourth_quadrant,
        "verdict": ("FOURTH QUADRANT: fat-tailed AND fragile — variance/Sharpe/Kelly sizing is "
                    "structurally unreliable here; OVERRIDE to size-down + convex hedge, do not trust vol-targeting"
                    if fourth_quadrant else
                    "fat-tailed but not convex-fragile — deep tail, but vol-based sizing is usable with an EVT/CVaR overlay"
                    if fat else
                    "not in the fourth quadrant — standard vol/Kelly sizing is applicable"),
        "read": f"loss tail index alpha={rnd(alpha,2)} (cubic-law frontier 3.0); fragility ratio {fr}. "
                + ("Distrust variance-based risk numbers." if fourth_quadrant else "Variance-based numbers usable."),
        "note": "Hill tail index vs Gabaix cubic law + Taleb-Douady fragility. Taleb 2009; Gabaix et al. 2003; Hill 1975.",
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd"); p.add_argument("ticker"); p.add_argument("dte", nargs="?", type=int, default=None)
    p.add_argument("--stressor", default="vol"); p.add_argument("--delta", type=float, default=0.25)
    p.add_argument("--otm", type=float, default=0.12); p.add_argument("--dte", dest="dte_opt", type=int, default=None)
    p.add_argument("--notional", type=float, default=100000); p.add_argument("--structure", default="auto")
    a = p.parse_args()
    dte = a.dte_opt or a.dte or (90 if a.cmd == "hedge" else 30)
    if a.cmd == "fragility": jprint(fragility(a.ticker, a.stressor, a.delta))
    elif a.cmd == "hedge": jprint(hedge(a.ticker, a.otm, dte, a.notional, a.structure))
    elif a.cmd == "rnd": jprint(rnd_density(a.ticker, dte))
    elif a.cmd == "quadrant": jprint(quadrant(a.ticker))
    else: jprint({"error": f"unknown cmd '{a.cmd}'"})


if __name__ == "__main__":
    main_guard(main)
