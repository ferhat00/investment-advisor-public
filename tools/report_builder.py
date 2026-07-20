"""Build the interactive HTML chart pack for a thesis run.
CLI: python report_builder.py NVDA theses/NVDA-2026-07-03
     python report_builder.py book <portfolio_context.json> theses/PORTFOLIO-<date> [period]
The book mode renders the whole-portfolio pack (cumulative return vs SPY/QQQ, drawdown,
rolling vol, weights vs Ledoit-Wolf risk contributions, correlation heatmap) so
portfolio-action runs stop hand-rolling per-run chart scripts.
"""
import sys, os
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from common import yf_history, jprint, main_guard
from technicals import rsi, macd

DARK = dict(template="plotly_dark", paper_bgcolor="#111418", plot_bgcolor="#111418",
            font=dict(family="Inter, Arial", size=12))

def chart_pack(ticker, outdir, period="2y"):
    os.makedirs(outdir, exist_ok=True)
    df = yf_history(ticker, period=period)
    c = df.Close
    e20, e50, e200 = c.ewm(span=20).mean(), c.ewm(span=50).mean(), c.ewm(span=200).mean()
    r = rsi(c); m, s, h = macd(c)
    dd = 100 * (c / c.cummax() - 1)
    rv = 100 * c.pct_change().rolling(21).std() * np.sqrt(252)

    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        row_heights=[0.45, 0.2, 0.175, 0.175],
                        subplot_titles=(f"{ticker} — price / EMA 20-50-200", "RSI-14 & MACD",
                                        "Drawdown %", "Rolling 1m vol (ann. %)"),
                        specs=[[{"secondary_y": True}], [{"secondary_y": True}], [{}], [{}]])
    fig.add_trace(go.Candlestick(x=df.index, open=df.Open, high=df.High, low=df.Low,
                                 close=df.Close, name="OHLC", showlegend=False), 1, 1)
    for ema, nm, col in [(e20, "EMA20", "#facc15"), (e50, "EMA50", "#38bdf8"), (e200, "EMA200", "#f87171")]:
        fig.add_trace(go.Scatter(x=df.index, y=ema, name=nm, line=dict(width=1.2, color=col)), 1, 1)
    fig.add_trace(go.Bar(x=df.index, y=df.Volume, name="Volume", marker_color="#3f4a5a", opacity=0.5),
                  1, 1, secondary_y=True)
    fig.add_trace(go.Scatter(x=df.index, y=r, name="RSI", line=dict(color="#a78bfa", width=1.2)), 2, 1)
    fig.add_hline(y=70, line_dash="dot", line_color="#666", row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="#666", row=2, col=1)
    fig.add_trace(go.Bar(x=df.index, y=h, name="MACD hist", marker_color="#34d399", opacity=0.6),
                  2, 1, secondary_y=True)
    fig.add_trace(go.Scatter(x=df.index, y=dd, name="Drawdown", fill="tozeroy",
                             line=dict(color="#fb7185", width=1)), 3, 1)
    fig.add_trace(go.Scatter(x=df.index, y=rv, name="Vol 1m", line=dict(color="#fbbf24", width=1.2)), 4, 1)
    fig.update_layout(height=1050, **DARK, xaxis_rangeslider_visible=False,
                      legend=dict(orientation="h", y=1.02),
                      title=f"{ticker} — Technical chart pack ({pd.Timestamp.now().date()})")
    fig.update_yaxes(secondary_y=True, showgrid=False, row=1, col=1)
    path = os.path.join(outdir, f"{ticker}_charts.html")
    fig.write_html(path, include_plotlyjs="cdn")
    jprint({"wrote": [path]})
    return path

def football_field(ticker, outdir, valuations: dict, price: float):
    """valuations: {'DCF base': (low, high), 'Comps EV/EBITDA': (low, high), ...}"""
    fig = go.Figure()
    for i, (name, (lo, hi)) in enumerate(valuations.items()):
        fig.add_trace(go.Bar(y=[name], x=[hi - lo], base=[lo], orientation="h",
                             marker_color="#38bdf8", opacity=0.75, name=name, showlegend=False))
    fig.add_vline(x=price, line_color="#facc15", line_width=2,
                  annotation_text=f"price {price}", annotation_font_color="#facc15")
    fig.update_layout(height=120 + 60 * len(valuations), **DARK,
                      title=f"{ticker} — valuation football field")
    path = os.path.join(outdir, f"{ticker}_football_field.html")
    fig.write_html(path, include_plotlyjs="cdn")
    jprint({"wrote": [path]})
    return path

def book_pack(bookpath, outdir, period="1y"):
    """Whole-book chart pack from a portfolio_context.json (reuses portfolio_opt's
    defensive book parser, yahoo-symbol resolution and Ledoit-Wolf covariance)."""
    from portfolio_opt import _load_book, _price_matrix, _cur_weights, lw_cov
    os.makedirs(outdir, exist_ok=True)
    rows, meta, gaps = _load_book(bookpath)
    rets, ymap, _scale = _price_matrix(rows, period, gaps)
    cols = list(rets.columns)
    w = _cur_weights(rows, cols)
    book = (rets[cols] * w).sum(axis=1)

    cum = (1 + book).cumprod()
    bench = {}
    for b in ("SPY", "QQQ"):
        try:
            r = yf_history(b, period=period).Close.pct_change().reindex(rets.index).fillna(0)
            bench[b] = (1 + r).cumprod()
        except Exception as e:
            gaps.append(f"benchmark {b}: {e}")
    dd = 100 * (cum / cum.cummax() - 1)
    rv = 100 * book.rolling(21).std() * np.sqrt(252)
    S, _delta, _rbar = lw_cov(rets)
    pvar = float(w @ S @ w)
    rc = 100 * w * (S @ w) / pvar if pvar > 0 else w * 0
    corr = rets.corr()

    fig = make_subplots(rows=4, cols=1, vertical_spacing=0.06,
                        row_heights=[0.3, 0.2, 0.25, 0.25],
                        subplot_titles=("Book cumulative return vs SPY / QQQ (equity sleeve, current weights)",
                                        "Book drawdown % and rolling 1m vol (ann. %)",
                                        "Weight % vs Ledoit-Wolf risk-contribution %",
                                        "Return correlation"),
                        specs=[[{}], [{"secondary_y": True}], [{}], [{"type": "heatmap"}]])
    fig.add_trace(go.Scatter(x=cum.index, y=100 * (cum - 1), name="Book",
                             line=dict(color="#facc15", width=1.8)), 1, 1)
    for (b, cb), col in zip(bench.items(), ("#38bdf8", "#a78bfa")):
        fig.add_trace(go.Scatter(x=cb.index, y=100 * (cb - 1), name=b,
                                 line=dict(color=col, width=1.2)), 1, 1)
    fig.add_trace(go.Scatter(x=dd.index, y=dd, name="Drawdown", fill="tozeroy",
                             line=dict(color="#fb7185", width=1)), 2, 1)
    fig.add_trace(go.Scatter(x=rv.index, y=rv, name="Vol 1m", line=dict(color="#fbbf24", width=1.2)),
                  2, 1, secondary_y=True)
    order = np.argsort(-rc)
    fig.add_trace(go.Bar(x=[cols[i] for i in order], y=[100 * w[i] for i in order],
                         name="Weight %", marker_color="#3f4a5a"), 3, 1)
    fig.add_trace(go.Bar(x=[cols[i] for i in order], y=[rc[i] for i in order],
                         name="Risk contrib %", marker_color="#34d399", opacity=0.8), 3, 1)
    fig.add_trace(go.Heatmap(z=corr.values, x=cols, y=cols, zmin=-1, zmax=1,
                             colorscale="RdBu", reversescale=True, showscale=False), 4, 1)
    fig.update_layout(height=1500, **DARK, barmode="group",
                      legend=dict(orientation="h", y=1.02),
                      title=f"Book chart pack — {len(cols)} names, {period} ({pd.Timestamp.now().date()})")
    path = os.path.join(outdir, "book_charts.html")
    fig.write_html(path, include_plotlyjs="cdn")
    jprint({"wrote": [path], "names_in_matrix": len(cols), "gaps": gaps or None})
    return path


def main():
    usage = ("usage: python report_builder.py TICKER OUTDIR (e.g. theses/NVDA-2026-07-03) | "
             "python report_builder.py book BOOK.json OUTDIR [period]")
    if len(sys.argv) >= 2 and sys.argv[1] == "book":
        if len(sys.argv) < 4:
            jprint({"error": usage})
            sys.exit(1)
        book_pack(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "1y")
        return
    if len(sys.argv) < 3:
        jprint({"error": usage})
        sys.exit(1)
    ticker = sys.argv[1]
    outdir = sys.argv[2]
    chart_pack(ticker, outdir)


if __name__ == "__main__":
    main_guard(main)
