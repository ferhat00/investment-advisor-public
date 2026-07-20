"""Realized trading statistics from IBKR trade history (get_account_trades JSON).

Input: file containing {"trades":[...]} — the raw MCP connector response.
Handles multi-fill orders (aggregates by order_id), filters FX-conversion noise.
IBKR supplies realized_pnl on closing fills, so no FIFO reconstruction is needed.
P&L is booked in each listing's native currency; aggregates convert to GBP (ISA base)
at CURRENT spot — an approximation, flagged in output.

CLI:
  python3 tools/trade_stats.py summary <trades.json>
  python3 tools/trade_stats.py symbol  <trades.json> NVDA
  python3 tools/trade_stats.py merge   out.json in1.json in2.json ...   (dedupe by trade_id)
"""
import sys, json
from collections import defaultdict
from datetime import datetime
from common import jprint, rnd, main_guard

def load(path):
    with open(path) as f:
        data = json.load(f)
    return data["trades"] if isinstance(data, dict) else data

def merge(out, paths):
    seen, trades = set(), []
    for p in paths:
        for t in load(p):
            if t["trade_id"] not in seen:
                seen.add(t["trade_id"]); trades.append(t)
    trades.sort(key=lambda t: t["trade_time"])
    with open(out, "w") as f:
        json.dump({"trades": trades}, f)
    jprint({"wrote": [out]})

def _orders(trades):
    """Aggregate fills into orders; keep stock (STK) only, FX noted separately."""
    stk = [t for t in trades if t.get("sec_type") == "STK"]
    by_order = defaultdict(list)
    for t in stk:
        by_order[t["order_id"]].append(t)
    orders = []
    for oid, fills in by_order.items():
        size = sum(f["size"] for f in fills)
        val = sum(f["size"] * f["price"] for f in fills)
        orders.append({
            "order_id": oid, "symbol": fills[0]["symbol"],
            "name": fills[0].get("company_name"), "currency": fills[0].get("currency"),
            "side": fills[0]["side"], "size": size,
            "avg_price": val / size if size else None, "value": rnd(val, 0),
            "commission": rnd(sum(f.get("commission", 0) for f in fills), 2),
            "realized_pnl": rnd(sum(f.get("realized_pnl", 0) for f in fills), 2),
            "time": min(f["trade_time"] for f in fills),
        })
    return sorted(orders, key=lambda o: o["time"])

def _dt(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))

_FX = {}
def to_gbp(amount, ccy):
    """Convert at CURRENT spot (approximation for aggregation; realized P&L is historical)."""
    if amount is None: return None
    ccy = (ccy or "GBP").upper()
    if ccy == "GBP": return amount
    if ccy not in _FX:
        try:
            import yfinance as yf
            h = yf.Ticker(f"{ccy}GBP=X").history(period="5d")
            _FX[ccy] = float(h["Close"].iloc[-1])
        except Exception:
            _FX[ccy] = None
    return amount * _FX[ccy] if _FX[ccy] else None

def summary(path):
    trades = load(path)
    orders = _orders(trades)
    for o in orders:  # currency-aware: convert realized P&L to GBP (ISA base)
        o["pnl_gbp"] = to_gbp(o["realized_pnl"], o["currency"]) if o["realized_pnl"] else 0
    closes = [o for o in orders if o["realized_pnl"]]
    conv_ok = all(o["pnl_gbp"] is not None for o in closes)
    key = "pnl_gbp" if conv_ok else "realized_pnl"
    wins = [o for o in closes if o["realized_pnl"] > 0]
    losses = [o for o in closes if o["realized_pnl"] < 0]
    gross_win = sum(o[key] for o in wins)
    gross_loss = -sum(o[key] for o in losses)
    per_symbol = defaultdict(lambda: {"buys": 0, "sells": 0, "buy_value": 0.0, "ccy": None,
                                      "realized_pnl": 0.0, "pnl_gbp": 0.0, "commissions": 0.0,
                                      "first": None, "last": None, "closes": []})
    for o in orders:
        s = per_symbol[o["symbol"]]
        s["ccy"] = o["currency"]
        s["buys" if o["side"] == "BUY" else "sells"] += 1
        if o["side"] == "BUY": s["buy_value"] += o["value"] or 0
        s["realized_pnl"] += o["realized_pnl"] or 0
        s["pnl_gbp"] += o["pnl_gbp"] or 0
        s["commissions"] += o["commission"] or 0
        s["first"] = min(s["first"] or o["time"], o["time"])
        s["last"] = max(s["last"] or o["time"], o["time"])
        if o["realized_pnl"]: s["closes"].append(o["realized_pnl"])
    sym_table = {}
    for sym, s in per_symbol.items():
        w = [x for x in s["closes"] if x > 0]
        sym_table[sym] = {"buys": s["buys"], "sells": s["sells"], "ccy": s["ccy"],
                          "realized_pnl_native": rnd(s["realized_pnl"], 0),
                          "realized_pnl_gbp": rnd(s["pnl_gbp"], 0),
                          "n_closes": len(s["closes"]),
                          "close_win_rate": rnd(len(w) / len(s["closes"]), 2) if s["closes"] else None,
                          "commissions_native": rnd(s["commissions"], 1),
                          "first": s["first"][:10], "last": s["last"][:10]}
    span_days = ((_dt(orders[-1]["time"]) - _dt(orders[0]["time"])).days or 1) if orders else 0
    fx = [t for t in trades if t.get("sec_type") == "CASH"]
    out = {
        "window": {"from": orders[0]["time"][:10] if orders else None,
                   "to": orders[-1]["time"][:10] if orders else None, "days": span_days},
        "orders_total": len(orders),
        "orders_per_week": rnd(len(orders) / (span_days / 7), 1) if span_days else None,
        "closing_trades": len(closes),
        "pnl_basis": "GBP at current spot (approx)" if conv_ok else "MIXED native currencies — conversion unavailable",
        "win_rate": rnd(len(wins) / len(closes), 2) if closes else None,
        "total_realized_pnl_gbp": rnd(sum(o[key] for o in closes), 0),
        "gross_wins_gbp": rnd(gross_win, 0), "gross_losses_gbp": rnd(-gross_loss, 0),
        "profit_factor": rnd(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win_gbp": rnd(gross_win / len(wins), 0) if wins else None,
        "avg_loss_gbp": rnd(-gross_loss / len(losses), 0) if losses else None,
        "expectancy_per_close_gbp": rnd(sum(o[key] for o in closes) / len(closes), 0) if closes else None,
        "largest_win_gbp": rnd(max((o[key] for o in wins), default=None)),
        "largest_loss_gbp": rnd(min((o[key] for o in losses), default=None)),
        "fx_rates_used": {k: rnd(v, 4) for k, v in _FX.items()},
        "fx_conversion_fills_ignored": len(fx),
        "by_symbol": dict(sorted(sym_table.items(), key=lambda kv: kv[1]["realized_pnl_gbp"] or 0)),
    }
    return out

def symbol_history(path, sym):
    orders = [o for o in _orders(load(path)) if o["symbol"].upper() == sym.upper()]
    if not orders:
        return {"symbol": sym, "history": "no trades in window"}
    closes = [o["realized_pnl"] for o in orders if o["realized_pnl"]]
    flags = []
    for i, o in enumerate(orders):
        if o["side"] == "BUY" and i > 0:
            prev = orders[i - 1]
            if prev["realized_pnl"] and prev["realized_pnl"] < 0:
                gap = (_dt(o["time"]) - _dt(prev["time"])).days
                if gap <= 7:
                    flags.append(f"re-entered {gap}d after a realized loss on {prev['time'][:10]} (revenge-trade risk)")
        if o["side"] == "BUY" and i > 0 and orders[i-1]["side"] == "BUY" and o["avg_price"] and orders[i-1]["avg_price"]:
            if o["avg_price"] < orders[i-1]["avg_price"] * 0.97:
                flags.append(f"averaged down {o['time'][:10]} ({rnd(o['avg_price'])} vs {rnd(orders[i-1]['avg_price'])})")
    buys = [o for o in orders if o["side"] == "BUY"]
    sells = [o for o in orders if o["side"] == "SELL"]
    if buys and sells:
        holds = []
        for s in sells:
            prior = [b for b in buys if b["time"] < s["time"]]
            if prior:
                holds.append((_dt(s["time"]) - _dt(prior[-1]["time"])).days)
        med_hold = sorted(holds)[len(holds) // 2] if holds else None
    else:
        med_hold = None
    return {"symbol": sym.upper(), "currency": orders[0]["currency"],
            "orders": [{k: o[k] for k in ("time", "side", "size", "avg_price", "realized_pnl")} for o in orders],
            "realized_pnl_total_native": rnd(sum(closes), 0) if closes else 0,
            "n_round_trips": len(closes),
            "win_rate": rnd(len([c for c in closes if c > 0]) / len(closes), 2) if closes else None,
            "median_hold_days_approx": med_hold,
            "behavioral_flags": flags or ["none detected"]}

def main():
    cmd = sys.argv[1]
    if cmd == "summary": jprint(summary(sys.argv[2]))
    elif cmd == "symbol": jprint(symbol_history(sys.argv[2], sys.argv[3]))
    elif cmd == "merge": merge(sys.argv[2], sys.argv[3:])

if __name__ == "__main__":
    main_guard(main)
