"""Returns-based factor attribution — the quantamental reconciliation anchor.

Regresses a stock's daily EXCESS returns on the market plus long-only style-factor
proxies (each style leg = style-ETF minus SPY, so the factor legs are market-
neutralized and far less collinear than raw ETF levels). Reports:
  - factor betas (market, momentum, value, quality, low-vol, size)
  - annualized alpha with a hand-rolled Newey-West (HAC) t-stat   [no statsmodels]
  - R^2 and idiosyncratic share (1 - R^2)
  - idiosyncratic volatility = residual std, annualized            [Ang-Hodrick-Xing-Zhang]
  - factor-regressor correlation condition number (multicollinearity guard)
  - the Harvey-Liu-Zhu t>3 multiple-testing verdict on alpha

The intercept IS the risk-vs-anomaly split the quantamental agent adjudicates: a
return fully spanned by factor betas is "edge is just risk exposure"; a large
surviving alpha is an anomaly the bottom-up read must justify. A high accounting-
characteristic z-score with near-zero factor beta is a Daniel-Titman
characteristic-vs-covariance divergence (the market isn't trading the name as that
style — genuine idiosyncratic mispricing, or a trap the tape already sees through).

Exports residual_returns() / residual_momentum() so quant_factors.py builds
factor-neutralized (residual) momentum from the SAME regression residuals — the
regime.py -> technicals.py export pattern.

Refs: Sharpe (1992, JPM 18(2)) returns-based style analysis; Fama-French (1993,
JFE 33) factor regression; Newey-West (1987, Econometrica 55(3)) HAC; Ang-Hodrick-
Xing-Zhang (2006, JF 61(1)) IVOL; Daniel-Titman (1997, JF 52(1)) characteristics
vs covariances; Kelly-Pruitt-Su (2019, JFE 134(3)) characteristics-are-covariances;
Harvey-Liu-Zhu (2016, RFS 29(1)) t>3 hurdle; Blitz-Huij-Martens (2011, JEmpFin
18(3)) residual momentum.

CLI:  python factor_attribution.py NVDA [PEERS]   ->  attribution.json
"""
import sys, math
import numpy as np
import pandas as pd
from common import yf_history, jprint, rnd, fred, main_guard, parse_list

_MKT = "SPY"
# long-only style proxies; each factor leg is (ETF - SPY) => market-neutralized style tilt
_STYLES = {"mom": "MTUM", "value": "VLUE", "quality": "QUAL", "low_vol": "USMV", "size": "IWM"}
_ALL = [_MKT] + list(_STYLES.values())


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _rf_daily(index):
    """Daily risk-free from FRED short-bill series (annualized %). Falls back across
    1m/3m bills, then to zero (noted by the caller)."""
    for sid in ("DGS1MO", "DTB3", "DGS3MO"):
        try:
            s = fred(sid) / 100.0
            s = s.reindex(index.union(s.index)).ffill().reindex(index)
            if s.notna().sum() > 0.5 * len(index):
                return (s / 252.0).fillna(0.0), sid
        except Exception:
            continue
    return pd.Series(0.0, index=index), None


def _load(ticker, period):
    """Aligned daily simple returns for ticker + market + style ETFs."""
    ret = {}
    for tk in [ticker] + _ALL:
        try:
            ret[tk] = yf_history(tk, period=period)["Close"].pct_change()
        except Exception:
            pass
    return pd.DataFrame(ret).dropna()


def _design(df):
    """(Xdf, rf, rf_id): market excess + style-minus-market legs, aligned to df."""
    rf, rf_id = _rf_daily(df.index)
    cols = {"MKT": df[_MKT] - rf}
    for name, etf in _STYLES.items():
        if etf in df.columns:
            cols[name] = df[etf] - df[_MKT]
    return pd.DataFrame(cols), rf, rf_id


def _newey_west_se(X, resid, lags):
    """HAC covariance of OLS coefficients with a Bartlett kernel (Newey-West 1987)."""
    XtX_inv = np.linalg.inv(X.T @ X)
    scores = X * resid.reshape(-1, 1)                 # u_t * x_t
    S = scores.T @ scores                             # lag 0
    for l in range(1, lags + 1):
        w = 1.0 - l / (lags + 1.0)
        G = scores[l:].T @ scores[:-l]
        S += w * (G + G.T)
    cov = XtX_inv @ (S) @ XtX_inv
    return np.sqrt(np.maximum(np.diag(cov), 0.0))


def _regress(y, Xdf):
    X = np.column_stack([np.ones(len(Xdf)), Xdf.to_numpy()])
    names = ["alpha"] + list(Xdf.columns)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    ss_res = float(resid @ resid)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = (1 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    lags = max(int(np.floor(4 * (len(y) / 100.0) ** (2.0 / 9.0))), 1)   # Newey-West rule
    se = _newey_west_se(X, resid, lags)
    return coef, se, resid, r2, names, lags


# ---- exported for quant_factors.py residual momentum -------------------------

def residual_returns(ticker, period="3y"):
    """Factor-model residual daily-return Series (stock-specific return)."""
    df = _load(ticker, period)
    if df.empty or ticker not in df.columns or len(df) < 60:
        return None
    Xdf, rf, _ = _design(df)
    y = (df[ticker] - rf).to_numpy()
    _c, _s, resid, _r2, _n, _l = _regress(y, Xdf)
    return pd.Series(resid, index=df.index)


def residual_momentum(ticker, period="3y", resid=None):
    """Blitz-Huij-Martens residual momentum: information ratio (and cumulative) of
    the factor-model residual over the 12-1 window (t-252..t-21, skipping the most
    recent month). Factor-neutral => isolates stock-specific drift, ~half the vol
    and far milder crashes than raw price momentum."""
    rs = resid if resid is not None else residual_returns(ticker, period)
    if rs is None or len(rs) < 252:
        return None
    win = rs.iloc[-252:-21]
    if len(win) < 60 or float(win.std()) == 0:
        return None
    return {
        "residual_mom_cum_pct": rnd(100 * float(win.sum())),
        "residual_mom_info_ratio": rnd(float(win.mean() / win.std()), 2),
        "window": "t-252..t-21 (12-1, skip 1m)",
        "note": "factor-neutral momentum; high vs raw price momentum => the momentum "
                "screen is stock-specific, not a crowded style bet",
    }


# ---- main attribution --------------------------------------------------------

def _attrib_one(df, ticker):
    """Regress one name already present in df; returns the summary dict + residual."""
    Xdf, rf, rf_id = _design(df)
    y = (df[ticker] - rf).to_numpy()
    coef, se, resid, r2, names, lags = _regress(y, Xdf)
    alpha_d = float(coef[0])
    t = alpha_d / se[0] if se[0] > 0 else float("nan")
    betas = {names[i]: rnd(float(coef[i]), 3) for i in range(1, len(names))}
    beta_t = {names[i] + "_t": rnd(float(coef[i] / se[i]), 2) if se[i] > 0 else None
              for i in range(1, len(names))}
    ivol = float(np.std(resid, ddof=1) * np.sqrt(252))
    pval = 2 * (1 - _norm_cdf(abs(t))) if t == t else None
    # multicollinearity diagnostics on the regressor correlation matrix
    C = np.corrcoef(Xdf.to_numpy(), rowvar=False)
    cond = float(np.linalg.cond(C))
    iu = np.triu_indices_from(C, k=1)
    max_abs_corr = float(np.max(np.abs(C[iu]))) if len(iu[0]) else 0.0
    return {
        "factor_betas": betas,
        "factor_beta_tstats": beta_t,
        "alpha_ann_pct": rnd(100 * alpha_d * 252),
        "alpha_daily_bps": rnd(1e4 * alpha_d, 2),
        "alpha_tstat_nw": rnd(t, 2),
        "alpha_pvalue_nw": rnd(pval, 3) if pval is not None else None,
        "alpha_significant_5pct": bool(t == t and abs(t) > 1.96),
        "alpha_survives_hlz_t3": bool(t == t and abs(t) > 3.0),
        "r2": rnd(r2, 3),
        "idiosyncratic_share_pct": rnd(100 * (1 - r2)) if r2 == r2 else None,
        "ivol_ann_pct": rnd(100 * ivol),
        "nw_lags": lags,
        "regressor_condition_number": rnd(cond, 1),
        "regressor_max_abs_corr": rnd(max_abs_corr, 2),
        "multicollinearity_flag": bool(cond > 30 or max_abs_corr > 0.8),
    }, pd.Series(resid, index=df.index)


def _read(a):
    idio = a.get("idiosyncratic_share_pct")
    t = a.get("alpha_tstat_nw")
    parts = []
    if idio is not None:
        parts.append(f"{idio:.0f}% of variance is idiosyncratic (R^2={a['r2']})")
    if t is not None:
        if a.get("alpha_survives_hlz_t3"):
            parts.append(f"alpha t={t} clears the Harvey-Liu-Zhu t>3 hurdle — a real anomaly the bottom-up read must own")
        elif a.get("alpha_significant_5pct"):
            parts.append(f"alpha t={t} is significant at 5% but does NOT clear t>3 — treat as a candidate, not a confirmed edge")
        else:
            parts.append(f"alpha t={t} is indistinguishable from zero — the return is spanned by factor betas (edge is risk exposure, not stock-specific)")
    if a.get("multicollinearity_flag"):
        parts.append(f"WARNING: regressors collinear (condition number {a['regressor_condition_number']}) — read individual betas with caution")
    return "; ".join(parts)


def attribution(ticker, peers=None, period="3y"):
    df = _load(ticker, period)
    if df.empty or ticker not in df.columns:
        return {"ticker": ticker, "error": "insufficient aligned price history for the factor-ETF set"}
    _Xdf, _rf, rf_id = _design(df)
    a, resid = _attrib_one(df, ticker)
    out = {"ticker": ticker, "period": period, "n_obs": int(len(df)),
           "rf_source": rf_id or "zero (FRED unavailable)",
           "factors": "MKT=SPY-rf; mom=MTUM-SPY; value=VLUE-SPY; quality=QUAL-SPY; low_vol=USMV-SPY; size=IWM-SPY",
           **a}
    out["residual_momentum"] = residual_momentum(ticker, period, resid=resid)
    out["read"] = _read(a)
    out["daniel_titman_note"] = ("compare these factor betas to the accounting-characteristic z-scores in factors.json: "
                                 "strong characteristic + weak matching beta = characteristic-vs-covariance divergence")
    if peers:
        peer_tbl = {}
        for pk in peers:
            try:
                dfp = _load(pk, period)
                if not dfp.empty and pk in dfp.columns:
                    ap, _ = _attrib_one(dfp, pk)
                    peer_tbl[pk] = {"alpha_ann_pct": ap["alpha_ann_pct"], "alpha_tstat_nw": ap["alpha_tstat_nw"],
                                    "idiosyncratic_share_pct": ap["idiosyncratic_share_pct"], "r2": ap["r2"]}
            except Exception as e:
                peer_tbl[pk] = {"error": str(e)}
        out["peers"] = peer_tbl
    return out


def main():
    ticker = sys.argv[1]
    peers = parse_list(sys.argv[2] if len(sys.argv) > 2 else "")
    period = sys.argv[3] if len(sys.argv) > 3 else "3y"
    jprint(attribution(ticker, peers or None, period))


if __name__ == "__main__":
    main_guard(main)
