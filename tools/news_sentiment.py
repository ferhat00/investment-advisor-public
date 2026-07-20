"""News/text tone via the Loughran-McDonald finance dictionary — an auditable, deterministic
replacement for a subjective "the tone is negative" read of news_notes.md.

  net_tone_pct      = 100 * (pos - neg) / total_words       (Tetlock-style tone; sign = polarity)
  pct_negative      = 100 * neg / total_words               (LM negative-word proportion)
  pct_positive, pct_uncertainty likewise.

Contrarian framing (Tetlock 2007, JF): high media pessimism forecasts near-term downward price
pressure that subsequently REVERTS — so uniformly extreme-negative, saturated coverage at a
sentiment extreme is a capitulation tell, not a reason to join the sell. Extreme-positive,
euphoric coverage is the mirror fade.

Refs: Loughran & McDonald (2011, JF 66(1)); Tetlock (2007, JF 62(3)); Tetlock, Saar-Tsechansky
& Macskassy (2008, JF 63(3)). An optional, clearly-fenced LLM corroboration layer exists but is
OFF by design and may only corroborate — never override — this deterministic score.

CLI:
  python news_sentiment.py file theses/NVDA-2026-07-03/news_notes.md
  python news_sentiment.py text "shares plunged after the guidance cut and a fresh probe"
"""
import sys
import re
from common import jprint, rnd, main_guard
import lm_dictionary as lm

_WORD = re.compile(r"[a-zA-Z][a-zA-Z'\-]+")
_NEG, _POS, _UNC, _SRC = lm.load()


def score_text(text):
    words = [w.lower() for w in _WORD.findall(text or "")]
    n = len(words)
    if n == 0:
        return {"error": "no scorable words"}
    neg = sum(1 for w in words if w in _NEG)
    pos = sum(1 for w in words if w in _POS)
    unc = sum(1 for w in words if w in _UNC)
    net_tone = 100 * (pos - neg) / n
    pct_neg, pct_pos, pct_unc = 100 * neg / n, 100 * pos / n, 100 * unc / n
    polarity = "negative" if net_tone < -0.3 else "positive" if net_tone > 0.3 else "neutral"
    reads = [f"net tone {net_tone:+.2f}% ({polarity}); {pct_neg:.2f}% negative, {pct_pos:.2f}% positive, {pct_unc:.2f}% uncertainty words"]
    if pct_neg > 2.0 and net_tone < -0.5:
        reads.append("heavily/uniformly negative coverage — Tetlock: near-term downward pressure that tends to REVERT; "
                     "at a positioning extreme this reads as capitulation, a contrarian-long tell (not a reason to sell)")
    elif pct_pos > 2.0 and net_tone > 0.5:
        reads.append("heavily positive/euphoric coverage — the mirror: crowded optimism, a fade tell if positioning is one-sided")
    if pct_unc > 2.5:
        reads.append("elevated hedging/uncertainty language — management or analysts are qualifying heavily")
    return {
        "total_words": n, "negative_words": neg, "positive_words": pos, "uncertainty_words": unc,
        "net_tone_pct": rnd(net_tone, 2), "pct_negative": rnd(pct_neg, 2),
        "pct_positive": rnd(pct_pos, 2), "pct_uncertainty": rnd(pct_unc, 2),
        "polarity": polarity, "read": "; ".join(reads),
        "dictionary_source": _SRC,
        "note": "deterministic LM lexicon score; sign is tone, magnitude needs a corpus baseline to z-score. "
                "Refs: Loughran-McDonald 2011; Tetlock 2007. High pessimism => revert (contrarian).",
        "llm_corroboration": llm_corroboration(),
    }


def llm_corroboration(text=None, enabled=False):
    """Optional non-deterministic LLM sentiment layer — OFF by design. If ever enabled it must
    only CORROBORATE the deterministic LM score (agree/disagree flag), never replace it, so the
    committee's verdict stays reproducible. Refs (context): Lopez-Lira & Tang (2023) LLM return
    forecasting; Ke-Kelly-Xiu SESTM."""
    return {"enabled": False,
            "note": "LLM tone layer disabled — deterministic LM lexicon is the source of truth; "
                    "an LLM read may only corroborate, never override (reproducibility)."}


def _read_file(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def main():
    mode = sys.argv[1]
    if mode == "file":
        txt = _read_file(sys.argv[2])
    elif mode == "text":
        txt = sys.argv[2]
    else:
        txt = _read_file(mode)   # allow a bare path
    jprint(score_text(txt))


if __name__ == "__main__":
    main_guard(main)
