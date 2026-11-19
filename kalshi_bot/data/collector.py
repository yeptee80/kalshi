
"""Kalshi market collection and 15-minute market discovery.

Commands:
    python -m kalshi_bot.data.collector snapshot
    python -m kalshi_bot.data.collector discover-15m
    python -m kalshi_bot.data.collector backfill
    python -m kalshi_bot.data.collector backfill-historical

Public market data only. No real-money orders.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sqlite3

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.data.store import connect, dollars


WEATHER_SERIES = [
    "KXHIGHNY",
    "KXHIGHCHI",
    "KXHIGHMIA",
    "KXHIGHAUS",
    "KXHIGHDEN",
    "KXHIGHLAX",
    "KXHIGHPHIL",
]

ECON_SERIES = [
    "KXFED",
    "KXCPIYOY",
    "KXCPI",
    "KXPAYROLLS",
    "KXU3",
]

TARGET_SERIES = WEATHER_SERIES + ECON_SERIES

HIST_ECON_SERIES = ECON_SERIES + [
    "KXGDP",
    "KXJOBLESS",
]

WORLD_SERIES = [
    "KXVENEZDEFACTO",
    "KXISRAELPM",
    "KXNEXTNATOSECGEN",
    "KXPRESTAIWAN",
    "KXTAIWANLVL4",
    "KXUKRAINEEU",
    "KXZELENSKYPUTIN",
    "KXIRANDEMOCRACY",
]

SPORTS_CULTURE_SERIES = [
    "KXMLBGAME",
    "KXWNBAGAME",
    "KXATPMATCH",
    "KXUFCFIGHT",
    "KXNETFLIXRANKMOVIE",
    "KXALBUMEQUIV",
    "KXAPRPOTUS",
    "KXTRUTHSOCIAL",
]

CRYPTO_WORDS = (
    "bitcoin",
    "btc",
    "ethereum",
    "ether",
    "eth",
    "solana",
    "sol",
    "crypto",
)

FIFTEEN_MINUTE_WORDS = (
    "15 minute",
    "15-minute",
    "15 min",
    "15min",
    "15m",
    "quarter hour",
    "quarter-hour",
)


def _iso_to_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(
            dt.datetime.fromisoformat(
                iso.replace("Z", "+00:00")
            ).timestamp()
        )
    except (ValueError, TypeError):
        return None


def _duration_minutes(market: dict) -> float | None:
    """Duration between market open and close, if known."""
    start = _iso_to_ts(market.get("open_time"))
    end = _iso_to_ts(market.get("close_time"))

    if start is None or end is None or end <= start:
        return None

    return (end - start) / 60.0


def _market_text(market: dict) -> str:
    fields = (
        "ticker",
        "event_ticker",
        "title",
        "subtitle",
        "yes_sub_title",
        "no_sub_title",
    )
    return " ".join(
        str(market.get(field) or "")
        for field in fields
    ).lower()


def _series_text(series: dict) -> str:
    return " ".join(
        str(series.get(field) or "")
        for field in (
            "ticker",
            "title",
            "category",
            "frequency",
        )
    ).lower()


def is_crypto_market(market: dict) -> bool:
    text = _market_text(market)
    return any(word in text for word in CRYPTO_WORDS)


def is_fifteen_minute_market(
    market: dict,
    series: dict | None = None,
) -> bool:
    """Find explicitly labeled 15-minute markets.

    Opening-to-closing duration is not used by itself:
    some markets open well before their 15-minute
    observation window.
    """
    text = _market_text(market)

    if series:
        text += " " + _series_text(series)

    return any(
        word in text
        for word in FIFTEEN_MINUTE_WORDS
    )


def upsert_market(
    conn: sqlite3.Connection,
    m: dict,
    series_ticker: str,
) -> None:
    conn.execute(
        """INSERT INTO markets (
            ticker, event_ticker, series_ticker,
            title, subtitle, market_type,
            strike_type, floor_strike, cap_strike,
            open_time, close_time, expiration_time,
            status, result, last_price, volume,
            open_interest, liquidity, raw
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            status=excluded.status,
            result=excluded.result,
            last_price=excluded.last_price,
            volume=excluded.volume,
            open_interest=excluded.open_interest,
            liquidity=excluded.liquidity,
            raw=excluded.raw,
            fetched_at=datetime('now')""",
        (
            m["ticker"],
            m.get("event_ticker"),
            series_ticker,
            m.get("title"),
            m.get("yes_sub_title") or m.get("subtitle"),
            m.get("market_type"),
            m.get("strike_type"),
            m.get("floor_strike"),
            m.get("cap_strike"),
            m.get("open_time"),
            m.get("close_time"),
            m.get("expiration_time"),
            m.get("status"),
            m.get("result"),
            dollars(
                m.get("last_price_dollars")
                or m.get("last_price")
            ),
            float(
                m.get("volume_fp")
                or m.get("volume")
                or 0
            ),
            float(
                m.get("open_interest_fp")
                or m.get("open_interest")
                or 0
            ),
            (
                dollars(m.get("liquidity_dollars"))
                if m.get("liquidity_dollars")
                else m.get("liquidity")
            ),
            json.dumps(m),
        ),
    )


async def collect_markets(
    client: KalshiClient,
    conn: sqlite3.Connection,
    series_ticker: str,
    status: str | None = None,
) -> list[dict]:
    """Collect and store markets from one series."""
    out = []
    cursor = None

    while True:
        response = await client.get_markets(
            series_ticker=series_ticker,
            status=status,
            limit=1000,
            cursor=cursor,
        )

        markets = response.get("markets", [])

        for market in markets:
            upsert_market(
                conn, market, series_ticker
            )

        out.extend(markets)
        cursor = response.get("cursor")

        if not cursor or not markets:
            break

    conn.commit()
    return out


async def discover_fifteen_minute_markets(
    client: KalshiClient,
    conn: sqlite3.Connection | None = None,
    max_pages: int = 100,
) -> list[dict]:
    """Discover explicitly labeled 15-minute markets.

    Searches open markets across categories rather than
    relying on a fixed list of series tickers.

    This is a candidate scanner, not a guarantee of
    exhaustive coverage.
    """
    found = {}
    cursor = None
    pages = 0
    total_checked = 0

    try:
        series_list = await client.get_series_list()
    except Exception as exc:
        print(f"Series lookup unavailable: {exc}")
        series_list = []

    series_lookup = {
        s.get("ticker"): s
        for s in series_list
        if s.get("ticker")
    }

    while pages < max_pages:
        response = await client.get_markets(
            status="open",
            limit=1000,
            cursor=cursor,
        )

        markets = response.get("markets", [])
        pages += 1
        total_checked += len(markets)

        for market in markets:
            ticker = market.get("ticker")
            if not ticker:
                continue

            series_ticker = (
                market.get("series_ticker")
                or ticker.split("-")[0]
            )

            series = series_lookup.get(
                series_ticker, {}
            )

            if not is_fifteen_minute_market(
                market, series
            ):
                continue

            found[ticker] = market

            if conn is not None:
                upsert_market(
                    conn, market, series_ticker
                )

        cursor = response.get("cursor")

        if not cursor or not markets:
            break

    if conn is not None:
        conn.commit()

    results = list(found.values())

    results.sort(
        key=lambda m: (
            not is_crypto_market(m),
            -(float(
                m.get("volume_fp")
                or m.get("volume")
                or 0
            )),
        )
    )

    print(
        f"Checked {total_checked} open markets "
        f"across {pages} pages."
    )
    print(
        f"Found {len(results)} explicitly "
        f"labeled 15-minute markets."
    )

    if cursor:
        print(
            "Warning: page limit reached; "
            "discovery may be incomplete."
        )

    crypto_count = sum(
        is_crypto_market(m)
        for m in results
    )

    print(
        f"Crypto candidates: {crypto_count}"
    )
    print(
        f"Other candidates: "
        f"{len(results) - crypto_count}"
    )

    for market in results[:50]:
        category = (
            "CRYPTO"
            if is_crypto_market(market)
            else "OTHER"
        )

        print(
            f"[{category}] "
            f"{market.get('ticker')} | "
            f"{market.get('title', '')}"
        )

    return results


async def collect_candles(
    client: KalshiClient,
    conn: sqlite3.Connection,
    series_ticker: str,
    market: dict,
    period_minutes: int = 60,
) -> int:
    open_ts = _iso_to_ts(
        market.get("open_time")
    )
    close_ts = _iso_to_ts(
        market.get("close_time")
    )

    if not open_ts or not close_ts:
        return 0

    now_ts = int(
        dt.datetime.now(dt.UTC).timestamp()
    )
    end = min(close_ts, now_ts)

    try:
        candles = await client.get_candlesticks(
            series_ticker,
            market["ticker"],
            open_ts,
            end,
            period_minutes,
        )
    except Exception:
        return 0

    rows = []

    for candle in candles:
        price = candle.get("price", {})

        rows.append((
            market["ticker"],
            period_minutes,
            candle["end_period_ts"],
            dollars(price.get("open_dollars")),
            dollars(price.get("high_dollars")),
            dollars(price.get("low_dollars")),
            dollars(price.get("close_dollars")),
            dollars(price.get("mean_dollars")),
            dollars(
                (candle.get("yes_bid") or {})
                .get("close_dollars")
            ),
            dollars(
                (candle.get("yes_ask") or {})
                .get("close_dollars")
            ),
            float(
                candle.get("volume_fp")
                or candle.get("volume")
                or 0
            ),
            float(
                candle.get("open_interest_fp")
                or candle.get("open_interest")
                or 0
            ),
        ))

    conn.executemany(
        """INSERT OR REPLACE INTO candles (
            market_ticker, period_minutes, end_ts,
            open, high, low, close, mean,
            yes_bid_close, yes_ask_close,
            volume, open_interest
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )

    conn.commit()
    return len(rows)


async def backfill(
    series_list: list[str],
    days: int,
    period_minutes: int = 60,
    max_markets: int | None = None,
    requests_per_second: float = 8.0,
) -> None:
    conn = connect()
    conn.execute("PRAGMA busy_timeout=30000")

    client = KalshiClient(
        requests_per_second=requests_per_second
    )

    cutoff = (
        dt.datetime.now(dt.UTC)
        - dt.timedelta(days=days)
    )

    try:
        for series_ticker in series_list:
            markets = await collect_markets(
                client, conn, series_ticker
            )

            recent = [
                m for m in markets
                if (
                    _iso_to_ts(
                        m.get("close_time")
                    ) or 0
                ) >= cutoff.timestamp()
            ]

            print(
                f"{series_ticker}: "
                f"{len(recent)} recent markets"
            )

            if (
                max_markets is not None
                and len(recent) > max_markets
            ):
                recent.sort(
                    key=lambda m: (
                        m.get("close_time") or ""
                    ),
                    reverse=True,
                )
                recent = recent[:max_markets]

            count = 0

            for market in recent:
                count += await collect_candles(
                    client,
                    conn,
                    series_ticker,
                    market,
                    period_minutes,
                )

            print(
                f"{series_ticker}: "
                f"{count} candles stored"
            )
    finally:
        await client.aclose()
        conn.close()


async def collect_historical_markets(
    client: KalshiClient,
    conn: sqlite3.Connection,
    series_ticker: str,
) -> list[dict]:
    out = []
    cursor = None

    while True:
        response = (
            await client.get_historical_markets(
                series_ticker=series_ticker,
                limit=1000,
                cursor=cursor,
            )
        )

        markets = response.get("markets", [])

        for market in markets:
            upsert_market(
                conn, market, series_ticker
            )

        out.extend(markets)
        cursor = response.get("cursor")

        if not cursor or not markets:
            break

    conn.commit()
    return out


async def collect_historical_candles(
    client: KalshiClient,
    conn: sqlite3.Connection,
    market: dict,
    period_minutes: int = 60,
) -> int:
    open_ts = _iso_to_ts(
        market.get("open_time")
    )
    close_ts = _iso_to_ts(
        market.get("close_time")
    )

    if not open_ts or not close_ts:
        return 0

    rows = []
    step = period_minutes * 60 * 4900

    for chunk_start in range(
        open_ts, close_ts, step
    ):
        chunk_end = min(
            chunk_start + step, close_ts
        )

        try:
            candles = (
                await client.get_historical_candlesticks(
                    market["ticker"],
                    chunk_start,
                    chunk_end,
                    period_minutes,
                )
            )
        except Exception:
            continue

        for candle in candles:
            price = candle.get("price", {})
            bid = candle.get("yes_bid") or {}
            ask = candle.get("yes_ask") or {}

            rows.append((
                market["ticker"],
                period_minutes,
                candle["end_period_ts"],
                dollars(
                    price.get("open_dollars")
                    or price.get("open")
                ),
                dollars(
                    price.get("high_dollars")
                    or price.get("high")
                ),
                dollars(
                    price.get("low_dollars")
                    or price.get("low")
                ),
                dollars(
                    price.get("close_dollars")
                    or price.get("close")
                ),
                dollars(
                    price.get("mean_dollars")
                    or price.get("mean")
                ),
                dollars(
                    bid.get("close_dollars")
                    or bid.get("close")
                ),
                dollars(
                    ask.get("close_dollars")
                    or ask.get("close")
                ),
                float(
                    candle.get("volume_fp")
                    or candle.get("volume")
                    or 0
                ),
                float(
                    candle.get("open_interest_fp")
                    or candle.get("open_interest")
                    or 0
                ),
            ))

    conn.executemany(
        """INSERT OR REPLACE INTO candles (
            market_ticker, period_minutes, end_ts,
            open, high, low, close, mean,
            yes_bid_close, yes_ask_close,
            volume, open_interest
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )

    conn.commit()
    return len(rows)


async def backfill_historical(
    series_list: list[str],
    days: int | None = None,
    period_minutes: int = 60,
    max_markets: int | None = None,
    requests_per_second: float = 8.0,
) -> None:
    conn = connect()
    conn.execute("PRAGMA busy_timeout=30000")

    client = KalshiClient(
        requests_per_second=requests_per_second
    )

    try:
        cutoff = await client.get_historical_cutoff()
        print(
            f"Historical tier cutoff: {cutoff}"
        )

        min_close = (
            (
                dt.datetime.now(dt.UTC)
                - dt.timedelta(days=days)
            ).timestamp()
            if days else 0
        )

        for series_ticker in series_list:
            markets = (
                await collect_historical_markets(
                    client, conn, series_ticker
                )
            )

            settled = [
                m for m in markets
                if m.get("result") in (
                    "yes", "no"
                )
                and (
                    _iso_to_ts(
                        m.get("close_time")
                    ) or 0
                ) >= min_close
            ]

            if (
                max_markets is not None
                and len(settled) > max_markets
            ):
                settled.sort(
                    key=lambda m: (
                        m.get("close_time") or ""
                    ),
                    reverse=True,
                )
                settled = settled[:max_markets]

            count = 0

            for market in settled:
                count += (
                    await collect_historical_candles(
                        client,
                        conn,
                        market,
                        period_minutes,
                    )
                )

            print(
                f"{series_ticker}: "
                f"{count} historical candles stored"
            )
    finally:
        await client.aclose()
        conn.close()


async def snapshot() -> None:
    conn = connect()
    client = KalshiClient()

    try:
        for series_ticker in TARGET_SERIES:
            markets = await collect_markets(
                client,
                conn,
                series_ticker,
                status="open",
            )

            print(
                f"{series_ticker}: "
                f"{len(markets)} open markets"
            )
    finally:
        await client.aclose()
        conn.close()


async def discover_command() -> None:
    conn = connect()
    client = KalshiClient()

    try:
        await discover_fifteen_minute_markets(
            client, conn
        )
    finally:
        await client.aclose()
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(
        dest="cmd", required=True
    )

    bf = sub.add_parser("backfill")

    bf.add_argument(
        "--series",
        nargs="*",
        default=(
            TARGET_SERIES
            + SPORTS_CULTURE_SERIES
        ),
    )
    bf.add_argument(
        "--days", type=int, default=90
    )
    bf.add_argument(
        "--period",
        type=int,
        default=60,
        choices=[1, 60, 1440],
    )
    bf.add_argument(
        "--max-markets",
        type=int,
        default=None,
    )
    bf.add_argument(
        "--rps",
        type=float,
        default=8.0,
    )

    bh = sub.add_parser(
        "backfill-historical"
    )
    bh.add_argument(
        "--series",
        nargs="*",
        default=HIST_ECON_SERIES,
    )
    bh.add_argument(
        "--days",
        type=int,
        default=None,
    )
    bh.add_argument(
        "--period",
        type=int,
        default=60,
        choices=[1, 60, 1440],
    )
    bh.add_argument(
        "--max-markets",
        type=int,
        default=None,
    )
    bh.add_argument(
        "--rps",
        type=float,
        default=8.0,
    )

    sub.add_parser("snapshot")
    sub.add_parser("discover-15m")

    args = parser.parse_args()

    if args.cmd == "backfill":
        asyncio.run(
            backfill(
                args.series,
                args.days,
                args.period,
                args.max_markets,
                args.rps,
            )
        )

    elif args.cmd == "backfill-historical":
        asyncio.run(
            backfill_historical(
                args.series,
                args.days,
                args.period,
                args.max_markets,
                args.rps,
            )
        )

    elif args.cmd == "discover-15m":
        asyncio.run(discover_command())

    else:
        asyncio.run(snapshot())


if __name__ == "__main__":
    main()
