"""Merger-arb spread calculator: gross/annualized spread, market-implied break probability, hedge ratio.
Pure calculator (no free deal feed exists) — the agent supplies deal terms from filings/news; only the
live target/acquirer prices are fetched. Implied break probability uses the standard risk-arb identity
price = p*downside + (1-p)*offer_value (Mitchell & Pulvino 2001 style, zero time value / no discounting).

CLI (cash deal):
  python deal_arb.py --target <TICKER> --offer <cash_per_share> [--close YYYY-MM-DD] [--downside <price>] [--dividends <per_share_until_close>]
CLI (stock or cash+stock deal):
  python deal_arb.py --target <TICKER> --acquirer <TICKER> --ratio <shares> [--cash <per_share>] [--close YYYY-MM-DD] [--downside <price>] [--dividends <per_share>]
"""
import argparse
import datetime as dt
from common import jprint, rnd, yf_history, main_guard


def _last_close(ticker):
    return float(yf_history(ticker, period="5d")["Close"].iloc[-1])


class _Parser(argparse.ArgumentParser):
    """Raise instead of printing usage+exit(2) so main_guard emits one JSON error."""
    def error(self, message):
        raise ValueError(f"usage error: {message}")


def main():
    ap = _Parser(description="Merger-arb spread calculator")
    ap.add_argument("--target", required=True, help="target ticker")
    ap.add_argument("--offer", type=float, help="all-cash offer per share")
    ap.add_argument("--acquirer", help="acquirer ticker (stock / cash+stock deals)")
    ap.add_argument("--ratio", type=float, help="acquirer shares per target share")
    ap.add_argument("--cash", type=float, default=0.0, help="cash component per share (with --ratio)")
    ap.add_argument("--close", help="expected close date YYYY-MM-DD")
    ap.add_argument("--downside", type=float, help="estimated target price on deal break")
    ap.add_argument("--dividends", type=float, default=0.0, help="target dividends per share until close")
    a = ap.parse_args()

    gaps = []
    if a.offer is None and not (a.acquirer and a.ratio):
        raise ValueError("supply --offer (cash deal) or --acquirer + --ratio (stock deal)")

    target_px = _last_close(a.target)

    # Offer value per target share: cash + stock consideration at the acquirer's live price.
    acq_px = None
    if a.acquirer and a.ratio:
        acq_px = _last_close(a.acquirer)
        offer_value = a.cash + a.ratio * acq_px
        deal_type = "cash+stock" if a.cash else "stock"
    else:
        offer_value = a.offer
        deal_type = "cash"

    gross_d = offer_value + a.dividends - target_px          # $ per share if deal closes
    gross_pct = 100.0 * gross_d / target_px

    # Annualized only with a close date (simple 365/days scaling, no compounding).
    days = ann_pct = None
    if a.close:
        days = (dt.date.fromisoformat(a.close) - dt.date.today()).days
        if days > 0:
            ann_pct = gross_pct * 365.0 / days
        else:
            gaps.append("close date is today or past; annualized not meaningful")
    else:
        gaps.append("no --close: annualized spread not computed")

    # Market-implied break probability: price = p*downside + (1-p)*offer_value, zero time value.
    p_break = None
    if a.downside is not None:
        denom = offer_value - a.downside
        if denom > 0:
            p_break = (offer_value - target_px) / denom
            if not 0.0 <= p_break <= 1.0:
                gaps.append("implied probability outside [0,1]: price trades outside the downside-offer band "
                            "(negative spread or overbid speculation); identity still reported raw")
        else:
            gaps.append("downside >= offer value; implied probability undefined")
    else:
        gaps.append("no --downside: implied break probability not computed")

    # Breakeven downside: break price where loss on break equals the spread captured on close,
    # i.e. 1:1 risk/reward (EV=0 at a 50% break probability): D = target - (offer_value - target).
    breakeven_downside = target_px - gross_d

    out = {
        "target": a.target.upper(),
        "deal_type": deal_type,
        "target_price": rnd(target_px),
        "offer_value_per_share": rnd(offer_value),
        "dividends_until_close": rnd(a.dividends),
        "gross_spread_per_share": rnd(gross_d),
        "gross_spread_pct": rnd(gross_pct),
        "close_date": a.close,
        "days_to_close": days,
        "annualized_spread_pct": rnd(ann_pct),
        "downside_on_break": rnd(a.downside) if a.downside is not None else None,
        "implied_break_probability": rnd(p_break, 3),
        "breakeven_downside_price": rnd(breakeven_downside),
        "breakeven_note": "break price at which loss equals spread gain (1:1 payoff; EV=0 at 50% break odds)",
    }
    if acq_px is not None:
        out["acquirer"] = a.acquirer.upper()
        out["acquirer_price"] = rnd(acq_px)
        out["exchange_ratio"] = a.ratio
        out["cash_component"] = rnd(a.cash)
        out["hedge_ratio_short_acquirer_per_target_share"] = a.ratio
        out["hedge_note"] = "short <ratio> acquirer shares per target share to lock the stock consideration"
    if p_break is not None:
        out["probability_note"] = "assumes price = p*downside + (1-p)*offer_value, zero time value / no discounting"
    if gaps:
        out["gaps"] = gaps
    jprint(out)


if __name__ == "__main__":
    main_guard(main)
