"""Event calendar: next/last earnings dates and ex-dividend dates per ticker.
Earnings dates are the canonical scheduled binary event (Barber et al.'s earnings-announcement
premium; options users care about the pre-print IV ramp) — flag anything within 14 days.

CLI:
  python event_calendar.py <TICKER> [TICKER2 ...]
  python event_calendar.py --book <portfolio_context.json>   # every symbol in the book

Both yfinance sources (.calendar and get_earnings_dates()) are flaky; .calendar is tried
first, get_earnings_dates() fills gaps, and every fetch is wrapped so one bad ticker
degrades into an entry in "errs" rather than aborting the batch.
"""
import datetime as dt
import json
import os
import sys
import pandas as pd
from common import jprint, safe, main_guard


def _to_date(v):
    """Best-effort convert yfinance's assorted date shapes to datetime.date, else None."""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)):  # epoch seconds (info dict style)
        try:
            return dt.datetime.fromtimestamp(float(v), dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        ts = pd.Timestamp(v)
        return None if pd.isna(ts) else ts.date()
    except Exception:
        return None


def _cal_get(cal, keys):
    """Pull a value from .calendar which is a dict (new yfinance) or DataFrame (old)."""
    if cal is None:
        return None
    for k in keys:
        if isinstance(cal, dict) and k in cal:
            v = cal[k]
            if isinstance(v, (list, tuple)):
                v = v[0] if v else None
            return v
        if isinstance(cal, pd.DataFrame) and k in cal.index:
            return cal.loc[k].iloc[0]
    return None


def _book_tickers(path):
    """Symbols from portfolio_context.json (mirrors risk_sizing._parse_book shapes)."""
    with open(path) as f:
        data = json.load(f)

    def walk(d):
        if isinstance(d, dict):
            for key in ("positions", "holdings", "book", "portfolio"):
                if key in d and isinstance(d[key], (list, dict)):
                    return walk(d[key])
            return [k.upper() for k, v in d.items()
                    if isinstance(k, str) and isinstance(v, (int, float))]
        if isinstance(d, list):
            out = []
            for it in d:
                if isinstance(it, str):
                    out.append(it.upper())
                elif isinstance(it, dict):
                    tk = it.get("ticker") or it.get("symbol") or it.get("Symbol")
                    if tk:
                        out.append(str(tk).upper())
            return out
        return []

    return walk(data)


def ticker_events(tk, today, errs):
    import yfinance as yf
    t = yf.Ticker(tk)
    cal = safe(lambda: t.calendar, f"{tk} calendar", errs)

    next_e = _to_date(_cal_get(cal, ["Earnings Date"]))
    ex_div = _to_date(_cal_get(cal, ["Ex-Dividend Date", "Ex-Dividend Date "]))
    last_e = None

    # get_earnings_dates covers past announcements and backs up a missing .calendar next date
    ed = safe(lambda: t.get_earnings_dates(limit=12), f"{tk} get_earnings_dates", errs)
    if ed is not None and not getattr(ed, "empty", True):
        dates = sorted(d for d in (_to_date(x) for x in ed.index) if d is not None)
        past = [d for d in dates if d <= today]
        fut = [d for d in dates if d > today]
        if past:
            last_e = past[-1]
        if next_e is None and fut:
            next_e = fut[0]

    if ex_div is None:
        info = safe(lambda: t.info or {}, f"{tk} info", errs) or {}
        ex_div = _to_date(info.get("exDividendDate"))

    days = (next_e - today).days if next_e else None
    row = {
        "ticker": tk,
        "next_earnings": str(next_e) if next_e else None,
        "days_to_earnings": days,
        "last_earnings": str(last_e) if last_e else None,
        "ex_dividend": str(ex_div) if ex_div else None,
        "binary_window": bool(days is not None and 0 <= days <= 14),
    }
    if next_e is None and last_e is None:
        row["gap"] = "no earnings dates from either yfinance source"
    return row


def main():
    args = sys.argv[1:]
    if not args:
        raise ValueError("usage: python event_calendar.py <TICKER> [...] | --book <portfolio_context.json>")
    if args[0] == "--book":
        if len(args) < 2 or not os.path.exists(args[1]):
            raise ValueError("--book requires an existing portfolio_context.json path")
        tickers = _book_tickers(args[1])
        if not tickers:
            raise ValueError(f"no symbols parsed from {args[1]}")
    else:
        tickers = [a.upper() for a in args]

    today = dt.date.today()
    errs = []
    events = [ticker_events(tk, today, errs) for tk in tickers]
    jprint({"events": events, "errs": errs, "asof": str(today)})


if __name__ == "__main__":
    main_guard(main)
