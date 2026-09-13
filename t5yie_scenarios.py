"""Compare tradable Inflation Compass variants without modifying the results DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as base


ROOT = Path(__file__).parent
OUT_DIR = ROOT / "results"
TRADING_COST_BPS = (0, 5, 10, 30)

# t5yie data started in 2014
# backtesting시 2014년 부터 백테스팅
PERIODS = {
    "full": ("2003-01-01", "2099-12-31"),
    "2003-2012": ("2003-01-01", "2012-12-31"),
    "2013-2019": ("2013-01-01", "2019-12-31"),
    "2020-present": ("2020-01-01", "2099-12-31"),
}


def load_raw_data():
    conn = sqlite3.connect(base.DB_PATH)
    prices_long = pd.read_sql("SELECT date, ticker, close FROM prices", conn, parse_dates=["date"])
    fred = pd.read_sql(
        "SELECT date, value FROM fred_series WHERE series_id='T5YIE' ORDER BY date",
        conn,
        parse_dates=["date"],
    ).set_index("date")["value"]
    conn.close()
    prices = prices_long.pivot(index="date", columns="ticker", values="close").sort_index()
    return prices, fred.sort_index()


def prepare_t5yie(raw: pd.Series, trading_index: pd.DatetimeIndex, mean_window: int, lag: int):
    """Smooth genuine FRED observations, align to trading days, then apply trading-day lag."""
    transformed = raw.rolling(mean_window, min_periods=mean_window).mean() if mean_window > 1 else raw.copy()
    return transformed.reindex(trading_index).ffill().shift(lag)


def signal_frame(prices: pd.DataFrame, t5yie: pd.Series):
    returns = prices.pct_change(fill_method=None)
    spy_sma = prices["SPY"].rolling(200).mean()
    growth = prices["SPY"] > spy_sma

    pos_ret = sum(returns[t] * w for t, w in base.POS_BASKET.items())
    neg_ret = sum(returns[t] * w for t, w in base.NEG_BASKET.items())
    valid_basket = pos_ret.notna() & neg_ret.notna()
    pos_cum = (1 + pos_ret.where(valid_basket, 0)).cumprod().where(valid_basket)
    neg_cum = (1 + neg_ret.where(valid_basket, 0)).cumprod().where(valid_basket)
    indicator = pos_cum / neg_cum
    asset_momentum = base.rolling_slope(indicator, 60) > 0

    prior = t5yie.shift(60)
    inflation = (t5yie > 2.0) & ((t5yie > prior) | asset_momentum)
    valid = spy_sma.notna() & base.rolling_slope(indicator, 60).notna() & t5yie.notna() & prior.notna()
    return pd.DataFrame({"growth_on": growth, "inflation_on": inflation})[valid], returns


def month_signals(signals: pd.DataFrame):
    return signals.groupby([signals.index.year, signals.index.month]).tail(1)


def simulate_tradable(signals: pd.DataFrame, returns: pd.DataFrame):
    """Signal at D close, trade at next trading-day close, earn the new sleeve from E+1."""
    monthly = month_signals(signals)
    idx = returns.index
    events = []
    for decision, row in monthly.iterrows():
        loc = idx.searchsorted(decision, side="right")
        if loc >= len(idx):
            continue
        execution = idx[loc]
        regime = (bool(row["growth_on"]), bool(row["inflation_on"]))
        events.append((decision, execution, regime, base.REGIME_POSITIONS[regime]))

    if not events:
        raise ValueError("No executable monthly signals")

    daily = pd.Series(0.0, index=idx, name="strategy_ret")
    weights = pd.DataFrame(0.0, index=idx, columns=["XLE", "XLK", "XLU", "XLP", "IEF"])
    for i, (_, execution, _, allocation) in enumerate(events):
        next_execution = events[i + 1][1] if i + 1 < len(events) else idx[-1]
        mask = (idx > execution) & (idx <= next_execution)
        daily.loc[mask] = sum(returns.loc[mask, t] * w for t, w in allocation.items())
        for ticker, weight in allocation.items():
            weights.loc[mask, ticker] = weight

    start = events[0][1]
    daily = daily.loc[start:]
    weights = weights.loc[start:]

    turnovers = []
    previous = {}
    for _, execution, _, allocation in events:
        tickers = set(previous) | set(allocation)
        turnover = sum(abs(allocation.get(t, 0) - previous.get(t, 0)) for t in tickers)
        turnovers.append((execution, turnover))
        previous = allocation
    return daily, weights, pd.DataFrame(turnovers, columns=["execution_date", "turnover"]), events


def apply_cost(ret: pd.Series, turnovers: pd.DataFrame, cost_bp: float):
    result = ret.copy()
    rate = cost_bp / 10_000
    for row in turnovers.itertuples(index=False):
        if row.execution_date in result.index:
            result.loc[row.execution_date] = (1 + result.loc[row.execution_date]) * (1 - row.turnover * rate) - 1
    return result


def stats(ret: pd.Series):
    ret = ret.dropna()
    equity = (1 + ret).cumprod()
    years = (ret.index[-1] - ret.index[0]).days / 365.2425
    cagr = equity.iloc[-1] ** (1 / years) - 1
    vol = ret.std() * np.sqrt(252)
    downside = ret.clip(upper=0).std() * np.sqrt(252)
    sharpe = ret.mean() * 252 / vol
    sortino = ret.mean() * 252 / downside
    maxdd = (equity / equity.cummax() - 1).min()
    return {
        "CAGR": cagr,
        "Vol": vol,
        "Sharpe": sharpe,
        "Sortino": sortino,
        "MaxDD": maxdd,
        "Calmar": cagr / abs(maxdd),
        "EndEquity": equity.iloc[-1],
    }


def static_returns(returns: pd.DataFrame, index: pd.DatetimeIndex):
    regime_static = {"XLE": .25, "XLK": .25, "XLU": .25, "XLP": .125, "IEF": .125}
    equal_static = {t: .2 for t in ["XLE", "XLK", "XLU", "XLP", "IEF"]}
    return {
        "SPY": returns.loc[index, "SPY"],
        "StaticRegimeMix": sum(returns.loc[index, t] * w for t, w in regime_static.items()),
        "StaticEqualWeight": sum(returns.loc[index, t] * w for t, w in equal_static.items()),
    }


def main():
    OUT_DIR.mkdir(exist_ok=True)
    prices, raw_t5yie = load_raw_data()
    rows, regime_rows = [], []
    scenario_returns = {}

    for mean_window, prefix in [(1, "A"), (3, "M")]:
        for lag in (0, 1, 2):
            name = f"{prefix}{lag}"
            t5yie = prepare_t5yie(raw_t5yie, prices.index, mean_window, lag)
            signals, returns = signal_frame(prices, t5yie)
            gross, weights, turnover, events = simulate_tradable(signals, returns)
            scenario_returns[name] = gross
            regime_rows.extend(
                {"scenario": name, "decision_date": d, "execution_date": e, "growth_on": r[0],
                 "inflation_on": r[1], "position": "+".join(w)}
                for d, e, r, w in events
            )
            for cost in TRADING_COST_BPS:
                net = apply_cost(gross, turnover, cost)
                for period, (start, end) in PERIODS.items():
                    sample = net.loc[start:end]
                    if len(sample) < 2:
                        continue
                    row = {"scenario": name, "mean_window": mean_window, "lag_days": lag,
                           "cost_bp": cost, "period": period, "turnover": turnover.turnover.sum(),
                           "position_changes": int((turnover.turnover > 0).sum())}
                    row.update(stats(sample))
                    rows.append(row)

    result = pd.DataFrame(rows)
    regimes = pd.DataFrame(regime_rows)
    result.to_csv(OUT_DIR / "t5yie_scenario_metrics.csv", index=False)
    regimes.to_csv(OUT_DIR / "t5yie_scenario_regimes.csv", index=False)

    common_start = max(s.index.min() for s in scenario_returns.values())
    common_end = min(s.index.max() for s in scenario_returns.values())
    _, returns = signal_frame(prices, prepare_t5yie(raw_t5yie, prices.index, 1, 0))
    bench_rows = []
    for name, ret in static_returns(returns, prices.loc[common_start:common_end].index).items():
        row = {"benchmark": name, "period": "full"}
        row.update(stats(ret.loc[common_start:common_end]))
        bench_rows.append(row)
    pd.DataFrame(bench_rows).to_csv(OUT_DIR / "benchmark_metrics.csv", index=False)

    full10 = result[(result.period == "full") & (result.cost_bp == 10)].copy()
    pivot = full10[["scenario", "CAGR", "Sharpe", "MaxDD", "Calmar", "turnover", "position_changes"]]
    report = ["# T5YIE scenario backtest", "", f"Common concept: signal at close, execute next trading-day close.", "",
              "## Full-period results at 10bp one-way cost", "", pivot.to_markdown(index=False, floatfmt=".4f"), "",
              "Raw outputs: `t5yie_scenario_metrics.csv`, `t5yie_scenario_regimes.csv`, `benchmark_metrics.csv`."]
    (OUT_DIR / "t5yie_scenario_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(pivot.to_string(index=False))
    print(f"saved results to {OUT_DIR}")


if __name__ == "__main__":
    main()
