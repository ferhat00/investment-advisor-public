"""Loughran-McDonald finance sentiment word lists (Loughran & McDonald 2011, JF 66(1)).

Generic (Harvard/psychology) word lists misclassify ~three-quarters of the words they flag as
negative in a financial context ("tax", "cost", "liability", "capital" are not negatives). LM
built finance-specific lists; this module bundles a CURATED, high-frequency SUBSET of the LM
categories so news_sentiment.py runs with zero external dependencies.

For production fidelity, drop the official master dictionary CSV at
`tools/data/LoughranMcDonald_MasterDictionary.csv` (or set env LM_DICT_PATH) and `load()` will
prefer it over the embedded subset. Source: https://sraf.nd.edu/loughranmcdonald-master-dictionary/

Categories are kept disjoint here (LM classifies a word into one category); tone uses only the
Positive/Negative lists, Uncertainty is reported separately (Loughran-McDonald weak-modal proxy).
"""
import os

# --- curated subset (lowercase). NOT the full LM master; flagged as a proxy. ---
NEGATIVE = {
    "loss", "losses", "decline", "declines", "declined", "declining", "weak", "weakness",
    "weaken", "weakened", "weakening", "adverse", "adversely", "litigation", "lawsuit",
    "lawsuits", "sued", "sue", "defendant", "plaintiff", "damages", "penalty", "penalties",
    "fine", "fined", "fines", "investigation", "investigations", "breach", "breached",
    "default", "defaulted", "bankruptcy", "bankrupt", "insolvency", "insolvent",
    "restructuring", "impairment", "impairments", "impaired", "writedown", "writeoff",
    "downgrade", "downgraded", "downgrades", "downturn", "recession", "slowdown",
    "deteriorate", "deteriorated", "deterioration", "shortfall", "miss", "missed", "misses",
    "disappointing", "disappoint", "disappointed", "disappointment", "cut", "cuts", "cutting",
    "layoff", "layoffs", "closure", "closures", "shut", "halt", "halted", "suspend",
    "suspended", "suspension", "delay", "delayed", "delays", "negative", "negatively",
    "deficit", "deficits", "unable", "failure", "failures", "fail", "failed", "failing",
    "warning", "warn", "warned", "warns", "plunge", "plunged", "plummet", "plummeted",
    "slump", "slumped", "drop", "dropped", "fell", "falling", "decrease", "decreased",
    "worse", "worsen", "worsening", "worsened", "trouble", "troubled", "distress",
    "distressed", "crisis", "damage", "damaged", "threat", "threats", "threatened",
    "dispute", "disputes", "disputed", "allegation", "allegations", "alleged", "fraud",
    "fraudulent", "misconduct", "violation", "violations", "violate", "violated", "recall",
    "recalls", "recalled", "defect", "defects", "defective", "obsolete", "oversupply", "glut",
    "headwind", "headwinds", "subpoena", "probe", "delist", "delisted", "dilution", "dilutive",
    "liquidation", "foreclosure", "delinquent", "nonperforming", "underperform",
    "underperformed", "underperforming", "sluggish", "softness", "contraction", "contracting",
    "bearish", "selloff", "capitulation", "panic", "curtail", "curtailed", "abandon",
    "abandoned", "terminate", "terminated", "termination", "wrote", "loss-making", "unprofitable",
}

POSITIVE = {
    "gain", "gains", "gained", "growth", "grow", "growing", "grew", "strong", "strength",
    "strengthen", "strengthened", "strengthening", "improve", "improved", "improving",
    "improvement", "improvements", "profit", "profits", "profitable", "profitability", "beat",
    "beats", "exceeded", "exceed", "exceeds", "outperform", "outperformed", "outperforming",
    "record", "records", "surge", "surged", "upside", "upgrade", "upgraded", "upgrades",
    "robust", "solid", "favorable", "favourable", "positive", "positively", "opportunity",
    "opportunities", "success", "successful", "successfully", "achieve", "achieved",
    "achievement", "expansion", "expanding", "accelerate", "accelerated", "acceleration",
    "momentum", "rebound", "rebounded", "recovery", "recovered", "recovering", "boost",
    "boosted", "benefit", "benefits", "benefited", "efficient", "efficiency", "optimistic",
    "confident", "upbeat", "bullish", "rally", "rallied", "breakthrough", "leading",
    "leadership", "innovative", "innovation", "winner", "winning", "outstanding", "exceptional",
    "tailwind", "tailwinds", "higher", "rose", "rising", "increase", "increased", "better",
    "best", "strongest", "attractive", "compelling",
}

# LM "Uncertainty" list (weak-modal / hedging), reported separately — high uncertainty language
# is itself a positioning tell (management/analyst hedging).
UNCERTAINTY = {
    "may", "might", "could", "possible", "possibly", "probable", "probably", "uncertain",
    "uncertainty", "uncertainties", "risk", "risks", "risky", "contingent", "contingency",
    "contingencies", "approximate", "approximately", "estimate", "estimates", "estimated",
    "assume", "assumption", "assumptions", "believe", "believes", "anticipate", "anticipated",
    "depend", "depends", "dependent", "fluctuate", "fluctuation", "fluctuations", "indefinite",
    "likelihood", "unclear", "unknown", "unpredictable", "pending", "tentative", "preliminary",
    "speculative", "speculation", "variability", "volatile", "volatility", "ambiguous",
    "conditional", "doubt", "doubts", "doubtful", "hinges", "precaution", "revise", "revised",
    "potential", "potentially", "exposure", "unforeseen", "unanticipated",
}


def _load_official(path):
    import csv
    neg, pos, unc = set(), set(), set()
    with open(path, encoding="utf-8", errors="ignore") as f:
        r = csv.DictReader(f)
        for row in r:
            w = (row.get("Word") or "").strip().lower()
            if not w:
                continue
            def nz(col):
                try:
                    return int(float(row.get(col, 0) or 0)) != 0
                except (TypeError, ValueError):
                    return False
            if nz("Negative"):
                neg.add(w)
            if nz("Positive"):
                pos.add(w)
            if nz("Uncertainty"):
                unc.add(w)
    return neg, pos, unc


def load():
    """Return (NEGATIVE, POSITIVE, UNCERTAINTY, source). Prefers the official master dictionary
    CSV if present (env LM_DICT_PATH or tools/data/LoughranMcDonald_MasterDictionary.csv)."""
    path = os.environ.get("LM_DICT_PATH") or os.path.join(
        os.path.dirname(__file__), "data", "LoughranMcDonald_MasterDictionary.csv")
    if os.path.exists(path):
        try:
            neg, pos, unc = _load_official(path)
            if neg and pos:
                return neg, pos, unc, f"official LM master dictionary ({os.path.basename(path)})"
        except Exception:
            pass
    return NEGATIVE, POSITIVE, UNCERTAINTY, "curated LM subset (embedded; drop official CSV for full fidelity)"
