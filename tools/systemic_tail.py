"""Systemic / common-tail diagnostics — lift single-name fat-tail stats to a BOOK-level read.

The single-name tail agent tells you one stock is fat-tailed. This tool answers the question
that actually governs sizing: do MANY names in the book share ONE fat COMMON tail? If they do,
the diversification is illusory in stress — the book is really one hidden crash factor — and the
correct response is to force AGGREGATE size down, not to trim names one at a time.

Two subcommands:
  hill  — pooled tail index (fatness of the shared left tail) + per-name tail-beta to the peer
          crash factor. Pooling across names is the Kelly-Jiang (2014) idea: the COMMON tail is
          estimated far more precisely from the panel than from any single name's handful of
          crash days.
  pca   — common-factor variance share from the return correlation matrix (Billio-Getmansky-Lo-
          Pelizzon 2012). A dominant first principal component => one factor drives the book =>
          "25 names" is a story, not a hedge.

Refs:
  Kelly, B. & Jiang, H. (2014). Tail Risk and Asset Prices. Review of Financial Studies 27(10):
    2841-2871.  (pooled cross-sectional Hill tail index; here reconstructed daily/approximately.)
  Hill, B. M. (1975). A Simple General Approach to Inference About the Tail of a Distribution.
    Annals of Statistics 3(5): 1163-1174.  (the tail-index estimator.)
  Gabaix, X., Gopikrishnan, P., Plerou, V. & Stanley, H. E. (2003). A theory of power-law
    distributions in financial market fluctuations. Nature 423: 267-270.  (cubic law, alpha ~ 3.)
  Billio, M., Getmansky, M., Lo, A. W. & Pelizzon, L. (2012). Econometric measures of connectedness
    and systemic risk in the finance and insurance sectors. Journal of Financial Economics 104(3):
    535-559.  (PCA / first-PC absorption as a connectedness/systemic-risk gauge.)
  Bali, T. G., Cakici, N. & Whitelaw, R. F. (2014). Hybrid tail risk and expected stock returns.
    Review of Asset Pricing Studies 4(2): 206-246.  (common vs idiosyncratic tail risk pricing.)

NOTE ON FIDELITY: Kelly-Jiang estimate a MONTHLY cross-sectional tail index from a large panel;
this is a lightweight DAILY reconstruction on a small, hand-picked peer list, so it only
APPROXIMATES the published series. It is also data-heavy / rate-limited / survivorship-biased
(the peer list is static and contains only names that survived) — read the caveats in the output.

House style: numpy + pandas + yfinance only (no scipy/statsmodels); returns a dict; prints via
jprint; rounds via rnd; carries an English "read" and an "errors" list.

CLI:
  python systemic_tail.py hill NVDA --peers AMD,AVGO,MU,QCOM,TSM
  python systemic_tail.py pca  --book portfolio_context.json
  python systemic_tail.py pca  --peers NVDA,AMD,AVGO,MU,QCOM,TSM
"""
import sys
import json
import os
import argparse
import numpy as np
import pandas as pd
from common import jprint, rnd, yf_history, main_guard, parse_list


# ------------------------------- universe / book parsing -------------------------------

def _load_universe(book_path, peers_csv, ticker):
    """Resolve the analysis universe (list of upper-case tickers).

    If `book_path` is given and the file exists, json.load it and defensively pull tickers from
    the common shapes a portfolio-context file takes:
      - a bare list of strings                    -> the strings
      - a list of dicts w/ 'ticker'/'symbol' key  -> those values
      - a {'positions':[...]} / {'holdings':[...]} wrapper -> recurse into the list
      - a dict mapping ticker -> weight            -> the keys
    If parsing yields nothing usable, fall back to `peers_csv` (comma-split). `ticker` (if given)
    is always included. Result is de-duped and upper-cased. Any file error degrades gracefully:
    fall back to peers_csv and append a note to the returned errors list.

    Returns (universe_list, source_str, errors_list).
    """
    errors = []
    tickers = []
    source = "peers"

    def _extract(obj):
        """Best-effort recursive extraction of ticker-like strings from a parsed JSON object."""
        found = []
        if isinstance(obj, str):
            found.append(obj)
        elif isinstance(obj, list):
            for el in obj:
                if isinstance(el, str):
                    found.append(el)
                elif isinstance(el, dict):
                    tk = el.get("ticker") or el.get("symbol") or el.get("Ticker") or el.get("Symbol")
                    if tk:
                        found.append(tk)
        elif isinstance(obj, dict):
            # Prefer an explicit positions/holdings wrapper if present.
            for key in ("positions", "holdings", "portfolio"):
                if key in obj and obj[key]:
                    found.extend(_extract(obj[key]))
            if not found:
                # dict mapping ticker -> weight/value: keys are the tickers.
                for k in obj.keys():
                    if isinstance(k, str):
                        found.append(k)
        return found

    if book_path:
        if os.path.exists(book_path):
            try:
                with open(book_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                tickers = _extract(data)
                if tickers:
                    source = f"book:{os.path.basename(book_path)}"
                else:
                    errors.append(f"book file '{book_path}' parsed but yielded no tickers; "
                                  "falling back to --peers")
            except Exception as e:  # bad JSON, encoding, IO, etc. -> graceful fallback
                errors.append(f"could not parse book file '{book_path}' ({e}); falling back to --peers")
        else:
            errors.append(f"book file '{book_path}' not found; falling back to --peers")

    if not tickers and peers_csv:
        tickers = [x for x in peers_csv.split(",")]

    if ticker:
        tickers.append(ticker)

    # clean: strip, upper, drop blanks; de-dupe preserving order.
    seen, universe = set(), []
    for t in tickers:
        if not isinstance(t, str):
            continue
        tk = t.strip().upper()
        if tk and tk not in seen:
            seen.add(tk)
            universe.append(tk)
    return universe, source, errors


def _returns_panel(universe, period="2y"):
    """Fetch ~2y daily returns for each name and align into a dropna'd DataFrame.
    Returns (df, errors). Names that fail to fetch are dropped and noted."""
    errors = []
    cols = {}
    for tk in universe:
        try:
            px = yf_history(tk, period=period)["Close"]
            r = px.pct_change().dropna()
            if len(r) > 20:
                cols[tk] = r
            else:
                errors.append(f"{tk}: too few return observations, dropped")
        except Exception as e:
            errors.append(f"{tk}: fetch failed ({e}), dropped")
    if not cols:
        return pd.DataFrame(), errors
    df = pd.DataFrame(cols).dropna()
    return df, errors


# ------------------------------------- Hill estimator -------------------------------------

def _hill_xi(losses, top_frac=0.05, k_min=20, k_max=500):
    """Hill (1975) tail-index estimator on a 1-D array of positive loss magnitudes.

    Sort descending L_(1) >= ... >= L_(n); choose k = top `top_frac` order statistics (clamped
    to [k_min, k_max] and to n-1), then
        xi_hill = (1/k) * sum_{i=1..k} ( ln L_(i) - ln L_(k+1) ).
    tail alpha = 1 / xi_hill.  Returns (xi, alpha, k_used, n_losses) or Nones if degenerate.
    """
    L = np.asarray(losses, dtype=float)
    L = L[L > 0]
    n = L.size
    if n < k_min + 1:
        return None, None, None, n
    L_sorted = np.sort(L)[::-1]                    # descending
    k = int(np.floor(top_frac * n))
    k = max(k_min, min(k, k_max, n - 1))
    thresh = L_sorted[k]                           # L_(k+1) (0-indexed k)
    if thresh <= 0:
        return None, None, None, n
    xi = float(np.mean(np.log(L_sorted[:k]) - np.log(thresh)))
    if xi <= 0:
        return None, None, k, n
    return xi, 1.0 / xi, k, n


def _name_alpha(r):
    """Own-return tail alpha for a single name's daily-return Series (losses = -r where r<0)."""
    losses = -r[r < 0].values
    _, alpha, _, _ = _hill_xi(losses)
    return rnd(alpha, 2) if alpha is not None else None


def hill(ticker, peers, book_path=None, period="2y"):
    """Pooled tail index (Kelly-Jiang 2014 style) + per-name tail-beta to the peer crash factor.

    - Pool losses ACROSS all names: stack every column's returns, keep those < 0, take magnitudes.
      Estimate one Hill tail index on that pooled panel -> pooled_tail_alpha. Precise because the
      panel has far more tail observations than any single name.
    - Benchmark vs Gabaix et al. (2003) cubic law (alpha ~ 3): alpha < 3 => a fatter-than-cubic
      COMMON tail => shared crash risk the correlation matrix understates.
    - Per-name own-tail alpha (dict) for context, plus the SUBJECT ticker's own alpha.
    - Name tail-beta: equal-weight PEER factor (excluding the subject), restricted to the pooled
      left-tail days (worst 5% of the equal-weight UNIVERSE return); regress subject returns on the
      peer factor over those days (numpy lstsq) -> tail_beta. Full-sample beta reported too.
    """
    errors = []
    universe, source, uerr = _load_universe(book_path, ",".join(peers) if peers else None, ticker)
    errors += uerr
    if len(universe) < 4:
        errors.append(f"low peer/name count ({len(universe)}) — pooled tail index and PCA are "
                      "unreliable below ~4 names; treat as directional only")

    df, ferr = _returns_panel(universe, period=period)
    errors += ferr
    if df.empty or df.shape[1] < 2:
        return {"subcommand": "hill", "ticker": ticker, "universe": universe, "source": source,
                "errors": errors + ["insufficient aligned return data to compute a pooled tail"],
                "read": "Not enough data to estimate a common tail."}

    names = list(df.columns)

    # --- pooled Hill on losses stacked across every name ---
    pooled_losses = -df.values[df.values < 0]
    p_xi, p_alpha, k_used, n_pooled = _hill_xi(pooled_losses)

    # --- per-name own-tail alpha ---
    name_alphas = {tk: _name_alpha(df[tk]) for tk in names}
    subject_alpha = name_alphas.get(ticker.upper()) if ticker else None

    # --- name tail-beta to the equal-weight PEER crash factor ---
    tail_beta = full_beta = None
    n_tail_days = None
    subj = ticker.upper() if ticker else None
    peer_cols = [c for c in names if c != subj]
    if subj in names and peer_cols:
        peer_factor = df[peer_cols].mean(axis=1)          # equal-weight peer return
        univ_factor = df[names].mean(axis=1)              # equal-weight universe return (tail selector)
        y_full = df[subj].values
        x_full = peer_factor.values
        # full-sample beta
        A = np.column_stack([np.ones_like(x_full), x_full])
        full_beta = float(np.linalg.lstsq(A, y_full, rcond=None)[0][1])
        # tail-day beta: worst 5% of the equal-weight universe return
        q = np.quantile(univ_factor.values, 0.05)
        mask = univ_factor.values <= q
        n_tail_days = int(mask.sum())
        if n_tail_days >= 10:
            yt, xt = y_full[mask], x_full[mask]
            At = np.column_stack([np.ones_like(xt), xt])
            tail_beta = float(np.linalg.lstsq(At, yt, rcond=None)[0][1])
        else:
            errors.append(f"only {n_tail_days} pooled left-tail days — tail-beta suppressed "
                          "(need >=10); use full-sample beta")
    elif subj not in names:
        errors.append(f"subject {ticker} not in the fetched panel — no tail-beta computed")

    # --- reads ---
    fatter = p_alpha is not None and p_alpha < 3.0
    reads = []
    if p_alpha is not None:
        reads.append(
            f"pooled common-tail alpha ~ {rnd(p_alpha,2)} "
            + ("(< 3, FATTER than the Gabaix cubic law): the shared left tail is heavier than a "
               "correlation-matrix view implies — crashes cluster across these names, so "
               "diversification across them is partly illusory. Force AGGREGATE size down."
               if fatter else
               "(>= 3, at/thinner than the cubic law): the common tail is not unusually fat; "
               "the usual diversification assumptions are less abused here."))
    if tail_beta is not None:
        if tail_beta > 1.2:
            reads.append(f"{ticker} tail-beta {rnd(tail_beta,2)} to the peer crash factor > 1: it "
                         "AMPLIFIES the shared crash — adding it levers the common tail, not diversifies it.")
        elif tail_beta < 0.6:
            reads.append(f"{ticker} tail-beta {rnd(tail_beta,2)} < ~0.6: relatively insulated from the "
                         "peer crash factor on the worst days — a genuine (partial) diversifier.")
        else:
            reads.append(f"{ticker} tail-beta {rnd(tail_beta,2)}: co-crashes roughly one-for-one with peers.")
    reads.append("Daily reconstruction on a static, survivorship-biased peer list — approximates but "
                 "does NOT reproduce the published monthly Kelly-Jiang cross-sectional tail series.")

    return {
        "subcommand": "hill",
        "ticker": ticker,
        "universe": names,
        "source": source,
        "n_names": len(names),
        "period": period,
        "pooled_tail": {
            "pooled_xi_hill": rnd(p_xi, 4),
            "pooled_tail_alpha": rnd(p_alpha, 2),
            "k_used": k_used,
            "n_pooled_losses": n_pooled,
            "cubic_law_alpha": 3.0,
            "fatter_than_cubic": bool(fatter) if p_alpha is not None else None,
        },
        "per_name_tail_alpha": name_alphas,
        "subject_own_tail_alpha": subject_alpha,
        "tail_beta": {
            "tail_factor": "equal-weight peer return (subject excluded), on worst-5% universe days",
            "tail_beta": rnd(tail_beta, 2),
            "full_sample_beta": rnd(full_beta, 2),
            "n_tail_days": n_tail_days,
        },
        "read": " ".join(reads),
        "errors": errors,
    }


# ------------------------------------------ PCA ------------------------------------------

def pca(peers, book_path=None, ticker=None, period="2y"):
    """Common-factor variance share from the return correlation matrix (Billio et al. 2012).

    Eigen-decompose the correlation matrix (np.linalg.eigh), sort eigenvalues descending, and
    report first_pc_share_pct = 100 * lambda_1 / sum(lambda) plus the top-3 shares. A high first-PC
    absorption (>~50-60%) means one dominant common factor runs the book — diversification is
    illusory in stress. Combined with a low pooled tail alpha (see `hill`), that is the trigger to
    force aggregate size down. Refs also: Bali-Cakici-Whitelaw 2014 (common vs idiosyncratic tail).
    """
    errors = []
    universe, source, uerr = _load_universe(book_path, ",".join(peers) if peers else None, ticker)
    errors += uerr
    if len(universe) < 4:
        errors.append(f"low name count ({len(universe)}) — a first-PC share on <4 names is barely "
                      "meaningful; treat as directional only")

    df, ferr = _returns_panel(universe, period=period)
    errors += ferr
    if df.empty or df.shape[1] < 2:
        return {"subcommand": "pca", "universe": universe, "source": source,
                "errors": errors + ["insufficient aligned return data for a correlation matrix"],
                "read": "Not enough data to estimate a common factor."}

    names = list(df.columns)
    C = np.corrcoef(df.values, rowvar=False)
    # eigh for a symmetric matrix; eigenvalues ascending -> reverse to descending.
    eigvals = np.linalg.eigh(C)[0][::-1]
    eigvals = np.clip(eigvals, 0.0, None)          # guard tiny negative numerical noise
    total = float(eigvals.sum())
    shares = (100.0 * eigvals / total) if total > 0 else np.zeros_like(eigvals)

    first = float(shares[0]) if shares.size else None
    top3 = [rnd(float(s), 1) for s in shares[:3]]

    read = ""
    if first is not None:
        if first > 60:
            read = (f"first principal component absorbs {rnd(first,1)}% of variance — one DOMINANT "
                    "common factor runs this set. Diversification is largely illusory in stress; if the "
                    "pooled tail alpha is also low (< 3), FORCE AGGREGATE SIZE DOWN — the book is one bet.")
        elif first > 50:
            read = (f"first PC absorbs {rnd(first,1)}% — a strong single common factor. The book leans "
                    "heavily on one driver; check the pooled tail alpha and cut aggregate size if it is fat.")
        elif first > 35:
            read = (f"first PC absorbs {rnd(first,1)}% — moderate common-factor loading; some genuine "
                    "diversification remains but a shared driver is present.")
        else:
            read = (f"first PC absorbs {rnd(first,1)}% — no single dominant factor; the names carry real "
                    "idiosyncratic content and diversify each other reasonably well.")
    read += (" NOTE: static/survivorship-biased peer list; correlation-based PCA understates tail "
             "co-movement — pair with the pooled tail alpha from `hill`.")

    return {
        "subcommand": "pca",
        "universe": names,
        "source": source,
        "n_names": len(names),
        "period": period,
        "first_pc_share_pct": rnd(first, 1),
        "top3_pc_share_pct": top3,
        "eigenvalues_desc": [rnd(float(v), 4) for v in eigvals],
        "read": read,
        "errors": errors,
    }


# ------------------------------------------ CLI ------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Systemic / common-tail diagnostics (book-level).")
    p.add_argument("cmd", choices=["hill", "pca"])
    p.add_argument("ticker", nargs="?", default=None,
                   help="subject ticker (required for hill; optional for pca)")
    p.add_argument("--peers", default=None, help="comma-separated peer tickers")
    p.add_argument("--book", default=None, help="path to a portfolio_context.json (book) file")
    p.add_argument("--period", default="2y", help="yfinance history period (default 2y)")
    a = p.parse_args()

    peers = parse_list(a.peers)

    if a.cmd == "hill":
        if not a.ticker:
            p.error("hill requires a subject TICKER")
        jprint(hill(a.ticker, peers, book_path=a.book, period=a.period))
    elif a.cmd == "pca":
        jprint(pca(peers, book_path=a.book, ticker=a.ticker, period=a.period))


if __name__ == "__main__":
    main_guard(main)
