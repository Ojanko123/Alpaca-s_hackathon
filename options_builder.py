"""
Options structure builder.

Translates a pairs-trading directional view into a defined-risk options
spread on each leg, so the strategy satisfies the hackathon's options
requirement while keeping max loss per trade capped by construction
(this is also what makes the risk-gate story credible - the options
structure itself, not just a stop order, bounds the downside).

ENTER_LONG_SPREAD (expect ticker_a to outperform ticker_b):
    -> bull call spread on ticker_a
    -> bear put spread on ticker_b

ENTER_SHORT_SPREAD (expect ticker_a to underperform ticker_b):
    -> bear put spread on ticker_a
    -> bull call spread on ticker_b
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from config import CONFIG
from signals import Action

logger = logging.getLogger(__name__)


@dataclass
class OptionLeg:
    symbol: str          # underlying ticker
    option_symbol: str   # OCC-style contract symbol, filled in once we query the chain
    strike: float
    expiry: date
    side: str            # "buy" or "sell"
    right: str           # "call" or "put"
    qty: int


@dataclass
class SpreadOrder:
    underlying: str
    strategy_name: str   # e.g. "bull_call_spread"
    legs: list[OptionLeg]
    max_loss: float
    max_gain: float


def pick_expiry(today: date) -> date:
    """
    Pick an expiry within [options_expiry_days_min, options_expiry_days_max].
    Actual contract selection will snap to the nearest available Friday
    expiry once we query the real options chain via the Alpaca client.
    """
    target = today + timedelta(days=(CONFIG.options_expiry_days_min + CONFIG.options_expiry_days_max) // 2)
    return target


def build_bull_call_spread(underlying: str, current_price: float, qty: int, today: date) -> SpreadOrder:
    """Buy a lower-strike call, sell a higher-strike call. Bullish, defined risk."""
    expiry = pick_expiry(today)
    long_strike = round(current_price * (1 - CONFIG.spread_width_pct / 2), 0)
    short_strike = round(current_price * (1 + CONFIG.spread_width_pct / 2), 0)

    legs = [
        OptionLeg(underlying, "", long_strike, expiry, "buy", "call", qty),
        OptionLeg(underlying, "", short_strike, expiry, "sell", "call", qty),
    ]
    width = short_strike - long_strike
    # Actual max_loss/max_gain need live premiums from the chain - placeholders here,
    # to be filled in by executor.py once quotes come back from Alpaca.
    return SpreadOrder(underlying, "bull_call_spread", legs, max_loss=0.0, max_gain=width * qty * 100)


def build_bear_put_spread(underlying: str, current_price: float, qty: int, today: date) -> SpreadOrder:
    """Buy a higher-strike put, sell a lower-strike put. Bearish, defined risk."""
    expiry = pick_expiry(today)
    long_strike = round(current_price * (1 + CONFIG.spread_width_pct / 2), 0)
    short_strike = round(current_price * (1 - CONFIG.spread_width_pct / 2), 0)

    legs = [
        OptionLeg(underlying, "", long_strike, expiry, "buy", "put", qty),
        OptionLeg(underlying, "", short_strike, expiry, "sell", "put", qty),
    ]
    width = long_strike - short_strike
    return SpreadOrder(underlying, "bear_put_spread", legs, max_loss=0.0, max_gain=width * qty * 100)


def build_spreads_for_signal(
    action: Action,
    ticker_a: str,
    ticker_b: str,
    price_a: float,
    price_b: float,
    qty_a: int,
    qty_b: int,
    today: date,
) -> list[SpreadOrder]:
    """
    Given an entry signal, build the two option spreads (one per leg)
    that express the pairs view.
    """
    if action == Action.ENTER_LONG_SPREAD:
        # expect A to rise relative to B
        spread_a = build_bull_call_spread(ticker_a, price_a, qty_a, today)
        spread_b = build_bear_put_spread(ticker_b, price_b, qty_b, today)
    elif action == Action.ENTER_SHORT_SPREAD:
        # expect A to fall relative to B
        spread_a = build_bear_put_spread(ticker_a, price_a, qty_a, today)
        spread_b = build_bull_call_spread(ticker_b, price_b, qty_b, today)
    else:
        raise ValueError(f"build_spreads_for_signal called with non-entry action: {action}")

    logger.info(
        "Built spreads for %s/%s (%s): %s + %s",
        ticker_a, ticker_b, action.value, spread_a.strategy_name, spread_b.strategy_name,
    )
    return [spread_a, spread_b]
