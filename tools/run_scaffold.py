"""Create the canonical run folder for a committee run (see ORCHESTRATOR.md defaults).

Layout enforced:
  theses/<ID>-<YYYY-MM-DD>/
    reports/        one <agent>.md per seat + redteam-*.md — ALWAYS persisted
    logs/           <tool>.err stderr captures
    charts/         report_builder / chart-pack output
    (data packs *.json + prices.csv live at the run root; THESIS.md, run_log.md,
     SHARED_DIGEST.md are written at the root by the orchestrator)

CLI: python run_scaffold.py NVDA [date]   ->  {"run_dir": "theses/NVDA-2026-07-10", ...}
"""
import os
import sys
from datetime import date

from common import jprint

SUBDIRS = ("reports", "logs", "charts")


def scaffold(run_id, run_date=None):
    run_date = run_date or date.today().isoformat()
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "theses")
    run_dir = os.path.join(root, f"{run_id}-{run_date}")
    created = []
    for sub in ("",) + SUBDIRS:
        p = os.path.join(run_dir, sub) if sub else run_dir
        if not os.path.isdir(p):
            os.makedirs(p)
            created.append(sub or ".")
    rel = os.path.relpath(run_dir, os.getcwd()).replace(os.sep, "/")
    return {"run_dir": rel, "created": created, "layout": list(SUBDIRS),
            "note": "data packs at run root; agent reports -> reports/; stderr -> logs/<tool>.err; charts -> charts/"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        jprint({"error": "usage: python run_scaffold.py <ID> [YYYY-MM-DD]"})
        sys.exit(1)
    jprint(scaffold(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
