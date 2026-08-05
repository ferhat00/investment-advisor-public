"""Score the committee's own DECISION_LOG calls against subsequent price action.

Parses DECISION_LOG.md entries ('### DEC-nnn' headers + the machine-readable
'- Ticker(s): / - Ref-price: / - Stance: / - Review-by: / - Outcome:' block),
fetches the latest close per single-ticker entry, and classifies each call
directionally. Consumers: CIO review-sweep, trade-historian. Directional price
move is a WEAK proxy for decision quality -- a review prompt, not a verdict
(cf. Brier-style calibration lit: grade the process, not one price path).

CLI:
  python decision_scoring.py [--log ../DECISION_LOG.md] [--asof YYYY-MM-DD]
  python decision_scoring.py entry DEC-014 [--log ../DECISION_LOG.md]
"""
import argparse
import datetime
import os
import re
import numpy as np
from common import jprint, rnd, parse_list, safe, yf_history, main_guard

_HEADER = re.compile(r"^###\s+(DEC-\d+)\s+[—–-]+\s+(\d{4}-\d{2}-\d{2})\s+[—–-]+\s+(.*)$")
_FIELD = re.compile(r"^-\s+(Ticker\(s\)|Ref-price|Stance|Review-by|Outcome):\s*(.*)$")
_NUM = re.compile(r"([0-9][\d,]*(?:\.\d+)?)")
_STANCE_TOK = re.compile(r"[A-Z][A-Z\-]*")

# stance token -> directional expectation ('right' if...)
_LONGISH = {"HOLD", "BUY", "LONG", "ADD", "ACCUMULATE"}       # right if price up
_SHORTISH = {"PASS", "AVOID", "TRIM", "DE-RISK", "EXIT",      # right if down/flat (<+2%)
             "SELL", "SHORT", "REDUCE"}
_INFO = {"WAIT-FOR", "WATCH", "NEUTRAL", "NO-EDGE"}           # informational only

_FLAT_PCT = 2.0  # <+2% counts as flat -> 'right' for the short-ish stances


def _default_log():
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "DECISION_LOG.md"))


def parse_log(path):
    """DECISION_LOG.md -> list of entry dicts (raw machine-readable fields)."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    entries, cur = [], None
    for ln in lines:
        m = _HEADER.match(ln)
        if m:
            cur = {"id": m.group(1), "date": m.group(2), "title": m.group(3).strip()}
            t = re.search(r"\((\w[\w-]*)\)\s*$", cur["title"])
            cur["type"] = t.group(1) if t else None
            entries.append(cur)
            continue
        if cur is None:
            continue
        f_ = _FIELD.match(ln)  # exact 'Review-by' (hyphen); prose 'Review by' won't match
        if f_:
            key = {"Ticker(s)": "tickers", "Ref-price": "ref_price_raw", "Stance": "stance_raw",
                   "Review-by": "review_by", "Outcome": "outcome_raw"}[f_.group(1)]
            cur.setdefault(key, f_.group(2).strip())
    return entries


def parse_ref_price(s):
    """First parseable numeric in a Ref-price string, kept in quote units.
    Handles '$215.62 USD (~$216)', '1399p GBX', '$1,769 USD / €1,548', '161.28 JPY per USD'.
    RR.L-style pence quotes stay in GBX -- yfinance quotes .L tickers in GBX, so they compare directly."""
    if not s:
        return None, None
    m = _NUM.search(s)
    if not m:
        return None, None
    val = float(m.group(1).replace(",", ""))
    tail = s[m.end():m.end() + 1]
    if "GBX" in s or tail.lower() == "p":
        units = "GBX"
    elif "£" in s:
        units = "GBP"
    elif "€" in s and "$" not in s:
        units = "EUR"
    elif "JPY" in s.upper():
        units = "JPY"
    elif "$" in s:
        units = "USD"
    else:
        units = None
    if tail.lower() == "k" and units == "GBP":  # 'NAV £102.3k' (BOOK entries; not price-scored anyway)
        val *= 1000
    return val, units


def stance_token(s):
    m = _STANCE_TOK.match(s or "")
    return m.group(0) if m else None


def score_entry(e, errs):
    """Attach price scoring in place. BOOK/multi-ticker/unparseable degrade with a stated gap."""
    tickers = parse_list(e.get("tickers"))
    e["stance"] = stance_token(e.get("stance_raw"))
    e["outcome"] = (e.get("outcome_raw") or "").split()[0].rstrip(",;") or None
    if not tickers:
        e["scoring"] = "skipped"
        e["gap"] = "no machine-readable Ticker(s) line (process entry)"
        return
    if tickers == ["BOOK"]:
        e["scoring"] = "book-level; see portfolio run"
        return
    if len(tickers) > 1:
        e["scoring"] = "skipped"
        e["gap"] = f"multi-ticker entry ({len(tickers)} tickers); single-ticker scoring only"
        return
    t = tickers[0]
    ref, units = parse_ref_price(e.get("ref_price_raw"))
    if ref is None:
        e["scoring"] = "skipped"
        e["gap"] = f"no parseable numeric in Ref-price: {e.get('ref_price_raw')!r}"
        return
    e["ref_price"] = rnd(ref, 4)
    e["units"] = units
    hist = safe(lambda: yf_history(t, "6mo"), f"{e['id']} {t} price fetch", errs)
    if hist is None or hist.empty:
        e["scoring"] = "skipped"
        e["gap"] = f"no price data for {t}"
        return
    last = float(hist["Close"].iloc[-1])
    if not np.isfinite(last):
        # A NaN last close would silently grade the call "wrong" (NaN > 0 is False) and then
        # blow up the by-stance average, since rnd(NaN) is None. Degrade with a stated gap.
        e["scoring"] = "skipped"
        e["gap"] = f"last close for {t} is not finite (stale/suspended line)"
        return
    e["last_close"] = rnd(last, 4)
    e["last_close_date"] = hist.index[-1].strftime("%Y-%m-%d")
    pct = (last / ref - 1.0) * 100.0
    e["pct_move_since_ref"] = rnd(pct, 2)
    st = e["stance"]
    if st in _LONGISH:
        e["call"] = "right" if pct > 0 else "wrong"
    elif st in _SHORTISH:
        e["call"] = "right" if pct < _FLAT_PCT else "wrong"
    elif st in _INFO:
        e["call"] = "informational"
    else:
        e["call"] = "unclassified"
        e["gap"] = f"unrecognized stance token {st!r}"
    e["scoring"] = "scored"


def aggregate(entries, asof):
    scored = [e for e in entries if e.get("scoring") == "scored"]
    graded = [e for e in scored if e.get("call") in ("right", "wrong")]
    hits = sum(1 for e in graded if e["call"] == "right")
    by_stance = {}
    for e in graded:
        b = by_stance.setdefault(e["stance"], {"n": 0, "hits": 0, "moves": []})
        b["n"] += 1
        b["hits"] += e["call"] == "right"
        b["moves"].append(e["pct_move_since_ref"])
    for st, b in by_stance.items():
        b["hit_rate"] = rnd(b["hits"] / b["n"], 2)
        b["avg_move_pct"] = rnd(sum(b["moves"]) / len(b["moves"]), 2)
        del b["moves"]
    is_open = lambda e: (e.get("outcome") or "").lower().startswith("open")
    past = [{"id": e["id"], "review_by": e["review_by"], "outcome": e.get("outcome")}
            for e in entries if e.get("review_by") and e["review_by"] < asof and is_open(e)]
    return {
        "entries_total": len(entries),
        "listed": sum(1 for e in entries if e.get("tickers")),  # market entries (incl BOOK)
        "book_entries": sum(1 for e in entries if e.get("scoring", "").startswith("book")),
        "scored": len(graded),
        "informational": [e["id"] for e in scored if e["call"] == "informational"],
        "hits": hits,
        "hit_rate_overall": rnd(hits / len(graded), 2) if graded else None,
        "by_stance": by_stance,
        "past_review_by": past,
        "past_review_by_count": len(past),
        "still_open": sum(1 for e in entries if is_open(e)),
    }


def main():
    ap = argparse.ArgumentParser(description="Score DECISION_LOG calls vs subsequent price action")
    ap.add_argument("cmd", nargs="?", choices=["entry"], help="'entry' for a single decision")
    ap.add_argument("dec_id", nargs="?", help="e.g. DEC-014 (with 'entry')")
    ap.add_argument("--log", default=_default_log())
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD; default today")
    a = ap.parse_args()
    asof = a.asof or datetime.date.today().isoformat()
    try:
        asof = datetime.date.fromisoformat(asof).isoformat()
    except ValueError:
        raise ValueError(f"--asof must be a valid YYYY-MM-DD date, got {asof!r}")
    if not os.path.exists(a.log):
        raise FileNotFoundError(f"decision log not found: {a.log}")
    entries = parse_log(a.log)
    errs = []
    caveats = ["directional price move is a weak proxy for decision quality; horizon-mismatch unadjusted",
               "flat threshold +2% favors PASS/TRIM stances; ref prices kept in quote units (GBX for .L)"]

    if a.cmd == "entry":
        if not a.dec_id:
            raise ValueError("usage: decision_scoring.py entry DEC-nnn")
        e = next((x for x in entries if x["id"].upper() == a.dec_id.upper()), None)
        if e is None:
            raise ValueError(f"{a.dec_id} not found in {a.log}")
        score_entry(e, errs)
        e["past_review_by"] = bool(e.get("review_by") and e["review_by"] < asof)
        jprint({"asof": asof, "log": a.log, "entry": e,
                "caveats": caveats, "errors": errs or None})
        return

    market = [e for e in entries if e.get("tickers")]
    for e in market:
        score_entry(e, errs)
    non_market = [{"id": e["id"], "date": e["date"], "type": e.get("type"), "title": e["title"]}
                  for e in entries if not e.get("tickers")]
    jprint({"asof": asof, "log": a.log,
            "entries": market,
            "non_market_entries": non_market,
            "aggregate": aggregate(entries, asof),
            "caveats": caveats,
            "errors": errs or None})


if __name__ == "__main__":
    main_guard(main)
