"""Backtest the Inflation Compass model using data from data/inflation_compass.db."""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "inflation_compass.db"

POS_BASKET = {"XLE": 0.5, "XLI": 1 / 6, "XLF": 1 / 6, "XLB": 1 / 6}
NEG_BASKET = {"XLU": 1 / 3, "XLV": 1 / 3, "XLP": 1 / 3}

TRANSACTION_COST_BP = 30  # 0.3% = 30bp, charged one-way on each unit of notional traded

REGIME_POSITIONS = {
    (True, True): {"XLE": 1.0},
    (True, False): {"XLK": 1.0},
    (False, True): {"XLU": 1.0},
    (False, False): {"XLP": 0.5, "IEF": 0.5},
}


def load_data():
    conn = sqlite3.connect(DB_PATH)

    prices_long = pd.read_sql(\
        "SELECT date, ticker, close FROM prices"\
        , conn, parse_dates=["date"])

    t5yie_long = pd.read_sql\
        ("SELECT date, value FROM fred_series \
         WHERE series_id='T5YIE'"\
         , conn, parse_dates=["date"])

    conn.close()

    prices = prices_long.pivot(index="date", columns="ticker", values="close").sort_index()
    t5yie = t5yie_long.set_index("date")["value"].sort_index()
    t5yie = t5yie.reindex(prices.index).ffill()
    return prices, t5yie


def rolling_slope(series, window=60):
    x = np.arange(window)
    x_mean = x.mean()
    denom = ((x - x_mean) ** 2).sum()

    def slope(y):
        return ((x - x_mean) * (y - y.mean())).sum() / denom

    return series.rolling(window).apply(slope, raw=True)


def compute_signals(prices, t5yie):
    returns = prices.pct_change()

    spy_sma200 = prices["SPY"].rolling(200).mean()
    growth_on = prices["SPY"] > spy_sma200

    pos_ret = sum(returns[t] * w for t, w in POS_BASKET.items())
    neg_ret = sum(returns[t] * w for t, w in NEG_BASKET.items())
    basket_valid = pos_ret.notna() & neg_ret.notna()
    pos_cum = (1 + pos_ret.where(basket_valid, 0)).cumprod().where(basket_valid)
    neg_cum = (1 + neg_ret.where(basket_valid, 0)).cumprod().where(basket_valid)
    indicator = pos_cum / neg_cum
    slope = rolling_slope(indicator, 60)
    asset_momentum_on = slope > 0

    level_on = t5yie > 2.0
    t5yie_60_ago = t5yie.shift(60)
    breakeven_momentum_on = t5yie > t5yie_60_ago

    inflation_on = level_on & (breakeven_momentum_on | asset_momentum_on)

    # NaN comparisons silently evaluate to False, so validity must be tracked
    # explicitly rather than relying on dropna() after the boolean ops above.
    valid = spy_sma200.notna() & slope.notna() & t5yie.notna() & t5yie_60_ago.notna()

    signals = pd.DataFrame({"growth_on": growth_on, "inflation_on": inflation_on})[valid]
    return signals, returns


def month_end_dates(index):
    df = pd.Series(index, index=index)
    return df.groupby([index.year, index.month]).last()


def build_positions(signals):
    ends = month_end_dates(signals.index)
    positions = []
    for i in range(len(ends) - 1):
        decision_date = ends.iloc[i]
        period_start = ends.iloc[i]
        period_end = ends.iloc[i + 1]
        regime = (bool(signals.loc[decision_date, "growth_on"]), bool(signals.loc[decision_date, "inflation_on"]))
        positions.append((decision_date, period_end, regime, REGIME_POSITIONS[regime]))
    return positions


def simulate(positions, returns):
    daily_ret = pd.Series(0.0, index=returns.index)
    for decision_date, period_end, regime, weights in positions:
        mask = (returns.index > decision_date) & (returns.index <= period_end)
        period_ret = sum(returns.loc[mask, t] * w for t, w in weights.items())
        daily_ret.loc[mask] = period_ret
    start = positions[0][0]
    end = positions[-1][1]
    return daily_ret.loc[start:end]


def compute_turnover(positions):
    turnover = []
    prev_weights = {}
    for decision_date, period_end, regime, weights in positions:
        tickers = set(weights) | set(prev_weights)
        t = sum(abs(weights.get(t, 0) - prev_weights.get(t, 0)) for t in tickers)
        turnover.append((decision_date, t))
        prev_weights = weights
    return turnover


def apply_costs(daily_ret, positions, cost_bp):
    cost_rate = cost_bp / 10000
    adj_ret = daily_ret.copy()
    for decision_date, t in compute_turnover(positions):
        if t == 0:
            continue
        trade_days = daily_ret.index[daily_ret.index > decision_date]
        if len(trade_days) == 0:
            continue
        first_day = trade_days[0]
        adj_ret.loc[first_day] = (1 + adj_ret.loc[first_day]) * (1 - t * cost_rate) - 1
    return adj_ret


def perf_stats(daily_ret):
    equity = (1 + daily_ret).cumprod()
    n_years = len(daily_ret) / 252
    cagr = equity.iloc[-1] ** (1 / n_years) - 1
    vol = daily_ret.std() * np.sqrt(252)
    sharpe = (daily_ret.mean() * 252) / vol
    mdd = (equity / equity.cummax() - 1).min()
    return {"CAGR": cagr, "Vol": vol, "Sharpe": sharpe, "MaxDD": mdd, "End equity ($1 start)": equity.iloc[-1]}, equity


def yearly_returns(daily_ret):
    return daily_ret.groupby(daily_ret.index.year).apply(lambda r: (1 + r).prod() - 1)


def cost_sensitivity(strat_ret, positions, cost_bps):
    results = {}
    for cost_bp in cost_bps:
        net_ret = apply_costs(strat_ret, positions, cost_bp)
        stats, _ = perf_stats(net_ret)
        results[cost_bp] = stats
    return results


def build_weights_df(positions, index):
    tickers = ["XLE", "XLK", "XLU", "XLP", "IEF"]
    weights_df = pd.DataFrame(0.0, index=index, columns=tickers)
    for decision_date, period_end, regime, weights in positions:
        mask = (index > decision_date) & (index <= period_end)
        for t, w in weights.items():
            weights_df.loc[mask, t] = w
    return weights_df


def main():
    prices, t5yie = load_data()
    signals, returns = compute_signals(prices, t5yie)
    positions = build_positions(signals)

    strat_ret = simulate(positions, returns)
    strat_ret_net = apply_costs(strat_ret, positions, TRANSACTION_COST_BP)
    bench_ret = returns["SPY"].loc[strat_ret.index]

    strat_stats, strat_equity = perf_stats(strat_ret)
    strat_net_stats, strat_net_equity = perf_stats(strat_ret_net)
    bench_stats, bench_equity = perf_stats(bench_ret)

    n_changes = sum(1 for _, t in compute_turnover(positions) if t > 0)
    print(f"Backtest period: {strat_ret.index.min().date()} ~ {strat_ret.index.max().date()} ({len(positions)} monthly decisions, {n_changes} position changes)")
    print()
    header = f"{'metric':<24}{'Strategy (gross)':>18}{f'Strategy (net {TRANSACTION_COST_BP}bp)':>22}{'SPY B&H':>12}"
    print(header)

    def fmt(v):
        return f"{v:.3f}" if v is not None else "n/a"

    for k in strat_stats:
        print(f"{k:<24}{fmt(strat_stats[k]):>18}{fmt(strat_net_stats[k]):>22}{fmt(bench_stats[k]):>12}")
    print("\nAll figures above are computed from the daily return path (correct way to measure risk).")

    total_turnover = sum(t for _, t in compute_turnover(positions))
    n_years = len(strat_ret) / 252
    cost_drag = 1 - strat_net_equity.iloc[-1] / strat_equity.iloc[-1]
    print(
        f"\nTotal turnover: {total_turnover:.1f}x notional over {n_changes} changes "
        f"({n_years:.1f} yrs) -> at {TRANSACTION_COST_BP}bp/unit traded, cumulative cost drag = {cost_drag * 100:.2f}%"
    )
    cost_sens = cost_sensitivity(strat_ret, positions, [0, 0.3, 1, 5, 10, 30, 50, 100])
    print("\nCost sensitivity (one-way bp charged per unit of notional traded):")
    print(f"{'cost (bp)':<12}{'CAGR':>10}{'End equity':>14}")
    for cost_bp, stats in cost_sens.items():
        print(f"{cost_bp:<12}{stats['CAGR'] * 100:>9.2f}%{stats['End equity ($1 start)']:>14.1f}")

    strat_yearly = yearly_returns(strat_ret)
    bench_yearly = yearly_returns(bench_ret)
    excess_yearly = strat_yearly - bench_yearly

    print("\nYearly returns (first/last year partial):")
    print(f"{'year':<8}{'Strategy':>12}{'SPY':>12}{'Excess':>12}")
    for y in strat_yearly.index:
        print(f"{y:<8}{strat_yearly[y] * 100:>11.1f}%{bench_yearly[y] * 100:>11.1f}%{excess_yearly[y] * 100:>11.1f}%")
    win_rate = (excess_yearly > 0).mean()
    print(f"\nYears beating SPY: {(excess_yearly > 0).sum()}/{len(excess_yearly)} ({win_rate * 100:.0f}%)")
    print(f"Total return vs SPY over full period: {(strat_equity.iloc[-1] / bench_equity.iloc[-1] - 1) * 100:.0f}%")
    print(f"CAGR excess vs SPY: {(strat_stats['CAGR'] - bench_stats['CAGR']) * 100:.1f}pp/yr")

    print()
    regime_counts = pd.Series([p[2] for p in positions]).value_counts()
    print("Regime distribution (months):")
    for regime, count in regime_counts.items():
        ticker = list(REGIME_POSITIONS[regime].keys())
        print(f"  growth_on={regime[0]!s:<5} inflation_on={regime[1]!s:<5} -> {ticker}: {count}")

    pos_df = pd.DataFrame(
        [(d, e, r[0], r[1], ",".join(w.keys())) for d, e, r, w in positions],
        columns=["decision_date", "period_end", "growth_on", "inflation_on", "position"],
    )
    equity_df = pd.DataFrame(
        {"strategy_equity": strat_equity, "strategy_equity_net": strat_net_equity, "spy_equity": bench_equity}
    )
    yearly_df = pd.DataFrame({"strategy": strat_yearly, "spy": bench_yearly, "excess": excess_yearly}).reset_index(names="year")
    weights_df = build_weights_df(positions, strat_ret.index)

    # Raw daily returns (gross, pre-cost) + the rebalance turnover schedule, so any
    # downstream consumer (charts, a dashboard) can recompute cost scenarios or stats
    # straight from the db without re-running load_data/compute_signals/simulate.
    daily_ret_df = pd.DataFrame({"strategy_ret": strat_ret, "spy_ret": bench_ret})
    turnover_df = pd.DataFrame(compute_turnover(positions), columns=["decision_date", "turnover"])
    turnover_df = turnover_df[turnover_df["turnover"] > 0]

    conn = sqlite3.connect(DB_PATH)
    pos_df.assign(decision_date=pos_df.decision_date.dt.strftime("%Y-%m-%d"), period_end=pos_df.period_end.dt.strftime("%Y-%m-%d")).to_sql(
        "backtest_positions", conn, if_exists="replace", index=False
    )
    equity_df.reset_index(names="date").assign(date=lambda d: d.date.dt.strftime("%Y-%m-%d")).to_sql(
        "backtest_equity", conn, if_exists="replace", index=False
    )
    yearly_df.to_sql("backtest_yearly", conn, if_exists="replace", index=False)
    weights_df.reset_index(names="date").assign(date=lambda d: d.date.dt.strftime("%Y-%m-%d")).to_sql(
        "backtest_weights", conn, if_exists="replace", index=False
    )
    daily_ret_df.reset_index(names="date").assign(date=lambda d: d.date.dt.strftime("%Y-%m-%d")).to_sql(
        "backtest_daily_returns", conn, if_exists="replace", index=False
    )
    turnover_df.assign(decision_date=turnover_df.decision_date.dt.strftime("%Y-%m-%d")).to_sql(
        "backtest_turnover_events", conn, if_exists="replace", index=False
    )
    conn.close()
    print(
        "\nsaved backtest_positions, backtest_equity, backtest_yearly, backtest_weights, "
        "backtest_daily_returns, backtest_turnover_events tables to db"
    )


if __name__ == "__main__":
    main()
