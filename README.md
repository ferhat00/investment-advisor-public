# Investment Committee — Multi-Agent Thesis System

A virtual investment committee of 36 specialist agents (analysts, traders, investors, internal) orchestrated by a CIO agent, producing a final investment thesis with explicit dissents.

## How to use

In a Cowork/Claude session with this folder mounted, just say what you want in plain English. Claude reads `ORCHESTRATOR.md`, classifies the request, builds the shared data pack via `tools/`, spawns the activated agents in parallel, runs the mandatory red-team round, and writes the thesis to `theses/<ID>-<date>/`. Examples, grouped by intent:

**Full committee run — single equity**
- `Run a thesis on NVDA` — full large-cap committee (core six + forensic-bear, sell-side, buy-side, value, growth, garp, quality, contrarian, momentum, factor)
- `Is TSMC a buy, hold, or pass at these levels?`
- `Build the bull/base/bear on Broadcom with a probability-weighted call`
- `Thesis on a small-cap name — QURE` — routes to the small/spec set (deep-value-distressed, pe-vc, swing, position, buy-side)
- `What would have to be true for AMD to be a mistake here?`

**Control the depth (output profile)**
- `Quick take on copper` — memo only, fewer agents
- `Thesis on MSFT` — default: memo + interactive HTML chart pack
- `Deep dive on TSMC, deep output` — memo + HTML + tunable Jupyter notebook + PDF

**Control the horizon & sizing**
- `6-month thesis on GOOG` / `Multi-year owner's view on ASML`
- `Swing-trade view on NBIS into earnings (days–weeks)`
- `Size a starter in URNM at 1% NAV risk instead of the default 2%`

**Portfolio & position actions** (pulls live IBKR positions first)
- `Should I add to my URNM position?`
- `Trim NVDA or trim the cluster around it?` — buy-side lead + passive-index + factor + market-maker
- `My AI-infra names are 33% of NAV — how do I get to 20%, and which names?`
- `How correlated is a new AEM buy with what I already hold?`
- `Should I just index this instead?` — forces the passive-index null hypothesis
- `Hedge my >50% long-USD book` — factor-level hedge construction

**FX**
- `Thesis on GBP/USD`
- `USD/JPY — long-yen bias, what's the entry trigger?`
- `Is the yen a hedge for my book right now?` — FX set: global-macro, position, momentum, mean-reversion, algo-systematic, quantamental

**Commodities**
- `Quick take on copper` (HG=F) / `Thesis on gold` (GC=F) / `Brent — carry and positioning` (CL=F)
- `Is the nuclear/uranium theme still worth owning? (CCJ, OKLO)`

**Rates, bonds & credit**
- `Credit thesis on a leveraged telecom name` — credit (incl. shadow letter-grade) + income lead
- `If EBITDA falls 20%, what's the covenant headroom and recovery?`
- `What through-the-cycle shadow rating does this balance sheet earn?`

**Macro themes & regime**
- `Where are we in the growth/inflation quadrant, and what does it favour?`
- `Cross-asset carry and trend — do they agree right now?`
- `Macro case for tactically short the dollar into Fed cuts`

**Pairs, arbitrage & events**
- `Merger-arb read on <announced deal> — spread yield and break risk?` — arbitrageur lead + market-maker + algo + credit
- `Pair trade: long NVDA / short MRVL — real stat-arb or just factor beta?`
- `Index-inclusion play on this small-cap — timing and expected move?`

**Short-horizon trades** (investors sit out)
- `How do I trade this earnings gap tomorrow?` — swing lead + day-trader + momentum + mean-reversion + market-maker
- `Is this oversold bounce a dip-buy or a falling knife?`

**Force-include / exclude a specialist**
- `Include the scalper and market-maker` — adds microstructure/execution voices that are off by default
- `Add the activist — is there a capital-allocation fix here?`
- `Bring in the ESG desk on this energy name`
- `Exclude the momentum trader — I think this setup is crowded`

**Invoke a specific lens or methodology** (power-user)
- `Value X as a Graham net-net — what's the NCAV floor?` — `valuation.py netnet`
- `Give me the Greenwald EPV floor vs the DCF upside` — `valuation.py epv`
- `Reverse-solve the price — what growth is the market pricing in?` — `valuation.py reverse`
- `Run the earnings-quality gate before we trust the multiple` — `forensic.py all` (Beneish / Piotroski / Sloan)
- `Check my 25% growth assumption against base rates for this size cohort` — `base_rates.py`
- `Factor-attribution: how much is alpha vs factor beta, and does it clear t>3?` — `factor_attribution.py`
- `Empirical ROIC-fade half-life vs the "wide moat" label` — `quality_tools.py`
- `Tail-risk battery: GPD 99.5% ES, Taleb fragility, cheapest convex hedge` — `risk_sizing.py evt` + `tail_convexity.py`
- `Is this name jump-prone — hedge with options instead of a stop?` — `jump_analysis.py`
- `Does my book share one fat common tail? Run the PCA / pooled Hill` — `systemic_tail.py`
- `Option skew and variance-risk-premium read on NVDA` — `options_analysis.py bkm/vrp`
- `COT positioning on copper — are specs crowded?` — `positioning.py`
- `Rule-of-40 and LTV/CAC on this SaaS name` — `unit_economics.py`

**Decision log, themes & track record**
- `Log this idea: long yen hedges the book's long-USD tilt`
- `Is the gold-miners theme still active or should we exit?`
- `How does this NBIS entry fit my actual fill history?` — the internal trade-historian's track-record adjustment
- `Review the log` / `Score the committee's calls` — review-sweep: `decision_scoring.py` scores every open DEC entry vs realized prices, closes/extends the lapsed ones
- `Am I over my cluster caps?` — CRO limit check (`portfolio_opt.py`, `book_scenario.py`); a documented breach binds the next hard call
- `Where should this dividend payer sit — ISA or SIPP?` — wrapper-tax after-tax placement adjustment

**Bear side & sector playbooks**
- `What's the short thesis on X?` / `Run the forensic screen before we buy` — forensic-bear originates the case (Beneish/accruals, insider clusters via `edgar_filings.py`, catalyst map), expressed wrapper-legal (EXIT/AVOID/BLOCK-BUY)
- `Thesis on a bank / pre-revenue biotech / REIT` — sector-specialist swaps in the right playbook (NIM & CET1, cash runway & trial catalysts, FFO & NAV)

## Structure

```
ORCHESTRATOR.md          CIO agent: activation matrix, debate protocol, synthesis rules
agents/
  _TEMPLATE.md           Contract every agent follows (incl. VERDICT block)
  analysts/  (11)        sell-side, buy-side, credit (incl. shadow rating), macro-strategist,
                         quant-researcher (statistical referee), forensic-bear (short-thesis
                         originator & default red-team bear), sector-specialist (rotating
                         financials/biotech/semis/REIT-EM playbooks), esg, fundamental,
                         technical, quantamental
  traders/   (9)         scalper, day, swing, position, momentum, mean-reversion,
                         arbitrageur, market-maker, algo-systematic
  investors/ (13)        value, growth, garp, quality, deep-value-distressed, contrarian,
                         income, global-macro, activist, passive-index, factor, pe-vc, tail-risk
  internal/  (3)         trade-historian — reviews your real IBKR ISA fill history
                         (Track-record adjustment); cro — book limit enforcement, breaches
                         bind the hard call; wrapper-tax — UK ISA/SIPP/GIA after-tax overlay
tools/                   Python library (yfinance, FRED, technicals, DCF, credit, factors,
                         options, risk/sizing, trade stats, report builder)
theses/                  Output folder, one subfolder per run
DECISION_LOG.md          Running log of decisions, ideas, themes, incl. which agents/tools
                         produced each call (auto-appended each run; say "log this idea"
                         to add ad-hoc entries)
```

## Setup

- Python 3.10+; install deps with `pip install -r requirements.txt` (numpy/pandas/yfinance/plotly — statistics are numpy-only by design, no scipy).
- Launcher: `python3 tools/<tool>.py …` in POSIX/container sessions, `python tools\<tool>.py …` on Windows. Docs and agent files write `python3`; substitute on Windows.
- Network: yfinance and keyless FRED CSV (no API keys needed). Optional premium feeds are wired via env vars — see `.env.example`; everything degrades gracefully when unset.
- IBKR data comes via the IBKR MCP connector in the Claude session (not a Python dependency); without it, runs proceed and note the missing portfolio context.
- Smoke tests: `tests/run_all.ps1` (Windows) or `tests/run_all.sh`.

## Public mirror

This repo is private (it accumulates real trade history, NAV, and position data). A sanitized public copy is published for reference/portfolio purposes via `scripts/sync_public.py`, which stages every tracked file except `theses/` (real thesis outputs — `theses/NVDA-smoketest/`, a synthetic fixture, is kept so the test suite still runs) and `DECISION_LOG.md` (running log of real decisions/trades/NAV), then pushes it as a single fresh commit:

```
python3 scripts/sync_public.py --remote <public-repo-git-url> --push
```

Omit `--push` for a dry run — it reports what would be copied/excluded and leaves the staged tree on disk. The script also refuses to publish (aborts) if it finds a hardcoded name or email in the staged files, as a regression safety net.

## Data sources

- **yfinance** — prices, fundamentals, financial statements, options chains
- **FRED** (keyless CSV) — yields, curve, CPI, credit spreads, DXY, unemployment
- **IBKR connector** — live positions/account (orchestrator pulls; agents receive in data pack)
- **Web research** — news, filings, transcripts (activated agents run targeted searches)

## Output profiles

| Profile | Deliverables |
|---|---|
| quick | Markdown memo only |
| standard (default) | Memo + interactive HTML chart pack |
| deep | Memo + HTML + Jupyter notebook (tunable assumptions) + PDF |

Not investment advice; a decision-support tool.
