"""Render Inflation Compass backtest charts from data/inflation_compass.db.

Produces three separate chart images in docs/images/charts/:
  equity-drawdown.png   - growth of $1 (strategy vs SPY) + absolute drawdown
  portfolio-weights.png           - daily position weight by ticker
  relative-performance.png          - strategy/SPY ratio (relative strength) + relative drawdown
"""

import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "inflation_compass.db"
OUT_DIR = Path(__file__).parent / "docs" / "images" / "charts"

SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
STRATEGY_COLOR = "#bb6b2c"  # copper - the model
BENCH_COLOR = "#3d5a73"  # slate blue - SPY baseline
RELATIVE_COLOR = STRATEGY_COLOR

WEIGHT_COLORS = {
    "XLE": "#2a78d6",  # blue
    "XLK": "#eb6834",  # orange
    "XLU": "#1baf7a",  # aqua
    "XLP": "#eda100",  # yellow
    "IEF": "#e87ba4",  # magenta
}


def load_table(name, date_col="date"):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(f"SELECT * FROM {name}", conn, parse_dates=[date_col])
    conn.close()
    return df.set_index(date_col)


def style_axis(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(BASELINE)
    ax.tick_params(colors=INK_MUTED, labelsize=9)


def save(fig, name):
    fig.tight_layout()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"saved {path}")


def plot_equity_drawdown(equity):
    drawdown = equity / equity.cummax() - 1
    fig, (ax_eq, ax_dd) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True, height_ratios=[2.2, 1], facecolor=SURFACE)

    ax_eq.plot(equity.index, equity["strategy_equity"], color=STRATEGY_COLOR, linewidth=2, label="Inflation Compass")
    ax_eq.plot(equity.index, equity["spy_equity"], color=BENCH_COLOR, linewidth=2, label="SPY buy & hold")
    ax_eq.set_yscale("log")
    ax_eq.set_title("Inflation Compass backtest - growth of $1 (log scale)", color=INK_PRIMARY, fontsize=12, loc="left")
    ax_eq.legend(frameon=False, labelcolor=INK_SECONDARY, loc="upper left")
    style_axis(ax_eq)

    ax_dd.fill_between(drawdown.index, drawdown["strategy_equity"] * 100, 0, color=STRATEGY_COLOR, alpha=0.25, linewidth=0)
    ax_dd.plot(drawdown.index, drawdown["strategy_equity"] * 100, color=STRATEGY_COLOR, linewidth=1.2)
    ax_dd.fill_between(drawdown.index, drawdown["spy_equity"] * 100, 0, color=BENCH_COLOR, alpha=0.15, linewidth=0)
    ax_dd.plot(drawdown.index, drawdown["spy_equity"] * 100, color=BENCH_COLOR, linewidth=1.2)
    ax_dd.set_title("Drawdown (%)", color=INK_PRIMARY, fontsize=12, loc="left")
    style_axis(ax_dd)

    save(fig, "equity-drawdown.png")


def plot_weights(weights):
    from matplotlib.lines import Line2D

    fig, ax = plt.subplots(figsize=(10, 3.9), facecolor=SURFACE)
    tickers = list(WEIGHT_COLORS.keys())
    ax.stackplot(
        weights.index,
        [weights[t] * 100 for t in tickers],
        colors=[WEIGHT_COLORS[t] for t in tickers],
        edgecolor=SURFACE,
        linewidth=1.1,
    )
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 50, 100])
    ax.axhline(50, color=GRIDLINE, linewidth=0.8, zorder=0.5)
    ax.set_title("Daily position weight (%)", color=INK_PRIMARY, fontsize=12, loc="left", pad=12)

    handles = [Line2D([0], [0], color=WEIGHT_COLORS[t], linewidth=7, solid_capstyle="round") for t in tickers]
    ax.legend(
        handles, tickers, frameon=False, labelcolor=INK_SECONDARY, loc="upper center", ncol=5,
        fontsize=9.5, bbox_to_anchor=(0.5, -0.13), handlelength=1.4, handletextpad=0.6, columnspacing=1.6,
    )
    style_axis(ax)
    ax.grid(False)
    save(fig, "portfolio-weights.png")


def plot_relative(equity):
    ratio = equity["strategy_equity"] / equity["spy_equity"]
    rel_dd = ratio / ratio.cummax() - 1

    fig, (ax_r, ax_dd) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True, height_ratios=[2.2, 1], facecolor=SURFACE)

    ax_r.plot(ratio.index, ratio, color=RELATIVE_COLOR, linewidth=1.6)
    ax_r.set_yscale("log")
    ax_r.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}x"))
    ax_r.yaxis.set_minor_formatter(mticker.NullFormatter())
    ax_r.set_title("Relative strength vs SPY (log scale)", color=INK_PRIMARY, fontsize=12, loc="left")
    style_axis(ax_r)

    ax_dd.fill_between(rel_dd.index, rel_dd * 100, 0, color=RELATIVE_COLOR, alpha=0.2, linewidth=0)
    ax_dd.plot(rel_dd.index, rel_dd * 100, color=RELATIVE_COLOR, linewidth=1.2)
    ax_dd.set_title("Relative drawdown (%)", color=INK_PRIMARY, fontsize=12, loc="left")
    style_axis(ax_dd)

    save(fig, "relative-performance.png")


def main():
    equity = load_table("backtest_equity")
    weights = load_table("backtest_weights")
    plot_equity_drawdown(equity)
    plot_weights(weights)
    plot_relative(equity)


if __name__ == "__main__":
    main()
