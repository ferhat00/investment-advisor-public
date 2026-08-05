"""Shared helpers for the investment-committee tool library."""
import hashlib
import io
import json
import math
import os
import pickle
import sys
import tempfile
import time
import urllib.request
import urllib.parse
import numpy as np
import pandas as pd

def _ser(o):
    if isinstance(o, np.integer): return int(o)
    if isinstance(o, np.floating): return None if np.isnan(o) else float(o)
    if isinstance(o, pd.Timestamp): return o.strftime("%Y-%m-%d")
    if isinstance(o, np.ndarray): return o.tolist()
    return str(o)

def jprint(obj):
    print(json.dumps(obj, indent=2, default=_ser))

def rnd(x, nd=2):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)): return None
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None

# --- Runtime hardening: main-guard, retry, network cache -------------------
# A committee run spawns many tools as parallel processes that hit the same
# yfinance/FRED endpoints. The cache is user-level (shared across tools AND
# runs, atomic writes so concurrent tools can't corrupt it); retry absorbs
# transient 429s; main_guard turns any crash into one machine-readable JSON
# line so the orchestrator's shell never has to parse a traceback.

_CACHE_TTL = 4 * 3600  # seconds; intraday staleness is analytically fine here


def main_guard(fn):
    """Run a tool's entry point; uncaught exceptions become a single JSON error
    on stdout + exit 1. The entry point must do its own sys.argv unpacking so
    missing-arg IndexErrors are caught too.
    Usage:  if __name__ == "__main__": main_guard(main)"""
    try:
        fn()
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}",
                          "tool": os.path.basename(sys.argv[0]),
                          "argv": sys.argv[1:]}))
        sys.exit(1)


def parse_list(s):
    """Comma-separated CLI argument -> clean list (the shared peer-list idiom)."""
    return [x.strip() for x in str(s or "").split(",") if x.strip()]


def _cache_dir():
    root = os.environ.get("IC_CACHE_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
        or tempfile.gettempdir(), "investment-advisor-cache")
    os.makedirs(root, exist_ok=True)
    return root


def _cached(kind, key, fetch, ttl=None):
    """File-based TTL cache. IC_CACHE_TTL=0 disables; corrupt/stale entries fall
    through to a live fetch; failures are never cached."""
    ttl = _CACHE_TTL if ttl is None else ttl
    env = os.environ.get("IC_CACHE_TTL")
    if env is not None:
        try:
            ttl = float(env)
        except ValueError:
            pass
    if ttl <= 0:
        return fetch()
    path = os.path.join(_cache_dir(),
                        f"{kind}-{hashlib.sha1(str(key).encode()).hexdigest()[:24]}.pkl")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            with open(path, "rb") as f:
                return pickle.load(f)
    except Exception:
        pass
    val = fetch()
    try:
        fd, tmp = tempfile.mkstemp(dir=_cache_dir())
        with os.fdopen(fd, "wb") as f:
            pickle.dump(val, f)
        os.replace(tmp, path)  # atomic — parallel tools can't see a torn write
    except Exception:
        pass
    return val


def _retry(fetch, attempts=3, base_delay=1.5, no_retry=()):
    """Retry transient network failures with exponential backoff. Exceptions in
    `no_retry` (deterministic outcomes like empty-ticker ValueError) re-raise at once."""
    for i in range(attempts):
        try:
            return fetch()
        except no_retry:
            raise
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def fred(series_id, start="2000-01-01"):
    """Keyless FRED fetch via fredgraph CSV endpoint. Returns a pandas Series.
    Cached (full series, sliced by `start` after) + retried."""
    def _fetch():
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        s = pd.read_csv(url, index_col=0, parse_dates=True).iloc[:, 0]
        return pd.to_numeric(s, errors="coerce").dropna()
    s = _cached("fred", series_id, lambda: _retry(_fetch))
    return s[s.index >= start]

def _yahoo_chart_fallback(ticker, period, interval):
    """Keyless Yahoo v8 chart JSON via urllib, shaped like yfinance's
    .history(auto_adjust=True). Used when yfinance's default transport is
    unavailable (e.g. curl_cffi's TLS gets reset behind an egress proxy while
    plain urllib — which honours the system CA + HTTPS_PROXY — still works).
    Same graceful-degradation seam as fetch_csv/fred above."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(str(ticker))}"
           f"?range={period}&interval={interval}&events=div,splits")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    res = (data.get("chart", {}).get("result") or [None])[0]
    if not res or "timestamp" not in res:
        raise ValueError(f"No price data for {ticker}")
    ind = res.get("indicators", {})
    q = (ind.get("quote") or [{}])[0]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True)
    tz = res.get("meta", {}).get("exchangeTimezoneName")
    if interval.endswith(("d", "wk", "mo")):  # daily+: normalize to session date
        idx = (idx.tz_convert(tz) if tz else idx).normalize().tz_localize(None)
    else:
        idx = idx.tz_localize(None)
    df = pd.DataFrame({"Open": q.get("open"), "High": q.get("high"),
                       "Low": q.get("low"), "Close": q.get("close"),
                       "Volume": q.get("volume")}, index=idx)
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose")
    if adj is not None:  # auto_adjust: scale OHLC by adjclose/close, close := adjclose
        adj_s = pd.Series(adj, index=idx)
        ratio = (adj_s / df["Close"]).replace([np.inf, -np.inf], np.nan)
        for c in ("Open", "High", "Low"):
            df[c] = df[c] * ratio
        df["Close"] = adj_s
    return df.dropna(how="all")

def _drop_unconfirmed_weekend_row(df):
    """Drop trailing Sat/Sun rows from a daily-bar frame. FX crosses (`=X`) trade
    through the weekend edge (Asia opens Sunday evening ET), and Yahoo can append
    that still-forming reopen print as a "daily" bar dated to the weekend day it
    started on -- flat OHLC, zero volume, and not a confirmed close. Seen live:
    JPY=X carrying a Sunday row 1.7% off the prior Friday close, which fed through
    every downstream technical indicator as if it were real. No confirmed daily
    session for any ticker in this codebase (equities, ETFs, indices) ever falls
    on a weekend, so this is a no-op for everything except FX."""
    while len(df) and df.index[-1].weekday() >= 5:
        df = df.iloc[:-1]
    return df

def yf_history(ticker, period="2y", interval="1d"):
    """Price history via yfinance. Cached + retried; a genuinely empty ticker
    raises ValueError immediately (not retried, not cached). Falls back to the
    keyless Yahoo chart API (urllib) when yfinance's transport itself fails."""
    def _fetch():
        df = None
        try:
            import yfinance as yf
            df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
        except Exception:
            df = None  # transport failure -> try the urllib fallback below
        if df is None or df.empty:
            df = _yahoo_chart_fallback(ticker, period, interval)
        if df.empty:
            raise ValueError(f"No price data for {ticker}")
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_localize(None)
        if interval == "1d":
            df = _drop_unconfirmed_weekend_row(df)
        if df.empty:
            raise ValueError(f"No price data for {ticker}")
        return df
    return _cached("yf", f"{ticker}|{period}|{interval}",
                   lambda: _retry(_fetch, no_retry=(ValueError,)))

def realized_vol_ann(returns, window=None):
    """Annualized realized volatility from a daily-return series. Uses the trailing
    `window` observations (e.g. 63 = one quarter), or the full series if window is None.
    Single source of truth so every tool reports vol over an explicit, consistent window."""
    r = returns.tail(window) if window else returns
    return float(r.std() * np.sqrt(252))
def yf_statements(ticker):
    """Return (income_stmt, balance_sheet, cashflow) annual DataFrames (most recent col first)."""
    import yfinance as yf
    t = yf.Ticker(ticker)
    return t.income_stmt, t.balance_sheet, t.cashflow


def _install_yahoo_urllib_shim():
    """Route yfinance's ``.info`` / statements / recommendations / ``.history``
    through the urllib transport (``yahoo_urllib``) because curl_cffi's TLS gets
    reset behind the egress proxy. Idempotent; no-ops if yfinance/yahoo_urllib
    aren't importable. Called once at module import so every tool that does
    ``from common import ...`` gets the patched yfinance for free."""
    try:
        import yfinance as yf
        import yahoo_urllib as Y
    except Exception:
        return
    T = yf.Ticker
    if getattr(T, "_ic_urllib_shim", False):
        return

    def _sym(self):
        return getattr(self, "ticker", None) or getattr(self, "_ticker", None)

    def _stmts(self):
        cached = self.__dict__.get("_ic_stmts")
        if cached is None:
            cached = Y.get_statements(_sym(self))
            self.__dict__["_ic_stmts"] = cached
        return cached

    T.info = property(lambda self: Y.get_info(_sym(self)))
    T.get_info = lambda self, *a, **k: Y.get_info(_sym(self))
    T.income_stmt = property(lambda self: _stmts(self)[0])
    T.financials = property(lambda self: _stmts(self)[0])
    T.get_income_stmt = lambda self, *a, **k: _stmts(self)[0]
    T.balance_sheet = property(lambda self: _stmts(self)[1])
    T.balancesheet = property(lambda self: _stmts(self)[1])
    T.get_balance_sheet = lambda self, *a, **k: _stmts(self)[1]
    T.cashflow = property(lambda self: _stmts(self)[2])
    T.cash_flow = property(lambda self: _stmts(self)[2])
    T.get_cashflow = lambda self, *a, **k: _stmts(self)[2]
    T.recommendations = property(lambda self: Y.get_recommendations(_sym(self)))
    # Without this, tools/dividend_analysis.py returns "no dividend history" for every
    # ticker — yfinance's own `.dividends` goes through the curl_cffi path the proxy resets,
    # and the shim patched everything around it. Found by the `income` seat on 2026-07-29,
    # which had to re-implement the whole tool against the chart endpoint to do its job.
    T.dividends = property(lambda self: Y.get_dividends(_sym(self)))
    T.get_dividends = lambda self, *a, **k: Y.get_dividends(_sym(self))

    _orig_hist = T.history

    def _history(self, period="1mo", interval="1d", *a, **k):
        try:
            df = _orig_hist(self, period=period, interval=interval, *a, **k)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass
        try:
            return _yahoo_chart_fallback(_sym(self), period, interval)
        except Exception:
            return pd.DataFrame()

    T.history = _history
    T._ic_urllib_shim = True


_install_yahoo_urllib_shim()

def stmt_row(df, names, n=1):
    """Pull a statement row by trying each alias in `names`; return the first `n` columns
    (most recent first) as a list of floats, NaN/missing -> None. Always length n.

    yfinance row labels drift between tickers/versions, so callers pass an alias list
    (same pattern as market_data.financial_trends and credit_tools._row)."""
    if df is not None and getattr(df, "empty", True) is False:
        for nm in names:
            if nm in df.index:
                vals = list(df.loc[nm].values)[:n]
                out = [None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
                       for v in vals]
                out += [None] * (n - len(out))
                return out
    return [None] * n

def stmt_val(df, names, idx=0):
    """Single value: alias lookup at column idx (0 = most recent). None if absent."""
    return stmt_row(df, names, n=idx + 1)[idx]


# Yahoo quotes some venues in the MINOR currency unit while statements are reported in the
# major one. Keys are case-sensitive on purpose: Yahoo's pence code is "GBp", which differs
# from real pounds "GBP" only by case, so an .upper() here would silently divide every
# GBP-quoted name by 100.
_MINOR_UNIT_QUOTES = {
    "GBp": 0.01,   # LSE quotes pence, statements in pounds
    "GBX": 0.01,   # same, alternate code
    "ZAc": 0.01,   # JSE quotes cents
    "ILA": 0.01,   # TASE quotes agorot
}

def minor_unit_scale(info):
    """Multiplier converting a Yahoo quote into the major currency unit its statements use.

    Any per-share value derived from a balance sheet (RIM, ICC, DCF) is 100x out against a
    raw LSE quote unless this is applied — the classic UK valuation error. Mirrors the
    marketCap convention already used in yahoo_urllib. Returns 1.0 for normal venues.

    NOTE this fixes the unit only, never the currency: an LSE name that REPORTS in USD
    (HSBC, Standard Chartered) still needs an explicit FX conversion on top."""
    return _MINOR_UNIT_QUOTES.get((info or {}).get("currency") or "", 1.0)

def quote_price(info, price_scale=1.0):
    """Live price expressed in the statements' currency unit.

    `price_scale` is the caller's optional FX factor for the cross-currency case above
    (e.g. pass GBPUSD spot for HSBA.L, which quotes in pence but reports in USD)."""
    px = (info or {}).get("currentPrice") or (info or {}).get("regularMarketPrice")
    if px is None:
        return None
    return float(px) * minor_unit_scale(info) * (price_scale or 1.0)

# --- Non-FRED fetch + graceful-degradation + numpy-only stats (macro pack helpers) ---

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def fetch_csv(url, **kw):
    """Fetch a CSV from a non-FRED URL with a browser User-Agent (many gov sites 403
    pandas' default agent). Returns a DataFrame. Raises on failure — wrap with safe()."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return pd.read_csv(io.BytesIO(raw), **kw)

def safe(fn, label, errs):
    """Run fn(), returning its value or None; on exception append '<label>: <err>' to errs.
    The shared graceful-degradation idiom (promoted from fx_data._safe) so one fragile
    fetch/compute can never abort a macro pack build."""
    try:
        return fn()
    except Exception as e:
        errs.append(f"{label}: {e}")
        return None


def _vendor_get(url_env, key_env, ticker):
    """Generic premium-feed adapter: GET <url>?ticker=<t> as JSON if <url_env> is set, else None.
    The single graceful-degradation seam for paid data — callers get None until a feed is wired."""
    url = os.environ.get(url_env)
    if not url:
        return None
    key = os.environ.get(key_env) if key_env else None
    headers = {"User-Agent": _UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(f"{url}{sep}ticker={ticker}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return None

def borrow_data(ticker):
    """Securities-lending adapter (fee_bps, utilization, on_loan, fee_vol). PREMIUM: S3 / S&P Global
    Securities Finance / DataLend / Ortex / IBKR. Set BORROW_FEED_URL (+optional BORROW_FEED_KEY)."""
    return _vendor_get("BORROW_FEED_URL", "BORROW_FEED_KEY", ticker)

def flow_data(ticker):
    """Fund-flow / forced-seller adapter (mutual-fund flow-induced trading, index-reconstitution
    calendar, MFFlow). PREMIUM. Set FLOW_FEED_URL (+optional FLOW_FEED_KEY)."""
    return _vendor_get("FLOW_FEED_URL", "FLOW_FEED_KEY", ticker)

def attention_data(ticker):
    """Retail attention / social-sentiment adapter (Google SVI, StockTwits/Twitter sentiment &
    disagreement). PREMIUM/rate-limited. Set ATTENTION_FEED_URL (+optional ATTENTION_FEED_KEY)."""
    return _vendor_get("ATTENTION_FEED_URL", "ATTENTION_FEED_KEY", ticker)

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))

def _norm_pdf(x):
    return math.exp(-0.5 * float(x) * float(x)) / math.sqrt(2.0 * math.pi)

def zscore_last(series, lookback=None):
    """z-score of the most recent observation vs a trailing window (full series if None)."""
    s = pd.Series(series).dropna()
    if lookback:
        s = s.tail(lookback)
    if len(s) < 5 or float(s.std()) == 0.0:
        return None
    return float((s.iloc[-1] - s.mean()) / s.std())

def probit_fit(y, X, iters=100):
    """Fisher-scoring probit (numpy-only; the repo avoids statsmodels/scipy). y in {0,1};
    X is (n,) or (n,k) WITHOUT an intercept (one is prepended). Returns [b0, b1, ...]."""
    y = np.asarray(y, float)
    X = np.asarray(X, float)
    if X.ndim == 1:
        X = X.reshape(-1, 1)
    n = len(y)
    Xd = np.column_stack([np.ones(n), X])
    k = Xd.shape[1]
    beta = np.zeros(k)
    eps = 1e-6
    for _ in range(iters):
        eta = Xd @ beta
        p = np.clip(np.array([_norm_cdf(v) for v in eta]), eps, 1 - eps)
        phi = np.array([_norm_pdf(v) for v in eta])
        w = phi ** 2 / (p * (1 - p))
        g = Xd.T @ (phi * (y - p) / (p * (1 - p)))
        H = Xd.T @ (Xd * w[:, None])
        try:
            step = np.linalg.solve(H + 1e-8 * np.eye(k), g)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta

def quantile_reg(y, X, tau=0.5, iters=200):
    """IRLS quantile regression (numpy-only). y (n,), X (n,) or (n,k) WITHOUT intercept.
    Returns [b0, b1, ...] for the tau-th conditional quantile (used for Growth-at-Risk)."""
    y = np.asarray(y, float)
    X = np.asarray(X, float)
    if X.ndim == 1:
        X = X.reshape(-1, 1)
    n = len(y)
    Xd = np.column_stack([np.ones(n), X])
    k = Xd.shape[1]
    beta = np.linalg.lstsq(Xd, y, rcond=None)[0]
    eps = 1e-6
    for _ in range(iters):
        r = y - Xd @ beta
        w = np.where(r >= 0, tau, 1 - tau) / np.maximum(np.abs(r), eps)
        WX = Xd * w[:, None]
        try:
            beta_new = np.linalg.solve(Xd.T @ WX + 1e-8 * np.eye(k), Xd.T @ (w * y))
        except np.linalg.LinAlgError:
            break
        if np.max(np.abs(beta_new - beta)) < 1e-8:
            beta = beta_new
            break
        beta = beta_new
    return beta
