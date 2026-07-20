"""Market data + shared data-pack builder.
CLI:
  python market_data.py quote NVDA
  python market_data.py trends NVDA
  python market_data.py margins NVDA           -> 4y margin trajectory + operating leverage
  python market_data.py pack NVDA [outdir]     -> writes prices.csv + data_pack.json
"""
import sys, os, json
import pandas as pd
import yfinance as yf
from common import jprint, rnd, yf_history, _ser, main_guard

# SPDR sector ETFs — the sector benchmark used to decontaminate a name's own move (residual
# reversal) and to read relative sentiment. yfinance `sector` strings on the left.
_SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Energy": "XLE",
    "Industrials": "XLI", "Basic Materials": "XLB", "Utilities": "XLU",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}

def _sector_etf(sector):
    return _SECTOR_ETF.get(sector)

def _analyst_distribution(t):
    """Buy/Hold/Sell COUNT distribution (not just the consensus key), plus the same distribution
    one and three months ago so a rec-CHANGE can be derived (Jegadeesh-Kim-Krische-Lee 2004:
    the revision predicts, the biased level does not). Newer yfinance exposes t.recommendations
    with relative-month periods (0m,-1m,-2m,-3m). Degrades to {} if unavailable."""
    keys = ["strongBuy", "buy", "hold", "sell", "strongSell"]
    try:
        r = t.recommendations
        if r is None or getattr(r, "empty", True):
            return {}
        r = r.reset_index()
        def row_for(period):
            if "period" in r.columns:
                sub = r[r["period"] == period]
                return sub.iloc[0] if not sub.empty else None
            return None
        def dist(row):
            if row is None:
                return None
            return {k: int(row.get(k, 0) or 0) for k in keys}
        cur = dist(row_for("0m")) or dist(r.iloc[0])
        out = {"current": cur, "prior_1m": dist(row_for("-1m")), "prior_3m": dist(row_for("-3m"))}
        return {k: v for k, v in out.items() if v}
    except Exception:
        return {}

def quote(ticker):
    t = yf.Ticker(ticker)
    i = t.info or {}
    return {
        "ticker": ticker,
        "name": i.get("longName") or i.get("shortName"),
        "price": i.get("currentPrice") or i.get("regularMarketPrice"),
        "currency": i.get("currency"),
        "market_cap": i.get("marketCap"),
        "sector": i.get("sector"), "industry": i.get("industry"),
        "country": i.get("country"),
        "pe_ttm": rnd(i.get("trailingPE")), "pe_fwd": rnd(i.get("forwardPE")),
        "ps_ttm": rnd(i.get("priceToSalesTrailing12Months")),
        "pb": rnd(i.get("priceToBook")),
        "ev_ebitda": rnd(i.get("enterpriseToEbitda")),
        "dividend_yield_pct": rnd((i.get("dividendYield") or 0) * (100 if (i.get("dividendYield") or 0) < 1 else 1)),
        "beta": rnd(i.get("beta")),
        "52w_high": i.get("fiftyTwoWeekHigh"), "52w_low": i.get("fiftyTwoWeekLow"),
        "short_pct_float": rnd((i.get("shortPercentOfFloat") or 0) * 100),
        "shares_short": i.get("sharesShort"),
        "shares_short_prior_month": i.get("sharesShortPriorMonth"),
        "short_ratio_dtc": rnd(i.get("shortRatio"), 2),   # Yahoo days-to-cover
        "analyst_target_mean": i.get("targetMeanPrice"),
        "analyst_target_median": i.get("targetMedianPrice"),
        "analyst_target_high": i.get("targetHighPrice"),
        "analyst_target_low": i.get("targetLowPrice"),
        "analyst_rec": i.get("recommendationKey"),
        "analyst_rec_mean": rnd(i.get("recommendationMean"), 2),  # 1=strongBuy .. 5=strongSell
        "analyst_distribution": _analyst_distribution(t),
        "n_analysts": i.get("numberOfAnalystOpinions"),
        "sector_etf": _sector_etf(i.get("sector")),
        "next_earnings": str(i.get("earningsTimestamp", "")),
        "shares_out": i.get("sharesOutstanding"),
        "total_cash": i.get("totalCash"), "total_debt": i.get("totalDebt"),
        "fcf_ttm": i.get("freeCashflow"), "ebitda_ttm": i.get("ebitda"),
        "revenue_ttm": i.get("totalRevenue"),
        "gross_margin_pct": rnd((i.get("grossMargins") or 0) * 100),
        "op_margin_pct": rnd((i.get("operatingMargins") or 0) * 100),
        "roe_pct": rnd((i.get("returnOnEquity") or 0) * 100),
        "rev_growth_pct": rnd((i.get("revenueGrowth") or 0) * 100),
        "earnings_growth_pct": rnd((i.get("earningsGrowth") or 0) * 100),
    }

def financial_trends(ticker, years=4):
    """Annual statement trends (most recent first)."""
    t = yf.Ticker(ticker)
    out = {}
    try:
        inc, bal, cf = t.income_stmt, t.balance_sheet, t.cashflow
        def row(df, names):
            for n in names:
                if n in df.index:
                    return [rnd(v, 0) for v in df.loc[n].head(years).tolist()]
            return None
        out["fiscal_years"] = [str(c.date()) for c in inc.columns[:years]]
        out["revenue"] = row(inc, ["Total Revenue"])
        out["gross_profit"] = row(inc, ["Gross Profit"])
        out["operating_income"] = row(inc, ["Operating Income", "EBIT"])
        out["net_income"] = row(inc, ["Net Income"])
        out["interest_expense"] = row(inc, ["Interest Expense"])
        out["total_assets"] = row(bal, ["Total Assets"])
        out["total_debt"] = row(bal, ["Total Debt"])
        out["cash"] = row(bal, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"])
        out["equity"] = row(bal, ["Stockholders Equity", "Common Stock Equity"])
        out["retained_earnings"] = row(bal, ["Retained Earnings"])
        out["working_capital"] = row(bal, ["Working Capital"])
        out["ocf"] = row(cf, ["Operating Cash Flow"])
        out["capex"] = row(cf, ["Capital Expenditure"])
        out["fcf"] = row(cf, ["Free Cash Flow"])
        out["buybacks"] = row(cf, ["Repurchase Of Capital Stock"])
        out["dividends_paid"] = row(cf, ["Cash Dividends Paid", "Common Stock Dividend Paid"])
    except Exception as e:
        out["error"] = str(e)
    return out

def _margins_from_trends(ft):
    """Margin trajectory + operating leverage from an already-built financial_trends dict (no
    extra fetch). Growth durability lives in the TREND — gross-margin expansion as revenue scales,
    and operating leverage (Δoperating-income% ÷ Δrevenue% > 1) — which a single snapshot hides.
    Series are most-recent-first, matching financial_trends."""
    rev, gp = ft.get("revenue"), ft.get("gross_profit")
    op, fcf = ft.get("operating_income"), ft.get("fcf")

    def pct(nu, de):
        nu, de = nu or [], de or []
        return [rnd(100 * n / d, 1) if (n is not None and d) else None for n, d in zip(nu, de)]

    out = {"fiscal_years": ft.get("fiscal_years"), "gross_margin_pct": pct(gp, rev),
           "operating_margin_pct": pct(op, rev), "fcf_margin_pct": pct(fcf, rev)}
    if rev and len(rev) >= 2 and rev[0] and rev[1]:
        d_rev = rev[0] / rev[1] - 1
        out["revenue_growth_pct"] = rnd(100 * d_rev, 1)
        if op and op[0] is not None and op[1]:
            out["operating_leverage"] = rnd((op[0] / op[1] - 1) / d_rev, 2) if d_rev else None
        if gp and gp[0] is not None and gp[1] is not None and (rev[0] - rev[1]):
            out["incremental_gross_margin_pct"] = rnd(100 * (gp[0] - gp[1]) / (rev[0] - rev[1]), 1)
    gm = out["gross_margin_pct"] or []
    if len([x for x in gm if x is not None]) >= 2 and gm[0] is not None and gm[-1] is not None:
        dg = gm[0] - gm[-1]
        out["gross_margin_trend"] = ("expanding with scale" if dg > 1 else "compressing" if dg < -1 else "stable")
    ol = out.get("operating_leverage")
    out["read"] = ("operating leverage positive (op income growing faster than revenue)" if (ol and ol > 1)
                   else "de-leveraging (op income lagging revenue)" if (ol is not None and ol < 1) else "n/a")
    return out


def margins_trend(ticker, years=4):
    """CLI wrapper: 4y gross/operating/FCF-margin progression + operating leverage."""
    return {"ticker": ticker, **_margins_from_trends(financial_trends(ticker, years))}


_SUBSCRIPTION_KWS = ("software", "internet", "saas", "application", "cloud",
                     "information technology services", "interactive media", "streaming")


def _is_software_like(q):
    """Auto-detect software/subscription names to surface SaaS unit-economics in the pack. Industry
    keyword match, or a high-gross-margin tech/communication name (the scalable-software signature)."""
    industry = (q.get("industry") or "").lower()
    sector = q.get("sector") or ""
    gm = q.get("gross_margin_pct") or 0
    if any(k in industry for k in _SUBSCRIPTION_KWS):
        return True
    return sector in ("Technology", "Communication Services") and gm >= 60


def _growth_quality_block(ticker, q):
    """Auto-surfaced SaaS/subscription durability metrics: Rule of 40 + S&M magic number
    (unit_economics.py), so the growth agent sees them in the pack without a separate call."""
    from unit_economics import rule40, magic_number
    block = {"detected_as": "software/subscription-like",
             "basis": "auto-surfaced Rule of 40 + S&M magic number (unit_economics.py)"}
    for key, fn in (("rule40", rule40), ("magic_number", magic_number)):
        try:
            block[key] = fn(ticker)
        except Exception as e:
            block[key] = {"error": str(e)}
    return block


def _market_context():
    """Market-wide fear/vol level (VIX) — the sentiment-regime backdrop against which a single
    name's extreme is judged. Fear is high => contrarian longs into panic are better odds."""
    ctx = {}
    try:
        v = yf_history("^VIX", period="6mo")["Close"]
        vix = float(v.iloc[-1]); med = float(v.median())
        ctx["vix"] = rnd(vix, 2)
        ctx["vix_6m_median"] = rnd(med, 2)
        ctx["vix_state"] = ("elevated/fear" if vix > 1.25 * med else "complacent" if vix < 0.8 * med else "normal")
    except Exception as e:
        ctx["vix"] = None; ctx["error"] = str(e)
    return ctx

def build_pack(ticker, outdir):
    os.makedirs(outdir, exist_ok=True)
    px = yf_history(ticker, period="5y")
    px_path = os.path.join(outdir, "prices.csv")
    px.to_csv(px_path)
    q = quote(ticker)
    ft = financial_trends(ticker)
    pack = {
        "as_of": str(pd.Timestamp.now().date()),
        "quote": q,
        "market_context": _market_context(),
        "financial_trends": ft,
        "margins": _margins_from_trends(ft),
        "price_stats": {
            "last": rnd(px["Close"].iloc[-1]),
            "ret_1m_pct": rnd(100 * (px["Close"].iloc[-1] / px["Close"].iloc[-21] - 1)),
            "ret_3m_pct": rnd(100 * (px["Close"].iloc[-1] / px["Close"].iloc[-63] - 1)),
            "ret_1y_pct": rnd(100 * (px["Close"].iloc[-1] / px["Close"].iloc[-252] - 1)) if len(px) > 252 else None,
            "vol_1y_ann_pct": rnd(100 * px["Close"].pct_change().tail(252).std() * (252 ** 0.5)),
            "max_dd_5y_pct": rnd(100 * ((px["Close"] / px["Close"].cummax()) - 1).min()),
            "off_52w_high_pct": rnd(100 * (px["Close"].iloc[-1] / px["Close"].tail(252).max() - 1)),
        },
    }
    if _is_software_like(q):
        pack["growth_quality"] = _growth_quality_block(ticker, q)
    path = os.path.join(outdir, "data_pack.json")
    with open(path, "w") as f:
        json.dump(pack, f, indent=2, default=_ser)
    jprint({"wrote": [px_path, path]})
    return pack

def main():
    cmd, ticker = sys.argv[1], sys.argv[2]
    if cmd == "quote":
        jprint(quote(ticker))
    elif cmd == "trends":
        jprint(financial_trends(ticker))
    elif cmd == "margins":
        jprint(margins_trend(ticker))
    elif cmd == "pack":
        outdir = sys.argv[3] if len(sys.argv) > 3 else f"theses/{ticker}"
        build_pack(ticker, outdir)

if __name__ == "__main__":
    main_guard(main)
