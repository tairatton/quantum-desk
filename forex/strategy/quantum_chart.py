"""Plotly chart for the Pine-only HTF Quantum Adaptive dashboard."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from . import config
SURFACE, INK, INK_2 = "#1a1a19", "#ffffff", "#c3c2b7"
MUTED, GRID, AXIS = "#898781", "#2c2c2a", "#383835"
UP, DOWN = "#0ca30c", "#d03b3b"


def build(df: pd.DataFrame, quantum: dict, timeframe: str, symbol: str) -> go.Figure:
    d = df.reset_index(drop=True).copy()
    d["time"] = pd.to_datetime(d["time"])
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[.82, .18], vertical_spacing=.025)
    fig.add_trace(go.Candlestick(
        x=d["time"], open=d["open"], high=d["high"], low=d["low"], close=d["close"],
        increasing_line_color=UP, decreasing_line_color=DOWN,
        increasing_fillcolor=UP, decreasing_fillcolor=DOWN, name="Price",
    ), row=1, col=1)
    volume = d["real_volume"] if "real_volume" in d and d["real_volume"].sum() > 0 else d["tick_volume"]
    colours = [UP if c >= o else DOWN for o, c in zip(d["open"], d["close"])]
    fig.add_trace(go.Bar(x=d["time"], y=volume, marker_color=colours,
                         opacity=.48, name="Volume", showlegend=False), row=2, col=1)

    qd = quantum["data"].tail(len(d)).reset_index(drop=True)
    for i, row in qd[qd["break_event"] != 0].iterrows():
        colour = UP if row["break_event"] == 1 else DOWN
        fig.add_annotation(x=d["time"].iloc[i], y=row["break_level"],
                           text=row["break_kind"], showarrow=True, arrowhead=1,
                           arrowcolor=colour, font=dict(color=colour, size=9), row=1, col=1)

    step = pd.Timedelta(seconds=config.TIMEFRAME_SECONDS.get(timeframe.upper(), 3600))
    x_end = d["time"].iloc[-1] + step * 18
    day = d["time"].iloc[-1].date()
    session_rows = d[d["time"].dt.date == day]
    x_start = session_rows["time"].iloc[0] if not session_rows.empty else d["time"].iloc[-1]
    levels = [("VWAP", quantum["vwap"], "#ff9800", "solid"),
              ("POC", quantum["poc"], "#f23645", "solid"),
              ("VAH", quantum["vah"], MUTED, "dot"), ("VAL", quantum["val"], MUTED, "dot")]
    for label, price, colour, dash in levels:
        fig.add_shape(type="line", x0=x_start, x1=x_end, y0=price, y1=price,
                      line=dict(color=colour, width=1.3, dash=dash), row=1, col=1)
        fig.add_annotation(x=x_end, y=price, text=f"{label} {price:,.2f}", showarrow=False,
                           xanchor="right", font=dict(color=colour, size=9),
                           bgcolor=SURFACE, row=1, col=1)

    plan = quantum.get("plan")
    if plan:
        action = "BUY" if plan["direction"] == 1 else "SELL"
        plan_levels = [(f"{action} ENTRY", plan["entry"], "#ffb300", "solid", 2),
                       ("SL", plan["stop"], DOWN, "solid", 2)]
        plan_levels += [(f"TP{i+1}", p, UP, "dash", 1.5) for i, p in enumerate(plan["targets"])]
        signal_time = quantum["data"]["time"].iloc[plan["signal_index"]]
        for label, price, colour, dash, width in plan_levels:
            fig.add_shape(type="line", x0=signal_time, x1=x_end, y0=price, y1=price,
                          line=dict(color=colour, width=width, dash=dash), row=1, col=1)
            suffix = ""
            if label.startswith("TP"):
                suffix = f" · {quantum['rates'][label]}%"
            fig.add_annotation(x=x_end, y=price, text=f"{label} {price:,.2f}{suffix}",
                               showarrow=False, xanchor="right", font=dict(color=colour, size=10),
                               bgcolor=SURFACE, row=1, col=1)

    fig.update_layout(
        template="plotly_dark", paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(color=INK_2, family="system-ui"),
        title=dict(text=f"{symbol}  {timeframe} — HTF Quantum Adaptive", font=dict(color=INK, size=15)),
        margin=dict(l=55, r=15, t=48, b=25), height=650,
        dragmode="pan", hovermode="x unified",
        xaxis_rangeslider_visible=False, uirevision=f"{symbol}-{timeframe}-quantum",
        showlegend=False,
    )
    fig.update_xaxes(range=[d["time"].iloc[0], x_end], gridcolor=GRID, linecolor=AXIS)
    fig.update_yaxes(gridcolor=GRID, linecolor=AXIS, title_text="Price (USD)", row=1, col=1)
    fig.update_yaxes(gridcolor=GRID, linecolor=AXIS, title_text="Volume", row=2, col=1)
    return fig
