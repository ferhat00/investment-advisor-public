"""urllib-based Yahoo Finance transport (proxy-safe fallback for yfinance).

Why this exists
---------------
Behind the session's egress proxy, yfinance's default ``curl_cffi`` transport has
its TLS handshake reset — the proxy re-terminates TLS and cannot reproduce
curl_cffi's browser-impersonation ClientHello (the "fingerprinted client" class
the proxy README says is unsupported). Plain ``urllib`` honours ``HTTPS_PROXY`` +
the system CA bundle and works fine.

``common._yahoo_chart_fallback`` already covers *price history* over urllib. This
module covers the other three surfaces the tool library needs from a
``yfinance.Ticker`` — ``.info``, the three financial statements, and analyst
recommendations — using the same keyless / cookie+crumb Yahoo endpoints over
urllib. ``common._install_yahoo_urllib_shim`` monkeypatches ``yfinance.Ticker`` to
route through these, so every tool keeps calling ``yf.Ticker(...).info`` etc.
unchanged.

All fetches degrade gracefully: a failure returns a sparse dict / empty
DataFrame rather than raising, so one flaky endpoint never aborts a tool. Row
labels and column dtypes match what yfinance itself produces (verified against
``yfinance.utils.camel2title`` + ``yfinance.const.fundamentals_keys``), so the
tools' alias lookups (``common.stmt_row`` etc.) resolve identically.
"""
import http.cookiejar
import json
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# quoteSummary modules that, flattened + merged, reproduce the fields the tools
# read off yfinance's ``.info`` (price, valuation, balance-sheet snapshot,
# analyst targets, profile).
_INFO_MODULES = ("price", "summaryDetail", "defaultKeyStatistics", "financialData",
                 "summaryProfile", "assetProfile", "quoteType", "recommendationTrend")

# yfinance's own camel2title acronym sets, per statement (yfinance/base.py).
_STMT_KEYS = None  # lazily built from yfinance.const to avoid import cost at module load

_opener = None
_crumb = None


def _ensure_stmt_keys():
    global _STMT_KEYS
    if _STMT_KEYS is None:
        from yfinance import const
        _STMT_KEYS = {
            "income": (list(const.fundamentals_keys["financials"]), ["EBIT", "EBITDA", "EPS", "NI"]),
            "balance": (list(const.fundamentals_keys["balance-sheet"]), ["PPE"]),
            "cash": (list(const.fundamentals_keys["cash-flow"]), ["PPE"]),
        }
    return _STMT_KEYS


def _session():
    """Return a cached (opener, crumb). Seeds a cookie then fetches a crumb — the
    same handshake yfinance does internally, but over urllib so it honours the proxy."""
    global _opener, _crumb
    if _opener is not None:
        return _opener, _crumb
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", _UA), ("Accept", "*/*"),
                     ("Accept-Language", "en-US,en;q=0.9")]
    for seed in ("https://finance.yahoo.com/quote/AAPL", "https://finance.yahoo.com"):
        try:
            op.open(seed, timeout=30).read()
            break
        except Exception:
            continue
    crumb = None
    for host in ("query1", "query2"):
        try:
            body = op.open(f"https://{host}.finance.yahoo.com/v1/test/getcrumb",
                           timeout=30).read().decode("utf-8", "ignore").strip()
            if body and "<" not in body and "too many" not in body.lower():
                crumb = body
                break
        except Exception:
            continue
    _opener, _crumb = op, crumb
    return _opener, _crumb


def _get_json(url):
    op, _ = _session()
    with op.open(url, timeout=30) as r:
        return json.loads(r.read())


def _flatten(module):
    """Flatten one quoteSummary module: {k: v['raw']} for {raw,fmt} cells, scalars kept."""
    out = {}
    if not isinstance(module, dict):
        return out
    for k, v in module.items():
        if isinstance(v, dict):
            if "raw" in v:
                out[k] = v["raw"]
        elif isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
    return out


# Yahoo valuation-measures timeseries -> yfinance ``.info`` keys (all crumbless).
_VALUATION_MAP = {
    "trailingPeRatio": "trailingPE",
    "trailingForwardPeRatio": "forwardPE",
    "trailingPsRatio": "priceToSalesTrailing12Months",
    "trailingPbRatio": "priceToBook",
    "trailingEnterprisesValueEBITDARatio": "enterpriseToEbitda",
    "trailingEnterprisesValueRevenueRatio": "enterpriseToRevenue",
    "trailingEnterpriseValue": "enterpriseValue",
    "trailingMarketCap": "trailingMarketCap",
}


def _fetch_chart_meta(ticker):
    for host in ("query1", "query2"):
        try:
            d = _get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                          f"{urllib.parse.quote(ticker)}?range=1d&interval=1d")
            return (d.get("chart", {}).get("result") or [{}])[0].get("meta") or {}
        except Exception:
            continue
    return {}


def _fetch_valuation(ticker):
    types = ",".join(_VALUATION_MAP)
    out = {}
    for host in ("query2", "query1"):
        url = (f"https://{host}.finance.yahoo.com/ws/fundamentals-timeseries/"
               f"v1/finance/timeseries/{urllib.parse.quote(ticker)}"
               f"?symbol={urllib.parse.quote(ticker)}&type={types}"
               f"&period1=1609459200&period2=1924992000")
        try:
            d = _get_json(url)
            for r in (d.get("timeseries") or {}).get("result") or []:
                typ = (r.get("meta", {}).get("type") or [None])[0]
                if not typ or typ not in _VALUATION_MAP:
                    continue
                pts = [p for p in (r.get(typ) or []) if p and (p.get("reportedValue") or {}).get("raw") is not None]
                if pts:
                    out[_VALUATION_MAP[typ]] = pts[-1]["reportedValue"]["raw"]
            if out:
                return out
        except Exception:
            continue
    return out


def _snapshot_from_statements(info, ticker):
    """Fill balance-sheet / income snapshot + derived margins from the (cached)
    statement DataFrames — the crumbless substitute for financialData."""
    try:
        inc, bal, cf = get_statements(ticker)
    except Exception:
        return

    def v(df, names):
        for n in names:
            if df is not None and getattr(df, "empty", True) is False and n in df.index:
                vals = df.loc[n].values
                if len(vals):
                    x = vals[0]
                    return None if (x is None or (isinstance(x, float) and np.isnan(x))) else float(x)
        return None

    rev = v(inc, ["Total Revenue", "Operating Revenue"])
    rev_prev = None
    if inc is not None and getattr(inc, "empty", True) is False and "Total Revenue" in inc.index:
        rv = inc.loc["Total Revenue"].values
        if len(rv) > 1 and rv[1] == rv[1]:
            rev_prev = float(rv[1])
    gp = v(inc, ["Gross Profit"])
    oi = v(inc, ["Operating Income", "EBIT"])
    ni = v(inc, ["Net Income", "Net Income Common Stockholders"])
    ebitda = v(inc, ["EBITDA", "Normalized EBITDA"])
    eq = v(bal, ["Stockholders Equity", "Common Stock Equity"])
    shares = v(bal, ["Ordinary Shares Number", "Share Issued"])
    setters = {
        "totalRevenue": rev,
        "ebitda": ebitda,
        "totalDebt": v(bal, ["Total Debt"]),
        "totalCash": v(bal, ["Cash And Cash Equivalents",
                             "Cash Cash Equivalents And Short Term Investments"]),
        "freeCashflow": v(cf, ["Free Cash Flow"]),
        "operatingCashflow": v(cf, ["Operating Cash Flow"]),
        "sharesOutstanding": shares,
        "grossMargins": (gp / rev) if (gp is not None and rev) else None,
        "operatingMargins": (oi / rev) if (oi is not None and rev) else None,
        "returnOnEquity": (ni / eq) if (ni is not None and eq) else None,
        "revenueGrowth": (rev / rev_prev - 1) if (rev and rev_prev) else None,
    }
    for k, val in setters.items():
        if info.get(k) is None and val is not None:
            info[k] = val


def _fetch_info(ticker):
    """Build a yfinance-shaped ``.info`` dict from crumbless endpoints (chart meta,
    valuation-measures timeseries, statement snapshot). quoteSummary is used only as
    optional enrichment when a crumb is obtainable — Yahoo throttles the crumb
    handshake behind a shared egress IP, so the dict must be complete without it."""
    info = {"symbol": ticker}

    meta = _fetch_chart_meta(ticker)
    if meta:
        px = meta.get("regularMarketPrice")
        info["regularMarketPrice"] = px
        info["currentPrice"] = px
        info["currency"] = meta.get("currency")
        info["exchange"] = meta.get("exchangeName")
        info["fiftyTwoWeekHigh"] = meta.get("fiftyTwoWeekHigh")
        info["fiftyTwoWeekLow"] = meta.get("fiftyTwoWeekLow")
        info["previousClose"] = meta.get("chartPreviousClose")
        info["regularMarketVolume"] = meta.get("regularMarketVolume")
        if meta.get("longName"):
            info["longName"] = meta["longName"]
        if meta.get("shortName"):
            info["shortName"] = meta["shortName"]

    info.update(_fetch_valuation(ticker))
    _snapshot_from_statements(info, ticker)

    # marketCap: prefer live price x shares (currency-correct for GBp pence quotes),
    # fall back to Yahoo's trailingMarketCap series.
    px = info.get("regularMarketPrice")
    shares = info.get("sharesOutstanding")
    if px is not None and shares:
        factor = 0.01 if (info.get("currency") == "GBp") else 1.0
        info["marketCap"] = px * factor * shares
    elif info.get("trailingMarketCap") is not None:
        info["marketCap"] = info["trailingMarketCap"]

    # Optional enrichment: sector/industry/country, beta, analyst targets, div yield —
    # only if the crumb handshake succeeds (best-effort, never required).
    try:
        _, crumb = _session()
        if crumb:
            mods = ",".join(_INFO_MODULES)
            for host in ("query1", "query2"):
                try:
                    d = _get_json(f"https://{host}.finance.yahoo.com/v10/finance/quoteSummary/"
                                  f"{urllib.parse.quote(ticker)}?modules={mods}&crumb={urllib.parse.quote(crumb)}")
                    res = (d.get("quoteSummary", {}).get("result") or [None])[0]
                    if not res:
                        continue
                    for module in res.values():
                        for k, val in _flatten(module).items():
                            info.setdefault(k, val)
                    prof = res.get("summaryProfile") or res.get("assetProfile") or {}
                    for k in ("sector", "industry", "country", "longBusinessSummary"):
                        if prof.get(k) is not None:
                            info.setdefault(k, prof[k])
                    break
                except Exception:
                    continue
    except Exception:
        pass

    return info


def _fetch_timeseries(ticker, keys):
    """Return {camelKey: {Timestamp: value}} for the given annual fundamentals keys."""
    _, crumb = _session()
    prefixed = ["annual" + k for k in keys]
    rows = {}
    for i in range(0, len(prefixed), 25):
        chunk = prefixed[i:i + 25]
        types = ",".join(chunk)
        for host in ("query2", "query1"):
            url = (f"https://{host}.finance.yahoo.com/ws/fundamentals-timeseries/"
                   f"v1/finance/timeseries/{urllib.parse.quote(ticker)}"
                   f"?symbol={urllib.parse.quote(ticker)}&type={types}"
                   f"&period1=1420070400&period2=1924992000&merge=false")
            if crumb:
                url += "&crumb=" + urllib.parse.quote(crumb)
            try:
                d = _get_json(url)
                res = (d.get("timeseries") or {}).get("result") or []
                for r in res:
                    typ = (r.get("meta", {}).get("type") or [None])[0]
                    if not typ or typ not in r:
                        continue
                    key = typ[len("annual"):]
                    for pt in r[typ]:
                        if not pt:
                            continue
                        d0 = pt.get("asOfDate")
                        val = (pt.get("reportedValue") or {}).get("raw")
                        if d0 is None or val is None:
                            continue
                        rows.setdefault(key, {})[pd.Timestamp(d0)] = val
                break  # host succeeded for this chunk
            except Exception:
                continue
    return rows


def _statement_df(ticker, which):
    from yfinance.utils import camel2title
    keys, acronyms = _ensure_stmt_keys()[which]
    rows = _fetch_timeseries(ticker, keys)
    if not rows:
        return pd.DataFrame()
    dates = sorted({d for series in rows.values() for d in series}, reverse=True)
    data = {}
    for key, series in rows.items():
        label = camel2title([key], sep=" ", acronyms=acronyms)[0]
        data[label] = [series.get(d, np.nan) for d in dates]
    df = pd.DataFrame(data, index=pd.DatetimeIndex(dates)).T  # rows=labels, cols=dates
    return df


# --- public API (cached via common; degrade to sparse on failure) ------------

def _cached_ok(kind, key, fetch, ok):
    """common._cached but never persist a failed/sparse fetch (so one bad call
    doesn't poison the 4h cache for the whole committee run)."""
    from common import _cached
    def _f():
        v = fetch()
        if not ok(v):
            raise RuntimeError("sparse fetch — not caching")
        return v
    try:
        return _cached(kind, key, _f)
    except Exception:
        try:
            return fetch()
        except Exception:
            return {} if kind == "yinfo" else (pd.DataFrame(), pd.DataFrame(), pd.DataFrame())


def get_info(ticker):
    ok = lambda v: isinstance(v, dict) and (v.get("regularMarketPrice") is not None
                                            or v.get("marketCap") is not None
                                            or v.get("currentPrice") is not None)
    return _cached_ok("yinfo", ticker, lambda: _fetch_info(ticker), ok)


def get_statements(ticker):
    def _fetch():
        return (_statement_df(ticker, "income"),
                _statement_df(ticker, "balance"),
                _statement_df(ticker, "cash"))
    ok = lambda t: isinstance(t, tuple) and any(getattr(x, "empty", True) is False for x in t)
    return _cached_ok("ystmt", ticker, _fetch, ok)


def get_recommendations(ticker):
    _, crumb = _session()
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v10/finance/quoteSummary/"
               f"{urllib.parse.quote(ticker)}?modules=recommendationTrend")
        if crumb:
            url += "&crumb=" + urllib.parse.quote(crumb)
        try:
            d = _get_json(url)
            res = (d.get("quoteSummary", {}).get("result") or [None])[0]
            trend = ((res or {}).get("recommendationTrend") or {}).get("trend") or []
            if trend:
                return pd.DataFrame(trend)
        except Exception:
            continue
    return pd.DataFrame()


def get_dividends(ticker, period="10y"):
    """Dividend history as a date-indexed Series, shaped like ``yfinance.Ticker.dividends``.

    The v8 chart endpoint returns dividend events alongside prices when asked for them, and
    it is the one Yahoo surface that works through this transport. Without this,
    ``tools/dividend_analysis.py`` returns "no dividend history" for every ticker, because
    yfinance's own ``.dividends`` goes through the curl_cffi path that the proxy resets.
    """
    def _fetch():
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(str(ticker))}?range={period}&interval=1d&events=div")
        d = _get_json(url)
        res = (d.get("chart", {}).get("result") or [None])[0]
        if not res:
            return pd.Series(dtype=float)
        divs = ((res.get("events") or {}).get("dividends") or {})
        if not divs:
            return pd.Series(dtype=float)
        rows = sorted((int(v["date"]), float(v["amount"])) for v in divs.values())
        idx = pd.to_datetime([r[0] for r in rows], unit="s", utc=True).tz_localize(None).normalize()
        return pd.Series([r[1] for r in rows], index=idx, name="Dividends")

    ok = lambda s: isinstance(s, pd.Series) and not s.empty
    return _cached_ok("ydiv", f"{ticker}|{period}", _fetch, ok)
