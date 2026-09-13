"""Local live-test operations and monitoring page."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import live_core


def read(sql, params=(), dates=()):
    with sqlite3.connect(live_core.DEFAULT_DB) as conn:
        live_core.initialize(conn)
        return pd.read_sql(sql, conn, params=params, parse_dates=list(dates))


with sqlite3.connect(live_core.DEFAULT_DB) as conn:
    live_core.initialize(conn)
    initial = live_core.setting(conn, "initial_capital", float)
    cost = live_core.setting(conn, "shadow_cost_bp", float)
    start = live_core.setting(conn, "live_start_date")

st.title("실전 테스트")

with st.expander("테스트 설정", expanded=not bool(start)):
    with st.form("settings"):
        new_start = st.date_input("시작일", value=pd.Timestamp(start).date() if start else pd.Timestamp.now().date())
        new_initial = st.number_input("초기자금", min_value=1.0, value=float(initial), step=100.0)
        new_cost = st.number_input("Shadow 편도비용(bp)", min_value=0.0, value=float(cost), step=1.0)
        if st.form_submit_button("설정 저장"):
            with sqlite3.connect(live_core.DEFAULT_DB) as conn:
                live_core.initialize(conn)
                live_core.set_setting(conn, "live_start_date", new_start.isoformat())
                live_core.set_setting(conn, "initial_capital", new_initial)
                live_core.set_setting(conn, "shadow_cost_bp", new_cost)
                live_core.freeze_signals(conn)
                live_core.rebuild_shadow_and_benchmark(conn)
            st.success("설정을 저장했습니다.")
            st.rerun()

runs = read("SELECT * FROM live_runs ORDER BY run_id DESC LIMIT 30", dates=("started_at", "finished_at"))
signals = read("SELECT * FROM live_signal_snapshots ORDER BY signal_date", dates=("signal_date",))
nav = read("SELECT * FROM live_daily_nav ORDER BY date", dates=("date",))
trades = read("SELECT * FROM live_trades ORDER BY trade_date,trade_id", dates=("trade_date",))

last_run = runs.iloc[0] if not runs.empty else None
last_signal = signals.iloc[-1] if not signals.empty else None
month_end = signals[signals.is_month_end == 1]
last_month_end = month_end.iloc[-1] if not month_end.empty else None

cols = st.columns(4)
cols[0].metric("자동 실행", last_run.status if last_run is not None else "기록 없음")
cols[1].metric("마지막 가격", last_run.latest_price_date if last_run is not None else "-")
cols[2].metric("현재 예상 포지션", last_signal.target_position if last_signal is not None else "-")
cols[3].metric("최근 월말 확정", last_month_end.target_position if last_month_end is not None else "-")

if last_run is not None and last_run.status != "SUCCESS":
    st.error(last_run.message)

if last_signal is not None:
    st.subheader("현재 신호")
    a, b, c, d = st.columns(4)
    a.metric("Growth", "ON" if last_signal.growth_on else "OFF")
    b.metric("Inflation", "ON" if last_signal.inflation_on else "OFF")
    c.metric("T5YIE", f"{last_signal.t5yie:.2f}%", f"2% 대비 {last_signal.t5yie-2:+.2f}%p")
    d.metric("SPY / SMA200", f"{last_signal.spy_close:.2f} / {last_signal.spy_sma200:.2f}")

if not nav.empty:
    st.subheader("Shadow · 실제 · SPY")
    fig = go.Figure()
    for portfolio, group in nav.groupby("portfolio"):
        group = group.sort_values("date")
        fig.add_trace(go.Scatter(x=group.date, y=group.equity / group.equity.iloc[0], name=portfolio))
    fig.update_layout(
        yaxis_title="초기값 대비", hovermode="x unified", height=410, autosize=True,
        margin=dict(l=4, r=4, t=42, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=10)),
    )
    st.plotly_chart(fig, width="stretch", config={"responsive": True, "displayModeBar": False})

st.subheader("실제 거래 입력")
with st.form("trade"):
    c1, c2, c3 = st.columns(3)
    trade_date = c1.date_input("거래일", value=pd.Timestamp.now().date())
    signal_date = c2.date_input("신호일", value=last_month_end.signal_date.date() if last_month_end is not None else trade_date)
    ticker = c3.selectbox("티커", live_core.TRADE_TICKERS)
    c4, c5, c6, c7 = st.columns(4)
    side = c4.selectbox("구분", ["BUY", "SELL"])
    quantity = c5.number_input("수량", min_value=0.000001, value=1.0, format="%.6f")
    price = c6.number_input("평균 체결가", min_value=0.000001, value=1.0, format="%.4f")
    fee = c7.number_input("수수료", min_value=0.0, value=0.0, format="%.4f")
    note = st.text_input("메모")
    if st.form_submit_button("거래 저장"):
        try:
            with sqlite3.connect(live_core.DEFAULT_DB) as conn:
                live_core.initialize(conn)
                live_core.add_trade(conn, trade_date, ticker, side, quantity, price, fee, signal_date, note)
                live_core.rebuild_live_nav(conn)
            st.success("거래와 실제 NAV를 저장했습니다.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

if not trades.empty:
    st.dataframe(trades, width="stretch", hide_index=True)

with st.expander("최근 자동 실행 기록"):
    st.dataframe(runs, width="stretch", hide_index=True)

with st.expander("월말 신호 기록"):
    if not month_end.empty:
        st.dataframe(month_end[["signal_date", "growth_on", "inflation_on", "t5yie", "target_position"]],
                     width="stretch", hide_index=True)
