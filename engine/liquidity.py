import numpy as np


def find_liquidity_levels(df, lookback=50):

    highs = df["high"].tail(lookback)
    lows = df["low"].tail(lookback)

    resistance = highs.max()
    support = lows.min()

    return support, resistance


def generate_liquidity_grid(price, grid_low, grid_high, levels, support, resistance):

    # generate evenly spaced grid
    grid = np.linspace(grid_low, grid_high, levels + 1)

    adjusted = []

    for level in grid:

        # distance from liquidity zones
        dist_res = abs(level - resistance)
        dist_sup = abs(level - support)

        # apply small bias toward liquidity
        if dist_res < (grid_high - grid_low) * 0.25:
            level = level * 1.0002

        elif dist_sup < (grid_high - grid_low) * 0.25:
            level = level * 0.9998

        adjusted.append(level)

    return adjusted