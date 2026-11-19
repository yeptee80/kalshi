
"""Kalshi paper-only trade execution.

Simulates:
- Marketable entries
- Resting limit bids
- Position accounting
- Fees and cash balances
- Trailing-profit exits

No real orders are submitted.
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.backtest.fees import maker_fee, taker_fee
from kalshi_bot.execution import state
from kalshi_bot.execution.util import Book, parse_orderbook
from kalshi_bot.strategies.base import Signal


MODE = "paper"

PROFIT_ACTIVATION = 0.20
PROFIT_TRAIL = 0.05
MIN_EXIT_PROFIT = 0.01


@dataclass
class RestingOrder:
    order_id: str
    ticker: str
    side: str
    contracts: float
    limit_price: float
    strategy: str


@dataclass
class PaperFill:
    ticker: str
    side: str
    contracts: float
    price: float
    yes_price: float
    fee: float
    resting: bool = False


class PaperExecutor:

    def __init__(
        self,
        client: KalshiClient,
        conn: sqlite3.Connection,
        starting_cash: float = 1000.0,
        profit_target: float = PROFIT_ACTIVATION,
    ):
        self.client = client
        self.conn = conn
        self.mode = MODE

        last = state.latest_equity(conn, MODE)
        self.cash = (
            float(last["cash"])
            if last
            else float(starting_cash)
        )

        self.resting: list[RestingOrder] = []
        self._oid = itertools.count(1)

        self.profit_target = max(
            0.01, float(profit_target)
        )

        # Tracks the highest estimated net profit
        # per contract for each open position.
        self.profit_peaks: dict[str, float] = {}

    def _new_id(self) -> str:
        return f"paper-{next(self._oid)}"

    def _reserved_cash(
        self,
        exclude: RestingOrder | None = None,
    ) -> float:
        reserved = 0.0

        for order in self.resting:
            if order is exclude:
                continue

            price = (
                order.limit_price
                if order.side == "yes"
                else 1.0 - order.limit_price
            )

            reserved += (
                order.contracts * price
                + taker_fee(
                    price, order.contracts
                )
            )

        return reserved

    def _can_afford(
        self,
        cost: float,
        exclude: RestingOrder | None = None,
    ) -> bool:
        return cost <= max(
            0.0,
            self.cash - self._reserved_cash(exclude),
        )

    async def execute(
        self,
        sig: Signal,
        contracts: float,
        book: Book | None = None,
    ) -> PaperFill | None:

        if contracts < 1:
            return None

        if sig.side not in ("yes", "no"):
            return None

        if book is None:
            book = parse_orderbook(
                await self.client.get_orderbook(
                    sig.ticker
                )
            )

        if sig.side == "yes":
            level = book.asks[0] if book.asks else None
            marketable = (
                level is not None
                and (
                    sig.limit_price is None
                    or level[0] <= sig.limit_price
                )
            )
        else:
            level = book.bids[0] if book.bids else None
            marketable = (
                level is not None
                and (
                    sig.limit_price is None
                    or level[0] >= sig.limit_price
                )
            )

        if marketable:
            yes_price, available = level
            qty = min(contracts, available)

            if qty < 1:
                return None

            price = (
                yes_price
                if sig.side == "yes"
                else 1.0 - yes_price
            )

            cost = (
                qty * price
                + taker_fee(price, qty)
            )

            if not self._can_afford(cost):
                return None

            return self._fill(
                sig.ticker,
                sig.side,
                qty,
                yes_price,
                sig.strategy,
                taker=True,
            )

        if sig.limit_price is None:
            return None

        if not 0 < sig.limit_price < 1:
            return None

        price = (
            sig.limit_price
            if sig.side == "yes"
            else 1.0 - sig.limit_price
        )

        cost = (
            contracts * price
            + taker_fee(price, contracts)
        )

        if not self._can_afford(cost):
            return None

        order = RestingOrder(
            order_id=f"paper-rest-{next(self._oid)}",
            ticker=sig.ticker,
            side=sig.side,
            contracts=contracts,
            limit_price=sig.limit_price,
            strategy=sig.strategy,
        )

        self.resting.append(order)

        return PaperFill(
            sig.ticker,
            sig.side,
            contracts,
            0.0,
            sig.limit_price,
            0.0,
            resting=True,
        )

    def check_resting(
        self,
        books: dict[str, Book],
    ) -> list[PaperFill]:

        fills = []
        remaining = []

        for order in self.resting:
            book = books.get(order.ticker)

            if book is None:
                remaining.append(order)
                continue

            if order.side == "yes":
                level = book.asks[0] if book.asks else None
                crossed = (
                    level is not None
                    and level[0] <= order.limit_price
                )
            else:
                level = book.bids[0] if book.bids else None
                crossed = (
                    level is not None
                    and level[0] >= order.limit_price
                )

            if not crossed:
                remaining.append(order)
                continue

            _, available = level
            qty = min(order.contracts, available)

            if qty < 1:
                remaining.append(order)
                continue

            price = (
                order.limit_price
                if order.side == "yes"
                else 1.0 - order.limit_price
            )

            cost = (
                qty * price
                + maker_fee(price, qty)
            )

            if not self._can_afford(
                cost, exclude=order
            ):
                remaining.append(order)
                continue

            fills.append(
                self._fill(
                    order.ticker,
                    order.side,
                    qty,
                    order.limit_price,
                    order.strategy,
                    taker=False,
                    order_id=order.order_id,
                )
            )

            leftover = order.contracts - qty

            if leftover >= 1:
                order.contracts = leftover
                remaining.append(order)

        self.resting = remaining
        return fills

    def _fill(
        self,
        ticker: str,
        side: str,
        qty: float,
        yes_price: float,
        strategy: str,
        taker: bool,
        order_id: str = "",
    ) -> PaperFill:

        price = (
            yes_price
            if side == "yes"
            else round(1.0 - yes_price, 4)
        )

        fee = (
            taker_fee(price, qty)
            if taker
            else maker_fee(price, qty)
        )

        self.cash -= qty * price + fee

        state.record_fill(
            self.conn,
            MODE,
            strategy,
            ticker,
            side,
            qty,
            price,
            fee,
            order_id=order_id or self._new_id(),
        )

        signed_qty = qty if side == "yes" else -qty

        state.apply_fill_to_position(
            self.conn,
            MODE,
            ticker,
            signed_qty,
            yes_price,
            strategy,
        )

        # A new entry changes the average cost basis.
        self.profit_peaks.pop(ticker, None)

        return PaperFill(
            ticker,
            side,
            qty,
            price,
            yes_price,
            fee,
        )

    def _exit_quote(
        self,
        ticker: str,
        position: dict,
        book: Book,
    ) -> dict | None:

        signed_qty = float(position["contracts"])

        if signed_qty == 0:
            return None

        is_yes = signed_qty > 0

        if is_yes:
            if not book.bids:
                return None

            yes_price, available = book.bids[0]
            exit_price = yes_price
            entry_price = float(
                position["avg_price"]
            )

        else:
            if not book.asks:
                return None

            yes_price, available = book.asks[0]
            exit_price = 1.0 - yes_price
            entry_price = (
                1.0 - float(position["avg_price"])
            )

        qty = min(abs(signed_qty), available)

        if qty < 1:
            return None

        # Estimate both sides' taker fees.
        entry_fee = taker_fee(entry_price, qty)
        exit_fee = taker_fee(exit_price, qty)

        net_profit = (
            (exit_price - entry_price) * qty
            - entry_fee
            - exit_fee
        )

        return {
            "ticker": ticker,
            "side": "yes" if is_yes else "no",
            "contracts": qty,
            "yes_price": yes_price,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_fee": exit_fee,
            "estimated_net_profit": net_profit,
            "profit_per_contract": net_profit / qty,
            "strategy": (
                position.get("strategy")
                or "quick_profit"
            ),
        }

    def quick_profit_opportunities(
        self,
        books: dict[str, Book],
    ) -> list[dict]:
        """Find trailing-profit exits.

        Start trailing at 20 cents net per contract.
        Hold while profit makes new highs.
        Exit after a 5-cent decline from the peak,
        if the estimated exit remains profitable.
        """

        opportunities = []
        positions = state.get_positions(
            self.conn, MODE
        )

        for ticker in list(self.profit_peaks):
            if ticker not in positions:
                self.profit_peaks.pop(ticker, None)

        for ticker, position in positions.items():
            book = books.get(ticker)

            if book is None:
                continue

            quote = self._exit_quote(
                ticker, position, book
            )

            if quote is None:
                continue

            current_profit = quote[
                "profit_per_contract"
            ]

            old_peak = self.profit_peaks.get(
                ticker, float("-inf")
            )

            peak = max(old_peak, current_profit)

            if peak >= self.profit_target:
                self.profit_peaks[ticker] = peak

            if peak < self.profit_target:
                continue

            decline = peak - current_profit

            if (
                decline >= PROFIT_TRAIL
                and current_profit >= MIN_EXIT_PROFIT
            ):
                quote["peak_profit_per_contract"] = peak
                quote["trailing_decline"] = decline
                opportunities.append(quote)

        return opportunities

    def take_quick_profits(
        self,
        books: dict[str, Book],
    ) -> list[dict]:
        """Close qualifying simulated positions only."""

        closed = []

        for item in self.quick_profit_opportunities(
            books
        ):
            ticker = item["ticker"]
            position = state.get_positions(
                self.conn, MODE
            ).get(ticker)

            if position is None:
                continue

            signed_qty = float(
                position["contracts"]
            )

            qty = item["contracts"]

            if abs(signed_qty) < qty:
                continue

            yes_price = item["yes_price"]
            exit_price = item["exit_price"]
            fee = item["exit_fee"]

            proceeds = qty * exit_price - fee

            self.cash += proceeds

            closing_side = (
                "no" if signed_qty > 0 else "yes"
            )

            signed_change = (
                -qty if signed_qty > 0 else qty
            )

            strategy = (
                item["strategy"] + "_trailing_exit"
            )

            state.record_fill(
                self.conn,
                MODE,
                strategy,
                ticker,
                closing_side,
                qty,
                exit_price,
                fee,
                order_id=self._new_id(),
            )

            state.apply_fill_to_position(
                self.conn,
                MODE,
                ticker,
                signed_change,
                yes_price,
                strategy,
            )

            self.profit_peaks.pop(ticker, None)

            closed.append({
                **item,
                "cash_proceeds": proceeds,
                "status": "paper_closed",
            })

        return closed

    def snapshot_equity(
        self,
        marks: dict[str, float],
    ) -> float:
        """Record simulated cash plus open positions."""

        value = 0.0

        for ticker, position in state.get_positions(
            self.conn, MODE
        ).items():
            qty = float(position["contracts"])
            avg = float(position["avg_price"])
            mark = marks.get(ticker, avg)

            if qty >= 0:
                value += qty * mark
            else:
                value += -qty * (1.0 - mark)

        exposure = state.total_exposure(
            self.conn, MODE
        )

        equity = self.cash + value

        state.record_equity(
            self.conn,
            MODE,
            self.cash,
            exposure,
            equity,
        )

        return equity
