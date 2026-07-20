"""Portfolio optimizer for the CRO / buy-side / passive-index seats.

Covariance: Ledoit-Wolf (2004) shrinkage to the constant-correlation target ("Honey, I
Shrunk the Sample Covariance Matrix", J. Portfolio Mgmt 30(4)). minvar/tangency: closed
form on the shrunk matrix, long-only via iterative clipping-renormalisation (approximate
— exact only when the discovered active set equals the true KKT active set). hrp: Lopez
de Prado (2016), J. Portfolio Mgmt 42(4) — single-linkage clustering on correlation
distance, quasi-diagonalisation, recursive bisection.

Book file: an IBKR-derived portfolio_context.json — {"positions":[...]}, {"rows":[...]}
or a raw list; per-position field names are probed defensively and unresolvable positions
degrade per-position. Weights are in base-currency (GBP) market-value space while risk
uses local-ccy daily returns, so local/base FX vol is NOT captured (stated in payload).

CLI:
  python portfolio_opt.py minvar    --book <portfolio_context.json> [--period 1y]
  python portfolio_opt.py tangency  --book <portfolio_context.json> [--rf 0.045] [--period 1y]
  python portfolio_opt.py hrp       --book <portfolio_context.json> [--period 1y]
  python portfolio_opt.py rebalance --book <portfolio_context.json> --targets "NVDA:6,AMZN:6.5,..."
"""
import argparse
import json
import sys
import numpy as np
import pandas as pd
from common import jprint, rnd, parse_list, yf_history, main_guard

# yahoo-suffix candidates by listing currency (probed, then validated against book price)
_SUFFIX = {"GBP": [".L"], "EUR": [".DE", ".PA", ".AS", ".MI", ".F"], "AUD": [".AX"],
           "CAD": [".TO"], "CHF": [".SW"], "JPY": [".T"], "HKD": [".HK"], "SEK": [".ST"]}
# known IBKR-symbol -> yahoo overrides for this book (same map book_quant.py hand-rolled)
_KNOWN = {"RR": "RR.L", "ENR": "ENR.DE", "SU": "SU.PA", "RHM": "RHM.DE",
          "HY9H": "HY9H.DE", "PLS": "PLS.AX"}
_MIN_OBS = 60          # a name needs this many aligned return days to enter the matrix
_USAGE = ("python portfolio_opt.py {minvar|tangency|hrp|rebalance} --book "
          "<portfolio_context.json> [--period 1y] [--rf 0.045] [--targets TICK:pct,...]")


# --- book parsing -----------------------------------------------------------

def _get(d, keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _load_book(path):
    """Parse portfolio_context.json defensively -> (rows, meta, gaps).
    Handles {"positions":[...]}, {"rows":[...]} and a raw list; probes field aliases."""
    with open(path) as f:
        raw = json.load(f)
    meta = raw if isinstance(raw, dict) else {}
    plist = raw if isinstance(raw, list) else _get(raw, ["positions", "rows", "holdings"]) or []
    base = meta.get("base_ccy") or "GBP"
    gaps = [] if meta.get("base_ccy") else ["base_ccy absent from book; assumed GBP"]
    rows, seen = {}, {}
    for p in plist:
        if not isinstance(p, dict):
            gaps.append(f"non-dict position skipped: {p!r}")
            continue
        tick = _get(p, ["ticker", "sym", "symbol", "contractDesc"])
        if not tick:
            gaps.append("position without a symbol field skipped")
            continue
        tick = str(tick).strip().upper()
        qty = _get(p, ["q", "qty", "position", "quantity", "shares"])
        px = _get(p, ["px", "mkt_price", "price", "mktPrice", "last"])
        ccy = _get(p, ["ccy", "currency"])
        mv_local = _get(p, ["mv", "mv_local", "marketValue", "market_value", "value"])
        mv_base = _get(p, ["mv_gbp", "gbp", "mv_base", "base_mv"])
        if mv_base is None and mv_local is not None:
            mv_base = mv_local
            if ccy and ccy != base:
                gaps.append(f"{tick}: no base-ccy market value; used local ({ccy}) value unconverted")
        if mv_base is None and qty is not None and px is not None:
            mv_base = float(qty) * float(px)
            gaps.append(f"{tick}: market value reconstructed as qty*price (local ccy)")
        if mv_base is None:
            gaps.append(f"{tick}: no market value resolvable; position dropped")
            continue
        r = {"ticker": tick, "yahoo": p.get("yahoo"), "ccy": ccy, "qty": qty,
             "px_book": float(px) if px is not None else None,
             "mv_local": float(mv_local) if mv_local is not None else None,
             "mv_base": float(mv_base)}
        if tick in rows:  # duplicate lines (e.g. lots) -> aggregate
            rows[tick]["mv_base"] += r["mv_base"]
            if qty is not None and rows[tick]["qty"] is not None:
                rows[tick]["qty"] += qty
            seen[tick] = seen.get(tick, 1) + 1
        else:
            rows[tick] = r
    for t, n in seen.items():
        gaps.append(f"{t}: {n} duplicate position lines aggregated")
    if not rows:
        raise ValueError(f"no usable positions parsed from {path}")
    return list(rows.values()), {"base_ccy": base, "as_of": meta.get("as_of"),
                                 "nav": _get(meta, ["nav_gbp", "nav"]),
                                 "cash": _get(meta, ["cash_gbp", "cash"])}, gaps


# --- symbol resolution + price fetch ---------------------------------------

def _px_sane(close, px_book):
    """Validate a candidate series against the book price; detect GBp-vs-GBP (100x).
    Returns (ok, pence_scale) where pence_scale divides yahoo prices into book units."""
    if px_book is None or px_book <= 0:
        return True, 1.0
    r = float(close) / px_book
    if 1 / 3 <= r <= 3:
        return True, 1.0
    if 60 <= r <= 160:       # yahoo quotes pence (GBp), book quotes pounds
        return True, 100.0
    if 1 / 160 <= r <= 1 / 60:
        return True, 0.01
    return False, 1.0


def _resolve(row, period, gaps):
    """Try yahoo hint -> known override -> ccy-suffix candidates -> raw ticker.
    Returns (yahoo_symbol, close_series, pence_scale) or None (gap recorded)."""
    t, ccy = row["ticker"], row["ccy"]
    cands = []
    if row.get("yahoo"):
        cands.append(str(row["yahoo"]))
    if t in _KNOWN:
        cands.append(_KNOWN[t])
    for suf in _SUFFIX.get(ccy or "USD", []):
        cands.append(t + suf)
    cands.append(t)
    tried = []
    for sym in dict.fromkeys(cands):        # dedupe, keep order
        tried.append(sym)
        try:
            close = yf_history(sym, period=period)["Close"].dropna()
        except Exception:
            continue
        if len(close) < 10:
            continue
        ok, scale = _px_sane(close.iloc[-1], row["px_book"])
        if ok:
            return sym, close, scale
    gaps.append(f"{t}: unresolvable on yahoo (tried {','.join(tried)}); skipped")
    return None


def _price_matrix(rows, period, gaps):
    """Aligned close-price frame (columns = book tickers, local ccy) + per-row yahoo map."""
    series, ymap, scale = {}, {}, {}
    for r in rows:
        res = _resolve(r, period, gaps)
        if res is None:
            continue
        sym, close, sc = res
        series[r["ticker"]], ymap[r["ticker"]], scale[r["ticker"]] = close, sym, sc
    if not series:
        raise ValueError("no book position resolved to a price history")
    px = pd.DataFrame(series).ffill(limit=5)
    rets = px.pct_change().dropna(how="all")
    for c in list(rets.columns):            # drop sparse names (new listings, dead feeds)
        if rets[c].notna().sum() < _MIN_OBS:
            gaps.append(f"{c}: only {int(rets[c].notna().sum())} return obs (<{_MIN_OBS}); dropped from matrix")
            rets = rets.drop(columns=c)
    rets = rets.dropna()
    if rets.shape[0] < _MIN_OBS or rets.shape[1] < 2:
        raise ValueError(f"insufficient aligned history: {rets.shape[0]} days x {rets.shape[1]} names")
    return rets, ymap, scale


# --- covariance: Ledoit-Wolf (2004) constant-correlation shrinkage ----------

def lw_cov(rets):
    """Shrunk daily covariance -> (sigma, delta, rbar). Hand-rolled per Ledoit-Wolf 2004."""
    X = rets.values - rets.values.mean(axis=0)
    t, n = X.shape
    S = X.T @ X / t
    var = np.diag(S).copy()
    sd = np.sqrt(var)
    rbar = ((S / np.outer(sd, sd)).sum() - n) / (n * (n - 1))
    F = rbar * np.outer(sd, sd)             # constant-correlation target
    np.fill_diagonal(F, var)
    phi_mat = (X ** 2).T @ (X ** 2) / t - S ** 2          # asy variances of S entries
    phi = phi_mat.sum()
    theta = (X ** 3).T @ X / t - var[:, None] * S          # asy cov(s_ii, s_ij)
    np.fill_diagonal(theta, 0.0)
    rho = np.trace(phi_mat) + rbar * (np.outer(1.0 / sd, sd) * theta).sum()
    gamma = ((S - F) ** 2).sum()
    kappa = (phi - rho) / gamma if gamma > 1e-16 else 0.0
    delta = float(max(0.0, min(1.0, kappa / t)))
    return delta * F + (1 - delta) * S, delta, float(rbar)


# --- allocation engines ------------------------------------------------------

def _clip_solve(S, b):
    """w proportional to S^-1 b, projected long-only by iteratively dropping negative-weight
    names and re-solving on the survivors. Approximate active-set method (stated in payload)."""
    p = len(b)
    active = list(range(p))
    for _ in range(p):
        Ss = S[np.ix_(active, active)] + 1e-12 * np.eye(len(active))
        try:
            x = np.linalg.solve(Ss, b[active])
        except np.linalg.LinAlgError:
            return None, "singular covariance"
        if x.sum() <= 0:
            return None, "no positive-weight solution on the active set"
        x = x / x.sum()
        if (x >= -1e-10).all():
            w = np.zeros(p)
            w[active] = np.clip(x, 0.0, None)
            return w / w.sum(), None
        active = [a for a, xi in zip(active, x) if xi > 0]
        if not active:
            return None, "long-only clipping removed every asset"
    return None, "clipping failed to converge"


def _hrp_order(D):
    """Single-linkage agglomerative clustering on the distance matrix; leaf order from
    merge concatenation (the standard HRP quasi-diagonalisation seriation)."""
    clusters = [[i] for i in range(len(D))]
    while len(clusters) > 1:
        best = None
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                d = min(D[i][j] for i in clusters[a] for j in clusters[b])
                if best is None or d < best[0]:
                    best = (d, a, b)
        _, a, b = best
        clusters[a] = clusters[a] + clusters[b]
        del clusters[b]
    return clusters[0]


def _hrp_weights(S, order):
    """Lopez de Prado recursive bisection: split the quasi-diagonal order in half, weight
    each half by inverse of its inverse-variance-portfolio variance, recurse."""
    def cluster_var(ix):
        sub = S[np.ix_(ix, ix)]
        ivp = 1.0 / np.diag(sub)
        ivp = ivp / ivp.sum()
        return float(ivp @ sub @ ivp)
    w = np.ones(S.shape[0])
    stack = [list(order)]
    while stack:
        items = stack.pop()
        if len(items) < 2:
            continue
        mid = len(items) // 2
        left, right = items[:mid], items[mid:]
        vl, vr = cluster_var(left), cluster_var(right)
        alpha = 1.0 - vl / (vl + vr)
        w[left] *= alpha
        w[right] *= 1.0 - alpha
        stack += [left, right]
    return w / w.sum()


def _portfolio_stats(S_daily, w, mu_daily=None, rf=None):
    v = float(w @ S_daily @ w)
    out = {"vol_ann_pct": rnd(100 * np.sqrt(252 * v))}
    mrc = S_daily @ w
    rc = w * mrc / v if v > 0 else np.zeros_like(w)
    out["_rc"] = rc
    if mu_daily is not None:
        er = float(w @ mu_daily) * 252
        out["exp_ret_ann_pct"] = rnd(100 * er)
        if rf is not None:
            out["sharpe"] = rnd((er - rf) / np.sqrt(252 * v), 2) if v > 0 else None
    return out


# --- subcommands -------------------------------------------------------------

def _common_payload(cmd, a, rows, meta, gaps, rets, ymap, delta=None, rbar=None):
    return {
        "cmd": cmd, "book": a.book, "as_of": meta["as_of"], "base_ccy": meta["base_ccy"],
        "period": a.period, "n_positions_book": len(rows), "n_in_matrix": rets.shape[1],
        "obs_days": rets.shape[0],
        "yahoo_map": ymap,
        "shrinkage": None if delta is None else {
            "method": "Ledoit-Wolf 2004 constant-correlation", "delta": rnd(delta, 3),
            "avg_corr_target": rnd(rbar, 3)},
        "currency_note": ("weights are base-ccy (GBP) market-value shares; risk uses "
                          "local-ccy daily returns, so local/base FX vol is not captured"),
        "gaps": gaps,
    }


def _cur_weights(rows, cols):
    mv = {r["ticker"]: r["mv_base"] for r in rows if r["ticker"] in cols}
    tot = sum(mv.values())
    return np.array([mv[c] / tot for c in cols])


def _wdict(cols, w):
    return {c: rnd(100 * x, 2) for c, x in sorted(zip(cols, w), key=lambda kv: -kv[1])}


def optimize(cmd, a):
    rows, meta, gaps = _load_book(a.book)
    rets, ymap, _ = _price_matrix(rows, a.period, gaps)
    cols = list(rets.columns)
    S, delta, rbar = lw_cov(rets)
    mu = rets.values.mean(axis=0)
    w_cur = _cur_weights(rows, cols)
    out = _common_payload(cmd, a, rows, meta, gaps, rets, ymap, delta, rbar)
    note = None
    if cmd == "minvar":
        w_opt, note = _clip_solve(S, np.ones(len(cols)))
        out["long_only"] = "iterative clipping-renormalisation (approximate KKT active set)"
    elif cmd == "tangency":
        excess = mu - a.rf / 252
        if (excess <= 0).all():
            w_opt, note = None, "all excess returns <= rf; tangency undefined for a long-only book"
        else:
            w_opt, note = _clip_solve(S, excess)
        out["rf"] = a.rf
        out["long_only"] = "iterative clipping-renormalisation (approximate KKT active set)"
    else:  # hrp
        sd = np.sqrt(np.diag(S))
        corr = np.clip(S / np.outer(sd, sd), -1.0, 1.0)
        D = np.sqrt(0.5 * (1.0 - corr))
        order = _hrp_order(D)
        w_opt = _hrp_weights(S, order)
        out["cluster_order"] = [cols[i] for i in order]
        out["method_note"] = "single-linkage on sqrt(0.5*(1-corr)) + recursive bisection (Lopez de Prado 2016)"
    cur = _portfolio_stats(S, w_cur, mu, a.rf)
    out["current"] = {"vol_ann_pct": cur["vol_ann_pct"], "exp_ret_ann_pct": cur.get("exp_ret_ann_pct"),
                      "sharpe": cur.get("sharpe"), "weights_pct": _wdict(cols, w_cur),
                      "risk_contrib_pct": _wdict(cols, cur["_rc"])}
    if w_opt is None:
        out["optimized"] = None
        gaps.append(f"optimizer degraded: {note}")
    else:
        opt = _portfolio_stats(S, w_opt, mu, a.rf)
        out["optimized"] = {"vol_ann_pct": opt["vol_ann_pct"], "exp_ret_ann_pct": opt.get("exp_ret_ann_pct"),
                            "sharpe": opt.get("sharpe"), "weights_pct": _wdict(cols, w_opt),
                            "risk_contrib_pct": _wdict(cols, opt["_rc"]),
                            "vol_reduction_pct": rnd(opt["vol_ann_pct"] and cur["vol_ann_pct"]
                                                     and (cur["vol_ann_pct"] - opt["vol_ann_pct"]), 2)}
    return out


def _fx_to_base(ccy, base, row, gaps):
    """Implied FX from the book (mv_base/mv_local, then mv_base/(qty*px)); falls back to a
    live yahoo cross; last resort 1.0 with a stated gap."""
    if ccy is None or ccy == base:
        return 1.0
    if row:
        if row.get("mv_local") and row.get("mv_base"):
            return row["mv_base"] / row["mv_local"]
        if row.get("qty") and row.get("px_book") and row.get("mv_base"):
            return row["mv_base"] / (row["qty"] * row["px_book"])
    try:
        return float(yf_history(f"{ccy}{base}=X", period="5d")["Close"].iloc[-1])
    except Exception:
        gaps.append(f"FX {ccy}->{base} unresolvable; assumed 1.0")
        return 1.0


def rebalance(a):
    if not a.targets:
        raise ValueError("rebalance requires --targets \"TICK:pct,TICK:pct,...\"")
    targets = {}
    for kv in parse_list(a.targets):
        k, _, v = kv.partition(":")
        if not _ or not k:
            raise ValueError(f"bad target token {kv!r}; expected TICK:pct")
        targets[k.strip().upper()] = float(v)
    rows, meta, gaps = _load_book(a.book)
    bymap = {r["ticker"]: r for r in rows}
    gross = sum(r["mv_base"] for r in rows)
    base = meta["base_ccy"]
    tsum = sum(targets.values())
    legs, buys, sells = [], 0.0, 0.0
    for t, pct in targets.items():
        row = bymap.get(t)
        ccy = row["ccy"] if row else "USD"
        if row is None:
            gaps.append(f"{t}: not in book; assumed USD listing for pricing")
        res = _resolve(row or {"ticker": t, "ccy": ccy, "yahoo": None, "px_book": None}, "3mo", gaps)
        if res is None:
            legs.append({"ticker": t, "target_pct": pct, "error": "unpriceable; leg skipped"})
            continue
        sym, close, scale = res
        px_local = float(close.iloc[-1]) / scale
        fx = _fx_to_base(ccy, base, row, gaps)
        px_base = px_local * fx
        cur_mv = row["mv_base"] if row else 0.0
        cur_sh = row["qty"] if row and row.get("qty") is not None else (
            round(cur_mv / px_base) if cur_mv else 0)
        if row and row.get("qty") is None:
            gaps.append(f"{t}: no share count in book; estimated from market value")
        tgt_mv = pct / 100.0 * gross
        tgt_sh = int(np.floor(tgt_mv / px_base)) if px_base > 0 else 0
        d_sh = int(tgt_sh - cur_sh)
        d_ntl = d_sh * px_base
        buys += max(d_ntl, 0.0)
        sells += -min(d_ntl, 0.0)
        legs.append({"ticker": t, "yahoo": sym, "ccy": ccy,
                     "live_px_local": rnd(px_local, 4), "px_base": rnd(px_base, 4),
                     "current_shares": cur_sh, "current_pct": rnd(100 * cur_mv / gross, 2),
                     "target_pct": pct, "target_shares": tgt_sh, "share_delta": d_sh,
                     "delta_notional_base": rnd(d_ntl, 2),
                     "residual_base": rnd(tgt_mv - tgt_sh * px_base, 2)})
    untargeted = {r["ticker"]: rnd(100 * r["mv_base"] / gross, 2)
                  for r in rows if r["ticker"] not in targets}
    return {
        "cmd": "rebalance", "book": a.book, "as_of": meta["as_of"], "base_ccy": base,
        "basis": "gross equity value of book (base ccy)", "gross_base": rnd(gross, 2),
        "nav": meta["nav"], "cash": meta["cash"],
        "targets_sum_pct": rnd(tsum, 2),
        "legs": legs,
        "buy_notional_base": rnd(buys, 2), "sell_notional_base": rnd(sells, 2),
        "net_cash_required_base": rnd(buys - sells, 2),
        "untargeted_positions_pct": untargeted,
        "note": ("share deltas floor target notionals to whole shares; tickers absent from "
                 "--targets are left untouched (listed under untargeted_positions_pct); "
                 "GBp/GBP 100x quotes auto-detected against book prices"),
        "gaps": gaps,
    }


def main():
    ap = argparse.ArgumentParser(prog="portfolio_opt.py", add_help=True)
    ap.add_argument("cmd", choices=["minvar", "tangency", "hrp", "rebalance"])
    ap.add_argument("--book", required=True)
    ap.add_argument("--period", default="1y")
    ap.add_argument("--rf", type=float, default=0.045)
    ap.add_argument("--targets", default=None)
    try:
        a = ap.parse_args()
    except SystemExit as e:                 # argparse errors -> single JSON error via main_guard
        if e.code not in (0, None):
            raise ValueError(f"usage: {_USAGE}")
        raise
    jprint(rebalance(a) if a.cmd == "rebalance" else optimize(a.cmd, a))


if __name__ == "__main__":
    main_guard(main)
