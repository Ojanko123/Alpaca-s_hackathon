"""
Risk manager.

Owns all the "is it safe to do this trade" decisions:
- position sizing per trade (% of capital)
- max concurrent open pairs
- ticker overlap across concurrent pairs (concentration risk)
- portfolio drawdown circuit-breaker (soft warning + hard halt)
- reconciling recorded state against what the broker actually holds

Designed to be checked far more often than the daily signal scan
(see CONFIG.risk_check_frequency_minutes) so a fast-moving options
position can't blow through the drawdown limit unnoticed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

from config import CONFIG
from signals import Action, OpenPositionInfo
from universe import CandidatePair

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "portfolio_state.json"


class DrawdownHalt(Exception):
    """Raised when the portfolio has breached the hard drawdown limit."""


@dataclass
class PortfolioState:
    starting_equity: float = CONFIG.starting_capital
    current_equity: float = CONFIG.starting_capital
    peak_equity: float = CONFIG.starting_capital
    open_positions: dict[str, OpenPositionInfo] = field(default_factory=dict)  # keyed by "A/B"
    trading_halted: bool = False
    soft_warning_active: bool = False

    def update_equity(self, new_equity: float) -> None:
        self.current_equity = new_equity
        self.peak_equity = max(self.peak_equity, new_equity)

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - self.current_equity) / self.peak_equity

    def tickers_in_use(self) -> set[str]:
        """
        Every underlying ticker currently involved in ANY open pair.
        Used to block a new entry that would concentrate exposure in a
        stock already committed to a different open position - e.g.
        without this, CBRE could end up as one leg of three separate
        concurrent pairs, silently tripling intended per-trade sizing on
        that one name.
        """
        tickers: set[str] = set()
        for position in self.open_positions.values():
            tickers.add(position.pair.ticker_a)
            tickers.add(position.pair.ticker_b)
        return tickers

    # ------------------------------------------------------------------
    # Persistence - each `python main.py` run is a fresh process, so state
    # (open positions, equity peak) must be saved to disk and reloaded, or
    # every run would "forget" positions opened in a previous run and never
    # be able to check them for stop-loss/take-profit/exit conditions.
    # ------------------------------------------------------------------
    def save(self, path: Path = STATE_FILE) -> None:
        data = {
            "starting_equity": self.starting_equity,
            "current_equity": self.current_equity,
            "peak_equity": self.peak_equity,
            "trading_halted": self.trading_halted,
            "soft_warning_active": self.soft_warning_active,
            "open_positions": {
                key: {
                    "pair": asdict(pos.pair),
                    "direction": pos.direction.value,
                    "entry_date": pos.entry_date.isoformat(),
                    "entry_zscore": pos.entry_zscore,
                    "entry_premium": pos.entry_premium,
                    "option_symbols": pos.option_symbols,
                }
                for key, pos in self.open_positions.items()
            },
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info("Portfolio state saved (%d open position(s))", len(self.open_positions))

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> "PortfolioState":
        if not path.exists():
            logger.info("No saved state found, starting fresh portfolio state")
            return cls()

        data = json.loads(path.read_text())
        open_positions = {}
        for key, pos_data in data.get("open_positions", {}).items():
            pair = CandidatePair(**pos_data["pair"])
            open_positions[key] = OpenPositionInfo(
                pair=pair,
                direction=Action(pos_data["direction"]),
                entry_date=date.fromisoformat(pos_data["entry_date"]),
                entry_zscore=pos_data["entry_zscore"],
                entry_premium=pos_data.get("entry_premium", 0.0),
                option_symbols=pos_data.get("option_symbols"),
            )

        state = cls(
            starting_equity=data.get("starting_equity", CONFIG.starting_capital),
            current_equity=data.get("current_equity", CONFIG.starting_capital),
            peak_equity=data.get("peak_equity", CONFIG.starting_capital),
            open_positions=open_positions,
            trading_halted=data.get("trading_halted", False),
            soft_warning_active=data.get("soft_warning_active", False),
        )
        logger.info("Portfolio state loaded (%d open position(s))", len(open_positions))
        return state


class RiskManager:
    def __init__(self, state: PortfolioState | None = None):
        self.state = state or PortfolioState()

    # ------------------------------------------------------------------
    # Drawdown monitoring - call this frequently (see risk_check_frequency_minutes)
    # ------------------------------------------------------------------
    def check_drawdown(self) -> None:
        dd = self.state.drawdown_pct

        if dd >= CONFIG.max_drawdown_pct:
            self.state.trading_halted = True
            logger.critical(
                "HARD DRAWDOWN HALT: %.2f%% >= max %.2f%%. All new trades blocked.",
                dd * 100, CONFIG.max_drawdown_pct * 100,
            )
            raise DrawdownHalt(f"Portfolio drawdown {dd:.2%} breached hard limit {CONFIG.max_drawdown_pct:.2%}")

        if dd >= CONFIG.soft_drawdown_pct and not self.state.soft_warning_active:
            self.state.soft_warning_active = True
            logger.warning(
                "SOFT DRAWDOWN WARNING: %.2f%% >= soft threshold %.2f%%. Reducing new position sizing.",
                dd * 100, CONFIG.soft_drawdown_pct * 100,
            )
        elif dd < CONFIG.soft_drawdown_pct and self.state.soft_warning_active:
            self.state.soft_warning_active = False
            logger.info("Drawdown back below soft threshold, resuming normal sizing.")

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------
    def position_size_dollars(self) -> float:
        """
        Dollars to allocate to a new trade. Halves the normal size while
        the soft drawdown warning is active, as a graduated risk response
        rather than an all-or-nothing switch.
        """
        base = self.state.current_equity * CONFIG.risk_per_trade_pct
        if self.state.soft_warning_active:
            return base * 0.5
        return base

    def can_open_new_position(self, pair: CandidatePair | None = None) -> tuple[bool, str]:
        """
        pair is optional so existing callers that don't pass one still work,
        but every entry-opening call site should pass it - without a pair,
        the ticker-overlap check can't run and concentration risk goes
        uncaught, exactly as happened with CBRE across three concurrent pairs.
        """
        if self.state.trading_halted:
            return False, "trading halted (hard drawdown limit breached)"
        if len(self.state.open_positions) >= CONFIG.max_concurrent_pairs:
            return False, f"max concurrent pairs reached ({CONFIG.max_concurrent_pairs})"

        if pair is not None:
            in_use = self.state.tickers_in_use()
            overlap = {pair.ticker_a, pair.ticker_b} & in_use
            if overlap:
                return False, f"ticker(s) {sorted(overlap)} already committed to another open pair"

        return True, "ok"

    # ------------------------------------------------------------------
    # Position bookkeeping
    # ------------------------------------------------------------------
    def register_entry(self, key: str, position: OpenPositionInfo) -> None:
        self.state.open_positions[key] = position
        logger.info("Opened position %s: %s @ z=%.2f", key, position.direction, position.entry_zscore)

    def register_exit(self, key: str, reason: str) -> None:
        if key in self.state.open_positions:
            del self.state.open_positions[key]
            logger.info("Closed position %s (%s)", key, reason)

    # ------------------------------------------------------------------
    # Broker reconciliation - our own state (portfolio_state.json) is only
    # ever a cache of what we *think* is open. This checks it against what
    # the broker actually holds and drops entries that are fully gone, so a
    # previous partial-close failure (like the CF/CSX 404) doesn't keep
    # getting re-evaluated against a position that no longer fully exists.
    # Positions with only SOME legs missing are logged, not auto-dropped -
    # those need an actual close attempt (executor.close_spread_legs),
    # which happens naturally on the next take-profit/exit check.
    # ------------------------------------------------------------------
    def reconcile_with_broker(self, executor) -> None:
        broker_positions = executor.get_broker_option_positions()
        stale_keys = []

        for key, position in self.state.open_positions.items():
            if not position.option_symbols:
                continue

            present = [leg for leg in position.option_symbols if leg["symbol"] in broker_positions]
            if not present:
                stale_keys.append(key)
            elif len(present) < len(position.option_symbols):
                missing = [
                    leg["symbol"] for leg in position.option_symbols
                    if leg["symbol"] not in broker_positions
                ]
                logger.warning(
                    "Reconciliation: %s has %d/%d leg(s) missing at broker (%s). "
                    "Leaving in state - will be closed out on the next exit/take-profit check.",
                    key, len(missing), len(position.option_symbols), missing,
                )

        for key in stale_keys:
            logger.warning(
                "Reconciliation: %s has zero legs left at the broker, removing stale state entry", key,
            )
            del self.state.open_positions[key]

    # ------------------------------------------------------------------
    # Take-profit check - separate from the z-score/time stops in signals.py
    # because it needs live option quotes (via executor), not just price history.
    # ------------------------------------------------------------------
    def check_take_profit(self, key: str, current_close_proceeds: float) -> bool:
        """
        Returns True if the position has gained >= CONFIG.take_profit_pct
        of the original premium paid, and should be closed to lock in gains.
        """
        position = self.state.open_positions.get(key)
        if position is None or position.entry_premium <= 0:
            return False

        gain = current_close_proceeds - position.entry_premium
        gain_pct = gain / position.entry_premium

        if gain_pct >= CONFIG.take_profit_pct:
            logger.info(
                "TAKE PROFIT triggered for %s: gained %.1f%% of premium (target %.1f%%)",
                key, gain_pct * 100, CONFIG.take_profit_pct * 100,
            )
            return True
        return False
