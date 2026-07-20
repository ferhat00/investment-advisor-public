"""Securities-lending / short-crowding metrics — the PRICE of shorting, not just the quantity.

Short-interest-%-of-float tells you HOW MANY shares are short but not at what COST they cleared,
and the cost is where the information is. The equity loan fee is the single best cross-sectional
short-return predictor and adds power beyond short interest because it separates a genuine
conviction short (expensive/special to borrow) from a crowded-but-uninformed one (cheap general
collateral — often mechanical/index/arbitrage shorting).

  fee_bps / special   : annualized borrow cost; special = fee well above general collateral.
                        Engelberg, Evans, Leonard, Reed & Ringgenberg, "The Loan Fee Anomaly"
                        (2025, Management Science 71(7)); D'Avolio (2002, JFE 66); Drechsler &
                        Drechsler (2016).
  utilization         : on-loan / lendable; near 100% => recall/squeeze risk.
  short_selling_risk  : trailing volatility of the loan fee — a HIGH-fee name is not a free short
                        if the fee/recall risk is itself volatile (that is squeeze priming).
                        Engelberg, Reed & Ringgenberg, "Short-Selling Risk" (2018, JF 73(2)).

PREMIUM: borrow fee/utilization require a securities-lending vendor feed (S3 / S&P Global / DataLend
/ Ortex / IBKR). Without one this tool DEGRADES GRACEFULLY to the free short primitives (short % of
float, days-to-cover, short-interest trend) and states plainly that the borrow-cost discriminant is
missing. Configure common.borrow_data (env BORROW_FEED_URL) to light up the full signal.

CLI: python crowding_metrics.py NVDA
"""
import sys
import yfinance as yf
from common import jprint, rnd, borrow_data, main_guard


def _free_short(i):
    ss = i.get("sharesShort")
    adv = (i.get("averageDailyVolume10Day") or i.get("averageDailyVolume3Month") or i.get("averageVolume"))
    dtc = (ss / adv) if (ss and adv) else i.get("shortRatio")
    spm = i.get("sharesShortPriorMonth")
    trend, delta = None, None
    if ss and spm:
        delta = 100 * (ss / spm - 1)
        trend = "rising" if ss > spm * 1.05 else "falling" if ss < spm * 0.95 else "flat"
    return {
        "short_pct_float": rnd((i.get("shortPercentOfFloat") or 0) * 100, 2),
        "shares_short": ss, "avg_daily_volume": adv,
        "days_to_cover": rnd(dtc, 2), "shares_short_prior_month": spm,
        "short_trend": trend, "short_delta_pct": rnd(delta),
    }


def borrow_status(ticker):
    i = yf.Ticker(ticker).info or {}
    free = _free_short(i)
    feed = borrow_data(ticker)
    borrow = {
        "available": False, "fee_bps": None, "special": None,
        "utilization_pct": None, "on_loan": None, "short_selling_risk_fee_vol": None,
        "note": "borrow fee/utilization require a securities-lending feed (S3 / S&P Global / DataLend / "
                "Ortex / IBKR). Set common.borrow_data (env BORROW_FEED_URL) to enable; degraded to free "
                "short primitives — the borrow-COST discriminant (Loan Fee Anomaly) is currently MISSING.",
    }
    if feed:
        fee = feed.get("fee_bps", feed.get("fee"))
        util = feed.get("utilization_pct", feed.get("utilization"))
        borrow.update({
            "available": True, "fee_bps": rnd(fee), "special": bool(fee is not None and fee >= 100),
            "utilization_pct": rnd(util), "on_loan": feed.get("on_loan"),
            "short_selling_risk_fee_vol": rnd(feed.get("fee_vol")) if feed.get("fee_vol") is not None else None,
        })
        borrow["note"] = f"securities-lending feed: {feed.get('source', 'configured vendor')}"

    sf, dtc = free["short_pct_float"], free["days_to_cover"]
    high_si = (sf or 0) > 10
    reads = []
    if borrow["available"] and borrow["fee_bps"] is not None:
        if borrow["fee_bps"] >= 300 and high_si:
            reads.append("expensive/special borrow + high short interest = genuine CONVICTION short "
                         "(Loan Fee Anomaly) — the bearish signal has teeth")
        elif high_si and borrow["fee_bps"] < 100:
            reads.append("high short interest but CHEAP general-collateral borrow = crowded-but-uninformed short "
                         "(mechanical/index/arb) — DOWNGRADE the bearish read")
        if (borrow["utilization_pct"] or 0) > 90:
            reads.append("utilization near 100% — recall / short-squeeze risk elevated (short-selling risk); "
                         "high SI here is squeeze-primed, a contrarian-long asymmetry on good news")
    else:
        reads.append("borrow COST unavailable — cannot separate a conviction short (expensive to borrow) from a "
                     "crowded-but-cheap one; the fee is the discriminant beyond short-interest%. Using SI%/DTC only.")
    if dtc is not None:
        reads.append("high days-to-cover — crowded/trapped short, squeeze fuel on good news" if dtc > 5
                     else "low days-to-cover — shorts exit easily" if dtc < 1 else "moderate days-to-cover")
    if free["short_trend"]:
        reads.append(f"short interest {free['short_trend']} vs prior month ({free['short_delta_pct']:+.0f}%)"
                     if free["short_delta_pct"] is not None else f"short interest {free['short_trend']}")

    return {
        "ticker": ticker, "free_short": free, "borrow": borrow, "read": "; ".join(reads),
        "note": "the borrow fee/utilization (premium) are what add power BEYOND short-interest% — expensive-to-borrow "
                "+ high-SI = informed; cheap + high-SI = crowded-but-uninformed. Refs: Engelberg-Evans-Leonard-Reed-"
                "Ringgenberg 2025; Engelberg-Reed-Ringgenberg 2018; D'Avolio 2002.",
    }


def main():
    jprint(borrow_status(sys.argv[1]))


if __name__ == "__main__":
    main_guard(main)
