"""Point-in-time T5YIE audit using month-end ALFRED vintage snapshots."""

from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

import backtest as base
from research import t5yie_scenarios as exp


FIRST_RELEASE = pd.Timestamp("2014-01-27")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results"
URL = "https://alfred.stlouisfed.org/graph/alfredgraph.csv?id=T5YIE&vintage_date={date}"


def fetch_vintage(date: pd.Timestamp, attempts: int = 4):
    date_text = date.strftime("%Y-%m-%d")
    for attempt in range(attempts):
        try:
            response = requests.get(URL.format(date=date_text), timeout=30)
            response.raise_for_status()
            frame = pd.read_csv(io.BytesIO(response.content), na_values=".")
            if frame.empty:
                raise ValueError("empty ALFRED response")
            frame.columns = ["observation_date", "value"]
            frame["observation_date"] = pd.to_datetime(frame["observation_date"])
            return date, frame.dropna(subset=["value"]).set_index("observation_date")["value"].sort_index()
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def inflation_components(prices, vintage, decision):
    aligned = vintage.reindex(prices.index).ffill()
    current = aligned.loc[decision]
    prior = aligned.shift(60).loc[decision]
    return current, prior, bool(current > 2.0), bool(current > prior)


def asset_momentum_by_date(prices):
    returns = prices.pct_change(fill_method=None)
    pos = sum(returns[t] * w for t, w in base.POS_BASKET.items())
    neg = sum(returns[t] * w for t, w in base.NEG_BASKET.items())
    valid = pos.notna() & neg.notna()
    indicator = (1 + pos.where(valid, 0)).cumprod().where(valid) / (1 + neg.where(valid, 0)).cumprod().where(valid)
    return base.rolling_slope(indicator, 60) > 0


def main():
    OUT.mkdir(exist_ok=True)
    prices, latest = exp.load_raw_data()
    trading_days = prices.index
    decisions = pd.Series(trading_days, index=trading_days).groupby([trading_days.year, trading_days.month]).last()
    decisions = pd.DatetimeIndex(decisions[decisions >= FIRST_RELEASE].values)

    snapshots = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_vintage, d): d for d in decisions}
        for future in as_completed(futures):
            date, series = future.result()
            snapshots[date] = series
    print(f"downloaded {len(snapshots)} ALFRED month-end snapshots")

    asset_momentum = asset_momentum_by_date(prices)
    spy_sma = prices["SPY"].rolling(200).mean()
    rows = []
    pit_signals, latest_signals = [], []
    for decision in decisions:
        pit = snapshots[decision]
        pit_value, pit_prior, pit_level, pit_momentum = inflation_components(prices, pit, decision)
        final_value, final_prior, final_level, final_momentum = inflation_components(prices, latest, decision)

        pit_last_obs = pit.index[pit.index <= decision].max()
        final_same_obs = latest.get(pit_last_obs, float("nan"))
        same_obs_revised = pd.notna(final_same_obs) and abs(float(final_same_obs) - float(pit.loc[pit_last_obs])) > 1e-12
        exact_month_end_available = decision in pit.index
        month_end_final = latest.get(decision, float("nan"))
        month_end_revised = exact_month_end_available and pd.notna(month_end_final) and abs(float(month_end_final) - float(pit.loc[decision])) > 1e-12

        growth = bool(prices.loc[decision, "SPY"] > spy_sma.loc[decision])
        asset = bool(asset_momentum.loc[decision])
        pit_inflation = pit_level and (pit_momentum or asset)
        final_inflation = final_level and (final_momentum or asset)
        pit_regime = (growth, pit_inflation)
        final_regime = (growth, final_inflation)
        pit_signals.append((decision, growth, pit_inflation))
        latest_signals.append((decision, growth, final_inflation))
        rows.append({
            "decision_date": decision,
            "pit_last_observation": pit_last_obs,
            "exact_month_end_available": exact_month_end_available,
            "pit_value": pit_value,
            "latest_value": final_value,
            "same_observation_revised": same_obs_revised,
            "month_end_revised": month_end_revised,
            "pit_level_on": pit_level,
            "latest_level_on": final_level,
            "level_flipped": pit_level != final_level,
            "pit_60d_momentum_on": pit_momentum,
            "latest_60d_momentum_on": final_momentum,
            "momentum_flipped": pit_momentum != final_momentum,
            "asset_momentum_on": asset,
            "pit_inflation_on": pit_inflation,
            "latest_inflation_on": final_inflation,
            "position_flipped": pit_regime != final_regime,
            "pit_position": "+".join(base.REGIME_POSITIONS[pit_regime]),
            "latest_position": "+".join(base.REGIME_POSITIONS[final_regime]),
        })

    audit = pd.DataFrame(rows)
    audit.to_csv(OUT / "alfred_month_end_audit.csv", index=False)

    def to_signal_frame(items):
        return pd.DataFrame(items, columns=["date", "growth_on", "inflation_on"]).set_index("date")

    returns = prices.pct_change(fill_method=None)
    performance = []
    for name, signal_items in [("ALFRED_PIT", pit_signals), ("FRED_LATEST", latest_signals)]:
        gross, _, turnover, _ = exp.simulate_tradable(to_signal_frame(signal_items), returns)
        for cost in (0, 10, 30):
            net = exp.apply_cost(gross, turnover, cost)
            row = {"model": name, "cost_bp": cost, "start": net.index.min(), "end": net.index.max(),
                   "turnover": turnover.turnover.sum(), "position_changes": int((turnover.turnover > 0).sum())}
            row.update(exp.stats(net))
            performance.append(row)
    perf = pd.DataFrame(performance)
    perf.to_csv(OUT / "alfred_performance_comparison.csv", index=False)

    summary = {
        "audited_months": len(audit),
        "exact_month_end_unavailable": int((~audit.exact_month_end_available).sum()),
        "same_observation_revised": int(audit.same_observation_revised.sum()),
        "month_end_value_revised": int(audit.month_end_revised.sum()),
        "level_flipped": int(audit.level_flipped.sum()),
        "momentum_flipped": int(audit.momentum_flipped.sum()),
        "position_flipped": int(audit.position_flipped.sum()),
    }
    pd.DataFrame([summary]).to_csv(OUT / "alfred_audit_summary.csv", index=False)
    print(summary)
    print(perf.to_string(index=False))


if __name__ == "__main__":
    main()
