"""Jump vs diffusion decomposition for a single name's returns — the tail-risk agent's
'is this name jump-prone?' gauge.

We split return variation into a continuous (diffusive) part and a discontinuous (JUMP)
part. A name whose variance is jump-heavy gaps THROUGH stops, so the right hedge is a
convex OPTION, not a stop, and the right size is smaller. Recent NEGATIVE jumps are an
early fragility trigger.

Method (numpy + pandas only; no scipy/statsmodels/arch):
  - Realized variance RV = sum r_t^2; Bipower variation BV = (pi/2) sum |r_{t-1}||r_t|
    (Barndorff-Nielsen & Shephard 2004, J. Financial Econometrics 2(1):1-37). BV is
    robust to jumps, so JV = max(0, RV-BV) isolates the jump contribution and
    jump_share = JV/RV is the fraction of variance that is discontinuous
    (Andersen-Bollerslev-Diebold 2007, REStat 89(4):701-720).
  - Lee & Mykland (2008, RFS 21(6):2535-2563) instantaneous jump test: standardize each
    day's return by a local bipower vol estimate (excluding that day) and flag |L|>z;
    the LM Gumbel max-stat gives a formal 'any jump today?' rejection.
  - Signed jump variation via realized semivariance (Barndorff-Nielsen-Kinnebrock-
    Shephard 2010; Patton & Sheppard 2015, REStat 97(3):683-697): RS+ - RS- signs the
    jump risk; negative => downside-jump dominated.

CLI:
  python jump_analysis.py TICKER [--window 252] [--z 4.0]
"""
import argparse
import numpy as np
import pandas as pd
from common import jprint, rnd, yf_history, main_guard

MU1_SQ = np.pi / 2.0          # 1/mu1^2 with mu1 = E|Z| = sqrt(2/pi); scales BV to variance
LOCAL_K = 17                  # Lee-Mykland local-vol window (trading weeks ~ RFS default)


def _lee_mykland_L(r, K=LOCAL_K):
    """Lee-Mykland statistic L(i) = r_i / sigma_hat(i), where sigma_hat(i)^2 is the local
    bipower variance over the K observations BEFORE i (excluding day i itself):
        sigma_hat(i)^2 = 1/(K-2) * sum_{j=i-K+2}^{i-1} |r_j| |r_{j-1}|.
    Returns an L array aligned to r (NaN where the local window is not yet full)."""
    r = np.asarray(r, float)
    n = len(r)
    absprod = np.abs(r[1:]) * np.abs(r[:-1])          # |r_j||r_{j-1}| at index j (j>=1)
    L = np.full(n, np.nan)
    for i in range(K, n):
        # products for j in [i-K+2, i-1] -> absprod indices [i-K+1 .. i-2]
        seg = absprod[i - K + 1:i - 1]
        if len(seg) < 1:
            continue
        var = seg.sum() / (K - 2)
        sig = np.sqrt(var)
        if sig > 0:
            L[i] = r[i] / sig
    return L


def _lm_gumbel(n):
    """Lee-Mykland Gumbel centering/scaling constants (Cn, Sn) for n observations.
    'No jump' is rejected at ~5% when (|L| - Cn)/Sn > 4.6 (Gumbel 0.05 critical value)."""
    c = np.sqrt(2.0 / np.pi)
    ln_n = np.log(n)
    Cn = (2.0 * ln_n) ** 0.5 / c - (np.log(np.pi) + np.log(ln_n)) / (2.0 * c * (2.0 * ln_n) ** 0.5)
    Sn = 1.0 / (c * (2.0 * ln_n) ** 0.5)
    return Cn, Sn


def analyze(ticker, window=252, z_thresh=4.0):
    errors = []
    out = {
        "ticker": ticker, "window": int(window), "z_thresh": float(z_thresh),
        "errors": errors,
    }

    df = None
    try:
        df = yf_history(ticker, period="3y")
    except Exception as e:
        errors.append(f"fetch: {e}")
        out["read"] = "no price data — cannot decompose jump vs diffusion"
        return out

    try:
        px = df["Close"].dropna()
        r = np.log(px / px.shift()).dropna()
        N = len(r)
        out["n_returns"] = int(N)
        if N < 30:
            errors.append(f"too few returns ({N}) for a jump decomposition")
            out["read"] = "insufficient history — jump decomposition unreliable"
            return out

        W = min(window, N)                       # effective trailing window
        out["window_used"] = int(W)
        rw = r.tail(W)
        a = rw.to_numpy()

        # --- 1) RV / BV / jump share over the trailing window -----------------
        RV = float(np.sum(a ** 2))
        # BV = (pi/2) * sum_{t=2..W} |r_{t-1}| |r_t|
        BV = float(MU1_SQ * np.sum(np.abs(a[1:]) * np.abs(a[:-1])))
        JV = max(0.0, RV - BV)
        jump_share = 0.0 if RV <= 0 else min(1.0, max(0.0, JV / RV))
        # annualize: mean daily variance over the window, scaled to a year
        rv_ann_vol = 100.0 * np.sqrt(RV / W * 252.0)
        bv_ann_vol = 100.0 * np.sqrt(BV / W * 252.0)
        jump_ann_vol = 100.0 * np.sqrt(JV / W * 252.0)   # absolute jump-vol magnitude
        out.update({
            "rv_ann_vol_pct": rnd(rv_ann_vol),
            "bv_ann_vol_pct": rnd(bv_ann_vol),      # continuous (diffusive) vol
            "jump_variation": rnd(JV, 6),
            "jump_ann_vol_pct": rnd(jump_ann_vol),  # annualized vol from jumps ONLY (absolute magnitude)
            "jump_share": rnd(jump_share, 3),        # JV/RV — the FRACTION that is jumps (window-sensitive)
            "jump_share_pct": rnd(100.0 * jump_share, 1),
        })

        # --- 2) Lee-Mykland instantaneous jump test (full sample) -------------
        L = _lee_mykland_L(r.to_numpy(), K=LOCAL_K)
        absL = np.abs(L)
        Cn, Sn = _lm_gumbel(W)
        lm_gumbel_stat = (absL - Cn) / Sn          # reject no-jump when > 4.6
        simple_flag = absL > z_thresh
        gumbel_flag = lm_gumbel_stat > 4.6
        out["lm_gumbel_Cn"] = rnd(Cn, 3)
        out["lm_gumbel_Sn"] = rnd(Sn, 4)
        out["lm_gumbel_crit_0.05"] = 4.6

        idx = r.index
        rvals = r.to_numpy()

        def _day(i):
            return {
                "date": idx[i].strftime("%Y-%m-%d"),
                "ret_pct": rnd(100.0 * (np.exp(rvals[i]) - 1.0)),
                "sign": "+" if rvals[i] >= 0 else "-",
                "L": rnd(float(L[i]), 2),
                "lm_gumbel_stat": rnd(float(lm_gumbel_stat[i]), 2),
                "lm_gumbel_reject": bool(gumbel_flag[i]),
            }

        # jump days in the last 21 sessions (simple |L|>z flag)
        recent_lo = max(0, N - 21)
        jd_recent = [_day(i) for i in range(recent_lo, N)
                     if not np.isnan(L[i]) and simple_flag[i]]
        out["jump_days_last_21"] = jd_recent

        # count over the trailing window
        win_lo = max(0, N - W)
        n_jump_win = int(np.nansum(simple_flag[win_lo:]))
        n_jump_gumbel_win = int(np.nansum(gumbel_flag[win_lo:]))
        out["n_jump_days_1y"] = n_jump_win
        out["n_jump_days_1y_gumbel"] = n_jump_gumbel_win

        # --- 3) signed jump variation / realized semivariance ----------------
        neg = a * (a < 0)
        pos = a * (a > 0)
        RSminus = float(np.sum(neg ** 2))
        RSplus = float(np.sum(pos ** 2))
        signed_jump = RSplus - RSminus            # raw variance units over the window
        # annualized-variance scaling for readability (per-year variance)
        sj_ann_var = signed_jump / W * 252.0
        out.update({
            "rs_plus": rnd(RSplus, 6),
            "rs_minus": rnd(RSminus, 6),
            "signed_jump_raw": rnd(signed_jump, 6),        # RS+ - RS- (window, raw var units)
            "signed_jump_ann_var": rnd(sj_ann_var, 6),     # same, scaled to annual variance
            "signed_jump_direction": ("downside-jump dominated" if signed_jump < 0
                                      else "upside-jump dominated" if signed_jump > 0
                                      else "balanced"),
        })

        # --- 4) most recent jump over the full sample ------------------------
        jump_positions = [i for i in range(N) if not np.isnan(L[i]) and simple_flag[i]]
        if jump_positions:
            j = jump_positions[-1]
            last = _day(j)
            last["days_ago"] = int(N - 1 - j)
            out["last_jump"] = last
        else:
            out["last_jump"] = None

        # --- read ------------------------------------------------------------
        js = jump_share
        recent_neg = any(d["sign"] == "-" for d in jd_recent)
        parts = []
        # absolute jump-vol context: share is a FRACTION and is window-sensitive, so a
        # high-vol name can show a low share yet large jumps. Flag large absolute jumps too.
        if jump_ann_vol >= 12.0:
            parts.append(f"large absolute jumps (jump-only vol {jump_ann_vol:.0f}%/yr) — "
                         "jump magnitude is material regardless of the share; hedge convexly")
        if js > 0.3:
            parts.append(f"HIGH jump share ({js:.0%} of variance is discontinuous) — this name "
                         "gaps through stops; prefer a convex OPTION hedge over a stop and size down")
        elif js > 0.15:
            parts.append(f"moderate jump share ({js:.0%}) — some discontinuity risk; a stop can be "
                         "gapped, keep an option overlay in mind")
        else:
            parts.append(f"low jump share ({js:.0%}) — variation is mostly diffusive; a stop is a "
                         "reasonable risk control")
        if jd_recent:
            parts.append(f"{len(jd_recent)} jump day(s) in the last 21 sessions"
                         + (" including a NEGATIVE jump — early fragility trigger, tighten hedges"
                            if recent_neg else ""))
        if signed_jump < 0:
            parts.append("signed jump variation is negative (downside-jump dominated) — tail is to the downside")
        out["read"] = "; ".join(parts)

    except Exception as e:
        errors.append(f"compute: {e}")
        out.setdefault("read", "jump decomposition failed — see errors")

    return out


def main():
    p = argparse.ArgumentParser(description="Jump vs diffusion decomposition for a single name.")
    p.add_argument("ticker")
    p.add_argument("--window", type=int, default=252)
    p.add_argument("--z", type=float, default=4.0)
    a = p.parse_args()
    jprint(analyze(a.ticker, window=a.window, z_thresh=a.z))


if __name__ == "__main__":
    main_guard(main)
