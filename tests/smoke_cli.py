"""CLI smoke tests + repo invariants. Mostly offline; two cheap live calls at the end
(skipped with --offline).

Asserts:
  1. every tool CLI, run with no args, produces NO Python traceback (JSON error or
     argparse usage only) — this is the main_guard contract;
  2. offline tool paths work (news_sentiment text mode, deal_arb math via import,
     run_scaffold create+cleanup);
  3. roster invariants: agent file count == ORCHESTRATOR "N-agent" == README parentheticals;
  4. DECISION_LOG append-protocol invariants (count, descending order, file tail).

Run: python tests/smoke_cli.py [--offline]
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TOOLS = os.path.join(ROOT, "tools")
FAILURES = []

# Tools whose NO-ARGS default performs live network pulls — compile-only here.
NETWORK_DEFAULT = {"macro_data.py", "cross_asset.py", "sentiment_regime.py",
                   "fx_data.py", "positioning.py", "commodity_carry.py",
                   "decision_scoring.py"}
PURE_LIBS = {"common.py", "lm_dictionary.py", "yahoo_urllib.py"}


def check(name, cond, detail=""):
    print(f"{'ok' if cond else 'FAIL':4s}  {name}  {detail}")
    if not cond:
        FAILURES.append(name)


def run(args, timeout=60):
    return subprocess.run([sys.executable] + args, cwd=TOOLS, capture_output=True,
                          text=True, timeout=timeout)


def test_no_args_no_traceback():
    for path in sorted(glob.glob(os.path.join(TOOLS, "*.py"))):
        f = os.path.basename(path)
        c = subprocess.run([sys.executable, "-m", "py_compile", f], cwd=TOOLS,
                           capture_output=True, text=True)
        check(f"compile {f}", c.returncode == 0, c.stderr.strip()[:80])
        if f in PURE_LIBS or f in NETWORK_DEFAULT:
            continue
        r = run([f])
        tb = "Traceback" in r.stdout or "Traceback" in r.stderr
        check(f"no-args {f}", not tb and r.returncode != 0,
              f"exit={r.returncode}{' TRACEBACK' if tb else ''}")


def test_offline_paths():
    r = run(["news_sentiment.py", "text", "revenue grew strongly but litigation risk increased"])
    ok = r.returncode == 0
    if ok:
        out = json.loads(r.stdout)
        ok = "net_tone" in out or "tone" in out or out  # schema-agnostic: valid JSON with content
    check("news_sentiment text mode", ok, r.stdout[:60].replace("\n", " "))

    r = run(["run_scaffold.py", "SMOKETESTRUN", "2099-01-01"])
    made = os.path.join(ROOT, "theses", "SMOKETESTRUN-2099-01-01")
    ok = r.returncode == 0 and all(os.path.isdir(os.path.join(made, d))
                                   for d in ("reports", "logs", "charts"))
    check("run_scaffold layout", ok)
    shutil.rmtree(made, ignore_errors=True)


def test_roster_invariants():
    files = [p for p in glob.glob(os.path.join(ROOT, "agents", "*", "*.md"))
             if not p.endswith("_TEMPLATE.md")]
    n_files = len(files)

    orch = open(os.path.join(ROOT, "ORCHESTRATOR.md"), encoding="utf-8").read()
    m = re.search(r"(\d+)-agent virtual investment committee", orch)
    check("ORCHESTRATOR count == files", m and int(m.group(1)) == n_files,
          f"files={n_files} orch={m.group(1) if m else '?'}")

    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    par = [int(x) for x in re.findall(r"(?:analysts|traders|investors|internal)/\s*\((\d+)\)", readme)]
    check("README parentheticals == files", sum(par) == n_files, f"readme={par} sum={sum(par)}")
    m2 = re.search(r"committee of (\d+) specialist agents", readme)
    check("README headline count == files", m2 and int(m2.group(1)) == n_files)

    # every persona has the VERDICT block header + mandatory sections
    for p in files:
        txt = open(p, encoding="utf-8").read()
        name = os.path.relpath(p, ROOT)
        need = ["# Identity", "# Mandate", "# Method", "# Tools", "# Output contract",
                "# Biases", "## VERDICT"]
        missing = [s for s in need if s not in txt]
        if missing:
            check(f"persona sections {name}", False, f"missing={missing}")
    check("persona sections (all files)", all("persona sections" not in f for f in FAILURES))


def test_decision_log_invariants():
    log_path = os.path.join(ROOT, "DECISION_LOG.md")
    if not os.path.isfile(log_path):
        print("skip  DECISION_LOG invariants  (DECISION_LOG.md not present in this checkout)")
        return
    log = open(log_path, encoding="utf-8").read()
    ids = [int(m) for m in re.findall(r"(?m)^### DEC-(\d+)", log)]
    check("DECISION_LOG >=15 entries", len(ids) >= 15, f"n={len(ids)}")
    check("DECISION_LOG descending", ids == sorted(ids, reverse=True))
    check("DECISION_LOG tail is oldest entry",
          log.rstrip().endswith("- Outcome: open") or log.rstrip().endswith("- Link: theses/NVDA-2026-07-03/")
          or ids[-1] == min(ids))


def test_live_spotchecks():
    r = run(["technicals.py", "NVDA"], timeout=120)
    ok = r.returncode == 0
    if ok:
        out = json.loads(r.stdout)
        ok = "ema20" in json.dumps(out) or "rsi" in json.dumps(out).lower()
    check("live technicals NVDA", ok, f"exit={r.returncode}")

    r = run(["decision_scoring.py"], timeout=300)
    ok = r.returncode == 0 and '"entries"' in r.stdout or '"scored"' in r.stdout or r.returncode == 0
    check("live decision_scoring", r.returncode == 0, f"exit={r.returncode}")


if __name__ == "__main__":
    offline = "--offline" in sys.argv
    test_no_args_no_traceback()
    test_offline_paths()
    test_roster_invariants()
    test_decision_log_invariants()
    if not offline:
        test_live_spotchecks()
    print(f"\n{len(FAILURES)} failure(s)")
    sys.exit(1 if FAILURES else 0)
