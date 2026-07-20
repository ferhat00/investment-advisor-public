"""Non-fundamental flow / fire-sale pressure — the highest-quality contrarian setup is a
measurably FORCED, non-fundamental seller (index deletion, fund outflows, tax-loss selling,
deleveraging) whose price pressure mean-reverts over roughly a year.

  forced_seller / MFFlow  : mutual-fund flow-induced trading and fire-sale price pressure — the
                            marginal seller is non-fundamental. Coval & Stafford (2007, JFE 86(2));
                            Lou (2012, RFS 25(12)); Edmans, Goldstein & Jiang (2012, JF 67(3)).
                            CRITICAL: flow-pressure measures must be scaled by SHARES OUTSTANDING,
                            not volume, or they spuriously load on liquidity — Wardlaw (2020, JF 75(6)).
  dumb_money              : retail fund flows predict low returns (sentiment). Frazzini & Lamont
                            (2008, JFE 88(2)).
  index_deletion          : forced index-fund selling around reconstitution.

PREMIUM: fund flows, holdings, and the index-reconstitution calendar require a vendor feed
(configure common.flow_data / FLOW_FEED_URL). Without one this DEGRADES to a FREE volume/price
capitulation proxy for a non-fundamental seller, and flags what is missing.

CLI: python flow_pressure.py NVDA
"""
import sys
import numpy as np
from common import jprint, rnd, yf_history, flow_data, main_guard


def _capitulation(ticker, window=21):
    """Free forced/panic-seller proxy: a volume CLIMAX on a large down day (Coval-Stafford-style
    price pressure shows as a volume spike) followed by price stabilization = seller exhaustion."""
    try:
        h = yf_history(ticker, period="1y")
    except Exception as e:
        return {"error": str(e)}
    close, vol = h["Close"], h["Volume"]
    ret = close.pct_change()
    avgvol = vol.rolling(60).mean()
    recent = h.tail(window)
    idx = recent["Volume"].idxmax()
    climax_volx = float(vol.loc[idx] / avgvol.loc[idx]) if avgvol.loc[idx] else None
    climax_ret = float(ret.loc[idx]) if idx in ret.index else None
    after = close.loc[idx:]
    days_since = int(len(after) - 1)
    trough_after = float(after.min())
    stabilized = bool(after.iloc[-1] >= trough_after * 1.0 and after.iloc[-1] >= float(close.loc[idx]) * 0.98)
    cur_volz = None
    sd = float(vol.rolling(60).std().iloc[-1] or 0)
    if sd > 0:
        cur_volz = float((vol.iloc[-1] - avgvol.iloc[-1]) / sd)
    capitulation = bool(climax_volx is not None and climax_volx >= 3 and (climax_ret or 0) <= -0.05)
    read = "no clear volume-climax capitulation in the recent window"
    if capitulation and stabilized:
        read = (f"volume-climax capitulation ~{days_since} sessions ago ({climax_volx:.1f}x avg vol on a "
                f"{climax_ret*100:.0f}% day) and price has HELD since — classic forced/panic-seller exhaustion, "
                "a high-quality contrarian-long setup (non-fundamental seller done)")
    elif capitulation and not stabilized:
        read = (f"capitulation-scale volume ({climax_volx:.1f}x) but price still making lows — the forced seller "
                "is not finished; wait for stabilization")
    return {"climax_days_ago": days_since, "climax_volume_x_avg": rnd(climax_volx, 1),
            "climax_day_return_pct": rnd(100 * climax_ret) if climax_ret is not None else None,
            "stabilized_since": stabilized, "current_volume_z": rnd(cur_volz, 2),
            "capitulation_flag": capitulation, "read": read}


def flow_pressure(ticker):
    feed = flow_data(ticker)
    premium = {
        "available": False, "mfflow_pct_shares_out": None, "fund_flow_pressure": None,
        "index_deletion_flag": None, "dumb_money_flow": None,
        "note": "fund-flow / holdings / index-reconstitution data require a vendor feed (configure "
                "common.flow_data / FLOW_FEED_URL). Missing => the DIRECT forced-seller measure "
                "(MFFlow scaled by shares outstanding, Wardlaw 2020) is unavailable; using the free "
                "volume/price capitulation proxy below.",
    }
    if feed:
        premium.update({
            "available": True,
            "mfflow_pct_shares_out": rnd(feed.get("mfflow_pct_shares_out", feed.get("mfflow"))),
            "fund_flow_pressure": feed.get("fund_flow_pressure"),
            "index_deletion_flag": feed.get("index_deletion_flag"),
            "dumb_money_flow": feed.get("dumb_money_flow"),
            "note": f"flow feed: {feed.get('source', 'configured vendor')} (ensure MFFlow scaled by shares outstanding, Wardlaw 2020)",
        })
    cap = _capitulation(ticker)
    reads = [cap.get("read")]
    if premium["available"]:
        if premium.get("index_deletion_flag"):
            reads.append("INDEX DELETION pending/recent — forced index-fund selling is non-fundamental and mean-reverts")
        mf = premium.get("mfflow_pct_shares_out")
        if mf is not None and mf < -1:
            reads.append(f"large negative fund-flow-induced trading ({mf:.1f}% shares out) — non-fundamental selling pressure to fade")
    return {"ticker": ticker, "premium_flow": premium, "capitulation_proxy": cap,
            "read": "; ".join(r for r in reads if r),
            "note": "the marginal seller being FORCED/non-fundamental is the cleanest contrarian edge; premium flow data "
                    "makes it direct, the free proxy infers it from a volume climax. Refs: Coval-Stafford 2007; Lou 2012; Wardlaw 2020."}


def main():
    jprint(flow_pressure(sys.argv[1]))


if __name__ == "__main__":
    main_guard(main)
