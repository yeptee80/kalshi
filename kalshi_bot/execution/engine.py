
"""Paper-only Kalshi execution engine.

Polls markets, evaluates strategies, checks risk, and records
simulated orders. Real-money and exchange-demo order execution
are disabled in this engine.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import logging
import signal as _signal
import sqlite3

from kalshi_bot.api.client import PROD_BASE, KalshiClient
from kalshi_bot.backtest.runner import load_context
from kalshi_bot.config import Settings, get_settings
from kalshi_bot.data.collector import collect_markets
from kalshi_bot.execution import state
from kalshi_bot.execution.paper import PaperExecutor
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.execution import settle
from kalshi_bot.execution.util import (
    Book,
    now_iso,
    parse_orderbook,
    today_utc,
)
from kalshi_bot.strategies import STRATEGIES
from kalshi_bot.strategies.base import MarketState, Strategy

log = logging.getLogger("kalshi_bot.engine")

DEFAULT_SERIES = ["KXHIGHNY"]


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        mode: str,
        strategies: list[Strategy],
        series: list[str],
        client: KalshiClient,
        executor,
        conn: sqlite3.Connection | None = None,
        interval: float = 60.0,
        bankroll: float = 1000.0,
    ):
        if mode != "paper":
            raise RuntimeError(
                "REFUSED: paper trading only"
            )

        if not isinstance(executor, PaperExecutor):
            raise RuntimeError(
                "REFUSED: PaperExecutor required"
            )

        self.settings = settings
        self.mode = "paper"
        self.strategies = strategies
        self.series = series
        self.client = client
        self.executor = executor
        self.conn = conn if conn is not None else state.connect()
        self.interval = interval

        self.risk = RiskManager(
            settings,
            self.conn,
            "paper",
            bankroll=bankroll,
        )

        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(
        self,
        max_seconds: float | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()

        for s in (_signal.SIGINT, _signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(
                    s, self.request_stop
                )

        deadline = (
            loop.time() + max_seconds
            if max_seconds is not None
            else None
        )

        state.set_status(
            self.conn, "engine_running", "true"
        )
        state.set_status(
            self.conn, "mode", "paper"
        )

        try:
            while not self._stop.is_set():
                try:
                    await self.cycle()
                except Exception:
                    log.exception("cycle failed")

                if (
                    deadline is not None
                    and loop.time() >= deadline
                ):
                    break

                with contextlib.suppress(
                    asyncio.TimeoutError
                ):
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.interval,
                    )
        finally:
            state.set_status(
                self.conn, "engine_running", "false"
            )
            log.info("paper engine stopped")

    async def cycle(self) -> None:
        now = dt.datetime.now(dt.UTC)

        state.set_status(
            self.conn, "last_heartbeat", now_iso()
        )
        state.set_status(
            self.conn, "engine_running", "true"
        )
        state.set_status(
            self.conn, "mode", "paper"
        )

        # Settle resolved simulated positions.
        try:
            settled = await settle.settle_open_positions(
                self.client,
                self.conn,
                "paper",
            )
            if settled:
                self.executor.cash += sum(
                    r.payout for r in settled
                )
                log.info(
                    "settled %d simulated positions",
                    len(settled),
                )
        except Exception:
            log.exception("settlement step failed")

        # Fetch open markets from configured series.
        markets: list[dict] = []

        for series_ticker in self.series:
            markets.extend(
                await collect_markets(
                    self.client,
                    self.conn,
                    series_ticker,
                    status="open",
                )
            )

        # Build market states from public orderbooks.
        books: dict[str, Book] = {}
        states: list[MarketState] = []
        marks: dict[str, float] = {}

        for market in markets:
            ticker = market["ticker"]

            try:
                book = parse_orderbook(
                    await self.client.get_orderbook(ticker)
                )
            except Exception:
                log.warning(
                    "orderbook fetch failed for %s",
                    ticker,
                )
                continue

            if book.empty:
                continue

            books[ticker] = book

            if book.best_bid is not None:
                marks[ticker] = book.best_bid

            states.append(
                MarketState(
                    ticker=ticker,
                    ts=now,
                    yes_bid=book.best_bid,
                    yes_ask=book.best_ask,
                    last=_f(
                        market.get("last_price_dollars")
                    ),
                    volume=float(
                        market.get("volume_fp")
                        or market.get("volume")
                        or 0
                    ),
                    open_interest=float(
                        market.get("open_interest_fp")
                        or market.get("open_interest")
                        or 0
                    ),
                    meta=market,
                )
            )

        # Check existing simulated resting orders.
        killed = self.risk.kill_switch_on()

        if not killed:
            for fill in self.executor.check_resting(books):
                log.info(
                    "paper resting fill: %s %s x%.1f",
                    fill.ticker,
                    fill.side,
                    fill.contracts,
                )

        # Update equity and daily P/L.
        equity = self.executor.snapshot_equity(marks)

        day_start = state.equity_at_day_start(
            self.conn,
            "paper",
            today_utc(),
        )

        daily_pnl = (
            equity - day_start
            if day_start is not None
            else 0.0
        )

        state.set_status(
            self.conn,
            "daily_pnl",
            f"{daily_pnl:.2f}",
        )

        # Update bankroll for dynamic position sizing.
        self.risk.bankroll = equity

        # Generate signals and simulate approved trades.
        ctx = load_context()

        ctx.positions = {
            ticker: position["contracts"]
            for ticker, position in state.get_positions(
                self.conn, "paper"
            ).items()
        }

        n_signals = 0
        n_acted = 0

        for market_state in states:
            book = books[market_state.ticker]

            for strategy in self.strategies:
                signals = strategy.generate_signals(
                    market_state, ctx
                )

                for sig in signals:
                    n_signals += 1

                    exec_price = (
                        book.best_ask
                        if sig.side == "yes"
                        else book.best_bid
                    )

                    acted = False

                    if (
                        killed
                        or self.risk.kill_switch_on()
                    ):
                        reason = "kill_switch on"

                    elif exec_price is None:
                        reason = "no opposing liquidity"

                    else:
                        decision = self.risk.check(
                            sig, exec_price
                        )
                        reason = decision.reason

                        if decision.allowed:
                            result = await self.executor.execute(
                                sig,
                                decision.contracts,
                                book,
                            )
                            acted = result is not None

                    state.record_signal(
                        self.conn,
                        sig.strategy,
                        sig.ticker,
                        sig.side,
                        sig.contracts,
                        sig.fair_value,
                        exec_price,
                        (
                            f"{sig.reason} "
                            f"| risk: {reason}"
                        ),
                        acted,
                    )

                    n_acted += int(acted)

        log.info(
            "paper cycle: %d markets, %d books, "
            "%d signals, %d simulated actions, "
            "equity=%.2f daily_pnl=%.2f%s",
            len(markets),
            len(books),
            n_signals,
            n_acted,
            equity,
            daily_pnl,
            (
                " [KILL SWITCH ON]"
                if self.risk.kill_switch_on()
                else ""
            ),
        )


def _f(value) -> float | None:
    try:
        return (
            float(value)
            if value is not None
            else None
        )
    except (TypeError, ValueError):
        return None


def build(
    args: argparse.Namespace,
    settings: Settings,
) -> ExecutionEngine:
    mode = args.mode or settings.trading_mode.value

    if mode != "paper":
        raise RuntimeError(
            "REFUSED: this engine supports "
            "paper trading only"
        )

    conn = state.connect()

    # Public market data only. No API credentials.
    client = KalshiClient(base_url=PROD_BASE)

    executor = PaperExecutor(
        client,
        conn,
        starting_cash=args.bankroll,
    )

    strategies = [
        STRATEGIES[name]()
        for name in args.strategies
    ]

    return ExecutionEngine(
        settings=settings,
        mode="paper",
        strategies=strategies,
        series=args.series,
        client=client,
        executor=executor,
        conn=conn,
        interval=args.interval,
        bankroll=args.bankroll,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s %(message)s"
        ),
    )

    parser = argparse.ArgumentParser(
        description="Kalshi paper-trading engine"
    )

    parser.add_argument(
        "--mode",
        choices=["paper"],
        default="paper",
    )

    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["expiry_convergence"],
        choices=sorted(STRATEGIES),
    )

    parser.add_argument(
        "--series",
        nargs="+",
        default=DEFAULT_SERIES,
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
    )

    parser.add_argument(
        "--bankroll",
        type=float,
        default=1000.0,
    )

    parser.add_argument(
        "--duration",
        type=float,
        default=None,
    )

    args = parser.parse_args()

    settings = get_settings()
    engine = build(args, settings)

    async def _run() -> None:
        try:
            await engine.run(
                max_seconds=args.duration
            )
        finally:
            await engine.client.aclose()
            engine.conn.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
