"""Interactive equity and drawdown chart for Quantum backtest plans."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from . import config

TARGETS = (("TP1", 1.0, "#3987e5"),
           ("TP2", 1.5, "#26a69a"),
           ("TP3", 2.0, "#00c853"))
RISK_PERCENT_PER_TRADE = 1.0


def _curve(df: pd.DataFrame, plans: list[dict], target_index: int,
           reward_r: float, point: float) -> pd.DataFrame:
    rows = []
    equity_value = peak_value = 100.0
    for number, plan in enumerate(plans, 1):
        result = plan["resolved"][target_index]
        gross_r = reward_r if result == "tp" else -1.0 if result == "sl" else 0.0
        fill_index = int(plan["entry_fill_index"])
        spread_points = float(df.iloc[fill_index].get("spread", 0) or 0)
        spread_r = spread_points * point / float(plan["risk"]) if plan["risk"] else 0.0
        net_r = gross_r - spread_r
        trade_return_pct = net_r * RISK_PERCENT_PER_TRADE
        equity_value *= 1.0 + trade_return_pct / 100.0
        peak_value = max(peak_value, equity_value)
        rows.append({
            "trade": number,
            "time": pd.Timestamp(df.iloc[fill_index]["time"]),
            "side": "BUY" if plan["direction"] == 1 else "SELL",
            "result": result.upper() if result else plan["status"],
            "net_r": net_r,
            "trade_return_pct": trade_return_pct,
            "equity_pct": equity_value - 100.0,
            "drawdown_pct": (equity_value / peak_value - 1.0) * 100.0,
        })
    return pd.DataFrame(rows)


def build(df: pd.DataFrame, quantum: dict, symbol: str, timeframe: str) -> go.Figure:
    """Build compounded equity percentages assuming 1R risks 1% of equity."""
    d = df.reset_index(drop=True).copy()
    d["time"] = pd.to_datetime(d["time"])
    plans = [p for p in quantum["plans"] if p["entry_fill_index"] is not None]
    if not plans:
        raise ValueError("No filled plans available for an equity curve")

    point = 10 ** -int(config.symbol_cfg(symbol)["decimals"])
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[.72, .28], vertical_spacing=.06,
                        subplot_titles=("Spread-adjusted equity", "Drawdown"))

    summaries = []
    for target_index, (label, reward_r, colour) in enumerate(TARGETS):
        curve = _curve(d, plans, target_index, reward_r, point)
        custom = curve[["side", "result", "net_r", "trade", "trade_return_pct"]].to_numpy()
        hover = ("%{x|%Y-%m-%d %H:%M}<br>" + label + " equity %{y:+.2f}%"
                 + "<br>%{customdata[0]} · %{customdata[1]}"
                 + "<br>Trade %{customdata[3]} · %{customdata[2]:+.3f}R"
                 + " · %{customdata[4]:+.3f}%<extra></extra>")
        fig.add_trace(go.Scatter(
            x=curve["time"], y=curve["equity_pct"], mode="lines",
            line=dict(color=colour, width=2), name=f"{label} ({reward_r:g}R)",
            customdata=custom, hovertemplate=hover,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=curve["time"], y=curve["drawdown_pct"], mode="lines",
            line=dict(color=colour, width=1.4), fill="tozeroy", opacity=.45,
            name=f"{label} drawdown", legendgroup=label, showlegend=False,
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>Drawdown %{y:.2f}%<extra>" + label + "</extra>",
        ), row=2, col=1)
        summaries.append(f"{label} {curve['equity_pct'].iloc[-1]:+.2f}%")

    fig.add_hline(y=0, line_color="#898781", line_width=1, row=1, col=1)
    fig.add_hline(y=0, line_color="#898781", line_width=1, row=2, col=1)
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#1a1a19", plot_bgcolor="#1a1a19",
        title=(f"{symbol} {timeframe} — HTF Quantum Equity | " + " · ".join(summaries)),
        height=760, margin=dict(l=65, r=25, t=75, b=40), hovermode="x unified",
        dragmode="pan", legend=dict(orientation="h", y=1.03, x=0),
        uirevision=f"{symbol}-{timeframe}-equity",
    )
    fig.update_xaxes(gridcolor="#2c2c2a", linecolor="#383835")
    fig.update_yaxes(gridcolor="#2c2c2a", linecolor="#383835", ticksuffix="%")
    fig.update_yaxes(title_text="Equity return (%)", row=1, col=1)
    fig.update_yaxes(title_text="Drawdown (%)", row=2, col=1)
    fig.add_annotation(
        xref="paper", yref="paper", x=1, y=-.09, showarrow=False, xanchor="right",
        text="Assumption: 1R = 1% equity risk, compounded · spread deducted · commission/slippage excluded",
        font=dict(size=10, color="#898781"),
    )
    return fig
