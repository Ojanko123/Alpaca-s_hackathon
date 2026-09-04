"""
Configuration for the Alpaca Pairs-Options Hackathon Agent.

All strategy/risk parameters live here so they're easy to audit and
reference in the write-up. Never commit real API keys - load them
from environment variables.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root if present, doesn't overwrite real env vars


# ---------------------------------------------------------------------------
# Alpaca API credentials (set these as environment variables, do not hardcode)
# ---------------------------------------------------------------------------
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = True  # must stay True for this competition
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_ACCOUNT_ID = os.environ.get("ALPACA_ACCOUNT_ID", "")  # the dedicated $100k comp account


@dataclass(frozen=True)
class StrategyConfig:
    # --- Universe & pair discovery ---
    lookback_days: int = 60          # rolling window for correlation/cointegration + spread stats
    min_correlation: float = 0.80    # minimum historical correlation to consider a pair
    cointegration_pvalue_max: float = 0.10  # Engle-Granger test threshold (loosened vs. usual 0.05)
    max_pairs_tracked: int = 25      # cap on how many candidate pairs we monitor daily

    # --- Signal / entry ---
    entry_zscore: float = 1.5        # loosened from typical 2.0 to get enough trades in 7 days
    exit_zscore: float = 0.0         # exit target: spread reverts back to its mean

    # --- Stop-loss ---
    stop_zscore: float = 3.0         # hard stop if spread keeps widening against us
    max_hold_days: int = 3           # time-based stop if no reversion within this many days
    take_profit_pct: float = 0.35    # close if position gains >= 35% of premium risked

    # --- Position sizing & portfolio risk ---
    starting_capital: float = 100_000.0
    risk_per_trade_pct: float = 0.025    # 2.5% of capital per trade (midpoint of 2-3%)
    max_concurrent_pairs: int = 6        # caps total exposure given per-trade sizing
    max_drawdown_pct: float = 0.12       # hard circuit-breaker: halt all new trades
    soft_drawdown_pct: float = 0.08      # warning threshold: reduce sizing, flag for review

    # --- Options structure ---
    options_expiry_days_min: int = 7     # avoid 0DTE-style contracts
    options_expiry_days_max: int = 35    # widened from 21 - many stocks only have monthly (not weekly)
                                          # expirations, so a narrow window can miss real, tradable
                                          # contracts on otherwise perfectly optionable large-cap names
    spread_width_pct: float = 0.03       # ~3% wide spreads (defined-risk structures)

    # --- Run cadence ---
    signal_check_frequency: str = "daily"      # entries evaluated once per day
    risk_check_frequency_minutes: int = 30     # stop-loss / drawdown monitored more often


CONFIG = StrategyConfig()
