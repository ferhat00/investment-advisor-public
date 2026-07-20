"""Retail attention & social-media crowding — the meme/euphoria dimension the contrarian lacked.
Attention spikes forecast NEGATIVE returns: retail buying pressure that subsequently reverses.

  google_svi / attention : abnormal search interest (Google SVI) predicts a temporary price run-up
                           that reverses within a year. Da, Engelberg & Gao (2011, JF 66(5)); Barber
                           & Odean (2008, RFS 21(2)) — attention-driven retail buying.
  social_sentiment /      : StockTwits/Twitter bullishness AND disagreement; extreme retail bullishness
  disagreement             + high message volume is a fade tell. Cookson, Lu, Mullins & Niessner,
                           "The Social Signal" (2024, JFE 158); Boehmer, Jones, Zhang & Zhang (2021,
                           JF 76(5)) retail order flow.

PREMIUM/rate-limited: Google SVI (pytrends) and StockTwits/Twitter sentiment require external APIs
(configure common.attention_data / ATTENTION_FEED_URL). Without one, degrades to a FREE abnormal-
VOLUME attention proxy (Barber-Odean: attention manifests as abnormal trading volume) plus 52-week-
range context to tell euphoric-attention (near highs) from panic-attention (near lows).

CLI: python retail_attention.py NVDA
"""
import sys
import numpy as np
from common import jprint, rnd, yf_history, attention_data, main_guard


def _abnormal_volume(ticker):
    try:
        h = yf_history(ticker, period="1y")
    except Exception as e:
        return {"error": str(e)}
    close, vol = h["Close"], h["Volume"]
    avg = vol.rolling(63).mean()
    sd = vol.rolling(63).std()
    volz = float((vol.iloc[-1] - avg.iloc[-1]) / sd.iloc[-1]) if sd.iloc[-1] else None
    vol5_x = float(vol.tail(5).mean() / avg.iloc[-1]) if avg.iloc[-1] else None
    hi, lo = float(close.tail(252).max()), float(close.tail(252).min())
    last = float(close.iloc[-1])
    rng_pos = (last - lo) / (hi - lo) if hi > lo else None      # 0 = at low, 1 = at high
    read = "attention (abnormal volume) unremarkable"
    if vol5_x is not None and vol5_x > 1.8:
        if rng_pos is not None and rng_pos > 0.85:
            read = ("attention spike (abnormal volume) near 52-week HIGHS — euphoric retail attention; "
                    "Da-Engelberg-Gao/Barber-Odean say attention-driven buying reverses — a fade tell")
        elif rng_pos is not None and rng_pos < 0.15:
            read = ("attention spike (abnormal volume) near 52-week LOWS — panic attention/capitulation; "
                    "a contrarian-long tell if the seller is non-fundamental")
        else:
            read = "attention spike (abnormal volume) mid-range — elevated retail interest, direction ambiguous"
    return {"volume_z_63d": rnd(volz, 2), "vol_5d_x_avg": rnd(vol5_x, 2),
            "range_position_52w": rnd(rng_pos, 2), "read": read,
            "note": "free proxy: attention shows up as abnormal volume (Barber-Odean 2008); range position "
                    "distinguishes euphoric-attention (near highs, fade) from panic-attention (near lows)."}


def retail_attention(ticker):
    feed = attention_data(ticker)
    social = {
        "available": False, "google_svi_z": None, "stocktwits_bullish_pct": None,
        "message_volume_z": None, "disagreement": None,
        "note": "Google SVI (pytrends) and StockTwits/Twitter sentiment require external/rate-limited APIs "
                "(configure common.attention_data / ATTENTION_FEED_URL). Missing => using the free abnormal-"
                "volume attention proxy below.",
    }
    reads = []
    if feed:
        social.update({
            "available": True, "google_svi_z": rnd(feed.get("google_svi_z", feed.get("svi_z"))),
            "stocktwits_bullish_pct": rnd(feed.get("stocktwits_bullish_pct")),
            "message_volume_z": rnd(feed.get("message_volume_z")),
            "disagreement": rnd(feed.get("disagreement")),
            "note": f"attention feed: {feed.get('source', 'configured vendor')}",
        })
        if (social["google_svi_z"] or 0) > 1.5:
            reads.append("abnormal Google search interest — attention-driven buying that tends to reverse (Da-Engelberg-Gao)")
        if (social["stocktwits_bullish_pct"] or 0) > 75 and (social["message_volume_z"] or 0) > 1:
            reads.append("extreme social bullishness on high message volume — crowded retail euphoria, a fade tell (Cookson et al)")
    vol = _abnormal_volume(ticker)
    if vol.get("read"):
        reads.append(vol["read"])
    return {"ticker": ticker, "social": social, "volume_attention_proxy": vol,
            "read": "; ".join(reads),
            "note": "attention/social crowding is an ENTRY-timing overlay (days to <1yr); attention spikes forecast "
                    "negative returns. Refs: Da-Engelberg-Gao 2011; Barber-Odean 2008; Cookson-Lu-Mullins-Niessner 2024."}


def main():
    jprint(retail_attention(sys.argv[1]))


if __name__ == "__main__":
    main_guard(main)
