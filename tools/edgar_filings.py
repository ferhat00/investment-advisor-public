"""Insider transactions, ownership structure, and SEC-filing red flags.

Insider read follows Lakonishok-Lee (2001): open-market purchases are the informative
signal, and clusters of >=3 distinct insiders buying within 30 days carry the most weight;
officer trades are more informative than director trades. Filing flags follow the forensic
literature: 8-K items 4.01/4.02 (auditor change / non-reliance on prior financials),
NT 10-K / NT 10-Q late filings, and S-1/424B dilution events.

CLI:
  python edgar_filings.py insiders <TICKER>   # 6m/12m net insider buy/sell + cluster detection
  python edgar_filings.py holders <TICKER>    # institutional top holders, concentration, insider %
  python edgar_filings.py flags <TICKER>      # keyless SEC EDGAR filing red flags (US/ADR only)
  python edgar_filings.py all <TICKER>
"""
import json
import os
import sys
import time
import urllib.request
import pandas as pd
from common import jprint, rnd, safe, main_guard, _cached

COVERAGE = "US + ADR EDGAR filers only — LSE/EU local filers not covered"
_EDGAR_UA = "Investment-Advisor research " + os.environ.get("EDGAR_CONTACT_EMAIL", "research@example.com")
_last_edgar_call = [0.0]


def _edgar_get(url, ttl=None):
    """Fetch EDGAR JSON keyless with the declared UA, paced <=10 req/s, TTL-cached."""
    def _fetch():
        wait = 0.12 - (time.time() - _last_edgar_call[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": _EDGAR_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        _last_edgar_call[0] = time.time()
        return json.loads(raw)
    return _cached("edgar", url, _fetch, ttl=ttl)


# --- insiders ---------------------------------------------------------------

def _classify(text, transaction):
    """Open-market buy vs sale from yfinance's Text/Transaction fields.
    Gifts, exercises, conversions and blank rows -> 'other' (excluded from the signal)."""
    s = f"{text or ''} {transaction or ''}".lower()
    if "gift" in s or "exercise" in s or "conversion" in s:
        return "other"
    if "sale" in s or "sold" in s:
        return "sell"
    if "purchase" in s or "buy" in s or "bought" in s:
        return "buy"
    return "other"


def _is_officer(position):
    p = str(position or "").lower()
    if "director" in p and "officer" not in p:
        return False
    return any(k in p for k in ["officer", "chief", "ceo", "cfo", "coo", "president",
                                "counsel", "vp", "vice pres", "treasurer", "secretary"])


def _window_stats(df, months):
    cutoff = pd.Timestamp.today() - pd.DateOffset(months=months)
    w = df[df["date"] >= cutoff]
    buys, sells = w[w["side"] == "buy"], w[w["side"] == "sell"]
    return {
        "buy_count": int(len(buys)),
        "sell_count": int(len(sells)),
        "buy_value": rnd(buys["value"].sum(), 0),
        "sell_value": rnd(sells["value"].sum(), 0),
        "net_value": rnd(buys["value"].sum() - sells["value"].sum(), 0),
        "distinct_buyers": int(buys["insider"].nunique()),
        "distinct_sellers": int(sells["insider"].nunique()),
        "officer_buys": int((buys["officer"]).sum()),
        "director_buys": int((~buys["officer"]).sum()),
        "officer_sells": int((sells["officer"]).sum()),
        "director_sells": int((~sells["officer"]).sum()),
    }


def _buy_clusters(df):
    """>=3 distinct insiders buying within any 30-day window (Lakonishok-Lee cluster)."""
    buys = df[df["side"] == "buy"].sort_values("date")
    clusters = []
    for d in buys["date"].unique():
        d = pd.Timestamp(d)
        w = buys[(buys["date"] >= d) & (buys["date"] <= d + pd.Timedelta(days=30))]
        names = sorted(w["insider"].unique())
        if len(names) >= 3:
            clusters.append({"window_start": d.strftime("%Y-%m-%d"),
                             "distinct_buyers": len(names), "insiders": names,
                             "total_value": rnd(w["value"].sum(), 0)})
    # keep non-overlapping maximal windows only
    dedup, last_end = [], None
    for c in clusters:
        start = pd.Timestamp(c["window_start"])
        if last_end is None or start > last_end:
            dedup.append(c)
            last_end = start + pd.Timedelta(days=30)
    return dedup


def insiders(ticker):
    import yfinance as yf
    errs = []
    it = safe(lambda: yf.Ticker(ticker).insider_transactions, "insider_transactions", errs)
    out = {"ticker": ticker, "coverage": COVERAGE}
    if it is None or getattr(it, "empty", True):
        out["insiders"] = None
        out["note"] = "no insider transaction data via yfinance (common for non-US listings)"
        if errs:
            out["gaps"] = errs
        return out
    df = pd.DataFrame({
        "date": pd.to_datetime(it["Start Date"], errors="coerce"),
        "insider": it["Insider"].astype(str),
        "position": it.get("Position", pd.Series(index=it.index, dtype=object)),
        "value": pd.to_numeric(it["Value"], errors="coerce").fillna(0.0),
        "side": [_classify(t, tr) for t, tr in zip(it.get("Text", ""), it.get("Transaction", ""))],
    }).dropna(subset=["date"])
    df["officer"] = df["position"].map(_is_officer)
    df = df[df["date"] >= pd.Timestamp.today() - pd.DateOffset(months=12)]
    clusters = _buy_clusters(df)
    n6 = _window_stats(df, 6)
    out.update({
        "window_6m": n6,
        "window_12m": _window_stats(df, 12),
        "buy_clusters_12m": clusters,
        "cluster_flag": len(clusters) > 0,
        "excluded_rows_12m": int((df["side"] == "other").sum()),
        "read": ("clustered insider buying (strong bullish per Lakonishok-Lee)" if clusters
                 else "net insider buying" if (n6["net_value"] or 0) > 0
                 else "net insider selling (weakly informative — routine diversification dominates)"
                 if n6["sell_count"] > 0 else "no directional insider activity 6m"),
        "note": "open-market buys/sales only; gifts, exercises, conversions excluded",
    })
    return out


# --- holders ----------------------------------------------------------------

def holders(ticker):
    import yfinance as yf
    errs = []
    t = yf.Ticker(ticker)
    ih = safe(lambda: t.institutional_holders, "institutional_holders", errs)
    mh = safe(lambda: t.major_holders, "major_holders", errs)
    out = {"ticker": ticker, "coverage": COVERAGE}
    if ih is not None and not getattr(ih, "empty", True):
        top = [{"holder": r["Holder"], "pct_held": rnd(100 * r.get("pctHeld"), 2),
                "value": rnd(r.get("Value"), 0),
                "pct_change": rnd(100 * r.get("pctChange"), 1) if r.get("pctChange") is not None else None,
                "date_reported": str(pd.Timestamp(r["Date Reported"]).date()) if r.get("Date Reported") is not None else None}
               for _, r in ih.iterrows()]
        pct = pd.to_numeric(ih["pctHeld"], errors="coerce").dropna()
        out["top_institutional_holders"] = top
        out["top5_concentration_pct"] = rnd(100 * pct.head(5).sum(), 1)
        out["top10_concentration_pct"] = rnd(100 * pct.head(10).sum(), 1)
    else:
        out["top_institutional_holders"] = None
    if mh is not None and not getattr(mh, "empty", True):
        mv = mh["Value"] if "Value" in mh.columns else mh.iloc[:, 0]
        g = lambda k: rnd(100 * float(mv[k]), 2) if k in mv.index else None
        out["insider_pct_held"] = g("insidersPercentHeld")
        out["institutions_pct_held"] = g("institutionsPercentHeld")
        out["institutions_pct_float"] = g("institutionsFloatPercentHeld")
        out["institutions_count"] = int(mv["institutionsCount"]) if "institutionsCount" in mv.index else None
    if errs:
        out["gaps"] = errs
    return out


# --- EDGAR flags ------------------------------------------------------------

def _cik_for(ticker):
    m = _edgar_get("https://www.sec.gov/files/company_tickers.json", ttl=7 * 86400)
    tk = ticker.upper().replace(".", "-")  # EDGAR uses BRK-B style
    for v in m.values():
        if v.get("ticker", "").upper() == tk:
            return int(v["cik_str"])
    return None


def flags(ticker):
    out = {"ticker": ticker, "coverage": COVERAGE}
    cik = _cik_for(ticker)
    if cik is None:
        out.update({"edgar": None, "note": "not a US/ADR EDGAR filer"})
        return out
    sub = _edgar_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    rec = sub.get("filings", {}).get("recent", {})
    f = pd.DataFrame({"form": rec.get("form", []),
                      "date": pd.to_datetime(rec.get("filingDate", []), errors="coerce"),
                      "items": rec.get("items", [""] * len(rec.get("form", []))),
                      "accession": rec.get("accessionNumber", [])})
    today = pd.Timestamp.today()
    m24, m12 = today - pd.DateOffset(months=24), today - pd.DateOffset(months=12)

    def _rows(mask):
        return [{"form": r["form"], "date": str(r["date"].date()), "items": r["items"] or None,
                 "accession": r["accession"]} for _, r in f[mask].iterrows()]

    k8 = f["form"].str.startswith("8-K") & (f["date"] >= m24)
    auditor_change = _rows(k8 & f["items"].astype(str).str.contains("4.01", regex=False))
    non_reliance = _rows(k8 & f["items"].astype(str).str.contains("4.02", regex=False))
    late = _rows(f["form"].isin(["NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A"]) & (f["date"] >= m24))
    dilution = _rows((f["form"].str.startswith("S-1") | f["form"].str.startswith("424B")) & (f["date"] >= m12))
    out.update({
        "edgar": {
            "cik": cik,
            "entity": sub.get("name"),
            "auditor_change_8k_401_24m": auditor_change,
            "non_reliance_8k_402_24m": non_reliance,
            "late_filings_nt_24m": late,
            "dilution_filings_12m": dilution,
            "recent_filings_scanned": int(len(f)),
        },
        "flag_count": len(auditor_change) + len(non_reliance) + len(late) + len(dilution),
        "read": ("CLEAN — no auditor-change/non-reliance/late-filing/dilution flags"
                 if not (auditor_change or non_reliance or late or dilution)
                 else "flags present — auditor change and non-reliance are the highest-severity forensic signals"),
    })
    return out


def main():
    cmd, ticker = sys.argv[1], sys.argv[2]
    fns = {"insiders": insiders, "holders": holders, "flags": flags}
    if cmd == "all":
        errs = []
        jprint({"ticker": ticker, "coverage": COVERAGE,
                **{k: safe(lambda fn=fn: fn(ticker), k, errs) for k, fn in fns.items()},
                **({"gaps": errs} if errs else {})})
    elif cmd in fns:
        jprint(fns[cmd](ticker))
    else:
        raise ValueError(f"unknown subcommand '{cmd}' (use insiders|holders|flags|all)")


if __name__ == "__main__":
    main_guard(main)
