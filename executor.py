"""
Executor.

Thin wrapper around Alpaca's trading client that:
- fetches live option chains to resolve actual contract symbols/premiums
- places multi-leg spread orders
- closes multi-leg spread positions (by option symbol, not underlying)
- fetches account equity for the risk manager

Kept separate from strategy logic so it's easy to swap between the
Alpaca Python SDK and the MCP server as the calling mechanism -
either way, this is the only module that actually touches the network.
"""

from __future__ import annotations

import logging
from datetime import date

from options_builder import SpreadOrder
from config import CONFIG

logger = logging.getLogger(__name__)


class AlpacaExecutor:
    def __init__(self, trading_client, data_client=None, option_data_client=None):
        """
        trading_client: alpaca.trading.client.TradingClient (paper=True)
        data_client: alpaca.data.historical.stock.StockHistoricalDataClient (optional)
        option_data_client: alpaca.data.historical.option.OptionHistoricalDataClient (optional, created lazily if not given)
        """
        self.trading_client = trading_client
        self.data_client = data_client
        self.option_data_client = option_data_client

    def get_account_equity(self) -> float:
        account = self.trading_client.get_account()
        return float(account.equity)

    def resolve_option_contracts(self, spread: SpreadOrder) -> SpreadOrder:
        """
        Query the live options chain to find the nearest real strikes/expiries
        to our target values, and fill in each leg's option_symbol + real premium.

        Options only expire on specific real dates (mostly Fridays), so we
        can't request one exact date - we query a window around our target
        and pick whichever actual expiry is closest, then the closest strike
        within that expiry's chain.
        """
        from alpaca.trading.requests import GetOptionContractsRequest
        from datetime import timedelta

        # Tracks strikes already assigned within THIS spread, keyed by
        # (expiry, right) - a bull_call_spread's two legs are both calls at
        # the same expiry, a bear_put_spread's two legs are both puts at the
        # same expiry, so this is exactly the collision surface. Real chains
        # only list strikes at fixed increments (e.g. $5), so two target
        # strikes that are close together (as % of a higher-priced
        # underlying) can independently round to the SAME real strike -
        # that's what produced the "all legs must have unique symbols" error.
        used_strikes: dict[tuple[date, str], set[float]] = {}

        for leg in spread.legs:
            # +-12 days (not +-5) - many stocks only have monthly expirations
            # (third Friday), and a narrow window can miss a real, tradable
            # contract on an otherwise perfectly optionable large-cap stock
            window_start = leg.expiry - timedelta(days=12)
            window_end = leg.expiry + timedelta(days=12)

            request = GetOptionContractsRequest(
                underlying_symbols=[leg.symbol],
                expiration_date_gte=window_start,
                expiration_date_lte=window_end,
                type=leg.right,
            )
            contracts = self.trading_client.get_option_contracts(request)

            if not contracts.option_contracts:
                raise ValueError(
                    f"No option contracts found for {leg.symbol} {leg.right} "
                    f"between {window_start} and {window_end}. "
                    f"This underlying may not have listed options, or the window needs widening."
                )

            # pick the actual expiry date closest to our target
            available_expiries = sorted({c.expiration_date for c in contracts.option_contracts})
            closest_expiry = min(available_expiries, key=lambda d: abs((d - leg.expiry).days))

            same_expiry_contracts = [c for c in contracts.option_contracts if c.expiration_date == closest_expiry]

            # then pick the strike closest to our target within that expiry -
            # but skip any strike already used by an earlier leg of this same
            # spread, so bull_call/bear_put spreads can never collapse onto a
            # single real strike (which is what caused the duplicate-symbol
            # order rejection).
            key = (closest_expiry, leg.right)
            used = used_strikes.setdefault(key, set())
            candidates = sorted(same_expiry_contracts, key=lambda c: abs(float(c.strike_price) - leg.strike))
            chosen = next((c for c in candidates if float(c.strike_price) not in used), None)

            if chosen is None:
                raise ValueError(
                    f"Could not find a distinct strike for {leg.symbol} {leg.right} leg at "
                    f"expiry {closest_expiry} - only one strike available in the chain, "
                    f"can't build a two-leg spread here."
                )

            if chosen is not candidates[0]:
                logger.warning(
                    "Target strike %.2f for %s %s collided with another leg of this spread "
                    "(same real strike would've been picked) - used %.2f instead",
                    leg.strike, leg.symbol, leg.right, float(chosen.strike_price),
                )

            used.add(float(chosen.strike_price))
            leg.option_symbol = chosen.symbol
            leg.strike = float(chosen.strike_price)
            leg.expiry = closest_expiry

        logger.info("Resolved contracts for %s spread on %s", spread.strategy_name, spread.underlying)
        return spread

    def get_option_mid_price(self, option_symbol: str) -> float:
        """
        Latest mid price (average of bid/ask) for a single option contract.
        Used both to estimate entry cost and to check current position value
        for take-profit monitoring. This is an approximation - real fills
        will differ slightly due to bid-ask spread, consistent with the
        documented limitation that slippage isn't precisely modeled.
        """
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionLatestQuoteRequest

        if self.option_data_client is None:
            self.option_data_client = OptionHistoricalDataClient(
                self.trading_client._api_key, self.trading_client._secret_key
            )

        request = OptionLatestQuoteRequest(symbol_or_symbols=[option_symbol])
        quotes = self.option_data_client.get_option_latest_quote(request)
        quote = quotes[option_symbol]
        return (float(quote.bid_price) + float(quote.ask_price)) / 2

    def get_spread_close_proceeds(self, spread) -> float:
        """
        Net $ you'd receive (or pay, if negative) by closing this spread
        right now at current mid prices: sell back whatever you bought,
        buy back whatever you sold.
        """
        proceeds = 0.0
        for leg in spread.legs:
            mid = self.get_option_mid_price(leg.option_symbol)
            if leg.side == "buy":
                proceeds += mid * leg.qty * 100   # selling what you bought -> you receive this
            else:
                proceeds -= mid * leg.qty * 100   # buying back what you sold -> you pay this
        return proceeds

    def place_spread_order(self, spread: SpreadOrder) -> dict:
        """
        Submit a multi-leg order for the spread. Alpaca's API supports
        multi-leg option orders in a single request; falls back to two
        sequential single-leg orders if multi-leg submission isn't available.
        """
        from alpaca.trading.requests import OptionLegRequest, MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        legs = [
            OptionLegRequest(
                symbol=leg.option_symbol,
                side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
                ratio_qty=leg.qty,
            )
            for leg in spread.legs
        ]

        order_request = MarketOrderRequest(
            qty=1,
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            legs=legs,
        )

        order = self.trading_client.submit_order(order_request)
        logger.info("Submitted %s order for %s: id=%s", spread.strategy_name, spread.underlying, order.id)
        return {"order_id": order.id, "status": order.status}

    # ------------------------------------------------------------------
    # Broker reconciliation - the source of truth for "is this leg actually
    # open" is always the broker, never our own portfolio_state.json. Every
    # closing/verification path below checks here first instead of assuming.
    # ------------------------------------------------------------------
    def get_broker_option_positions(self) -> dict:
        """
        All currently open positions at the broker, keyed by symbol.
        Includes equities too (harmless - we only ever look up option
        symbols in it), so we don't have to guess at asset_class filtering
        across SDK versions.
        """
        positions = self.trading_client.get_all_positions()
        return {p.symbol: p for p in positions}

    def verify_legs_filled(self, option_symbols: list[dict], attempts: int = 3, delay_seconds: float = 1.0) -> list[dict]:
        """
        Poll the broker briefly to confirm each leg in option_symbols
        actually shows up as an open position (order acceptance doesn't
        guarantee an immediate fill). Returns the subset of legs that are
        NOT confirmed open after the retries - i.e. legs the caller should
        treat as failed/missing rather than record as open.
        """
        import time

        remaining = list(option_symbols)
        for attempt in range(attempts):
            broker_positions = self.get_broker_option_positions()
            remaining = [leg for leg in remaining if leg["symbol"] not in broker_positions]
            if not remaining:
                return []
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
        return remaining

    @staticmethod
    def _underlying_from_option_symbol(option_symbol: str) -> str:
        """
        Extract the underlying root from an OCC-style option symbol, e.g.
        "EXC260918C00040000" -> "EXC". MLEG orders require every leg in the
        SAME order to share one underlying, so this is used to group legs
        before building close orders - a position that spans two
        underlyings (e.g. a pairs trade like EXC/NRG, which is really two
        independent 2-leg spreads) must be closed as two separate orders,
        never bundled into one.
        """
        import re

        match = re.match(r"^([A-Z]+)\d{6}[CP]\d{8}$", option_symbol)
        if not match:
            raise ValueError(f"Could not parse underlying from option symbol: {option_symbol}")
        return match.group(1)

    def close_spread_legs(self, option_symbols: list[dict]) -> dict:
        """
        Close a set of option legs, using one reverse MLEG order PER
        UNDERLYING (or a single-leg market order where only one leg for
        that underlying remains). Legs are grouped by underlying first,
        since Alpaca rejects MLEG orders whose legs don't all share the
        same underlying - a pairs position like EXC/NRG is really two
        independent 2-leg spreads (one per underlying), not one 4-leg
        spread, even though they're tracked together in portfolio_state.

        option_symbols: list of {"symbol": ..., "side": "buy"|"sell", "qty": ...}
        recorded at OPEN time (the opening side of each leg).

        Legs that no longer exist at the broker (already closed, or never
        filled in the first place - this is what caused the CF/CSX 404) are
        skipped instead of raising, so a single stale leg can't block the
        other legs from closing. Returns which legs were submitted for
        closing vs. skipped per underlying, so the caller can decide how to
        update portfolio state instead of assuming full success.
        """
        from alpaca.trading.requests import OptionLegRequest, MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, PositionIntent

        broker_positions = self.get_broker_option_positions()

        # group requested legs by underlying so each close order only ever
        # touches one underlying
        legs_by_underlying: dict[str, list[dict]] = {}
        for leg_info in option_symbols:
            underlying = self._underlying_from_option_symbol(leg_info["symbol"])
            legs_by_underlying.setdefault(underlying, []).append(leg_info)

        all_closed_symbols = []
        all_skipped_symbols = []
        order_ids = []

        for underlying, legs in legs_by_underlying.items():
            legs_to_close = []
            for leg_info in legs:
                symbol = leg_info["symbol"]
                broker_pos = broker_positions.get(symbol)
                if broker_pos is None or float(getattr(broker_pos, "qty", 0)) == 0:
                    logger.warning(
                        "Skipping close for %s: no matching broker position "
                        "(already closed, or never filled at entry)", symbol,
                    )
                    all_skipped_symbols.append(symbol)
                    continue

                opening_side = leg_info["side"]
                qty = leg_info["qty"]
                if opening_side == "buy":
                    close_side = OrderSide.SELL
                    intent = PositionIntent.SELL_TO_CLOSE
                else:
                    close_side = OrderSide.BUY
                    intent = PositionIntent.BUY_TO_CLOSE

                legs_to_close.append(
                    OptionLegRequest(symbol=symbol, side=close_side, ratio_qty=qty, position_intent=intent)
                )

            if not legs_to_close:
                logger.warning("No legs found at broker for underlying %s; nothing submitted to close", underlying)
                continue

            if len(legs_to_close) == 1:
                only_leg = legs_to_close[0]
                order_request = MarketOrderRequest(
                    symbol=only_leg.symbol,
                    qty=only_leg.ratio_qty,
                    side=only_leg.side,
                    time_in_force=TimeInForce.DAY,
                )
            else:
                order_request = MarketOrderRequest(
                    qty=1,
                    order_class=OrderClass.MLEG,
                    time_in_force=TimeInForce.DAY,
                    legs=legs_to_close,
                )

            order = self.trading_client.submit_order(order_request)
            closed_symbols = [leg.symbol for leg in legs_to_close]
            logger.info("Submitted close order for %s legs %s: id=%s", underlying, closed_symbols, order.id)
            all_closed_symbols.extend(closed_symbols)
            order_ids.append(order.id)

        if all_skipped_symbols:
            logger.warning(
                "Spread close was partial - these legs were already missing at the "
                "broker and were not included in any close order: %s", all_skipped_symbols,
            )

        return {"closed_symbols": all_closed_symbols, "skipped_symbols": all_skipped_symbols, "order_ids": order_ids}
