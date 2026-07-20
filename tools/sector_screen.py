"""Sector-relative context + peer auto-selection.
Peers via yfinance's Industry top-companies screen (market-weight ranked, same industry);
relative strength vs the sector SPDR ETF is the classic RS-line read (Levy 1967 relative-
strength ranks; O'Neil RS line) that the sector specialist uses to place a name in its group.

CLI:
  python sector_screen.py peers <TICKER> [--n 5]
  python sector_screen.py rs <TICKER>
"""
import sys
import numpy as np
import pandas as pd
from common import jprint, rnd, safe, yf_history, main_guard

# GICS sector -> SPDR select-sector ETF
SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Energy": "XLE", "Industrials": "XLI", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Basic Materials": "XLB", "Real Estate": "XLRE",
    "Utilities": "XLU", "Communication Services": "XLC",
}

# Curated fallback when the Industry screen API fails: sector -> large-cap representatives
FALLBACK_PEERS = {
    "Technology": ["MSFT", "AAPL", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE", "CSCO", "TXN"],
    "Financial Services": ["JPM", "BAC", "WFC", "GS", "MS", "BLK", "SCHW", "AXP", "C", "SPGI"],
    "Healthcare": ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "AMGN", "PFE", "ISRG"],
    "Energy": ["XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "OXY", "WMB"],
    "Industrials": ["GE", "CAT", "RTX", "HON", "UNP", "BA", "DE", "LMT", "UPS", "ETN"],
    "Consumer Cyclical": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG", "TJX", "GM"],
    "Consumer Defensive": ["WMT", "PG", "COST", "KO", "PEP", "PM", "MDLZ", "CL", "TGT", "KMB"],
    "Basic Materials": ["LIN", "SHW", "FCX", "APD", "ECL", "NEM", "NUE", "DOW", "PPG", "ALB"],
    "Real Estate": ["PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "CCI", "DLR", "VICI"],
    "Utilities": ["NEE", "SO", "DUK", "CEG", "SRE", "AEP", "D", "PCG", "EXC", "XEL"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "TMUS", "VZ", "CMCSA", "T", "CHTR", "EA"],
}


def _info(ticker, errs):
    """Ticker .info is a flaky scrape endpoint — wrap in safe, return {} on failure."""
    import yfinance as yf
    return safe(lambda: yf.Ticker(ticker).info or {}, "yf-info", errs) or {}


def peers(ticker, n=5):
    ticker = ticker.upper()
    errs = []
    info = _info(ticker, errs)
    sector, industry = info.get("sector"), info.get("industry")
    ind_key = info.get("industryKey")

    peer_list, source = None, None
    if ind_key:
        def _top():
            import yfinance as yf
            tc = yf.Industry(ind_key).top_companies  # ranked by market weight (~cap)
            if tc is None or tc.empty:
                raise ValueError("empty top_companies")
            return [s for s in tc.index.tolist() if s.upper() != ticker]
        got = safe(_top, "industry-screen", errs)
        if got:
            peer_list, source = got[:n], "yfinance-industry"

    if peer_list is None:
        pool = FALLBACK_PEERS.get(sector) or []
        peer_list = [p for p in pool if p != ticker][:n]
        source = "fallback-map"
        if not peer_list:
            errs.append(f"no fallback peers for sector {sector!r}")

    jprint({
        "ticker": ticker,
        "sector": sector,
        "industry": industry,
        "peers": peer_list,
        "peers_csv": ",".join(peer_list),  # paste-ready for quant_factors.py PEERS arg
        "source": source,
        "gaps": errs or None,
    })


def _rel_ret(rs, days):
    if len(rs) <= days:
        return None
    return rnd(100 * (rs.iloc[-1] / rs.iloc[-1 - days] - 1), 2)


def rs(ticker):
    ticker = ticker.upper()
    errs = []
    info = _info(ticker, errs)
    sector = info.get("sector")
    etf = SECTOR_ETF.get(sector)
    if etf is None:
        jprint({"ticker": ticker, "sector": sector,
                "error": f"no SPDR ETF mapped for sector {sector!r}", "gaps": errs or None})
        return

    px = yf_history(ticker, period="1y")["Close"]
    bx = yf_history(etf, period="1y")["Close"]
    df = pd.concat([px, bx], axis=1, keys=["t", "b"]).dropna()
    rs_line = df["t"] / df["b"]

    dma50 = rs_line.rolling(50).mean()
    last = float(rs_line.iloc[-1])
    pctile = rnd(100 * float((rs_line <= last).mean()), 1)  # percentile vs own 1y history

    jprint({
        "ticker": ticker,
        "sector": sector,
        "sector_etf": etf,
        "obs_days": int(len(rs_line)),
        "relative_return_pct": {
            "3m": _rel_ret(rs_line, 63),
            "6m": _rel_ret(rs_line, 126),
            "12m": _rel_ret(rs_line, 252) or _rel_ret(rs_line, len(rs_line) - 1),
        },
        "rs_line_percentile_1y": pctile,
        "rs_above_50dma": bool(last > float(dma50.iloc[-1])) if not np.isnan(dma50.iloc[-1]) else None,
        "read": ("leadership" if pctile is not None and pctile >= 80
                 else "improving/neutral" if pctile is not None and pctile >= 40
                 else "laggard"),
        "gaps": errs or None,
    })


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("peers", "rs"):
        raise ValueError("usage: python sector_screen.py peers <TICKER> [--n 5] | rs <TICKER>")
    cmd, ticker = args[0], args[1]
    if cmd == "peers":
        n = int(args[args.index("--n") + 1]) if "--n" in args else 5
        peers(ticker, n)
    else:
        rs(ticker)


if __name__ == "__main__":
    main_guard(main)
