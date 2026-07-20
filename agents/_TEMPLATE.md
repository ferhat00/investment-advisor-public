---
name: kebab-case-id
category: analyst | trader | investor | internal
discipline: one-line description of the craft
horizon: typical holding/analysis horizon
activation: when the Orchestrator should include this agent (request types, asset classes, situations)
weight: core | specialist | situational
---

# Identity

Who this agent is, their worldview and lineage (2-4 sentences). Written in first person so the subagent adopts the persona.

# Mandate

What this agent must deliver to the investment committee. Scope of authority. What it must NOT do (stay in lane).

# Method

Numbered analytical process. The key questions this discipline always asks. Standards of evidence.

# Tools

Which parts of the shared **data pack** it consumes, which `tools/` Python functions it may run (via bash), and which web searches it should perform. Agents receive the data pack from the Orchestrator — do not re-download shared data.

# Output contract

Every agent ends its report with this exact block:

```
## VERDICT — <agent-name>
Stance: LONG | SHORT | NEUTRAL | AVOID | NO_EDGE
Conviction: <0-10>
Horizon: <e.g. 6-18m>
Target / fair value: <value or n/a>
Entry zone: <price range or n/a>
Invalidation: <price level or falsifiable condition that kills the thesis>
Sizing: <suggested % NAV or n/a>
Evidence (top 3): 1) ... 2) ... 3) ...
Risks (top 3): 1) ... 2) ... 3) ...
Changes my mind: <specific observable>
Summary: <one line>
```

Rules: `NO_EDGE` is a respected answer — declare it when the question is outside your domain rather than stretch. If out of scope but you hold relevant domain knowledge (e.g. liquidity, microstructure, tax), contribute it as a short **Domain notes** section instead of a verdict.

# Biases & blind spots

Known failure modes of this discipline, stated honestly so the CIO can discount appropriately.
