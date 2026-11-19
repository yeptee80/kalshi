
"""Percentage-based risk checks for PAPER trading only."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from kalshi_bot.config import Settings
from kalshi_bot.execution import state
from kalshi_bot.execution.util import today_utc
from kalshi_bot.strategies.base import Signal

KELLY_FRACTION = 0.25
RESERVE_FRACTION = 0.20
TRADE_FRACTION = 0.03
TOTAL_FRACTION = 0.10
DAILY_LOSS_FRACTION = 0.10


@dataclass
class RiskDecision:
    allowed: bool
    contracts: float
    reason: str = ""


class RiskManager:
    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        mode: str,
        bankroll: float = 1000.0,
    ):
        self.settings = settings
        self.conn = conn
        self.mode = mode
        self.bankroll = float(bankroll)
        self.start_of_day_balance = float(bankroll)

    def kill_switch_on(self) -> bool:
        return (state.get_status(
            self.conn, "kill_switch", "off"
        ) or "off").lower() in ("on", "true", "1")

    def halt(self, reason: str) -> None:
        state.set_status(self.conn, "kill_switch", "on")
        state.set_status(self.conn, "halt_reason", reason)

    def _equity(self) -> float:
        snapshot = state.latest_equity(self.conn, self.mode)
        if snapshot is None:
            return self.bankroll
        return max(0.0, float(snapshot["equity"]))

    def _day_start(self) -> float:
        start = state.equity_at_day_start(
            self.conn, self.mode, today_utc()
        )
        if start is not None:
            return max(0.0, float(start))
        return self.start_of_day_balance

    def _limits(self) -> tuple[float, float, float]:
        balance = self._equity()
        available = max(
            0.0, balance * (1.0 - RESERVE_FRACTION)
        )
        return (
            available,
            available * TRADE_FRACTION,
            available * TOTAL_FRACTION,
        )

    def daily_loss_breached(self) -> bool:
        raw = state.get_status(
            self.conn, "daily_pnl", "0"
        ) or "0"
        try:
            daily_pnl = float(raw)
        except (ValueError, TypeError):
            self.halt("invalid daily P/L")
            return True
        return daily_pnl <= -(
            self._day_start() * DAILY_LOSS_FRACTION
        )

    def size(self, sig: Signal, price: float) -> float:
        if sig.side == "yes":
            p, f = price, sig.fair_value
        elif sig.side == "no":
            p, f = 1.0 - price, 1.0 - sig.fair_value
        else:
            return 0.0

        if not (0.0 < p < 1.0):
            return 0.0

        kelly = (f - p) / (1.0 - p)
        if kelly <= 0:
            return 0.0

        available, trade_cap, total_cap = self._limits()

        market_remaining = max(
            0.0,
            trade_cap - state.market_exposure(
                self.conn, self.mode, sig.ticker
            ),
        )
        total_remaining = max(
            0.0,
            total_cap - state.total_exposure(
                self.conn, self.mode
            ),
        )

        budget = min(
            KELLY_FRACTION * kelly * available,
            trade_cap,
            market_remaining,
            total_remaining,
        )

        if budget <= 0:
            return 0.0

        return float(
            min(sig.contracts, math.floor(budget / p))
        )

    def check(
        self, sig: Signal, price: float
    ) -> RiskDecision:
        if self.mode != "paper":
            return RiskDecision(
                False, 0.0, "paper trading only"
            )

        if self.kill_switch_on():
            return RiskDecision(
                False, 0.0,
                "kill_switch on: " +
                (state.get_status(
                    self.conn, "halt_reason", ""
                ) or ""),
            )

        if self.daily_loss_breached():
            self.halt(
                "daily loss limit "
                "(10% of start-of-day equity) breached"
            )
            return RiskDecision(
                False, 0.0, "daily loss halt"
            )

        if sig.side not in ("yes", "no"):
            return RiskDecision(
                False, 0.0, "invalid side"
            )

        unit_cost = (
            price if sig.side == "yes"
            else 1.0 - price
        )

        if not (0.0 < unit_cost < 1.0):
            return RiskDecision(
                False, 0.0, f"bad price {price}"
            )

        contracts = self.size(sig, price)
        if contracts < 1:
            return RiskDecision(
                False, 0.0, "sized to zero (edge/cap)"
            )

        cost = contracts * unit_cost
        _, trade_cap, total_cap = self._limits()

        market_exp = state.market_exposure(
            self.conn, self.mode, sig.ticker
        )
        total_exp = state.total_exposure(
            self.conn, self.mode
        )

        if market_exp + cost > trade_cap + 1e-9:
            return RiskDecision(
                False, 0.0, "3% per-market cap"
            )

        if total_exp + cost > total_cap + 1e-9:
            return RiskDecision(
                False, 0.0, "10% total exposure cap"
            )

        return RiskDecision(True, contracts, "ok")
