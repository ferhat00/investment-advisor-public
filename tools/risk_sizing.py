"""Risk stats + position sizing.
CLI:
  python risk_sizing.py stats NVDA
  python risk_sizing.py corr NVDA SPY,QQQ,TLT,GLD
  python risk_sizing.py size NVDA --nav 100000 --risk_pct 3.0 --stop_atr 2
  python risk_sizing.py arbgate NVDA --mispricing_pct 75 --catalyst_days 30 --borrow_fee_bps 500 [--momentum_crash]
"""
import argparse, json, os
import numpy as np
import pandas as pd
from common import (yf_history, jprint, rnd, realized_vol_ann, fred, zscore_last,
                    safe, _norm_pdf, main_guard, parse_list, minor_unit_scale)

# Standard-normal quantiles (numpy-only; the repo avoids scipy). Used by the parametric
# and Cornish-Fisher tail readouts below.
_Z = {0.90: 1.2815516, 0.95: 1.6448536, 0.975: 1.9599640,
      0.99: 2.3263479, 0.995: 2.5758293, 0.999: 3.0902323}

# Canonical realized-vol lookback for regime + sizing (one trading quarter). Trailing
# realized vol is the standard input to vol-targeting (Barroso-Santa-Clara 2015;
# Moreira-Muir 2017). Both `stats` and `size` report vol over this same window so the
# figures reconcile; `vol_ann_pct` remains the long-run (full-sample) context number.
VOL_WINDOW = 63

def stats(ticker):
    px = yf_history(ticker, period="3y")["Close"]
    r = px.pct_change().dropna()
    dd = px / px.cummax() - 1
    var95 = float(np.percentile(r, 5))
    # momentum-crash regime: high vol following a drawdown is where trend/momentum
    # strategies reverse violently (Daniel-Moskowitz 2016; Barroso-Santa-Clara 2015)
    rv = r.rolling(VOL_WINDOW).std() * np.sqrt(252)          # trailing-quarter rolling vol
    vol_recent = realized_vol_ann(r, VOL_WINDOW)             # == rv.iloc[-1]; matches size()
    vol_med = float(rv.median())
    dd_6m = float(px.iloc[-1] / px.tail(126).max() - 1)
    vol_regime = ("elevated" if vol_recent > 1.3 * vol_med
                  else "calm" if vol_recent < 0.7 * vol_med else "normal")
    return {
        "ticker": ticker,
        "vol_ann_pct": rnd(100 * realized_vol_ann(r)),       # long-run (full 3y sample)
        "vol_ann_63d_pct": rnd(100 * vol_recent),            # trailing quarter (sizing/regime vol)
        "max_dd_3y_pct": rnd(100 * dd.min()),
        "var95_daily_pct": rnd(100 * var95),
        "cvar95_daily_pct": rnd(100 * r[r <= var95].mean()),
        "worst_day_pct": rnd(100 * r.min()),
        "sharpe_3y_rf4": rnd((r.mean() * 252 - 0.04) / (r.std() * np.sqrt(252)), 2),
        "skew": rnd(float(r.skew()), 2), "kurtosis": rnd(float(r.kurtosis()), 1),
        "vol_regime": vol_regime,
        "dd_from_6m_high_pct": rnd(100 * dd_6m),
        "momentum_crash_flag": bool(vol_recent > 1.5 * vol_med and dd_6m < -0.10),
    }

def corr(ticker, others):
    px = {tk: yf_history(tk, period="2y")["Close"].pct_change() for tk in [ticker] + others}
    import pandas as pd
    df = pd.DataFrame(px).dropna()
    return {f"corr_{ticker}_vs_{c}": rnd(df[ticker].corr(df[c]), 2) for c in others}

_MINOR_TO_MAJOR_CCY = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}

# Below this fraction of the VOL_WINDOW showing an exact zero daily return, flag it as
# possible non-printing/stale-quote days (common on thin FX crosses) rather than silently
# trust a realized vol that a stale feed has deflated. Detection only — the tool reports the
# flag rather than auto-correcting the vol, since a genuinely flat trading day is legitimate
# too and a heuristic "fix" risks being wrong in the other direction.
STALE_DAY_FLAG_PCT = 8.0


def _quote_info(ticker):
    """Best-effort (unit_scale, major_ccy) for a ticker's Yahoo quote: unit_scale converts a
    minor-unit quote into its major currency unit (GBp -> GBP, 0.01), and major_ccy is that
    major unit's ISO code, used for the cross-currency FX bridge in size(). (1.0, None) if
    the info scrape fails — sizing must not die on a flaky info scrape."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception:  # noqa: BLE001 — sizing must not die on a flaky info scrape
        return 1.0, None
    ccy = info.get("currency")
    return minor_unit_scale(info), _MINOR_TO_MAJOR_CCY.get(ccy, ccy)


def _fx_rate(from_ccy, to_ccy, errs=None):
    """Live FX rate converting an amount in `from_ccy` into `to_ccy`
    (amount_to = amount_from * rate). 1.0 if the currencies already match, either is
    unknown, or the quote can't be resolved (noted in `errs`) — the same
    degrade-to-unconverted fallback used across the tool library (portfolio_opt._fx_to_base,
    trade_stats.to_gbp)."""
    if not from_ccy or not to_ccy:
        return 1.0
    from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
    if from_ccy == to_ccy:
        return 1.0
    try:
        return float(yf_history(f"{from_ccy}{to_ccy}=X", period="5d")["Close"].iloc[-1])
    except Exception as e:
        if errs is not None:
            errs.append(f"fx_{from_ccy}{to_ccy}: {e}")
        return 1.0


def _size_core(ticker, last, atr, nav, risk_pct, stop_atr_mult, vol_target, unit, fx,
               quote_ccy, nav_ccy, vol_ann):
    """Pure risk-based + vol-target sizing arithmetic (no I/O) — see size() for the
    data-fetch wrapper. `fx` converts an amount in nav_ccy into quote_ccy
    (amount_quote = amount_nav_ccy * fx); pass fx=1.0 when quote_ccy == nav_ccy or is
    unknown.

    Two fixes over the original formula (both reproduced on FXY/LJPY, a low-vol FX name,
    against a GBP NAV — see DECISION_LOG / LJPY-2026-08-02 committee run):

    1. Currency bridge: risk_amt (nav_ccy) used to be divided by stop_dist (quote_ccy)
       directly, silently mixing currencies whenever quote_ccy != nav_ccy (~1.35x off at
       GBPUSD~1.35). risk_amt is now converted into quote_ccy before sizing shares, and
       position_value is converted back into nav_ccy (position_value_nav_ccy) for an
       apples-to-apples position_pct_nav.
    2. Leverage cap: the risk leg alone (risk_pct / stop_distance_pct) is otherwise
       uncapped — a tight ATR-based stop on a low-vol name can imply well over 100% NAV
       (3% risk / a 1.12% 2xATR stop = 268% NAV). The stop distance used for sizing is
       floored at last*risk_pct/100 (the distance at which the risk leg exactly implies
       100% NAV), so implied leverage from this leg can never exceed ~100% NAV.
    """
    stop_dist_raw = stop_atr_mult * atr
    risk_amt = nav * risk_pct / 100
    risk_amt_quote = risk_amt * fx
    min_stop_dist = last * risk_pct / 100
    stop_dist = max(stop_dist_raw, min_stop_dist)
    stop_floored = stop_dist > stop_dist_raw
    shares = int(risk_amt_quote / (stop_dist * unit)) if stop_dist > 0 and unit > 0 else 0
    pos_val = shares * last * unit
    pos_val_nav = pos_val / fx if fx else pos_val
    vt_weight = min(100.0, 100 * vol_target / vol_ann) if vol_ann and vol_ann > 0 else None
    cross_ccy = bool(quote_ccy and quote_ccy != nav_ccy)
    if quote_ccy is None:
        note_currency = "quote currency unresolvable (info scrape failed); FX conversion skipped, treated as nav_ccy"
    elif cross_ccy:
        note_currency = (f"position_value is in {quote_ccy}; FX-converted at 1 {nav_ccy} = "
                         f"{rnd(fx, 4)} {quote_ccy} into position_value_nav_ccy, which "
                         f"position_pct_nav uses")
    else:
        note_currency = "quote currency matches nav_ccy; no FX conversion needed"
    return {
        "ticker": ticker, "last": rnd(last), "atr14": rnd(atr),
        "stop_price": rnd(last - stop_dist), "stop_distance_pct": rnd(100 * stop_dist / last) if last else None,
        "stop_distance_floored": stop_floored,
        "risk_amount": rnd(risk_amt, 0), "risk_amount_ccy": nav_ccy, "shares": shares,
        "position_value": rnd(pos_val, 0), "position_value_ccy": quote_ccy or nav_ccy,
        "position_value_nav_ccy": rnd(pos_val_nav, 0),
        "position_pct_nav": rnd(100 * pos_val_nav / nav, 1) if nav else None,
        "quote_unit_scale": unit,
        "fx_nav_ccy_to_quote_ccy": rnd(fx, 4) if cross_ccy else None,
        "note_currency": note_currency,
        "vol_ann_63d_pct": rnd(100 * vol_ann) if vol_ann is not None else None,
        "vol_target_ann_pct": rnd(100 * vol_target),
        "vol_target_weight_pct": rnd(vt_weight, 1),
        "note_vol_target": (f"to run this name at a {vol_target:.0%} standalone vol budget "
                            f"(asset vol {vol_ann:.0%}), weight ≈ {rnd(vt_weight, 1)}% NAV; "
                            f"cut it in high-vol regimes") if vol_ann is not None else None,
    }


def size(ticker, nav, risk_pct, stop_atr_mult, vol_target=0.20, nav_ccy="GBP"):
    """Risk-based sizing: risk_pct of NAV lost if stop (stop_atr_mult x ATR) hit,
    plus a volatility-target weight (Moreira-Muir 2017) that scales exposure
    inversely to realized vol so risk — not notional — is held roughly constant.
    NAV and risk_pct are denominated in `nav_ccy` (default GBP, the fund's base currency —
    matches portfolio_opt.py/trade_stats.py); see _size_core for the currency-bridge and
    leverage-cap fixes applied on top of the raw ATR-stop formula."""
    df = yf_history(ticker, period="1y")
    tr = pd.concat([df.High - df.Low, (df.High - df.Close.shift()).abs(),
                    (df.Low - df.Close.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    last = float(df.Close.iloc[-1])
    nav_ccy = (nav_ccy or "GBP").upper()
    unit, quote_ccy = _quote_info(ticker)
    errs = []
    fx = _fx_rate(nav_ccy, quote_ccy, errs)
    r = df.Close.pct_change().dropna()
    vol_ann = realized_vol_ann(r, VOL_WINDOW)                # trailing quarter — same as stats()
    out = _size_core(ticker, last, atr, nav, risk_pct, stop_atr_mult, vol_target, unit, fx,
                     quote_ccy, nav_ccy, vol_ann)
    window_r = r.tail(VOL_WINDOW)
    if len(window_r):
        stale_pct = 100 * float((window_r == 0).mean())
        out["vol_window_stale_days_pct"] = rnd(stale_pct, 1)
        if stale_pct >= STALE_DAY_FLAG_PCT:
            out["note_stale_vol"] = (f"{rnd(stale_pct, 0)}% of the {VOL_WINDOW}d vol window is an exact "
                                     f"zero return (possible non-printing/stale quotes, common on thin "
                                     f"FX crosses) — vol_ann_63d_pct may be UNDERSTATED")
    if errs:
        out["errors"] = errs
    return out

def kelly(win_p, win_loss_ratio, fraction=0.33):
    f = win_p - (1 - win_p) / win_loss_ratio
    return {"full_kelly_pct": rnd(100 * f, 1), "fractional_kelly_pct": rnd(100 * f * fraction, 1),
            "fraction_used": fraction}


def _macro_funding_stress():
    """Free FRED funding/credit-stress gauge for the carry-crash guard: SOFR-IORB spread +
    HY-OAS level and 5y z-score. Wide spread / high credit-spread z = de-lever regime."""
    import pandas as pd
    fs = {}
    try:
        sofr, iorb = fred("SOFR", start="2019-01-01"), fred("IORB", start="2019-01-01")
        j = pd.concat([sofr.rename("s"), iorb.rename("i")], axis=1).dropna()
        if len(j):
            fs["sofr_iorb_spread_bp"] = rnd(100 * (j["s"].iloc[-1] - j["i"].iloc[-1]), 0)
    except Exception:
        pass
    try:
        hy = fred("BAMLH0A0HYM2", start="2010-01-01")
        fs["hy_oas_pct"] = rnd(float(hy.iloc[-1]), 2)
        fs["hy_oas_z_5y"] = rnd(zscore_last(hy, lookback=1250), 2)
    except Exception:
        pass
    return fs


def _sizing_artifact_warning(weight_pct, vol_ann_pct):
    """Shared low-vol/low-CVaR sizing-artifact flag for macro_size/esbudget/ddbudget: all three cap
    a weight by inverting a small risk denominator (realized vol, CVaR, or CDaR) with no input for
    liquidity, wrapper constraints, counterparty risk, or holding-period cost, so on an illiquid
    low-vol name (FX ETPs are the documented case — FXY: macro_size 268.4% NAV, esbudget 73.5%,
    ddbudget 44.9%, one shared denominator) the output is not a usable position size. Flags, does
    not resize or block. Refs: DEC-054; LJPY-2026-08-02 tail-risk report §8."""
    if weight_pct is None:
        return None
    reasons = []
    if weight_pct > 25.0:
        reasons.append(f"computed weight {weight_pct:g}% NAV > 25% sanity ceiling")
    if vol_ann_pct is not None and vol_ann_pct < 10.0:
        reasons.append(f"annualised realised vol {vol_ann_pct:g}% < 10% low-vol threshold")
    if not reasons:
        return None
    return ("SIZING ARTIFACT (" + "; ".join(reasons) + "): this weight inverts a low realised-vol/"
            "loss-budget denominator with no liquidity, wrapper, counterparty, or holding-period-cost "
            "input — do not read it as a position size on an illiquid/low-vol name without a separate "
            "liquidity check. Refs: DEC-054; LJPY-2026-08-02 tail-risk report §8.")


def macro_size(ticker, nav, risk_pct, stop_atr_mult, vol_target=0.20, funding_stress_override=False,
               nav_ccy="GBP"):
    """Crash-aware macro sizing: the risk_sizing.size() base weight, then CONDITIONAL
    vol-targeting (scale to the vol budget only in an elevated-vol regime — Moreira-Muir 2017;
    Malitsky 2021 — rather than always, to avoid over-hedging bull trends) and a funding/
    carry-crash guard that cuts size on a momentum-crash regime or funding stress
    (Daniel-Moskowitz 2016; Daniel-Hodrick-Lu 2017 carry drawdowns)."""
    base = size(ticker, nav, risk_pct, stop_atr_mult, vol_target, nav_ccy)
    st = stats(ticker)
    vol_regime, crash = st["vol_regime"], st["momentum_crash_flag"]
    fs = _macro_funding_stress()
    stress_on = bool(funding_stress_override
                     or (fs.get("sofr_iorb_spread_bp") or 0) > 5
                     or (fs.get("hy_oas_z_5y") or 0) > 1.0)
    vt_weight = base.get("vol_target_weight_pct")
    base_pct = base.get("position_pct_nav")
    if vol_regime == "elevated" and vt_weight is not None:
        cond_weight, vt_note = vt_weight, "elevated-vol regime — scale to the vol budget (Moreira-Muir/Malitsky)"
    else:
        cond_weight, vt_note = base_pct, "calm/normal regime — hold base size, do not over-hedge (conditional vol-target)"
    mult, reads = 1.0, []
    if crash:
        mult *= 0.6
        reads.append("momentum-crash regime (elevated vol after drawdown) — inverse-vol de-risk (Daniel-Moskowitz 2016)")
    if stress_on:
        mult *= 0.6
        reads.append("funding/carry-crash stress (wide SOFR-IORB or HY-OAS z>1 or override) — cut size, "
                     "demand a near-term catalyst (Daniel-Hodrick-Lu 2017)")
    mult = rnd(max(0.1, min(1.0, mult)), 2)
    recommended = rnd((cond_weight or 0) * mult, 1) if cond_weight is not None else None
    return {
        "ticker": ticker,
        "base_position_pct_nav": base_pct,
        "vol_ann_63d_pct": base.get("vol_ann_63d_pct"), "vol_regime": vol_regime,
        "vol_target_ann_pct": base.get("vol_target_ann_pct"),
        "vol_target_weight_pct": vt_weight,
        "conditional_weight_pct_nav": rnd(cond_weight, 1) if cond_weight is not None else None,
        "conditional_vol_target_note": vt_note,
        "momentum_crash_flag": crash,
        "funding_stress": fs, "funding_stress_on": stress_on,
        "crash_guard_multiplier": mult,
        "recommended_pct_nav": recommended,
        "sizing_artifact_warning": _sizing_artifact_warning(recommended, base.get("vol_ann_63d_pct")),
        "reads": reads,
        "note": "Conditional vol-targeting + funding/carry-crash guard applied on top of the 3%-risk "
                "base size. Refs: Moreira-Muir 2017; Malitsky 2021; Daniel-Moskowitz 2016; Daniel-Hodrick-Lu 2017.",
    }


def arb_gate(ticker, mispricing_pct=None, catalyst_days=None, horizon_days=365,
             borrow_fee_bps=None, momentum_crash=False):
    """Limits-to-arbitrage 'early vs wrong' sizing gate — the contrarian's discipline against
    catching falling knives. Turns four frictions into a size multiplier on the base
    risk_sizing.size() weight and an explicit catalyst requirement:

      - Idiosyncratic volatility as the arbitrage COST (Ang-Hodrick-Xing-Zhang 2006, JF): the
        higher a name's IVOL, the wider a mispricing can persist before it pays — size down.
      - Arbitrage ASYMMETRY (Stambaugh-Yu-Yuan 2015, JF; Shleifer-Vishny 1997, JF): overpricing
        persists more than underpricing because shorting is costlier, so an overpriced + high-IVOL
        short can stay wrong for long — demand a datable catalyst within the horizon.
      - Short-selling risk / borrow cost (Engelberg-Reed-Ringgenberg 2018, JF; D'Avolio 2002): an
        expensive/special borrow adds carry and recall/squeeze risk to the short.
      - Momentum-crash regime (Daniel-Moskowitz 2016): inverse-vol de-risking when momentum is fragile.

    mispricing_pct: 0-100 peer percentile from quant_factors mispricing (100 = most overpriced).
    Inputs are meant to be fed from quant_factors crowding/mispricing and crowding_metrics."""
    try:
        import factor_attribution
        a = factor_attribution.attribution(ticker)
        ivol, idio = a.get("ivol_ann_pct"), a.get("idiosyncratic_share_pct")
    except Exception as e:
        ivol = idio = None
        a = {"error": str(e)}
    ivol_regime = ("high" if (ivol or 0) >= 45 else "low" if (ivol or 0) < 25 else "moderate") if ivol is not None else "unknown"
    overpriced = mispricing_pct is not None and mispricing_pct >= 70
    underpriced = mispricing_pct is not None and mispricing_pct <= 30

    mult, reads = 1.0, []
    if ivol_regime == "high":
        mult *= 0.6
        reads.append(f"high idiosyncratic vol ({ivol}%/yr) = large arbitrage cost; mispricing can persist — size down")
    elif ivol_regime == "moderate":
        mult *= 0.85
    catalyst_required = False
    if overpriced and ivol_regime in ("high", "moderate"):
        mult *= 0.6
        catalyst_required = True
        reads.append("overpriced + non-trivial IVOL: arbitrage asymmetry says the short can stay wrong for long — "
                     "require a datable catalyst within horizon and keep size token until then")
    if underpriced and ivol_regime == "high":
        mult *= 0.8
        reads.append("underpriced but high IVOL — a long is less constrained than a short; modest haircut only")

    borrow = None
    if borrow_fee_bps is not None:
        special = borrow_fee_bps >= 300
        borrow = {"borrow_fee_bps": borrow_fee_bps, "special": bool(special)}
        if special and not underpriced:
            mult *= 0.6
            catalyst_required = True
            reads.append(f"expensive/special borrow ({borrow_fee_bps:.0f} bps): carry + recall/squeeze risk — cut short "
                         "size and treat high short interest here as squeeze-primed, not a free short")

    if momentum_crash:
        mult *= 0.6
        reads.append("momentum-crash regime — inverse-vol de-risk; extremes can snap back violently either way")

    # catalyst timing vs horizon
    catalyst_status = "not specified"
    if catalyst_days is not None:
        if catalyst_days <= horizon_days:
            catalyst_status = f"catalyst in {catalyst_days}d (within {horizon_days}d horizon) — open-ended-wait risk bounded"
            catalyst_required = False        # requirement satisfied
        else:
            catalyst_status = f"nearest catalyst {catalyst_days}d out — beyond horizon; keep size token"
            mult *= 0.7

    mult = rnd(max(0.1, min(1.0, mult)), 2)
    verdict = ("SIZE DOWN / demand catalyst — this is an 'early vs wrong' setup" if (catalyst_required or mult <= 0.5)
               else "frictions modest — normal contrarian sizing acceptable")
    return {
        "ticker": ticker, "ivol_ann_pct": ivol, "idiosyncratic_share_pct": idio, "ivol_regime": ivol_regime,
        "mispricing_pct": mispricing_pct, "direction": ("overpriced/short-lean" if overpriced
                                                        else "underpriced/long-lean" if underpriced else "neutral"),
        "borrow": borrow, "momentum_crash_regime": bool(momentum_crash),
        "size_multiplier": mult, "catalyst_required": bool(catalyst_required), "catalyst_status": catalyst_status,
        "verdict": verdict, "reads": reads,
        "note": "multiply this onto the base risk_sizing.size() weight; feed mispricing_pct from quant_factors "
                "mispricing, borrow_fee_bps from crowding_metrics, momentum_crash from quant_factors crowding gate. "
                "Refs: Ang-Hodrick-Xing-Zhang 2006; Stambaugh-Yu-Yuan 2015; Shleifer-Vishny 1997; Engelberg-Reed-Ringgenberg 2018.",
        "attribution": None if isinstance(a, dict) and "error" not in a else a.get("error"),
    }


# ════════════════════════════════════════════════════════════════════════════════════
# TAIL-RISK BATTERY — parametric fat-tail, conditional, co-crash, drawdown, higher-moment,
# and tail-loss-budget tools for the tail-risk agent. All numpy/pandas + free yfinance/FRED
# (no scipy/statsmodels/arch); every fat-tail estimator is written from closed forms.
# ════════════════════════════════════════════════════════════════════════════════════

def _ret(ticker, period="5y", log=False):
    """Daily returns Series (simple by default, log if requested), most recent history."""
    px = yf_history(ticker, period=period)["Close"]
    r = np.log(px / px.shift()) if log else px.pct_change()
    return r.dropna()


def _ewma_sigma(r, lam=0.94):
    """Predictive EWMA daily-vol Series: sigma_t uses returns through t-1 (RiskMetrics 1996;
    IGARCH(1,1) special case, so no MLE). Seed with the sample variance."""
    r = pd.Series(r).astype(float)
    v = float(r.var())
    out = np.empty(len(r))
    for i, x in enumerate(r.values):
        out[i] = np.sqrt(v)
        v = lam * v + (1 - lam) * x * x
    return pd.Series(out, index=r.index)


def _parse_book(data):
    """Defensively pull {ticker: weight} from portfolio_context.json's various shapes:
    a list of strings; list of dicts with ticker/symbol(+weight/pct); a ticker->weight dict;
    or a {'positions'|'holdings': [...]} wrapper. Equal-weights when weights are absent."""
    if isinstance(data, dict):
        for key in ("positions", "holdings", "book", "portfolio"):
            if key in data and isinstance(data[key], (list, dict)):
                return _parse_book(data[key])
        # ticker -> weight/qty dict
        out = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, (int, float)):
                out[k.upper()] = float(v)
        if out:
            return out
    if isinstance(data, list):
        out = {}
        for it in data:
            if isinstance(it, str):
                out[it.upper()] = 1.0
            elif isinstance(it, dict):
                tk = it.get("ticker") or it.get("symbol") or it.get("Symbol")
                if tk:
                    w = it.get("weight") or it.get("pct") or it.get("weight_pct") or it.get("value") or 1.0
                    try:
                        out[str(tk).upper()] = abs(float(w))
                    except (TypeError, ValueError):
                        out[str(tk).upper()] = 1.0
        if out:
            return out
    return {}


def _book_series(book_path, period="5y", errs=None):
    """Weighted book daily-return Series from portfolio_context.json, or None if unavailable."""
    if not book_path or not os.path.exists(book_path):
        return None
    try:
        holds = _parse_book(json.load(open(book_path)))
        if not holds:
            return None
        rets = {}
        for tk in holds:
            r = safe(lambda tk=tk: yf_history(tk, period=period)["Close"].pct_change(), f"book:{tk}", errs or [])
            if r is not None:
                rets[tk] = r
        if not rets:
            return None
        # Row-wise weighted mean ignoring NaNs (renormalizing weights over the names present
        # each day) — robust to holdings with short history or off-calendar (foreign) dates,
        # which an inner-join dropna() would otherwise collapse to an empty series.
        df = pd.DataFrame(rets)
        w = pd.Series({c: holds[c] for c in df.columns}, dtype=float)
        wsum = df.notna().mul(w, axis=1).sum(axis=1).replace(0, np.nan)
        book = df.mul(w, axis=1).sum(axis=1, min_count=1) / wsum
        return book.dropna()
    except Exception as e:
        if errs is not None:
            errs.append(f"book: {e}")
        return None


def _aligned(ticker, market="SPY", book=None, period="5y"):
    """Aligned (name_returns, reference_returns, ref_label, errs). Reference is the weighted
    book (if portfolio_context.json parses) else the `market` proxy."""
    errs = []
    name = yf_history(ticker, period=period)["Close"].pct_change()
    ref = _book_series(book, period, errs)
    label = "book(portfolio_context)"
    if ref is None:
        ref = yf_history(market, period=period)["Close"].pct_change()
        label = market
    df = pd.DataFrame({"x": name, "y": ref}).dropna()
    return df["x"], df["y"], label, errs


def _gpd_pwm(exceed):
    """GPD (shape xi, scale beta) by probability-weighted moments (Hosking-Wallis 1987,
    Technometrics 29(3)) — beats MLE for small exceedance samples, no scipy needed. Sign:
    xi>0 = heavy (Pareto) tail. Falls back to method-of-moments if the PWM scale degenerates."""
    y = np.sort(np.asarray(exceed, float))
    n = len(y)
    if n < 5:
        return None, None
    w0 = float(y.mean())
    j = np.arange(1, n + 1)
    w1 = float(np.mean(((n - j) / (n - 1)) * y))
    denom = w0 - 2 * w1
    if abs(denom) < 1e-12:
        return None, None
    xi = 2.0 - w0 / denom
    beta = 2.0 * w0 * w1 / denom
    if beta <= 0 or not np.isfinite(beta):
        v = float(y.var())
        if v > 0:
            xi = 0.5 * (1 - w0 ** 2 / v)      # MOM: mean^2/var = 1-2xi
            beta = w0 * (1 - xi)
        else:
            return None, None
    return float(xi), float(beta)


def _pot_var_es(u, xi, beta, n, nu, p):
    """McNeil-Frey (2000, J. Empirical Finance 7) peaks-over-threshold VaR/ES at confidence p.
    Losses positive; returns (VaR, ES) as positive loss magnitudes. ES undefined for xi>=1."""
    if xi is None or beta is None or nu <= 0:
        return None, None
    ratio = (n / nu) * (1 - p)
    if ratio <= 0:
        return None, None
    if abs(xi) < 1e-6:
        var = u - beta * np.log(ratio)
    else:
        var = u + (beta / xi) * (ratio ** (-xi) - 1)
    es = (var / (1 - xi) + (beta - xi * u) / (1 - xi)) if xi < 1 else None
    return float(var), (float(es) if es is not None else None)


def _hill(losses, k=None):
    """Hill (1975) tail-index estimator on the top-k losses: xi_hill = mean(ln L_(i) - ln L_(k+1));
    tail alpha = 1/xi_hill. Unambiguous heavy-tail read that cross-checks the GPD fit."""
    L = np.sort(np.asarray(losses, float))[::-1]
    L = L[L > 0]
    n = len(L)
    if n < 20:
        return None
    if k is None:
        k = max(10, int(0.05 * n))
    k = min(k, n - 1)
    if L[k] <= 0:
        return None
    xi = float(np.mean(np.log(L[:k]) - np.log(L[k])))
    return xi


def _expected_mdd(sigma, n, sims=400):
    """Zero-drift expected maximum drawdown via a numpy GBM-null Monte-Carlo — the random-walk
    benchmark a realized max-DD is judged against (Magdon-Ismail et al. 2004, J. Appl. Prob. 41)."""
    mdds = []
    for _ in range(sims):
        p = np.cumsum(np.random.normal(0.0, sigma, n))
        dd = p - np.maximum.accumulate(p)
        mdds.append(-dd.min())
    return float(np.mean(mdds))


def _cf_z(z, skew, exkurt):
    """Cornish-Fisher-adjusted normal quantile for skew/excess-kurtosis (Favre-Galeano 2002,
    JAI 5(2); Zangari, RiskMetrics Monitor Q1 1996). Non-monotone at extreme kurtosis — caller caps."""
    return (z + (z ** 2 - 1) / 6 * skew + (z ** 3 - 3 * z) / 24 * exkurt
            - (2 * z ** 3 - 5 * z) / 36 * skew ** 2)


# ─────────────────────────────── PARAMETRIC FAT TAIL (EVT) ──────────────────────────────

def evt(ticker, threshold_pct=90, conf=0.99, conditional=False, lam=0.94,
        bounded=False, loss_cap=1.0, backtest=False):
    """GPD peaks-over-threshold tail: shape xi (tail index), extrapolated VaR/ES beyond the
    worst observed day, at 99/99.5/99.9. `--conditional` fits the GPD on EWMA-standardized
    residuals then rescales by current vol (McNeil-Frey 2000 GARCH-EVT, EWMA-approximated).
    `--bounded` applies the Cirillo-Taleb (2016) dual transform for apparently-infinite-mean
    losses. `--backtest` adds the Acerbi-Szekely Z2 ES test + threshold-stability scan.
    Refs: McNeil-Frey 2000 (JEmpFin 7); Hosking-Wallis 1987 (Technometrics 29);
    Rockafellar-Uryasev 2002 (JBF 26); Cirillo-Taleb 2016 (Quant. Finance 16)."""
    errs = []
    r = _ret(ticker, period="5y")
    sig_now = None
    if conditional:
        sig = _ewma_sigma(r, lam)
        z = (r / sig).dropna()
        losses = (-z.values)
        sig_now = float(sig.iloc[-1])
    else:
        losses = (-r.values)
    losses_raw = losses.copy()
    if bounded:
        # dual map of a bounded loss on [0, loss_cap] to [0, inf): Z = -cap*ln(1 - L/cap)
        L = np.clip(losses[losses > 0], 1e-6, loss_cap - 1e-6)
        losses = -loss_cap * np.log(1 - L / loss_cap)
    else:
        losses = losses[losses > 0]
    n = len(losses)
    u = float(np.percentile(losses, threshold_pct))
    exceed = losses[losses > u] - u
    nu = len(exceed)
    xi, beta = _gpd_pwm(exceed)
    scale = sig_now if conditional else 1.0

    def _q(p):
        var, es = _pot_var_es(u, xi, beta, n, nu, p)
        if var is None:
            return None, None
        if bounded:                              # map quantiles back to bounded loss space
            var = loss_cap * (1 - np.exp(-var / loss_cap))
            es = loss_cap * (1 - np.exp(-es / loss_cap)) if es is not None else None
        return (-100 * scale * var, (-100 * scale * es) if es is not None else None)

    levels = sorted({round(conf, 4), 0.99, 0.995, 0.999})
    var_es = {}
    for p in levels:
        v, e = _q(p)
        var_es[f"var{int(p*1000)/10:g}_daily_pct"] = rnd(v, 2)
        var_es[f"es{int(p*1000)/10:g}_daily_pct"] = rnd(e, 2)
    emp_var95 = float(np.percentile(-r.values, 95))          # empirical baseline for comparison
    emp_cvar95 = float((-r.values)[(-r.values) >= emp_var95].mean())
    hill_xi = _hill(losses_raw[losses_raw > 0])
    out = {
        "ticker": ticker, "conditional": bool(conditional), "bounded": bool(bounded),
        "threshold_pct": threshold_pct, "threshold_loss_pct": rnd(100 * u * scale, 2),
        "n_obs": n, "n_exceedances": nu,
        "xi_gpd": rnd(xi, 3), "tail_alpha_gpd": rnd(1 / xi, 2) if (xi and xi > 0.02) else None,
        "xi_hill": rnd(hill_xi, 3), "tail_alpha_hill": rnd(1 / hill_xi, 2) if (hill_xi and hill_xi > 0.02) else None,
        **var_es,
        "empirical_cvar95_daily_pct": rnd(-100 * emp_cvar95, 2),
        "errors": errs,
    }
    if nu < 30:
        errs.append(f"only {nu} exceedances above the {threshold_pct}th pct — GPD fit is noisy; widen history or lower threshold")
    reads = []
    if xi is not None:
        reads.append(f"GPD tail shape xi={rnd(xi,2)} ({'fat — extremes not rare, ES >> VaR' if xi > 0.3 else 'moderate' if xi > 0.1 else 'near-thin'})")
    if out.get("es99_daily_pct") is not None and out.get("var99_daily_pct"):
        reads.append(f"99% ES {out['es99_daily_pct']}% vs empirical CVaR95 {out['empirical_cvar95_daily_pct']}% — parametric tail extrapolates past the worst observed day")
    if conditional:
        reads.append(f"conditional on current EWMA vol (sigma_now={rnd(100*sig_now,1)}%/day) — reacts to stress within days")

    if backtest:
        p = round(conf, 4)
        var_p, es_p = _pot_var_es(u, xi, beta, n, nu, p)
        if var_p is not None and es_p:
            lv = losses if not bounded else losses_raw[losses_raw > 0]
            excs = lv[lv > var_p]
            z2 = float(excs.sum() / (len(lv) * (1 - p) * es_p) - 1) if es_p > 0 else None
            grid = [85, 88, 90, 92, 94]
            xis = []
            for tp in grid:
                uu = float(np.percentile(losses, tp))
                xg, _ = _gpd_pwm(losses[losses > uu] - uu)
                if xg is not None:
                    xis.append(xg)
            xi_range = (max(xis) - min(xis)) if len(xis) >= 2 else None
            out["backtest"] = {
                "acerbi_szekely_z2": rnd(z2, 3),
                "z2_read": ("ES adequate (|Z2| small)" if (z2 is not None and abs(z2) < 0.10)
                            else "ES may understate the tail (Z2<0) — treat as fragile, defer to empirical CVaR" if (z2 is not None and z2 < -0.10)
                            else "ES conservative (Z2>0)" if z2 is not None else None),
                "xi_across_thresholds": [rnd(x, 3) for x in xis],
                "xi_range": rnd(xi_range, 3),
                "threshold_stable": bool(xi_range is not None and xi_range < 0.25),
            }
            reads.append("backtest: " + (out["backtest"]["z2_read"] or "n/a")
                         + ("; xi stable across thresholds" if out["backtest"]["threshold_stable"] else "; xi UNSTABLE across thresholds — fragile fit"))
    out["read"] = "; ".join(reads)
    out["note"] = ("GPD-POT fit by probability-weighted moments (Hosking-Wallis); ES = mean loss "
                   "beyond VaR. Refs: McNeil-Frey 2000; Rockafellar-Uryasev 2002; Cirillo-Taleb 2016.")
    return out


def condvol(ticker, lam=0.94):
    """Conditional (EWMA) volatility + Gaussian conditional VaR/ES. The sigma_now/static ratio
    is the cleanest 'fragile now vs historically wild' read; conditional VaR reacts in days so
    the size-down fires entering stress, not in calm. Refs: RiskMetrics 1996; Bollerslev 1986."""
    r = _ret(ticker, period="3y")
    sig = _ewma_sigma(r, lam)
    sig_now = float(sig.iloc[-1])
    static = float(r.std())
    ratio = sig_now / static if static > 0 else None
    def cvar_es(p):
        z = _Z[p]
        return -100 * z * sig_now, -100 * sig_now * _norm_pdf(z) / (1 - p)
    v95, e95 = cvar_es(0.95)
    v99, e99 = cvar_es(0.99)
    return {
        "ticker": ticker, "lambda": lam,
        "ewma_vol_daily_pct": rnd(100 * sig_now, 2),
        "ewma_vol_ann_pct": rnd(100 * sig_now * np.sqrt(252), 1),
        "static_vol_daily_pct": rnd(100 * static, 2),
        "cond_over_static_ratio": rnd(ratio, 2),
        "cond_var95_daily_pct": rnd(v95, 2), "cond_es95_daily_pct": rnd(e95, 2),
        "cond_var99_daily_pct": rnd(v99, 2), "cond_es99_daily_pct": rnd(e99, 2),
        "regime": ("vol expanding — entering stress" if ratio and ratio > 1.3
                   else "vol compressing — calm" if ratio and ratio < 0.7 else "vol near its own norm"),
        "read": f"EWMA vol {rnd(100*sig_now*np.sqrt(252),0)}%/yr is {rnd(ratio,2)}x the static full-sample vol; "
                + ("conditioning flags stress the unconditional stats miss" if ratio and ratio > 1.3 else "no conditional stress signal"),
        "note": "EWMA filtering (lambda=0.94), not a fitted GARCH; Gaussian conditional VaR/ES. RiskMetrics 1996.",
    }


# ─────────────────────────────── CO-CRASH / CORRELATION REGIME ──────────────────────────

def mes(ticker, market="SPY", tail_pct=5):
    """Marginal Expected Shortfall: mean name return on the market's worst `tail_pct`% days —
    the core 'does it crash WITH the system' number, plus an EWMA-vol-scaled stress MES.
    Refs: Acharya-Pedersen-Philippon-Richardson 2017 (RFS 30); Brownlees-Engle 2017 (RFS 30)."""
    x, y, label, errs = _aligned(ticker, market, None)
    if len(x) < 20:
        return {"ticker": ticker, "market": label, "error": "insufficient overlapping history", "errors": errs}
    thr = float(np.percentile(y.values, tail_pct))
    mask = y.values <= thr
    xt = x.values[mask]
    mes_val = float(xt.mean()) if len(xt) else None
    beta_tail = float(np.cov(x.values[mask], y.values[mask])[0, 1] / np.var(y.values[mask])) if mask.sum() > 2 else None
    sig = _ewma_sigma(x, 0.94)
    stress_scale = float(sig.iloc[-1] / sig.mean()) if sig.mean() > 0 else 1.0
    return {
        "ticker": ticker, "market": label, "tail_pct": tail_pct, "n_tail_days": int(mask.sum()),
        "mes_pct": rnd(100 * mes_val, 2) if mes_val is not None else None,
        "stress_mes_pct": rnd(100 * mes_val * stress_scale, 2) if mes_val is not None else None,
        "tail_beta": rnd(beta_tail, 2),
        "read": (f"on the market's worst {tail_pct}% days this name averages {rnd(100*mes_val,1)}% "
                 + ("— amplifies system crashes (size down)" if (mes_val or 0) < -0.02 else "— resilient in market tails")) if mes_val is not None else "insufficient tail days",
        "note": "Historical MES + EWMA stress scaling; LRMES would need GARCH-DCC (out of stack). Acharya et al. 2017.",
        "errors": errs,
    }


def exceedcorr(ticker, market="SPY", book=None, thresholds=(0, 0.5, 1.0, 1.5)):
    """Longin-Solnik (2001, JF 56) exceedance correlation: correlation conditional on BOTH
    series exceeding +/- theta std, down-side vs up-side, with the Ang-Chen (2002, JFE 63) H
    asymmetry vs a bivariate-normal Monte-Carlo benchmark. Builds the claimed-but-missing
    correlation regime: does diversification evaporate on the downside."""
    x, y, label, errs = _aligned(ticker, market, book)
    if len(x) < 20:
        return {"ticker": ticker, "reference": label, "error": "insufficient overlapping history", "errors": errs}
    xs = (x - x.mean()) / x.std()
    ys = (y - y.mean()) / y.std()
    xv, yv = xs.values, ys.values
    rho_full = float(np.corrcoef(xv, yv)[0, 1])
    down, up = {}, {}
    for th in thresholds:
        dm = (xv < -th) & (yv < -th)
        um = (xv > th) & (yv > th)
        down[th] = float(np.corrcoef(xv[dm], yv[dm])[0, 1]) if dm.sum() > 3 else None
        up[th] = float(np.corrcoef(xv[um], yv[um])[0, 1]) if um.sum() > 3 else None
    # Gaussian-copula MC benchmark for the deepest threshold (symmetric by construction)
    th_deep = max(thresholds)
    L = np.linalg.cholesky([[1, rho_full], [rho_full, 1]])
    sim = (np.random.normal(size=(20000, 2)) @ L.T)
    dm = (sim[:, 0] < -th_deep) & (sim[:, 1] < -th_deep)
    rho_norm_deep = float(np.corrcoef(sim[dm, 0], sim[dm, 1])[0, 1]) if dm.sum() > 3 else None
    pairs = [(down[t], up[t]) for t in thresholds if down[t] is not None and up[t] is not None]
    h_asym = float(np.mean([d - u for d, u in pairs])) if pairs else None
    return {
        "ticker": ticker, "reference": label, "corr_full": rnd(rho_full, 2),
        "exceed_corr_down": {str(t): rnd(down[t], 2) for t in thresholds},
        "exceed_corr_up": {str(t): rnd(up[t], 2) for t in thresholds},
        "h_asymmetry_down_minus_up": rnd(h_asym, 2),
        "normal_benchmark_down_deep": rnd(rho_norm_deep, 2), "deepest_threshold": th_deep,
        "read": (("downside exceedance corr exceeds upside (H={}) and stays above the normal benchmark "
                  "— diversification vs {} evaporates in the tail (correlation-goes-to-one)".format(rnd(h_asym, 2), label))
                 if (h_asym or 0) > 0.05 else "no material downside correlation asymmetry"),
        "note": "Longin-Solnik 2001 exceedance corr; Ang-Chen 2002 asymmetry; a bivariate normal gives symmetric exceedance corr decaying to 0 in the tail.",
        "errors": errs,
    }


def taildep(ticker, market="SPY", book=None, q=(0.05, 0.10)):
    """Lower-tail dependence lambda_L = C(q,q)/q, the empirical probability the name is in its
    left tail GIVEN the book is — a co-crash FREQUENCY that beta/downside-beta/coskew miss
    (Chabi-Yo-Ruenzi-Weigert 2018, JFQA 53; Patton 2006, IER 47)."""
    x, y, label, errs = _aligned(ticker, market, book)
    if len(x) < 20:
        return {"ticker": ticker, "reference": label, "error": "insufficient overlapping history", "errors": errs}
    out = {"ticker": ticker, "reference": label, "lower_tail_dependence": {}, "conditioning_days": {}}
    reads = []
    for qq in q:
        qx, qy = np.quantile(x.values, qq), np.quantile(y.values, qq)
        ymask = y.values <= qy
        both = (x.values <= qx) & ymask
        n_ref = int(ymask.sum())
        lam = float(both.sum() / n_ref) if n_ref > 0 else None
        out["lower_tail_dependence"][str(qq)] = rnd(lam, 2)
        out["conditioning_days"][str(qq)] = n_ref
        if lam is not None:
            reads.append(f"q={qq}: lambda_L={rnd(lam,2)}")
        if n_ref < 15:
            errs.append(f"q={qq}: only {n_ref} conditioning days — lambda_L noisy")
    lam10 = out["lower_tail_dependence"].get("0.1")
    out["read"] = ("; ".join(reads) + " — " +
                   ("HIGH co-crash probability with the book (size down)" if (lam10 or 0) > 0.4
                    else "modest tail co-dependence"))
    out["note"] = "Nonparametric empirical-copula estimator (no fitted copula/scipy). Chabi-Yo et al. 2018."
    out["errors"] = errs
    return out


def dbeta(ticker, market="SPY", book=None):
    """Downside beta (beta on down-market days) vs upside beta, with a block-bootstrap
    Hong-Tu-Zhou significance gate so only statistically-real asymmetry fires (anti-cries-wolf).
    Refs: Ang-Chen-Xing 2006 (RFS 19); Hong-Tu-Zhou 2007 (RFS 20)."""
    x, y, label, errs = _aligned(ticker, market, book)
    if len(x) < 20:
        return {"ticker": ticker, "reference": label, "error": "insufficient overlapping history", "errors": errs}
    xv, yv = x.values, y.values
    ybar = yv.mean()
    def beta(mask):
        if mask.sum() < 5:
            return None
        return float(np.cov(xv[mask], yv[mask])[0, 1] / np.var(yv[mask]))
    b_full = float(np.cov(xv, yv)[0, 1] / np.var(yv))
    b_dn, b_up = beta(yv < ybar), beta(yv > ybar)
    asym = (b_dn - b_up) if (b_dn is not None and b_up is not None) else None
    # block bootstrap p-value that (beta_minus - beta_plus) > 0
    pval = None
    if asym is not None:
        n = len(xv); bl = 10; gaps = []
        idx = np.arange(n)
        for _ in range(500):
            starts = np.random.randint(0, n - bl, size=n // bl)
            sel = np.concatenate([idx[s:s + bl] for s in starts])
            xb, yb = xv[sel], yv[sel]
            yb_bar = yb.mean()
            dm, um = yb < yb_bar, yb > yb_bar
            if dm.sum() < 5 or um.sum() < 5:
                continue
            gd = np.cov(xb[dm], yb[dm])[0, 1] / np.var(yb[dm])
            gu = np.cov(xb[um], yb[um])[0, 1] / np.var(yb[um])
            gaps.append(gd - gu)
        if gaps:
            pval = float(np.mean(np.array(gaps) <= 0))    # one-sided: prob asym not positive
    return {
        "ticker": ticker, "reference": label,
        "beta_full": rnd(b_full, 2), "beta_minus": rnd(b_dn, 2), "beta_plus": rnd(b_up, 2),
        "beta_asymmetry": rnd(asym, 2), "bootstrap_p_value": rnd(pval, 3),
        "significant": bool(pval is not None and pval < 0.10 and (asym or 0) > 0),
        "read": ((f"downside beta {rnd(b_dn,2)} > upside beta {rnd(b_up,2)} (p={rnd(pval,2)}) — "
                  "amplifies losses more than gains; a fragility multiplier") if (pval is not None and pval < 0.10 and (asym or 0) > 0)
                 else "no significant up/down beta asymmetry"),
        "note": "Down/up-market conditioned betas; block-bootstrap approximates the Hong-Tu-Zhou test. Ang-Chen-Xing 2006.",
        "errors": errs,
    }


def comoments(ticker, bench="SPY", book=None):
    """Systematic coskewness & cokurtosis: does the name get more negatively skewed exactly when
    the market crashes. Negative coskew = a PAID systematic risk; positive = an unpaid lottery
    (overpriced). Refs: Harvey-Siddique 2000 (JF 55); Dittmar 2002 (JF 57)."""
    x, y, label, errs = _aligned(ticker, bench, book)
    if len(x) < 20:
        return {"ticker": ticker, "reference": label, "error": "insufficient overlapping history", "errors": errs}
    ex = (x - x.mean()).values
    em = (y - y.mean()).values
    si, sm, vm = ex.std(), em.std(), em.var()
    coskew = float(np.mean(ex * em ** 2) / (si * vm)) if si > 0 and vm > 0 else None
    cokurt = float(np.mean(ex * em ** 3) / (si * sm ** 3)) if si > 0 and sm > 0 else None
    # Harvey-Siddique direct coskewness beta from OLS of ex on [em, em^2]
    A = np.column_stack([np.ones_like(em), em, em ** 2])
    coef, *_ = np.linalg.lstsq(A, ex, rcond=None)
    return {
        "ticker": ticker, "reference": label,
        "coskewness": rnd(coskew, 3), "cokurtosis": rnd(cokurt, 2),
        "coskew_beta_hs": rnd(float(coef[2]), 3),
        "read": (("negative coskew — more downside-skewed when the market falls; a PAID systematic tail risk"
                  if (coskew or 0) < -0.02 else
                  "positive coskew — lottery-like (right-skewed with the market); prone to overpricing"
                  if (coskew or 0) > 0.02 else "near-zero coskew")),
        "note": "Standardized co-moments + Harvey-Siddique OLS coskew beta; physical (realized) estimator. Harvey-Siddique 2000.",
        "errors": errs,
    }


# ─────────────────────────────── DRAWDOWN / SEMIVARIANCE / LEFT TAIL ─────────────────────

def drawdown(ticker, window=63, alpha=0.10):
    """Conditional Expected Drawdown: the tail of the drawdown DISTRIBUTION (mean of the worst
    alpha% of rolling-window max-drawdowns), a random-walk expected-max-DD null, and
    time-under-water. Refs: Goldberg-Mahmoud 2017 (Math.&Fin.Econ. 11); Magdon-Ismail et al. 2004."""
    px = yf_history(ticker, period="5y")["Close"]
    r = px.pct_change().dropna()
    dd = (px / px.cummax() - 1)
    # rolling-window max drawdown distribution
    roll_mdd = []
    vals = px.values
    for i in range(0, len(vals) - window):
        w = vals[i:i + window]
        roll_mdd.append((w / np.maximum.accumulate(w) - 1).min())
    roll_mdd = np.array(roll_mdd)
    ced = float(np.mean(np.sort(roll_mdd)[:max(1, int(alpha * len(roll_mdd)))])) if len(roll_mdd) else None
    max_dd = float(dd.min())
    exp_mdd = _expected_mdd(float(r.std()), len(r))
    # time under water: current + longest spell
    uw = (dd.values < 0).astype(int)
    spells, cur = [], 0
    for v in uw:
        cur = cur + 1 if v else 0
        spells.append(cur)
    cur_uw = int(spells[-1])
    max_uw = int(max(spells)) if spells else 0
    return {
        "ticker": ticker, "window_days": window, "alpha": alpha,
        "ced_pct": rnd(100 * ced, 1) if ced is not None else None,
        "max_dd_pct": rnd(100 * max_dd, 1),
        "expected_mdd_null_pct": rnd(-100 * exp_mdd, 1),
        "dd_excess_ratio": rnd(abs(max_dd) / exp_mdd, 2) if exp_mdd > 0 else None,
        "current_underwater_days": cur_uw, "max_underwater_days": max_uw,
        "read": (f"CED (worst {int(alpha*100)}% of {window}d drawdowns) = {rnd(100*ced,1)}%; realized max-DD is "
                 f"{rnd(abs(max_dd)/exp_mdd,1)}x the random-walk null — "
                 + ("grinding/self-reinforcing drawdowns beyond chance (fragile)" if (exp_mdd > 0 and abs(max_dd) / exp_mdd > 1.5)
                    else "drawdowns roughly in line with a random walk")) if ced is not None else "insufficient history",
        "note": "CED = tail of the rolling max-DD distribution; expected-MDD null via GBM Monte-Carlo. Goldberg-Mahmoud 2017.",
    }


def semivar(ticker, window=252):
    """Realized semivariance: split total variance into downside vs upside, with Sortino and a
    short-window rolling realized skew — isolates loss-driven (persistent) variance from harmless
    upside vol. Refs: Barndorff-Nielsen-Kinnebrock-Shephard 2010; Patton-Sheppard 2015 (REStat 97)."""
    r = _ret(ticker, period="3y").tail(window)
    rv = r.values
    dn = float(np.sum(rv[rv < 0] ** 2))
    up = float(np.sum(rv[rv > 0] ** 2))
    dd_dev = float(np.sqrt(np.mean(np.minimum(rv, 0) ** 2)))
    sortino = float((rv.mean() * 252 - 0.04) / (dd_dev * np.sqrt(252))) if dd_dev > 0 else None
    return {
        "ticker": ticker, "window_days": int(len(r)),
        "semivar_down_ann_vol_pct": rnd(100 * np.sqrt(dn / len(rv) * 252), 1),
        "semivar_up_ann_vol_pct": rnd(100 * np.sqrt(up / len(rv) * 252), 1),
        "down_up_semivar_ratio": rnd(dn / up, 2) if up > 0 else None,
        "sortino_rf4": rnd(sortino, 2),
        "roll_skew_21d": rnd(float(r.tail(21).skew()), 2),
        "roll_skew_63d": rnd(float(r.tail(63).skew()), 2),
        "read": (("downside variance exceeds upside (ratio {}) — loss-driven, forecast-relevant vol; "
                  "genuine fragility not just choppiness".format(rnd(dn / up, 2))) if (up > 0 and dn / up > 1.1)
                 else "variance is symmetric/upside-tilted — high vol here is not fragility"),
        "note": "Daily analogue of realized-semivariance (the free-data proxy). Barndorff-Nielsen et al. 2010; Patton-Sheppard 2015.",
    }


def lefttail(ticker):
    """Left-tail momentum: persistence of the name's own VaR/ES (left-tail risk is sticky and
    under-reacted to) plus the MAX lottery flag. Forward-looking, name-specific.
    Refs: Atilgan-Bali-Demirtas-Gunaydin 2020 (JFE 135); Bali-Cakici-Whitelaw 2011 (JFE 99)."""
    r = _ret(ticker, period="3y")
    rv = r.values
    def es(window_vals):
        v = np.percentile(window_vals, 5)
        tail = window_vals[window_vals <= v]
        return float(tail.mean()) if len(tail) else None
    es_now = es(rv[-21:])
    es_prev = es(rv[-42:-21])
    es_252 = es(rv[-252:])
    # own-history percentile of the current 21d ES
    roll_es = pd.Series(rv).rolling(21).apply(lambda w: es(w), raw=True).dropna()
    es_pct = float((roll_es <= (es_now if es_now is not None else roll_es.min())).mean() * 100) if len(roll_es) else None
    max5 = float(np.mean(np.sort(rv[-21:])[-5:]))
    roll_max = pd.Series(rv).rolling(21).apply(lambda w: np.mean(np.sort(w)[-5:]), raw=True).dropna()
    max_pct = float((roll_max <= max5).mean() * 100) if len(roll_max) else None
    persist = bool(es_now is not None and es_prev is not None and es_now < es_prev and (es_pct or 0) > 60)
    return {
        "ticker": ticker,
        "es_21d_daily_pct": rnd(100 * es_now, 2) if es_now is not None else None,
        "es_prev_21d_daily_pct": rnd(100 * es_prev, 2) if es_prev is not None else None,
        "es_252d_daily_pct": rnd(100 * es_252, 2) if es_252 is not None else None,
        "es_own_history_pctile": rnd(es_pct, 0),
        "left_tail_persistent": persist,
        "max5_21d_daily_pct": rnd(100 * max5, 2), "max_own_history_pctile": rnd(max_pct, 0),
        "read": (("left tail is deep AND in the worst {}th pct of its own history and still worsening — "
                  "sticky fragility (size down)".format(rnd(es_pct, 0))) if persist
                 else "left-tail risk not currently elevated/persistent vs the name's own history"),
        "note": "Rolling VaR/ES persistence + MAX lottery signal. Atilgan et al. 2020; Bali-Cakici-Whitelaw 2011.",
    }


# ─────────────────────────────── TIER 2/3: DRAWDOWN-BETA, COVAR, VOL, CRASH, SIZING ──────

def ddbeta(ticker, market="SPY"):
    """Drawdown beta: co-movement of the name's drawdown path with the market's during the
    market's WORST drawdowns — high = de-diversifies in a crash; low/negative = hedge candidate.
    Refs: Zabarankin-Pavlikov-Uryasev 2014 (EJOR 234); Chekhlov-Uryasev-Zabarankin 2005."""
    px_n = yf_history(ticker, period="5y")["Close"]
    px_m = yf_history(market, period="5y")["Close"]
    df = pd.DataFrame({"n": px_n, "m": px_m}).dropna()
    dd_n = (df["n"] / df["n"].cummax() - 1).values
    dd_m = (df["m"] / df["m"].cummax() - 1).values
    thr = np.percentile(dd_m, 10)                     # market's worst-drawdown observations
    mask = dd_m <= thr
    ddb = float(np.cov(dd_n[mask], dd_m[mask])[0, 1] / np.var(dd_m[mask])) if mask.sum() > 3 else None
    return {
        "ticker": ticker, "market": market, "n_worst_dd_obs": int(mask.sum()),
        "drawdown_beta": rnd(ddb, 2),
        "read": (("high drawdown beta — de-diversifies during market drawdowns (correlation-goes-to-one on the path)"
                  if (ddb or 0) > 1.0 else "low/negative drawdown beta — behaves like a drawdown hedge") if ddb is not None else "insufficient drawdown episodes"),
        "note": "Co-movement of drawdown paths restricted to the market's worst-DD windows. Zabarankin et al. 2014.",
    }


def covar(ticker, market="SPY", book=None, q=(0.05, 0.10)):
    """delta-CoVaR: how much the book/market tail (VaR) widens when THIS name is in distress
    vs at its median — the name->system spillover, i.e. how much trimming it de-risks the book.
    Empirical conditional-VaR approximation. Ref: Adrian-Brunnermeier 2016 (AER 106)."""
    x, y, label, errs = _aligned(ticker, market, book)
    if len(x) < 20:
        return {"ticker": ticker, "reference": label, "error": "insufficient overlapping history", "errors": errs}
    xv, yv = x.values, y.values
    out = {"ticker": ticker, "system": label, "delta_covar_pct": {}, "n": {}}
    reads = []
    for qq in q:
        qx = np.quantile(xv, qq)
        med = np.median(xv)
        band = (np.abs(xv - med) <= 0.5 * xv.std())
        distress = xv <= qx
        if distress.sum() > 3 and band.sum() > 3:
            covar_d = np.percentile(yv[distress], 100 * qq)
            covar_m = np.percentile(yv[band], 100 * qq)
            out["delta_covar_pct"][str(qq)] = rnd(100 * (covar_d - covar_m), 2)
            out["n"][str(qq)] = int(distress.sum())
            reads.append(f"q={qq}: dCoVaR={rnd(100*(covar_d-covar_m),2)}%")
    out["read"] = ("; ".join(reads) + " — larger (more negative) = trimming this name most reduces book tail") if reads else "insufficient tail days"
    out["note"] = "Empirical conditional-VaR proxy (exact CoVaR needs quantile regression). Note SPY 'system' contains the name. Adrian-Brunnermeier 2016."
    out["errors"] = errs
    return out


def rangevol(ticker, window=21):
    """Yang-Zhang (2000, J. Business 73) drift-independent, gap-robust OHLC volatility — ~5-14x
    more efficient than close-to-close, so it detects a vol expansion weeks earlier. Parkinson
    (1980) as a cross-check."""
    df = yf_history(ticker, period="6mo").tail(window + 1)
    o = np.log(df.Open / df.Close.shift()).dropna()
    u = np.log(df.High / df.Open)
    d = np.log(df.Low / df.Open)
    c = np.log(df.Close / df.Open)
    n = len(o)
    o, u, d, c = o.values, u.values[-n:], d.values[-n:], c.values[-n:]
    Vo = np.sum((o - o.mean()) ** 2) / (n - 1)
    Vc = np.sum((c - c.mean()) ** 2) / (n - 1)
    Vrs = np.mean(u * (u - c) + d * (d - c))
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    yz = Vo + k * Vc + (1 - k) * Vrs
    park = np.mean(np.log(df.High / df.Low).values[-n:] ** 2) / (4 * np.log(2))
    cc = _ret(ticker, period="6mo").tail(window).var()
    return {
        "ticker": ticker, "window_days": window,
        "yang_zhang_ann_vol_pct": rnd(100 * np.sqrt(max(yz, 0) * 252), 1),
        "parkinson_ann_vol_pct": rnd(100 * np.sqrt(park * 252), 1),
        "close_to_close_ann_vol_pct": rnd(100 * np.sqrt(cc * 252), 1),
        "read": "Yang-Zhang OHLC vol is the low-variance stand-in for high-frequency realized vol; "
                "a YZ reading above close-to-close vol is an early vol-expansion tell.",
        "note": "Yang-Zhang 2000 (overnight + Rogers-Satchell + open-close); Parkinson 1980 cross-check.",
    }


def crashfc(ticker):
    """Firm-level crash-risk forecast: NCSKEW and DUVOL (down-vs-up volatility), conditioned on a
    volume surge + prolonged run-up — a conditional (not unconditional) crash trigger.
    Ref: Chen-Hong-Stein 2001 (JFE 61)."""
    px = yf_history(ticker, period="3y")["Close"]
    vol = yf_history(ticker, period="3y")["Volume"]
    r = px.pct_change().dropna().values
    n = len(r); s2 = np.sum(r ** 2)
    ncskew = float(-(n * (n - 1) ** 1.5 * np.sum(r ** 3)) / ((n - 1) * (n - 2) * s2 ** 1.5)) if s2 > 0 else None
    dn, up = r[r < r.mean()], r[r >= r.mean()]
    duvol = float(np.log(((len(up) - 1) * np.sum(dn ** 2)) / ((len(dn) - 1) * np.sum(up ** 2)))) if len(dn) > 1 and len(up) > 1 and np.sum(up ** 2) > 0 else None
    v = vol.tail(126).values
    vtrend = float(np.polyfit(np.arange(len(v)), v, 1)[0]) if len(v) > 10 else None
    runup = float(px.iloc[-1] / px.tail(756).iloc[0] - 1) if len(px) > 60 else None
    score = 0
    for cond in [(ncskew or 0) > 0.3, (duvol or 0) > 0.2, (vtrend or 0) > 0, (runup or 0) > 0.5]:
        score += int(bool(cond))
    return {
        "ticker": ticker, "ncskew": rnd(ncskew, 2), "duvol": rnd(duvol, 2),
        "volume_trend_up": bool((vtrend or 0) > 0), "runup_3y_pct": rnd(100 * runup, 0) if runup is not None else None,
        "crash_forecast_score_0_4": score,
        "read": ("elevated firm-level crash risk (negative skew + down-vol dominance + volume surge/run-up) — size down"
                 if score >= 3 else "crash-risk conditions not aligned"),
        "note": "NCSKEW/DUVOL conditioned on detrended volume + run-up. Chen-Hong-Stein 2001.",
    }


def esbudget(ticker, nav=100000, tail_budget_pct=3.0, alpha=0.95, horizon_days=10):
    """CVaR tail-loss-budget sizing: the max weight whose horizon CVaR fits a tail-loss budget.
    Because CVaR is left-tail-dominated, it sizes fat-tailed names down harder than same-vol
    symmetric names. Refs: Rockafellar-Uryasev 2000/2002 (J. Risk / JBF 26)."""
    r_ser = _ret(ticker, period="3y")
    r = r_ser.values
    var_a = np.percentile(r, 100 * (1 - alpha))
    cvar_d = float(r[r <= var_a].mean())               # daily CVaR (negative)
    cvar_h = cvar_d * np.sqrt(horizon_days)
    tb = tail_budget_pct / 100.0
    w = min(1.0, tb / abs(cvar_h)) if cvar_h < 0 else 1.0
    vol_ann_pct = rnd(100 * realized_vol_ann(r_ser, VOL_WINDOW), 1)
    max_weight_pct = rnd(100 * w, 1)
    return {
        "ticker": ticker, "alpha": alpha, "horizon_days": horizon_days, "tail_budget_pct": tail_budget_pct,
        "cvar_daily_pct": rnd(100 * cvar_d, 2), "cvar_horizon_pct": rnd(100 * cvar_h, 2),
        "vol_ann_63d_pct": vol_ann_pct,
        "max_weight_pct": max_weight_pct,
        "max_position_value": rnd(nav * w, 0),
        "sizing_artifact_warning": _sizing_artifact_warning(max_weight_pct, vol_ann_pct),
        "read": f"to keep {int(horizon_days)}d CVaR({int(alpha*100)}%) within a {tail_budget_pct}% NAV tail budget, "
                f"cap weight at ~{rnd(100*w,1)}% NAV",
        "note": "Empirical CVaR + sqrt-time horizon scaling; single-name scalar inversion (not a portfolio LP). Rockafellar-Uryasev 2000.",
    }


def ddbudget(ticker, nav=100000, dd_budget_pct=15.0, beta=0.90):
    """CDaR drawdown-budget sizing + a Grossman-Zhou (1993) surplus note: cap weight so the
    conditional drawdown fits a max-drawdown budget. Ref: Chekhlov-Uryasev-Zabarankin 2005."""
    px = yf_history(ticker, period="5y")["Close"]
    ret = px.pct_change().dropna()
    dd = (px / px.cummax() - 1).values
    thr = np.percentile(dd, 100 * (1 - beta))
    cdar = float(dd[dd <= thr].mean())                 # conditional drawdown-at-risk (negative)
    exp_mdd = _expected_mdd(float(ret.std()), len(px))
    db = dd_budget_pct / 100.0
    w = min(1.0, db / abs(cdar)) if cdar < 0 else 1.0
    vol_ann_pct = rnd(100 * realized_vol_ann(ret, VOL_WINDOW), 1)
    max_weight_pct = rnd(100 * w, 1)
    return {
        "ticker": ticker, "beta": beta, "dd_budget_pct": dd_budget_pct,
        "cdar_pct": rnd(100 * cdar, 1), "expected_mdd_null_pct": rnd(-100 * exp_mdd, 1),
        "vol_ann_63d_pct": vol_ann_pct,
        "max_weight_pct": max_weight_pct, "max_position_value": rnd(nav * w, 0),
        "sizing_artifact_warning": _sizing_artifact_warning(max_weight_pct, vol_ann_pct),
        "read": f"to keep the conditional drawdown within a {dd_budget_pct}% NAV budget, cap weight at ~{rnd(100*w,1)}% NAV; "
                "tighten the brake automatically as the holding approaches its drawdown floor (Grossman-Zhou surplus)",
        "note": "Empirical CDaR (mean of worst 1-beta drawdowns); Grossman-Zhou 1993 surplus principle. Chekhlov et al. 2005.",
    }


def kelly_tail(ticker, fraction=0.33, dd_threshold=20.0):
    """Fat-tail fractional Kelly: shrink the growth-optimal fraction for skew/excess-kurtosis
    (Cornish-Fisher) and report P(drawdown > threshold). The current Gaussian kelly() over-bets
    fat-tailed names. Refs: MacLean-Thorp-Ziemba 2010 (Quant.Finance 10); Favre-Galeano 2002 (JAI 5)."""
    r = _ret(ticker, period="3y").values
    mu, var = float(r.mean()), float(r.var())
    f_gauss = mu / var if var > 0 else None
    sk, ek = float(pd.Series(r).skew()), float(pd.Series(r).kurtosis())
    # Use a DEEP quantile (1%) for the shrink: at 1% excess kurtosis fattens the tail
    # (at 5% Cornish-Fisher is non-monotone and can narrow it), so a fat-tailed name shrinks.
    z = -_Z[0.99]
    z_cf = _cf_z(z, sk, ek)
    shrink = abs(z) / abs(z_cf) if z_cf != 0 else 1.0
    shrink = float(np.clip(shrink, 0.2, 1.0))          # CF non-monotone at extreme kurtosis — cap
    f_tail = f_gauss * shrink if f_gauss is not None else None
    frac_kelly = f_tail * fraction if f_tail is not None else None
    # P(max drawdown > threshold): bootstrap 1y wealth paths at the FRACTIONAL-Kelly leverage
    prob = None
    if frac_kelly is not None and frac_kelly > 0:
        hits = 0
        for _ in range(400):
            samp = np.random.choice(r, 252, replace=True)
            p = np.cumprod(1 + np.clip(frac_kelly * samp, -0.99, None))
            mdd = (p / np.maximum.accumulate(p) - 1).min()
            hits += int(mdd < -dd_threshold / 100.0)
        prob = hits / 400.0
    return {
        "ticker": ticker, "fraction": fraction,
        "f_gaussian_pct": rnd(100 * f_gauss, 1) if f_gauss is not None else None,
        "cf_shrink": rnd(shrink, 2), "skew": rnd(sk, 2), "excess_kurtosis": rnd(ek, 1),
        "f_tail_pct": rnd(100 * f_tail, 1) if f_tail is not None else None,
        "fractional_kelly_pct": rnd(100 * f_tail * fraction, 1) if f_tail is not None else None,
        "prob_drawdown_exceeds_pct": rnd(100 * prob, 0) if prob is not None else None,
        "dd_threshold_pct": dd_threshold,
        "read": (f"fat left tail (skew {rnd(sk,1)}, excess-kurt {rnd(ek,0)}) shrinks the Kelly fraction by {rnd((1-shrink)*100,0)}%; "
                 f"P(drawdown>{int(dd_threshold)}%) ~ {rnd(100*prob,0)}%" if prob is not None else "insufficient data"),
        "note": "Cornish-Fisher-shrunk Kelly + bootstrap drawdown probability. MacLean-Thorp-Ziemba 2010; Favre-Galeano 2002 (Zangari, RiskMetrics Monitor Q1 1996).",
    }


def robust(ticker, nav=100000, stat="cvar", n_boot=2000):
    """Estimation-error robustness: bootstrap the driving tail statistic, report a confidence band,
    and shrink the point estimate toward a conservative prior in proportion to the noise —
    'when the fragility signal is itself uncertain, size smaller'. Refs: DeMiguel-Garlappi-Uppal
    2009 (RFS 22); Michaud 1989 (FAJ 45)."""
    r = _ret(ticker, period="3y").values
    def compute(sample):
        if stat == "vol":
            return float(sample.std() * np.sqrt(252))
        if stat == "kelly":
            v = sample.var()
            return float(sample.mean() / v) if v > 0 else 0.0
        var_a = np.percentile(sample, 5)               # default: cvar95
        return float(sample[sample <= var_a].mean())
    point = compute(r)
    boots = np.array([compute(np.random.choice(r, len(r), replace=True)) for _ in range(min(n_boot, 3000))])
    lo, hi = float(np.percentile(boots, 5)), float(np.percentile(boots, 95))
    band = abs(hi - lo)
    rel_band = band / (abs(point) + 1e-9)
    shrink = float(np.clip(1 - rel_band, 0.3, 1.0))    # wider band -> shrink weight harder
    return {
        "ticker": ticker, "stat": stat, "n_boot": int(min(n_boot, 3000)),
        "point_estimate": rnd(point, 4), "boot_5pct": rnd(lo, 4), "boot_95pct": rnd(hi, 4),
        "band_width": rnd(band, 4), "relative_band": rnd(rel_band, 2), "shrink_factor": rnd(shrink, 2),
        "read": (f"the {stat} estimate is uncertain (5-95 band {rnd(lo,3)}..{rnd(hi,3)}); shrink the sizing input by "
                 f"{rnd((1-shrink)*100,0)}% for estimation risk" if rel_band > 0.3
                 else f"the {stat} estimate is stable — no material estimation-risk haircut"),
        "note": "Nonparametric bootstrap band + noise-proportional shrinkage. DeMiguel-Garlappi-Uppal 2009; Michaud 1989.",
    }


def condcorr(ticker, market="SPY", book=None, lam=0.94, floor=0.8):
    """EWMA conditional correlation trajectory vs the book + a crisis-shrinkage floor so the
    sizing input never credits diversification that evaporates in stress — flags fragility
    BUILDING before the crash. Refs: Engle 2002 (JBES 20); Ledoit-Wolf 2004 (JPM 30)."""
    x, y, label, errs = _aligned(ticker, market, book)
    if len(x) < 20:
        return {"ticker": ticker, "reference": label, "error": "insufficient overlapping history", "errors": errs}
    xv, yv = x.values, y.values
    # EWMA covariance/variance recursions
    cov = float(np.cov(xv, yv)[0, 1]); vx = float(xv.var()); vy = float(yv.var())
    rho = np.empty(len(xv))
    for i in range(len(xv)):
        rho[i] = cov / np.sqrt(vx * vy) if vx > 0 and vy > 0 else np.nan
        cov = lam * cov + (1 - lam) * xv[i] * yv[i]
        vx = lam * vx + (1 - lam) * xv[i] ** 2
        vy = lam * vy + (1 - lam) * yv[i] ** 2
    rho = pd.Series(rho).dropna()
    rho_now = float(rho.iloc[-1])
    rho_1y_max = float(rho.tail(252).max())
    rho_60d_rise = float(rho.iloc[-1] - rho.iloc[-60]) if len(rho) > 60 else None
    rho_static = float(np.corrcoef(xv, yv)[0, 1])
    rho_floor = max(rho_now, floor)
    return {
        "ticker": ticker, "reference": label, "lambda": lam,
        "corr_static": rnd(rho_static, 2), "corr_ewma_now": rnd(rho_now, 2),
        "corr_ewma_1y_max": rnd(rho_1y_max, 2), "corr_ewma_60d_change": rnd(rho_60d_rise, 2),
        "corr_floor_applied": rnd(rho_floor, 2), "floor": floor,
        "read": (f"conditional correlation to {label} is {rnd(rho_now,2)} and " +
                 ("RISING toward one — diversification is thinning (size down)" if (rho_60d_rise or 0) > 0.1
                  else "stable") + f"; sizing uses the shrinkage floor {rnd(rho_floor,2)}"),
        "note": "EWMA conditional correlation (numpy cousin of DCC, no MLE) + Ledoit-Wolf-style crisis floor. Engle 2002; Ledoit-Wolf 2004.",
        "errors": errs,
    }


def main():
    p = argparse.ArgumentParser(); p.add_argument("cmd"); p.add_argument("ticker")
    p.add_argument("others", nargs="?", default="SPY,QQQ,TLT,GLD")
    p.add_argument("--nav", type=float, default=100000); p.add_argument("--risk_pct", type=float, default=3.0)
    p.add_argument("--stop_atr", type=float, default=2.0)
    p.add_argument("--nav_ccy", default="GBP")
    p.add_argument("--vol_target", type=float, default=0.20)
    p.add_argument("--win_p", type=float, default=0.55); p.add_argument("--wl", type=float, default=1.5)
    p.add_argument("--mispricing_pct", type=float, default=None)
    p.add_argument("--catalyst_days", type=int, default=None)
    p.add_argument("--horizon_days", type=int, default=365)
    p.add_argument("--borrow_fee_bps", type=float, default=None)
    p.add_argument("--momentum_crash", action="store_true")
    p.add_argument("--funding_stress", action="store_true")
    # tail-risk battery args
    p.add_argument("--market", default="SPY"); p.add_argument("--bench", default="SPY")
    p.add_argument("--book", default=None)
    p.add_argument("--threshold_pct", type=float, default=90.0)
    p.add_argument("--conf", type=float, default=0.99)
    p.add_argument("--conditional", action="store_true")
    p.add_argument("--lambda", dest="lam", type=float, default=0.94)
    p.add_argument("--bounded", action="store_true"); p.add_argument("--loss_cap", type=float, default=1.0)
    p.add_argument("--backtest", action="store_true")
    p.add_argument("--tail_pct", type=float, default=5.0)
    p.add_argument("--thresholds", default="0,0.5,1.0,1.5")
    p.add_argument("--q", default="0.05,0.10")
    p.add_argument("--window", type=int, default=None)
    p.add_argument("--alpha", type=float, default=None)
    p.add_argument("--beta", type=float, default=0.90)
    p.add_argument("--tail_budget_pct", type=float, default=3.0)
    p.add_argument("--dd_budget_pct", type=float, default=15.0)
    p.add_argument("--dd_threshold", type=float, default=20.0)
    p.add_argument("--horizon", type=int, default=10)
    p.add_argument("--fraction", type=float, default=0.33)
    p.add_argument("--n_boot", type=int, default=2000)
    p.add_argument("--floor", type=float, default=0.8)
    p.add_argument("--stat", default="cvar")
    a = p.parse_args()
    _floats = lambda s: tuple(float(x) for x in s.split(",") if x != "")
    if a.cmd == "stats": jprint(stats(a.ticker))
    elif a.cmd == "corr": jprint(corr(a.ticker, parse_list(a.others)))
    elif a.cmd == "size": jprint(size(a.ticker, a.nav, a.risk_pct, a.stop_atr, a.vol_target, a.nav_ccy))
    elif a.cmd == "kelly": jprint(kelly(a.win_p, a.wl))
    elif a.cmd == "macro_size": jprint(macro_size(a.ticker, a.nav, a.risk_pct, a.stop_atr,
                                                  a.vol_target, a.funding_stress, a.nav_ccy))
    elif a.cmd == "arbgate": jprint(arb_gate(a.ticker, a.mispricing_pct, a.catalyst_days,
                                             a.horizon_days, a.borrow_fee_bps, a.momentum_crash))
    # ── tail-risk battery ──
    elif a.cmd == "evt": jprint(evt(a.ticker, a.threshold_pct, a.conf, a.conditional, a.lam,
                                    a.bounded, a.loss_cap, a.backtest))
    elif a.cmd == "condvol": jprint(condvol(a.ticker, a.lam))
    elif a.cmd == "mes": jprint(mes(a.ticker, a.market, a.tail_pct))
    elif a.cmd == "exceedcorr": jprint(exceedcorr(a.ticker, a.market, a.book, _floats(a.thresholds)))
    elif a.cmd == "taildep": jprint(taildep(a.ticker, a.market, a.book, _floats(a.q)))
    elif a.cmd == "dbeta": jprint(dbeta(a.ticker, a.market, a.book))
    elif a.cmd == "comoments": jprint(comoments(a.ticker, a.bench, a.book))
    elif a.cmd == "drawdown": jprint(drawdown(a.ticker, a.window or 63, a.alpha or 0.10))
    elif a.cmd == "semivar": jprint(semivar(a.ticker, a.window or 252))
    elif a.cmd == "lefttail": jprint(lefttail(a.ticker))
    elif a.cmd == "ddbeta": jprint(ddbeta(a.ticker, a.market))
    elif a.cmd == "covar": jprint(covar(a.ticker, a.market, a.book, _floats(a.q)))
    elif a.cmd == "rangevol": jprint(rangevol(a.ticker, a.window or 21))
    elif a.cmd == "crashfc": jprint(crashfc(a.ticker))
    elif a.cmd == "esbudget": jprint(esbudget(a.ticker, a.nav, a.tail_budget_pct, a.alpha or 0.95, a.horizon))
    elif a.cmd == "ddbudget": jprint(ddbudget(a.ticker, a.nav, a.dd_budget_pct, a.beta))
    elif a.cmd == "kelly_tail": jprint(kelly_tail(a.ticker, a.fraction, a.dd_threshold))
    elif a.cmd == "robust": jprint(robust(a.ticker, a.nav, a.stat, a.n_boot))
    elif a.cmd == "condcorr": jprint(condcorr(a.ticker, a.market, a.book, a.lam, a.floor))
    else: jprint({"error": f"unknown cmd '{a.cmd}'"})


if __name__ == "__main__":
    main_guard(main)
