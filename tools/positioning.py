"""Positioning, crowding & economic-surprise signals for macro timing and reflexivity.

Three literature-backed blocks a discretionary global-macro trader uses to gauge how
crowded a trade is and where in the reflexive loop it sits:

  cot MARKET   — CFTC Commitments of Traders speculator positioning (net long/short as a
                 percentile + z-score of its own history). Managed Money for commodities
                 (disaggregated report), Leveraged Funds for financials (TFF report).
                 Speculator-vs-hedger positioning carries a required risk premium and mean
                 reverts from extremes (Ederington-Lee 2002; De Roon-Nijman-Veld 2000;
                 hedging-pressure theory). Free — CFTC Socrata API; degrades to n/a.
  surprise     — real-activity economic-surprise index (Scotti 2016 style). The official
                 Scotti index needs Bloomberg consensus (paid); this is a transparent FREE
                 PROXY: standardized surprise of each release vs a trailing random-walk-with-
                 drift forecast, equal-weighted. Labelled a proxy, not the official series.
  dispersion   — consensus forecast dispersion (Philadelphia Fed SPF cross-forecaster std).
                 Fragile xlsx feed → degrades to an n/a pointer.
  pack         — {cot: {a few key markets}, surprise, dispersion} for the data pack.

CLI:
  python positioning.py cot crude|gold|copper|spx|ust10|eur|jpy ...
  python positioning.py surprise | dispersion | pack

Conventions: JSON to stdout via common.jprint; every fetch wrapped in common.safe so a
fragile feed degrades to "n/a (manual — …)" rather than aborting (macro_data.py idiom).
"""
import sys
import urllib.parse
import numpy as np
import pandas as pd
from common import fred, jprint, rnd, fetch_csv, safe, zscore_last, main_guard

# CFTC Legacy COT (Futures-Only) Socrata dataset covers commodities AND financials with one
# schema; the speculator is "Non-Commercial" (large specs). Net = noncomm long − short. The
# LIKE filter can match several contracts (e.g. GOLD / MICRO GOLD) — we keep the highest-
# open-interest one so the net series tracks a single primary contract.
COT_RESOURCE = "6dca-aqww"
COT_SPECULATOR = "Non-Commercial (large speculators)"
COT_MARKETS = {
    "crude":  "CRUDE OIL, LIGHT SWEET",
    "natgas": "NATURAL GAS",
    "gold":   "GOLD",
    "silver": "SILVER",
    "copper": "COPPER",
    "corn":   "CORN",
    "wheat":  "WHEAT",
    "spx":    "E-MINI S&P 500",
    "nasdaq": "NASDAQ",
    "ust10":  "10-YEAR U.S. TREASURY",
    "ust2":   "2-YEAR U.S. TREASURY",
    "ust30":  "U.S. TREASURY BONDS",
    "eur":    "EURO FX",
    "jpy":    "JAPANESE YEN",
    "gbp":    "BRITISH POUND",
    "aud":    "AUSTRALIAN DOLLAR",
}

_SPEC_LONG = ["noncomm_positions_long_all", "noncomm_positions_long"]
_SPEC_SHORT = ["noncomm_positions_short_all", "noncomm_positions_short"]
_OI_ALIASES = ["open_interest_all", "open_interest"]
_DATE_ALIASES = ["report_date_as_yyyy_mm_dd", "report_date"]
_NAME_ALIASES = ["contract_market_name", "market_and_exchange_names"]


def _pick(df, aliases):
    for a in aliases:
        if a in df.columns:
            return a
    return None


def _cot_pull(name_filter):
    """Fetch a market's CFTC Legacy COT history and return an ascending Series of
    Non-Commercial NET positioning (long − short) for the highest-OI matching contract,
    plus latest open interest."""
    where = f"contract_market_name like '%{name_filter}%'"
    q = urllib.parse.urlencode({"$where": where, "$limit": "5000",
                                "$order": "report_date_as_yyyy_mm_dd DESC"})
    url = f"https://publicreporting.cftc.gov/resource/{COT_RESOURCE}.csv?{q}"
    df = fetch_csv(url)
    dcol, lcol, scol = _pick(df, _DATE_ALIASES), _pick(df, _SPEC_LONG), _pick(df, _SPEC_SHORT)
    ncol, oicol = _pick(df, _NAME_ALIASES), _pick(df, _OI_ALIASES)
    if not (dcol and lcol and scol):
        raise ValueError(f"expected COT columns not present (have {list(df.columns)[:8]}…)")
    keep = [c for c in [dcol, lcol, scol, ncol, oicol] if c]
    df = df[keep].copy()
    df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
    for c in (lcol, scol, oicol):
        if c:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[dcol, lcol, scol])
    # keep the single highest-open-interest contract if LIKE matched several
    if ncol and df[ncol].nunique() > 1:
        latest = df.sort_values(dcol).groupby(ncol).last()
        key = oicol if oicol else lcol
        df = df[df[ncol] == latest[key].idxmax()]
    df = df.sort_values(dcol).set_index(dcol)
    net = (df[lcol] - df[scol]).dropna()
    oi = float(df[oicol].iloc[-1]) if oicol and pd.notna(df[oicol].iloc[-1]) else None
    return net, oi


def cot(market):
    market = market.lower()
    if market not in COT_MARKETS:
        return {"market": market, "error": f"unknown market; known: {', '.join(COT_MARKETS)}"}
    name_filter = COT_MARKETS[market]
    spec = COT_SPECULATOR
    errs = []
    r = safe(lambda: _cot_pull(name_filter), f"cot_{market}", errs)
    if r is None:
        return {"market": market, "speculator": spec,
                "positioning": "n/a (manual — CFTC COT; wire feed / check contract name)",
                "source": f"https://publicreporting.cftc.gov/resource/{COT_RESOURCE}.csv",
                "errors": errs}
    net, oi = r
    last = float(net.iloc[-1])
    hist = net.tail(520)  # ~10y of weekly obs
    pct = float((hist < last).mean()) if len(hist) > 20 else None
    z = zscore_last(net, lookback=260)
    prev = float(net.iloc[-5]) if len(net) > 5 else None
    extreme = bool(z is not None and abs(z) >= 1.5) or bool(pct is not None and (pct >= 0.9 or pct <= 0.1))
    read = "n/a"
    if pct is not None:
        if pct >= 0.9:
            read = f"{spec} crowded LONG ({pct:.0%}ile) — contrarian fade risk if a bearish catalyst hits"
        elif pct <= 0.1:
            read = f"{spec} crowded SHORT ({pct:.0%}ile) — squeeze-primed"
        else:
            read = f"{spec} net positioning mid-range ({pct:.0%}ile) — no crowding extreme"
    return {
        "market": market, "speculator": spec,
        "net_position": rnd(last, 0),
        "net_pct_open_interest": rnd(100 * last / oi, 1) if oi else None,
        "net_position_pctile": rnd(pct, 2) if pct is not None else None,
        "net_position_z_5y": rnd(z, 2),
        "net_position_4w_ago": rnd(prev, 0) if prev is not None else None,
        "direction_4w": ("building" if prev is not None and last > prev else "reducing") if prev is not None else None,
        "extreme_flag": extreme,
        "as_of": str(net.index[-1].date()),
        "read": read,
        "refs": "Ederington-Lee 2002; De Roon-Nijman-Veld 2000 (hedging pressure).",
        "errors": errs,
    }


def surprise():
    """Real-activity economic-surprise index — a FREE PROXY for Scotti (2016). For each
    monthly release we standardize the latest month-over-month change vs a trailing
    random-walk-with-drift forecast (12m mean) using the historical MoM-change std, then
    equal-weight. Positive = data beating its own recent trend. NOT the official Scotti
    index (which uses Bloomberg consensus surprises) — a transparent proxy, labelled so."""
    errs = []
    basket = {"payems": "PAYEMS", "indpro": "INDPRO", "retail": "RSAFS",
              "real_pce": "PCEC96", "mfg_ip": "IPMAN", "core_capex": "NEWORDER"}
    comps, zs = {}, []
    for key, sid in basket.items():
        s = safe(lambda sid=sid: fred(sid, start="2000-01-01"), sid, errs)
        if s is None or len(s) < 40:
            continue
        d = s.diff().dropna()                      # month-over-month change
        drift = d.iloc[-13:-1].mean()              # trailing-12m expected change (ex latest)
        sd = float(d.iloc[:-1].std())
        if sd == 0:
            continue
        z = float((d.iloc[-1] - drift) / sd)
        comps[key] = rnd(z, 2)
        zs.append(z)
    out = {"proxy_note": "FREE PROXY for Scotti (2016) real-activity surprise index — "
                         "standardized release-vs-trailing-drift surprise, equal-weighted; "
                         "not the official Bloomberg-consensus series.",
           "components_z": comps}
    if zs:
        idx = float(np.mean(zs))
        out["surprise_index_z"] = rnd(idx, 2)
        out["read"] = ("data beating trend (reflation/upside surprise)" if idx > 0.4 else
                       "data missing trend (downside surprise)" if idx < -0.4 else
                       "data broadly in line with trend")
    out["errors"] = errs
    return out


def dispersion():
    """Consensus forecast dispersion (Philadelphia Fed Survey of Professional Forecasters,
    cross-forecaster dispersion). Distributed as fragile xlsx workbooks → degrades to an
    n/a pointer. Low dispersion + a large surprise = the asymmetric-payoff setup: the
    market is tightly positioned and a beat/miss moves it more (Badidi 2023, SSRN)."""
    return {
        "spf_dispersion": "n/a (manual — Philadelphia Fed SPF cross-forecaster dispersion, "
                          "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/"
                          "survey-of-professional-forecasters; xlsx feed not wired)",
        "note": "Low dispersion + large surprise ⇒ crowded consensus, larger price impact "
                "(fade the consensus). Use with positioning.py surprise.",
    }


def pack():
    """Compact positioning pack for the data pack: a few headline markets + surprise + dispersion."""
    errs = []
    key_markets = ["spx", "ust10", "gold", "crude", "eur", "jpy"]
    cot_out = {}
    for m in key_markets:
        c = safe(lambda m=m: cot(m), f"cot_pack_{m}", errs)
        if c is not None:
            cot_out[m] = c
    return {"as_of": str(pd.Timestamp.now().date()),
            "cot": cot_out, "surprise": surprise(), "dispersion": dispersion(),
            "build_errors": errs}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pack"
    if cmd == "cot":
        markets = sys.argv[2:] or ["spx"]
        jprint({m: cot(m) for m in markets} if len(markets) > 1 else cot(markets[0]))
    elif cmd == "surprise":
        jprint(surprise())
    elif cmd == "dispersion":
        jprint(dispersion())
    elif cmd == "pack":
        jprint(pack())
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main_guard(main)
