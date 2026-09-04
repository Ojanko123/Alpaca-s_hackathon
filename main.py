"""
Main daily entrypoint. Run this once per trading day (see
CONFIG.signal_check_frequency). For stop-loss/drawdown monitoring at higher frequency,
risk_manager.check_drawdown() should also be called on its own faster schedule
(e.g. a cron every CONFIG.risk_check_frequency_minutes) - see risk_check_loop() below.

Usage:
    python main.py                 # run today's daily signal + entry/exit cycle
    python main.py --risk-check    # run just the fast drawdown/stop check
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from config import CONFIG, ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER

COMPETITION_START_DATE = date(2026, 8, 28)  # Day 1 of the hackathon


def current_competition_day() -> int:
    """Auto-calculates which competition day today is, so scheduled tasks never need manual editing of a --day argument."""
    delta = (date.today() - COMPETITION_START_DATE).days + 1
    return max(1, delta)


from universe import get_sp500_tickers, fetch_price_history, find_candidate_pairs
from signals import evaluate_pair, Action, OpenPositionInfo
from risk_manager import RiskManager, DrawdownHalt, PortfolioState
from options_builder import build_spreads_for_signal
from executor import AlpacaExecutor
from logger import log_decision, daily_summary_header
from alpaca.common.exceptions import APIError

logger = logging.getLogger(__name__)


def build_clients():
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical.stock import StockHistoricalDataClient

    trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return trading_client, data_client


def _close_pair_positions(executor: AlpacaExecutor, key: str, open_position: OpenPositionInfo | None, reason: str, risk: RiskManager) -> None:
    """
    Shared close path for both the daily exit cycle and the take-profit
    check. Always closes by option symbol (never the underlying ticker),
    and only removes the position from state once the close order was
    actually accepted by the broker.
    """
    if not open_position or not open_position.option_symbols:
        logger.warning("No option_symbols recorded for %s; nothing to close, dropping from state", key)
        risk.register_exit(key, reason)
        return

    try:
        executor.close_spread_legs(open_position.option_symbols)
        risk.register_exit(key, reason)
    except APIError as exc:
        logger.warning("Close order failed for %s, leaving in state to retry next cycle: %s", key, exc)


def daily_cycle(day_number: int) -> None:
    today = date.today()
    daily_summary_header(day_number, today.isoformat())
    logger.info("=== Starting daily cycle: Day %d (%s) ===", day_number, today)

    trading_client, data_client = build_clients()
    executor = AlpacaExecutor(trading_client, data_client)
    risk = RiskManager(state=PortfolioState.load())

    # sync equity from the live account before making any decisions
    equity = executor.get_account_equity()
    risk.state.update_equity(equity)

    try:
        risk.check_drawdown()
    except DrawdownHalt as exc:
        logger.critical("Trading halted for the day: %s", exc)
        log_decision("PORTFOLIO", "HALT", 0.0, str(exc), equity=equity, drawdown_pct=risk.state.drawdown_pct)
        return

    # catch up on any state that's drifted from the broker (e.g. a previous
    # take-profit attempt that closed some legs but not others) before
    # making any new decisions off of it
    risk.reconcile_with_broker(executor)

    # --- Universe & pairs ---
    tickers = get_sp500_tickers(trading_client)
    prices = fetch_price_history(tickers, CONFIG.lookback_days, data_client)
    candidate_pairs = find_candidate_pairs(prices)

    # --- Evaluate each pair (open positions checked first for exits/stops, then fresh entries) ---
    for pair in candidate_pairs:
        key = f"{pair.ticker_a}/{pair.ticker_b}"
        open_position: OpenPositionInfo | None = risk.state.open_positions.get(key)
        result = evaluate_pair(prices, pair, open_position)

        log_decision(
            pair=key,
            action=result.action.value,
            zscore=result.current_zscore,
            reason=result.reason,
            equity=risk.state.current_equity,
            drawdown_pct=risk.state.drawdown_pct,
        )

        if result.action in (Action.EXIT_TARGET, Action.STOP_LOSS_Z, Action.STOP_LOSS_TIME):
            _close_pair_positions(executor, key, open_position, result.reason, risk)

        elif result.action in (Action.ENTER_LONG_SPREAD, Action.ENTER_SHORT_SPREAD):
            # pass `pair` so the risk manager can block this entry if either
            # ticker is already committed to a different open pair (e.g.
            # CBRE showing up across CBRE/ROP, CBRE/IQV, and CBRE/LH at once,
            # which silently tripled intended exposure to that one name)
            can_open, why_not = risk.can_open_new_position(pair)
            if not can_open:
                logger.info("Skipping entry for %s: %s", key, why_not)
                continue

            trade_dollars = risk.position_size_dollars()
            price_a = float(prices[pair.ticker_a].iloc[-1])
            price_b = float(prices[pair.ticker_b].iloc[-1])
            qty_a = max(1, int(trade_dollars / 2 / (price_a * 100)))  # rough options contract sizing
            qty_b = max(1, int(trade_dollars / 2 / (price_b * 100)))

            spreads = build_spreads_for_signal(
                result.action, pair.ticker_a, pair.ticker_b, price_a, price_b, qty_a, qty_b, today,
            )

            try:
                total_premium = 0.0
                all_legs_info = []
                for spread in spreads:
                    resolved = executor.resolve_option_contracts(spread)
                    total_premium += executor.get_spread_close_proceeds(resolved)
                    executor.place_spread_order(resolved)
                    all_legs_info.extend(
                        {"symbol": leg.option_symbol, "side": leg.side, "qty": leg.qty} for leg in resolved.legs
                    )
            except ValueError as exc:
                logger.warning("Skipping %s due to options resolution failure: %s", key, exc)
                continue
            except APIError as exc:
                logger.warning("Skipping %s due to order rejection: %s", key, exc)
                continue

            # Don't trust our own accounting - confirm every leg actually
            # filled at the broker before recording the position as open.
            # This is what caused the CF/CSX 404: a leg got written into
            # portfolio_state even though it was never actually filled.
            missing_legs = executor.verify_legs_filled(all_legs_info)
            if missing_legs:
                logger.error(
                    "Entry for %s: %d leg(s) never confirmed filled at the broker: %s. "
                    "Unwinding any legs that did fill and skipping this entry.",
                    key, len(missing_legs), [leg["symbol"] for leg in missing_legs],
                )
                filled_legs = [leg for leg in all_legs_info if leg not in missing_legs]
                if filled_legs:
                    try:
                        executor.close_spread_legs(filled_legs)
                    except APIError as exc:
                        logger.critical(
                            "Failed to unwind partially-filled legs for %s - "
                            "MANUAL INTERVENTION NEEDED: %s", key, exc,
                        )
                continue

            risk.register_entry(
                key,
                OpenPositionInfo(
                    pair=pair,
                    direction=result.action,
                    entry_date=today,
                    entry_zscore=result.current_zscore,
                    entry_premium=abs(total_premium),
                    option_symbols=all_legs_info,
                ),
            )

    logger.info("=== Daily cycle complete: Day %d ===", day_number)
    risk.state.save()


def risk_check_loop() -> None:
    """
    Lightweight check meant to run more frequently than the daily cycle
    (see CONFIG.risk_check_frequency_minutes). Checks portfolio drawdown
    AND take-profit on each open position, since options can move fast
    enough within a day that waiting for the next daily cycle could miss
    a good exit.
    """
    trading_client, data_client = build_clients()
    executor = AlpacaExecutor(trading_client, data_client)
    risk = RiskManager(state=PortfolioState.load())

    equity = executor.get_account_equity()
    risk.state.update_equity(equity)

    try:
        risk.check_drawdown()
        logger.info("Risk check OK. Drawdown: %.2f%%", risk.state.drawdown_pct * 100)
    except DrawdownHalt as exc:
        logger.critical("DRAWDOWN HALT TRIGGERED: %s", exc)
        # In production this would also trigger closing/hedging open positions.

    # clean up any state left over from a previous partial close (like the
    # CF/CSX situation) before evaluating take-profit on it again
    risk.reconcile_with_broker(executor)

    # Take-profit check on each open position
    for key, position in list(risk.state.open_positions.items()):
        if not position.option_symbols:
            continue
        try:
            close_proceeds = 0.0
            for leg_info in position.option_symbols:
                mid = executor.get_option_mid_price(leg_info["symbol"])
                if leg_info["side"] == "buy":
                    close_proceeds += mid * leg_info["qty"] * 100
                else:
                    close_proceeds -= mid * leg_info["qty"] * 100

            if risk.check_take_profit(key, close_proceeds):
                _close_pair_positions(
                    executor, key, position,
                    f"take-profit target ({CONFIG.take_profit_pct:.0%}) reached", risk,
                )
        except Exception as exc:
            logger.warning("Take-profit check failed for %s: %s", key, exc)

    risk.state.save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--risk-check", action="store_true", help="Run only the fast drawdown/risk check")
    parser.add_argument("--day", type=int, default=None, help="Day number of the competition (auto-calculated if omitted)")
    args = parser.parse_args()

    day_number = args.day if args.day is not None else current_competition_day()

    try:
        if args.risk_check:
            risk_check_loop()
        else:
            daily_cycle(day_number)
    except Exception:
        logger.exception("Unhandled exception - run did not complete")
        raise
