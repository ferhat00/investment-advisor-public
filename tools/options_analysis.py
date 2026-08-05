"""Options market read: IV, expected move, skew, put/call positioning + option-implied
contrarian signals (call-put IV spread, volatility smirk, option/stock volume ratio).

The put/call ratio and a 90/110 skew ratio are coarse. The three signals added here are
the option-implied positioning measures with the strongest cross-sectional evidence:

  - cpiv_call_minus_put_iv_pct : OI-weighted mean(IV_call - IV_put) over strikes listed on
                                 BOTH sides. Deviations from put-call parity predict returns;
                                 negative (puts richer than matched calls) => informed downside.
                                 Cremers & Weinbaum (2010, JFQA 45(2)); Bali & Hovakimian
                                 (2009, Management Science 55(11)).
  - smirk_otm_put_vs_atm_call_pct : IV(~5% OTM put) - IV(ATM call), in vol points. A steep
                                 positive smirk = informed put-buying ahead of bad news and
                                 forecasts lower future equity returns. Xing, Zhang & Zhao
                                 (2010, JFQA 45(3)).
  - os_option_stock_volume_ratio : near-expiry option volume / share volume. Elevated relative
                                 option activity forecasts UNDERperformance because informed
                                 bad news migrates to options under short-sale costs. Roll,
                                 Schwartz & Subrahmanyam (2010, JFE 96(1)); Johnson & So
                                 (2012, JFE 106(2)).

These read "excitement" the contrarian way: rich downside puts / abnormal option activity are
bearish tells, not confirmation. Existing keys are UNCHANGED so downstream consumers keep working.

CLI: python options_analysis.py NVDA [dte]
"""
import sys
import numpy as np
import pandas as pd
import yfinance as yf
from common import jprint, rnd, yf_history, fred, main_guard

# numpy 2.x renamed np.trapz -> np.trapezoid (np.trapz removed in 3.14/np 2.4+).
_TRAPZ = getattr(np, "trapz", None) or np.trapezoid


def _pick_expiry(t, target_dte=30, exclude=None):
    """Pick the listed expiry whose days-to-expiry is nearest `target_dte` (the existing
    expiry-selection idiom, factored out). `exclude` is an optional set of expiry strings
    to skip so term-structure can force two DISTINCT expiries. Returns (expiry, dte) or
    (None, None) if no options are listed."""
    exps = t.options
    if not exps:
        return None, None
    exclude = exclude or set()
    exps = [e for e in exps if e not in exclude]
    if not exps:
        return None, None
    today = pd.Timestamp.now()
    dtes = [(e, abs((pd.Timestamp(e) - today).days - target_dte)) for e in exps]
    expiry = min(dtes, key=lambda x: x[1])[0]
    dte = (pd.Timestamp(expiry) - today).days
    return expiry, dte


def _cpiv_spread(calls, puts, spot, band=0.10):
    """OI-weighted mean of (IV_call - IV_put) across NEAR-THE-MONEY strikes present on both
    sides (Cremers-Weinbaum 2010). Weight = combined call+put open interest at the strike.
    Restricted to |strike/spot - 1| <= band so illiquid deep wings (where the IV smile makes
    same-strike call/put IVs diverge for non-parity reasons) don't dominate the average."""
    cols = ["strike", "impliedVolatility", "openInterest"]
    c = calls.dropna(subset=["impliedVolatility"])[cols]
    p = puts.dropna(subset=["impliedVolatility"])[cols]
    m = c.merge(p, on="strike", suffixes=("_c", "_p"))
    if spot:
        m = m[(m["strike"] / spot - 1).abs() <= band]
    if m.empty:
        return None, 0
    d = (m["impliedVolatility_c"] - m["impliedVolatility_p"]).to_numpy(float)
    w = (m["openInterest_c"].fillna(0) + m["openInterest_p"].fillna(0)).to_numpy(float)
    val = float(np.average(d, weights=w)) if w.sum() > 0 else float(np.mean(d))
    return val, int(len(m))


def _os_ratio(t, exps, ticker, n_exp=4):
    """Option/stock volume ratio O/S (Roll-Schwartz-Subrahmanyam 2010; Johnson-So 2012).
    Sums option volume across the nearest `n_exp` expiries (where most volume concentrates)
    and divides by the most-recent-session share volume. This is a proxy for true O/S (all
    expiries) and reads as a LEVEL — de-mean vs the name's own history for a real abnormal-O/S
    signal. Returns (ratio, note)."""
    opt_vol = 0.0
    for e in exps[:n_exp]:
        try:
            c = t.option_chain(e)
            opt_vol += float(c.calls["volume"].fillna(0).sum() + c.puts["volume"].fillna(0).sum())
        except Exception:
            continue
    if opt_vol <= 0:                       # session option volume resets daily; 0 => not yet traded
        return None, None
    try:
        h = yf_history(ticker, period="1mo")
        shv = float(h["Volume"].iloc[-1])
        avg = float(h["Volume"].tail(20).mean())
    except Exception:
        return None, None
    shv = shv if shv > 0 else avg
    if not shv or shv <= 0:
        return None, None
    return opt_vol / shv, f"front-{n_exp}-expiry option volume / last-session share volume"


def options_read(ticker, target_dte=30):
    t = yf.Ticker(ticker)
    info = t.info or {}
    spot = info.get("currentPrice") or info.get("regularMarketPrice")
    exps = t.options
    if not exps:
        return {"error": "no listed options"}
    expiry, dte = _pick_expiry(t, target_dte)
    ch = t.option_chain(expiry)
    calls, puts = ch.calls, ch.puts

    def nearest_iv(df, k):
        df = df.dropna(subset=["impliedVolatility"])
        if df.empty:
            return None
        return float(df.iloc[(df.strike - k).abs().argsort()].iloc[0].impliedVolatility)

    atm_iv_c, atm_iv_p = nearest_iv(calls, spot), nearest_iv(puts, spot)
    atm_iv = np.nanmean([v for v in [atm_iv_c, atm_iv_p] if v])
    otm_put_iv = nearest_iv(puts, spot * 0.90)
    otm_call_iv = nearest_iv(calls, spot * 1.10)
    otm_put_iv_95 = nearest_iv(puts, spot * 0.95)
    pc_oi = puts.openInterest.sum() / max(calls.openInterest.sum(), 1)
    pc_vol = puts.volume.fillna(0).sum() / max(calls.volume.fillna(0).sum(), 1)
    exp_move = spot * atm_iv * np.sqrt(dte / 365) if atm_iv else None

    cpiv, cpiv_n = _cpiv_spread(calls, puts, spot)
    smirk = (otm_put_iv_95 - atm_iv_c) if (otm_put_iv_95 is not None and atm_iv_c is not None) else None
    os_ratio, _os_note = _os_ratio(t, exps, ticker)

    out = {
        "ticker": ticker, "spot": rnd(spot), "expiry_used": expiry, "dte": dte,
        "atm_iv_pct": rnd(100 * atm_iv, 1) if atm_iv else None,
        "expected_move_to_expiry": rnd(exp_move),
        "expected_move_pct": rnd(100 * exp_move / spot, 1) if exp_move else None,
        "skew_90_110_ratio": rnd(otm_put_iv / otm_call_iv, 2) if (otm_put_iv and otm_call_iv) else None,
        "smirk_otm_put_vs_atm_call_pct": rnd(100 * smirk, 1) if smirk is not None else None,
        "cpiv_call_minus_put_iv_pct": rnd(100 * cpiv, 2) if cpiv is not None else None,
        "cpiv_matched_strikes": cpiv_n,
        "os_option_stock_volume_ratio": rnd(os_ratio, 4) if os_ratio is not None else None,
        "put_call_oi_ratio": rnd(pc_oi, 2), "put_call_volume_ratio": rnd(pc_vol, 2),
        "total_oi": int(puts.openInterest.sum() + calls.openInterest.sum()),
    }
    reads = []
    if out["skew_90_110_ratio"]:
        reads.append("downside puts rich vs upside calls (fear premium)" if out["skew_90_110_ratio"] > 1.15
                     else "flat/greedy skew — upside chased" if out["skew_90_110_ratio"] < 0.95 else "normal skew")
    if out["smirk_otm_put_vs_atm_call_pct"] is not None:
        s = out["smirk_otm_put_vs_atm_call_pct"]
        reads.append("steep smirk — informed put-buying / downside fear (Xing-Zhang-Zhao bearish tell)" if s > 5
                     else "inverted smirk — calls bid over downside puts (upside speculation)" if s < -2
                     else "moderate smirk")
    if out["cpiv_call_minus_put_iv_pct"] is not None:
        v = out["cpiv_call_minus_put_iv_pct"]
        reads.append("calls richer than matched puts — bullish option-implied direction" if v > 1.0
                     else "puts richer than matched calls — bearish option-implied direction (put-call-parity deviation)" if v < -1.0
                     else "call/put IV balanced")
    if out["os_option_stock_volume_ratio"] is not None:
        reads.append("elevated option-vs-stock volume — abnormal option interest (often informed/bearish tell; confirm vs own history)"
                     if out["os_option_stock_volume_ratio"] > 0.05 else "option activity modest relative to stock volume")
    if out["put_call_oi_ratio"]:
        reads.append("hedged/bearish positioning" if pc_oi > 1.1 else "bullish positioning" if pc_oi < 0.6 else "balanced positioning")
    out["read"] = "; ".join(reads)
    out["note"] = ("cpiv/smirk/os are cross-sectional positioning signals; os_ratio uses the near-expiry "
                   "chain as a proxy and reads as a level (de-mean vs the name's own history for a true "
                   "abnormal-O/S signal). Refs: Cremers-Weinbaum 2010; Xing-Zhang-Zhao 2010; Johnson-So 2012.")
    return out


# --- Risk-neutral moments via Bakshi-Kapadia-Madan (2003, RFS 16(1):101-143) ------------

def _spot_r_q(t, info):
    """Spot from info (currentPrice/regularMarketPrice) or last close; risk-free r from FRED
    DGS3MO (fallback 0.04); dividend yield q (decimal; percent-scaled inputs are /100)."""
    spot = info.get("currentPrice") or info.get("regularMarketPrice")
    if not spot:
        try:
            spot = float(yf_history(t.ticker, period="5d")["Close"].iloc[-1])
        except Exception:
            spot = None
    try:
        r = float(fred("DGS3MO").iloc[-1]) / 100.0
    except Exception:
        r = 0.04
    q = info.get("dividendYield") or 0.0
    try:
        q = float(q)
        # yfinance reports dividendYield as a percent number (e.g. 0.98 -> 0.98%, 1.5 -> 1.5%).
        # A real decimal yield above ~25% is implausible, so treat such values as percents.
        if q > 0.25:
            q = q / 100.0
    except (TypeError, ValueError):
        q = 0.0
    return spot, r, q


def _otm_frames(calls, puts, spot):
    """Build (strike, price) frames of usable OTM options. OTM calls: K >= spot; OTM puts:
    K < spot. price = mid (bid+ask)/2 when both quotes positive, else lastPrice. Drops
    rows with price<=0 or NaN IV/price. Returns (otm_puts_df, otm_calls_df) sorted by strike."""
    def _prep(df, side):
        d = df.copy()
        for col in ("bid", "ask", "lastPrice", "impliedVolatility", "strike"):
            if col not in d.columns:
                d[col] = np.nan
        bid = pd.to_numeric(d["bid"], errors="coerce")
        ask = pd.to_numeric(d["ask"], errors="coerce")
        last = pd.to_numeric(d["lastPrice"], errors="coerce")
        mid = (bid + ask) / 2.0
        good_quote = (bid > 0) & (ask > 0)
        price = mid.where(good_quote, last)
        out = pd.DataFrame({
            "strike": pd.to_numeric(d["strike"], errors="coerce"),
            "price": price,
            "iv": pd.to_numeric(d["impliedVolatility"], errors="coerce"),
        })
        out = out.dropna(subset=["strike", "price", "iv"])
        out = out[out["price"] > 0]
        if side == "call":
            out = out[out["strike"] >= spot]
        else:
            out = out[out["strike"] < spot]
        return out.sort_values("strike").reset_index(drop=True)

    return _prep(puts, "put"), _prep(calls, "call")


def _bkm_integrals(spot, r, q, tau, calls, puts):
    """Risk-neutral (V, W, X, mu) via Bakshi-Kapadia-Madan (2003). Returns a dict with the
    volatility/cubic/quartic contract integrals V, W, X, the mu drift term, OTM strike counts,
    and R = exp(r*tau). OTM options only; trapezoidal integration over strikes with weights:

      calls (K>=spot):  wV = 2*(1 - ln(K/S))/K**2
                        wW = (6*ln(K/S) - 3*ln(K/S)**2)/K**2
                        wX = (12*ln(K/S)**2 - 4*ln(K/S)**3)/K**2
      puts  (K<spot):   wV = 2*(1 + ln(S/K))/K**2
                        wW = -(6*ln(S/K) + 3*ln(S/K)**2)/K**2
                        wX = (12*ln(S/K)**2 + 4*ln(S/K)**3)/K**2

    V,W,X are the trapezoid integrals of (weight*price) over the concatenated puts-then-calls
    strike grid; mu = R - 1 - (R/2)*V - (R/6)*W - (R/24)*X."""
    R = float(np.exp(r * tau))
    otm_puts, otm_calls = _otm_frames(calls, puts, spot)

    cwV = cwW = cwX = None
    if not otm_calls.empty:
        Kc = otm_calls["strike"].to_numpy(float)
        lc = np.log(Kc / spot)
        cwV = 2.0 * (1.0 - lc) / Kc**2
        cwW = (6.0 * lc - 3.0 * lc**2) / Kc**2
        cwX = (12.0 * lc**2 - 4.0 * lc**3) / Kc**2
    pwV = pwW = pwX = None
    if not otm_puts.empty:
        Kp = otm_puts["strike"].to_numpy(float)
        lp = np.log(spot / Kp)
        pwV = 2.0 * (1.0 + lp) / Kp**2
        pwW = -(6.0 * lp + 3.0 * lp**2) / Kp**2
        pwX = (12.0 * lp**2 + 4.0 * lp**3) / Kp**2

    K = np.concatenate([otm_puts["strike"].to_numpy(float), otm_calls["strike"].to_numpy(float)])
    price = np.concatenate([otm_puts["price"].to_numpy(float), otm_calls["price"].to_numpy(float)])

    def _stack(pw, cw):
        parts = []
        if pw is not None:
            parts.append(pw)
        if cw is not None:
            parts.append(cw)
        return np.concatenate(parts)

    wV = _stack(pwV, cwV)
    wW = _stack(pwW, cwW)
    wX = _stack(pwX, cwX)

    if len(K) < 2:
        return {"V": None, "W": None, "X": None, "mu": None, "R": R,
                "n_otm_calls": int(len(otm_calls)), "n_otm_puts": int(len(otm_puts))}

    V = float(_TRAPZ(wV * price, K))
    W = float(_TRAPZ(wW * price, K))
    X = float(_TRAPZ(wX * price, K))
    mu = R - 1.0 - (R / 2.0) * V - (R / 6.0) * W - (R / 24.0) * X
    return {"V": V, "W": W, "X": X, "mu": mu, "R": R,
            "n_otm_calls": int(len(otm_calls)), "n_otm_puts": int(len(otm_puts))}


def bkm(ticker, dte=30):
    """Risk-neutral moments (implied volatility, skewness, kurtosis) of the dte-horizon return
    distribution via Bakshi-Kapadia-Madan (2003, RFS 16(1):101-143). rn_skew is typically
    NEGATIVE for index/large-cap equity (priced left-tail/crash fear) and excess kurtosis
    POSITIVE (fat tails). cboe_skew_style rescales rn_skew to CBOE-SKEW-index units."""
    errors = []
    t = yf.Ticker(ticker)
    try:
        info = t.info or {}
    except Exception:
        info = {}
    if not t.options:
        return {"ticker": ticker, "error": "no listed options", "errors": ["no listed options"]}
    spot, r, q = _spot_r_q(t, info)
    if not spot:
        return {"ticker": ticker, "error": "no spot price", "errors": ["no spot price"]}

    expiry, real_dte = _pick_expiry(t, dte)
    if expiry is None:
        return {"ticker": ticker, "error": "no expiry", "errors": ["no expiry"]}
    try:
        ch = t.option_chain(expiry)
    except Exception as e:
        return {"ticker": ticker, "error": f"chain fetch failed: {e}", "errors": [str(e)]}

    tau = dte / 365.0
    m = _bkm_integrals(spot, r, q, tau, ch.calls, ch.puts)
    if m["n_otm_puts"] < 6 or m["n_otm_calls"] < 6:
        errors.append(f"thin chain: {m['n_otm_puts']} OTM puts / {m['n_otm_calls']} OTM calls "
                      "(<6 per side) — moments noisy")
    if m["V"] is None or (m["R"] * m["V"] - m["mu"] ** 2) <= 0:
        errors.append("degenerate BKM integrals — cannot compute moments")
        return {"ticker": ticker, "spot": rnd(spot), "expiry_used": expiry, "dte": real_dte,
                "n_otm_calls": m["n_otm_calls"], "n_otm_puts": m["n_otm_puts"],
                "read": "insufficient OTM option data for risk-neutral moments", "errors": errors}

    R, V, W, X, mu = m["R"], m["V"], m["W"], m["X"], m["mu"]
    rn_var = R * V - mu ** 2
    rn_vol_ann = float(np.sqrt(max(rn_var, 0.0) / tau))
    rn_skew = (R * W - 3.0 * mu * R * V + 2.0 * mu ** 3) / (R * V - mu ** 2) ** 1.5
    rn_kurt = (R * X - 4.0 * mu * R * W + 6.0 * R * mu ** 2 * V - 3.0 * mu ** 4) / (R * V - mu ** 2) ** 2
    rn_excess_kurt = rn_kurt - 3.0

    out = {
        "ticker": ticker, "spot": rnd(spot), "expiry_used": expiry, "dte": real_dte,
        "target_dte": dte, "r": rnd(r, 4), "q": rnd(q, 4),
        "rn_vol_ann_pct": rnd(100 * rn_vol_ann, 1),
        "rn_skew": rnd(rn_skew, 2),
        "rn_excess_kurt": rnd(rn_excess_kurt, 1),
        "cboe_skew_style": rnd(100 - 10 * rn_skew, 1),
        "n_otm_calls": m["n_otm_calls"], "n_otm_puts": m["n_otm_puts"],
    }
    tone = ("pronounced" if rn_skew < -1.0 else "mild" if rn_skew < -0.3 else
            "flat/near-zero" if rn_skew < 0.3 else "positive (unusual — upside-skewed)")
    out["read"] = (
        f"risk-neutral skew {out['rn_skew']} ({tone} left-tail pricing); "
        f"negative skew = the option market is paying up for downside/crash protection "
        f"(typical equity range -0.5 to -2). excess kurtosis {out['rn_excess_kurt']} "
        f"({'fat-tailed' if rn_excess_kurt > 0 else 'thin-tailed'}). "
        f"model-free RN vol {out['rn_vol_ann_pct']}% ann."
    )
    out["errors"] = errors
    return out


def vrp(ticker, horizon=30):
    """Variance risk premium: model-free implied variance (BKM) minus trailing realized
    variance (Carr-Wu 2009, RFS 22(3); Bollerslev-Tauchen-Zhou 2009, RFS 22(11)). A large
    POSITIVE VRP means variance/convexity is expensive now — do NOT buy hedges when insurance
    is dear; a compressed/near-zero/negative VRP means protection is cheap and adding
    convexity is attractive. SPY VRP is usually positive."""
    errors = []
    b = bkm(ticker, dte=horizon)
    for e in b.get("errors", []):
        errors.append(f"bkm: {e}")
    iv_vol = b.get("rn_vol_ann_pct")
    if iv_vol is None:
        return {"ticker": ticker, "error": "no implied variance from BKM",
                "read": "cannot compute VRP without model-free implied variance",
                "errors": errors + [b.get("error", "bkm failed")]}
    iv_vol = iv_vol / 100.0
    implied_var_ann = iv_vol ** 2

    rv_vol = None
    try:
        h = yf_history(ticker, period="6mo")
        logret = np.log(h["Close"] / h["Close"].shift(1)).dropna()
        win = horizon if horizon and horizon >= 5 else 21
        r = logret.tail(win)
        if len(r) < 5:
            r = logret.tail(21)
        realized_var_ann = float(r.var(ddof=1) * 252)
        rv_vol = float(np.sqrt(max(realized_var_ann, 0.0)))
    except Exception as e:
        realized_var_ann = None
        errors.append(f"realized vol: {e}")

    if realized_var_ann is None:
        return {"ticker": ticker, "spot": b.get("spot"), "expiry_used": b.get("expiry_used"),
                "iv_vol_ann_pct": rnd(100 * iv_vol, 1),
                "read": "realized variance unavailable — VRP not computable", "errors": errors}

    vrp_var = implied_var_ann - realized_var_ann
    vrp_vol_points = 100 * (iv_vol - rv_vol)

    out = {
        "ticker": ticker, "spot": b.get("spot"), "expiry_used": b.get("expiry_used"),
        "dte": b.get("dte"), "horizon": horizon,
        "iv_vol_ann_pct": rnd(100 * iv_vol, 1),
        "rv_vol_ann_pct": rnd(100 * rv_vol, 1),
        "implied_var_ann": rnd(implied_var_ann, 4),
        "realized_var_ann": rnd(realized_var_ann, 4),
        "vrp_var": rnd(vrp_var, 4),
        "vrp_vol_points": rnd(vrp_vol_points, 1),
    }
    if vrp_var > 0.02:
        signal = ("large positive VRP — convex protection is expensive; insurance is dear, "
                  "so do NOT buy hedges here (harvest premium instead)")
    elif vrp_var > 0.005:
        signal = "moderate positive VRP — variance carries a normal premium; hedges fairly priced-to-rich"
    elif vrp_var >= -0.005:
        signal = ("compressed/near-zero VRP — insurance is cheap; adding convexity/tail hedges "
                  "is attractive here")
    else:
        signal = ("negative VRP — realized exceeds implied; protection is cheap relative to actual "
                  "movement, adding convexity is attractive")
    out["read"] = (f"IV {out['iv_vol_ann_pct']}% vs RV {out['rv_vol_ann_pct']}% "
                   f"(spread {out['vrp_vol_points']} vol pts). {signal}.")
    out["errors"] = errors
    return out


def _mfiv_ann(t, target_dte, info, exclude=None):
    """Annualized model-free implied vol sqrt(R*V/tau) (Britten-Jones-Neuberger 2000, JF 55(2);
    Jiang-Tian 2005, RFS 18(4)) at the listed expiry nearest `target_dte`. Returns
    (iv_ann, expiry, dte, n_otm_puts, n_otm_calls, err) with err set on failure."""
    expiry, dte = _pick_expiry(t, target_dte, exclude=exclude)
    if expiry is None:
        return None, None, None, 0, 0, "no distinct expiry"
    spot, r, q = _spot_r_q(t, info)
    if not spot:
        return None, expiry, dte, 0, 0, "no spot"
    try:
        ch = t.option_chain(expiry)
    except Exception as e:
        return None, expiry, dte, 0, 0, f"chain fetch failed: {e}"
    tau = max(dte, 1) / 365.0
    m = _bkm_integrals(spot, r, q, tau, ch.calls, ch.puts)
    if m["V"] is None or m["R"] * m["V"] <= 0:
        return None, expiry, dte, m["n_otm_puts"], m["n_otm_calls"], "degenerate integrals"
    iv_ann = float(np.sqrt(m["R"] * m["V"] / tau))
    return iv_ann, expiry, dte, m["n_otm_puts"], m["n_otm_calls"], None


def termstructure(ticker, dte=30):
    """Model-free IV term structure: annualized MFIV at ~30d vs ~90d (Britten-Jones-Neuberger
    2000, JF 55(2); Jiang-Tian 2005, RFS 18(4)). An INVERTED curve (front > back) prices acute
    near-term crash risk — hedge short-dated / size down and don't overpay for long-dated
    protection; an upward (contango) curve is ordinary."""
    errors = []
    t = yf.Ticker(ticker)
    try:
        info = t.info or {}
    except Exception:
        info = {}
    if not t.options:
        return {"ticker": ticker, "error": "no listed options", "errors": ["no listed options"]}

    iv30, exp30, dte30, p30, c30, err30 = _mfiv_ann(t, 30, info)
    if err30:
        errors.append(f"30d: {err30}")
    if p30 < 6 or c30 < 6:
        errors.append(f"30d thin chain ({p30} OTM puts / {c30} OTM calls)")

    iv90, exp90, dte90, p90, c90, err90 = _mfiv_ann(t, 90, info, exclude={exp30} if exp30 else None)
    if err90:
        errors.append(f"90d: {err90}")
    if exp90 is None or exp90 == exp30:
        errors.append("only one distinct expiry available — term structure undefined")
    elif p90 < 6 or c90 < 6:
        errors.append(f"90d thin chain ({p90} OTM puts / {c90} OTM calls)")

    if iv30 is None and iv90 is None:
        return {"ticker": ticker, "error": "no model-free IV at either horizon",
                "read": "term structure not computable", "errors": errors}

    out = {
        "ticker": ticker,
        "iv30_ann_pct": rnd(100 * iv30, 1) if iv30 is not None else None,
        "iv90_ann_pct": rnd(100 * iv90, 1) if iv90 is not None else None,
        "expiry_30d": exp30, "dte_30d": dte30,
        "expiry_90d": exp90, "dte_90d": dte90,
    }
    if iv30 is not None and iv90 is not None:
        slope = 100 * (iv30 - iv90)
        inverted = iv30 > iv90
        out["slope_pts"] = rnd(slope, 1)
        out["inverted"] = bool(inverted)
        if inverted:
            out["read"] = (f"INVERTED term structure (30d {out['iv30_ann_pct']}% > 90d "
                           f"{out['iv90_ann_pct']}%, slope {out['slope_pts']} pts): acute "
                           f"near-term crash pricing — hedge short-dated / size down, don't "
                           f"overpay for long-dated protection.")
        else:
            out["read"] = (f"upward/contango term structure (30d {out['iv30_ann_pct']}% < 90d "
                           f"{out['iv90_ann_pct']}%, slope {out['slope_pts']} pts): ordinary — "
                           f"no acute near-term crash premium.")
    else:
        out["slope_pts"] = None
        out["inverted"] = None
        out["read"] = "only one horizon available — term-structure slope undefined."
    out["errors"] = errors
    return out


_SUBCMDS = {"bkm": bkm, "vrp": vrp, "termstructure": termstructure}

def main():
    args = sys.argv[1:]
    if args and args[0] in _SUBCMDS:
        fn = _SUBCMDS[args[0]]
        ticker = args[1] if len(args) > 1 else None
        if not ticker:
            jprint({"error": f"usage: python options_analysis.py {args[0]} TICKER [dte]"})
        else:
            n = int(args[2]) if len(args) > 2 else (30 if args[0] != "vrp" else 30)
            jprint(fn(ticker, n))
    else:
        # Backward compatible: bare ticker (data-pack builder calls `... TICKER`).
        jprint(options_read(args[0], int(args[1]) if len(args) > 1 else 30))


if __name__ == "__main__":
    main_guard(main)
