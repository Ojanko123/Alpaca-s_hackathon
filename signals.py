"""
Signal engine.

For each candidate pair, computes the hedge-ratio-adjusted spread and
its rolling z-score, then classifies the pair into an action:
ENTER_LONG_SPREAD, ENTER_SHORT_SPREAD, EXIT, STOP_LOSS, or HOLD/NONE.

"Long the spread" = long ticker_a / short ticker_b (expressed via options
later in options_builder.py). "Short the spread" is the reverse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum

import numpy as np
import pandas as pd

from config import CONFIG
from universe import CandidatePair

logger = logging.getLogger(__name__)


class Action(str, Enum):
    ENTER_LONG_SPREAD = "ENTER_LONG_SPREAD"    # z <= -entry_zscore -> expect spread to rise
    ENTER_SHORT_SPREAD = "ENTER_SHORT_SPREAD"  # z >= +entry_zscore -> expect spread to fall
    EXIT_TARGET = "EXIT_TARGET"                # reverted back toward mean
    STOP_LOSS_Z = "STOP_LOSS_Z"                # z moved further against an open position
    STOP_LOSS_TIME = "STOP_LOSS_TIME"          # held too long without reverting
    HOLD = "HOLD"
    NONE = "NONE"


@dataclass
class SignalResult:
    pair: CandidatePair
    spread_series: pd.Series
    zscore_series: pd.Series
    current_zscore: float
    action: Action
    reason: str


def compute_spread(prices: pd.DataFrame, pair: CandidatePair) -> pd.Series:
    """Spread = price_a - hedge_ratio * price_b."""
    spread = prices[pair.ticker_a] - pair.hedge_ratio * prices[pair.ticker_b]
    return spread.dropna()


def compute_zscore(spread: pd.Series, window: int) -> pd.Series:
    mean = spread.rolling(window).mean()
    std = spread.rolling(window).std()
    z = (spread - mean) / std
    return z


def evaluate_pair(
    prices: pd.DataFrame,
    pair: CandidatePair,
    open_position: "OpenPositionInfo | None" = None,
) -> SignalResult:
    """
    Evaluate a single pair against current data and (if any) an open
    position on that pair, returning the action to take today.
    """
    spread = compute_spread(prices, pair)
    zscore = compute_zscore(spread, CONFIG.lookback_days)
    current_z = float(zscore.iloc[-1])

    if pd.isna(current_z):
        return SignalResult(pair, spread, zscore, current_z, Action.NONE, "insufficient data for z-score")

    # --- If we have an open position on this pair, check exit/stop conditions first ---
    if open_position is not None:
        days_held = (date.today() - open_position.entry_date).days

        # Time-based stop takes priority - if we've overstayed, get out regardless of z
        if days_held >= CONFIG.max_hold_days:
            return SignalResult(
                pair, spread, zscore, current_z, Action.STOP_LOSS_TIME,
                f"held {days_held}d >= max_hold_days={CONFIG.max_hold_days}",
            )

        # Z-score stop-loss: spread moved further against the position
        if open_position.direction == Action.ENTER_LONG_SPREAD and current_z <= -CONFIG.stop_zscore:
            return SignalResult(
                pair, spread, zscore, current_z, Action.STOP_LOSS_Z,
                f"z={current_z:.2f} breached stop at -{CONFIG.stop_zscore}",
            )
        if open_position.direction == Action.ENTER_SHORT_SPREAD and current_z >= CONFIG.stop_zscore:
            return SignalResult(
                pair, spread, zscore, current_z, Action.STOP_LOSS_Z,
                f"z={current_z:.2f} breached stop at +{CONFIG.stop_zscore}",
            )

        # Exit target: reverted back to (near) the mean
        if open_position.direction == Action.ENTER_LONG_SPREAD and current_z >= CONFIG.exit_zscore:
            return SignalResult(pair, spread, zscore, current_z, Action.EXIT_TARGET, "reverted to mean")
        if open_position.direction == Action.ENTER_SHORT_SPREAD and current_z <= CONFIG.exit_zscore:
            return SignalResult(pair, spread, zscore, current_z, Action.EXIT_TARGET, "reverted to mean")

        return SignalResult(pair, spread, zscore, current_z, Action.HOLD, "within thresholds, holding")

    # --- No open position: check for a fresh entry signal ---
    if current_z <= -CONFIG.entry_zscore:
        return SignalResult(
            pair, spread, zscore, current_z, Action.ENTER_LONG_SPREAD,
            f"z={current_z:.2f} <= -{CONFIG.entry_zscore}",
        )
    if current_z >= CONFIG.entry_zscore:
        return SignalResult(
            pair, spread, zscore, current_z, Action.ENTER_SHORT_SPREAD,
            f"z={current_z:.2f} >= {CONFIG.entry_zscore}",
        )

    return SignalResult(pair, spread, zscore, current_z, Action.NONE, "no signal")


# Lightweight struct describing an open position, populated by risk_manager.py
from dataclasses import dataclass as _dataclass


@_dataclass
class OpenPositionInfo:
    pair: CandidatePair
    direction: Action
    entry_date: date
    entry_zscore: float
    entry_premium: float = 0.0       # net $ cost paid to open both spread legs combined
    option_symbols: list | None = None  # resolved OCC symbols + side, needed to check current value later
