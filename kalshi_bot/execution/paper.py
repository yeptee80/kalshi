
"""Paper-only Kalshi execution.

Simulates entries, resting limit bids, fills, quick-profit
exits, fees, cash, and equity. Never submits exchange orders.
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
QUICK_PROFIT_TARGET = 0.20
MIN_CONTRACTS = 1.0


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
    """Simulated order executor with optional quick exits."""

    def __init__(
        self,
        client: KalshiClient,
        conn: sqlite3.Connection,
        starting_cash: float = 1000.0,
        profit_target: float = QUICK_PROFIT_TARGET,
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

    def _new_id(self) -> str:
        return f"paper-{next(self._oid)}"

    def _position(self, ticker: str) -> dict | None:
        return state.get_positions(
            self.conn, MODE
        ).get(ticker)

    def _reserved_cash(self) -> float:
        """Estimated cash needed by resting bids."""
        total = 0.0

        for order in self.resting:
            price = (
                order.limit_price
                if order.side == "yes"
                else 1.0 - order.limit_price
            )
            total += (
                order.contracts * price
                + taker_fee(price, order.contracts)
            )

        return total

    def _can_afford(
        self,
        cost: float,
        exclude_order: RestingOrder | None = None,
    ) -> bool:
        reserved = self._reserved_cash()

        if exclude_order is not None:
            price = (
                exclude_order.limit_price
                if exclude_order.side == "yes"
                else 1.0 - exclude_order.limit_price
            )
            reserved -= (
                exclude_order.contracts * price
                + taker_fee(
                    price, exclude_order.contracts
                )
            )

        return cost <= max(
            0.0, self.cash - reserved
        )

    async def execute(
        self,
        sig: Signal,
        contracts: float,
        book: Book | None = None,
    ) -> PaperFill | None:
        """Simulate a buy or park a limit bid."""

        if (
            contracts < MIN_CONTRACTS
            or sig.side not in ("yes", "no")
        ):
            return None

        if book is None:
            book = parse_orderbook(
                await self.client.get_orderbook(
                    sig.ticker
                )
            )

        if sig.side == "yes":
            level = (
                book.asks[0] if book.asks else None
            )
            marketable = (
                level is not None
                and (
                    sig.limit_price is None
                    or level[0] <= sig.limit_price
                )
            )
        else:
            level = (
                book.bids[0] if book.bids else None
            )
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

            if qty < MIN_CONTRACTS:
                return None

            side_price = (
                yes_price
                if sig.side == "yes"
                else 1.0 - yes_price
            )
            fee = taker_fee(side_price, qty)
            cost = qty * side_price + fee

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

        side_price = (
            sig.limit_price
            if sig.side == "yes"
            else 1.0 - sig.limit_price
        )

        estimated_cost = (
            contracts * side_price
            + taker_fee(side_price, contracts)
        )

        if not self._can_afford(estimated_cost):
            return None

        order = RestingOrder(
            order_id=(
                f"paper-rest-{next(self._oid)}"
            ),
            ticker=sig.ticker,
            side=sig.side,
            contracts=contracts,
            limit_price=sig.limit_price,
            strategy=sig.strategy,
        )

        self.resting.append(order)

        return PaperFill(
            ticker=sig.ticker,
            side=sig.side,
            contracts=contracts,
            price=0.0,
            yes_price=sig.limit_price,
            fee=0.0,
            resting=True,
        )

    def check_resting(
        self,
        books: dict[str, Book],
    ) -> list[PaperFill]:
        """Simulate fills for resting limit orders."""

        fills = []
        remaining_orders = []

        for order in self.resting:
            book = books.get(order.ticker)

            if book is None:
                remaining_orders.append(order)
                continue

            if order.side == "yes":
                level = (
                    book.asks[0] if book.asks else None
                )
                crossed = (
                    level is not None
                    and level[0] <= order.limit_price
                )
            else:
                level = (
                    book.bids[0] if book.bids else None
                )
                crossed = (
                    level is not None
                    and level[0] >= order.limit_price
                )

            if not crossed:
                remaining_orders.append(order)
                continue

            _, available = level
            qty = min(
                order.contracts, available
            )

            if qty < MIN_CONTRACTS:
                remaining_orders.append(order)
                continue

            yes_price = order.limit_price
            side_price = (
                yes_price
                if order.side == "yes"
                else 1.0 - yes_price
            )

            fee = maker_fee(side_price, qty)
            cost = qty * side_price + fee

            if not self._can_afford(
                cost, exclude_order=order
            ):
                remaining_orders.append(order)
                continue

            fills.append(
                self._fill(
                    order.ticker,
                    order.side,
                    qty,
                    yes_price,
                    order.strategy,
                    taker=False,
                    order_id=order.order_id,
                )
            )

            leftover = order.contracts - qty

            if leftover >= MIN_CONTRACTS:
                order.contracts = leftover
                remaining_orders.append(order)

        self.resting = remaining_orders
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
        """Record a simulated opening purchase."""

        side_price = (
            yes_price
            if side == "yes"
            else round(1.0 - yes_price, 4)
        )

        fee_fn = (
            taker_fee if taker else maker_fee
        )
        fee = fee_fn(side_price, qty)

        self.cash -= (
            qty * side_price + fee
        )

        state.record_fill(
            self.conn,
            MODE,
            strategy,
            ticker,
            side,
            qty,
            side_price,
            fee,
            order_id=(
                order_id or self._new_id()
            ),
        )

        signed = (
            qty if side == "yes" else -qty
        )

        state.apply_fill_to_position(
            self.conn,
            MODE,
            ticker,
            signed,
            yes_price,
            strategy,
        )

        return PaperFill(
            ticker=ticker,
            side=side,
            contracts=qty,
            price=side_price,
            yes_price=yes_price,
            fee=fee,
        )

    def quick_profit_opportunities(
        self,
        books: dict[str, Book],
    ) -> list[dict]:
        """Find positions with sufficient estimated profit.

        Uses executable bid prices and available size,
        not the midpoint or last traded price.
        """

        opportunities = []
        positions = state.get_positions(
            self.conn, MODE
        )

        for ticker, position in positions.items():
            signed_qty = float(
                position["contracts"]
            )

            if abs(signed_qty) < MIN_CONTRACTS:
                continue

            book = books.get(ticker)

            if book is None:
                continue

            is_yes = signed_qty > 0

            if is_yes:
                if not book.bids:
                    continue

                yes_price, available = (
                    book.bids[0]
                )
                entry_price = float(
                    position["avg_price"]
                )
                exit_price = yes_price

            else:
                if not book.asks:
                    continue

                yes_price, available = (
                    book.asks[0]
                )
                entry_price = (
                    1.0
                    - float(
                        position["avg_price"]
                    )
                )
                exit_price = (
                    1.0 - yes_price
                )

            qty = min(
                abs(signed_qty), available
            )

            if qty < MIN_CONTRACTS:
                continue

            entry_fee = taker_fee(
                entry_price, qty
            )
            exit_fee = taker_fee(
                exit_price, qty
            )

            net_profit = (
                (exit_price - entry_price) * qty
                - entry_fee
                - exit_fee
            )

            net_per_contract = (
                net_profit / qty
            )

            if (
                net_per_contract
                < self.profit_target
            ):
                continue

            opportunities.append({
                "ticker": ticker,
                "side": (
                    "yes" if is_yes else "no"
                ),
                "contracts": qty,
                "yes_price": yes_price,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "exit_fee": exit_fee,
                "estimated_net_profit": net_profit,
                "profit_per_contract": (
                    net_per_contract
                ),
                "strategy": (
                    position.get("strategy")
                    or "quick_profit"
                ),
            })

        return opportunities

    def take_quick_profits(
        self,
        books: dict[str, Book],
    ) -> list[dict]:
        """Simulate early exits on profitable positions.

        Closing a YES position means selling YES.
        Closing a NO position means selling NO.

        These are accounting operations only.
        """

        closed = []

        for item in self.quick_profit_opportunities(
            books
        ):
            ticker = item["ticker"]
            qty = item["contracts"]

            current = self._position(ticker)

            if current is None:
                continue

            signed_qty = float(
                current["contracts"]
            )

            if abs(signed_qty) < qty:
                continue

            yes_price = item["yes_price"]
            exit_price = item["exit_price"]
            exit_fee = item["exit_fee"]

            proceeds = (
                qty * exit_price - exit_fee
            )

            self.cash += proceeds

            closing_side = (
                "no"
                if signed_qty > 0
                else "yes"
            )

            signed_change = (
                -qty
                if signed_qty > 0
                else qty
            )

            strategy = (
                item["strategy"]
                + "_quick_exit"
            )

            state.record_fill(
                self.conn,
                MODE,
                strategy,
                ticker,
                closing_side,
                qty,
                exit_price,
                exit_fee,
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
        """Record simulated account equity."""

        value = 0.0

        positions = state.get_positions(
            self.conn, MODE
        )

        for ticker, position in positions.items():
            qty = float(
                position["contracts"]
            )
            avg = float(
                position["avg_price"]
            )

            mark = marks.get(ticker)

            if mark is None:
                mark = avg

            if qty >= 0:
                value += qty * mark
            else:
                value += (
                    -qty * (1.0 - mark)
                )

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
