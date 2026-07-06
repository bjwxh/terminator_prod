"""
Test WebSocket update frequency for multiple SPX 0DTE options.
"""

import asyncio
import json
import time
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import defaultdict

from schwab.auth import easy_client
from schwab.streaming import StreamClient

CHICAGO = ZoneInfo("America/Chicago")
RUN_SECONDS = 120

CREDS_PATH = Path.home() / ".api_keys" / "schwab" / "schwab_api.json"
TOKEN_PATH = Path.home() / ".api_keys" / "schwab" / "schwab_token.json"


def today_expiry() -> str:
    return datetime.now(CHICAGO).strftime("%y%m%d")


def make_symbol(expiry: str, side: str, strike: int) -> str:
    return f"SPXW  {expiry}{side}{strike * 1000:08d}"


# Define strikes and sides (spaced by 5 points)
PUT_STRIKES = list(range(7400, 7480 + 1, 5))
CALL_STRIKES = list(range(7480, 7560 + 1, 5))

# State: symbol -> list of tick timestamps
ticks_by_symbol = defaultdict(list)
total_raw_messages = 0


def handle_option_update(msg: dict) -> None:
    global total_raw_messages
    total_raw_messages += 1
    now = datetime.now(CHICAGO)
    for entry in msg.get("content", []):
        symbol = entry.get("key", "")
        if symbol:
            ticks_by_symbol[symbol].append(now)


def print_summary(elapsed: float) -> None:
    print("\n" + "=" * 80)
    print("  Summary: Multi-Option Subscription Update Statistics")
    print("=" * 80)
    print(f"  Observation window   : {elapsed:.1f}s")
    print(f"  Total raw WS messages: {total_raw_messages}")
    print(f"  Distinct symbols recvd: {len(ticks_by_symbol)}")
    print("-" * 80)
    print(f"{'Symbol':24} | {'Ticks':>5} | {'Rate (/sec)':>11} | {'Avg Gap (s)':>11} | {'Max Gap (s)':>11}")
    print("-" * 80)

    for symbol, tss in sorted(ticks_by_symbol.items()):
        n = len(tss)
        if n == 0:
            print(f"{symbol:24} | {0:5d} | {0.0:>11.2f} | {'N/A':>11} | {'N/A':>11}")
            continue

        rate = n / elapsed
        if n >= 2:
            gaps = [(tss[i] - tss[i-1]).total_seconds() for i in range(1, n)]
            avg_gap = sum(gaps) / len(gaps)
            max_gap = max(gaps)
            avg_gap_s = f"{avg_gap:.2f}s"
            max_gap_s = f"{max_gap:.2f}s"
        else:
            avg_gap_s = "N/A"
            max_gap_s = "N/A"

        print(f"{symbol:24} | {n:5d} | {rate:>11.2f} | {avg_gap_s:>11} | {max_gap_s:>11}")
    print("=" * 80)


async def main() -> None:
    if not CREDS_PATH.exists():
        print(f"ERROR: credentials not found at {CREDS_PATH}")
        return

    with open(CREDS_PATH) as f:
        creds = json.load(f)

    client = easy_client(
        api_key=creds["api_key"],
        app_secret=creds["api_secret"],
        callback_url=creds.get("callback_url", "https://127.0.0.1"),
        token_path=str(TOKEN_PATH),
        asyncio=True,
        enforce_enums=False,
    )

    expiry = today_expiry()
    symbols = []
    for strike in PUT_STRIKES:
        symbols.append(make_symbol(expiry, "P", strike))
    for strike in CALL_STRIKES:
        symbols.append(make_symbol(expiry, "C", strike))

    print(f"Subscribing to {len(symbols)} symbols...")
    for sym in symbols:
        print(f"  {sym}")

    stream = StreamClient(client)
    await stream.login()
    stream.add_level_one_option_handler(handle_option_update)
    await stream.level_one_option_subs(symbols)

    print(f"\nListening for {RUN_SECONDS} seconds...")
    start_time = time.monotonic()
    deadline = start_time + RUN_SECONDS
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                await asyncio.wait_for(stream.handle_message(), timeout=remaining)
            except asyncio.TimeoutError:
                break
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        await stream.logout()

    elapsed = time.monotonic() - start_time
    # Initialize entries for unsubscribed/non-received symbols so they show in summary
    for sym in symbols:
        if sym not in ticks_by_symbol:
            ticks_by_symbol[sym] = []

    print_summary(elapsed)


if __name__ == "__main__":
    asyncio.run(main())
