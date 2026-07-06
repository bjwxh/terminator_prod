"""
Test WebSocket update frequency for SPX 0DTE put at strike 7480.

Subscribes to SPXW 260618P07480000, prints each tick with timestamp,
and summarizes update frequency at end.
"""

import asyncio
import json
import time
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from schwab.auth import easy_client
from schwab.streaming import StreamClient

CHICAGO = ZoneInfo("America/Chicago")
TARGET_STRIKE = 7480
RUN_SECONDS = 120  # how long to stream

# Use test credentials (not SLI prod)
CREDS_PATH = Path.home() / ".api_keys" / "schwab" / "schwab_api.json"
TOKEN_PATH = Path.home() / ".api_keys" / "schwab" / "schwab_token.json"


def today_expiry() -> str:
    """Return today's date in YYMMDD format (Chicago time)."""
    return datetime.now(CHICAGO).strftime("%y%m%d")


def make_symbol(expiry: str, side: str, strike: int) -> str:
    """Build Schwab OCC-style streaming symbol: 'SPXW  YYMMDDSstrike*1000'."""
    return f"SPXW  {expiry}{side}{strike * 1000:08d}"


# ── State ──────────────────────────────────────────────────────────────────────

ticks: list[dict] = []  # each tick: {ts, bid, ask, mid, last, volume, delta}


def handle_option_update(msg: dict) -> None:
    for entry in msg.get("content", []):
        key = entry.get("key", "")
        if "P07480000" not in key:
            continue

        now = datetime.now(CHICAGO)
        bid   = entry.get("BID_PRICE")   or entry.get("2")
        ask   = entry.get("ASK_PRICE")   or entry.get("3")
        last  = entry.get("LAST_PRICE")  or entry.get("4")
        vol   = entry.get("TOTAL_VOLUME") or entry.get("8")
        delta = entry.get("DELTA")        or entry.get("29")
        theta = entry.get("THETA")        or entry.get("30")

        bid  = float(bid)  if bid  is not None else None
        ask  = float(ask)  if ask  is not None else None
        last = float(last) if last is not None else None
        mid  = round((bid + ask) / 2, 2) if bid is not None and ask is not None else None

        tick = {
            "ts":    now,
            "bid":   bid,
            "ask":   ask,
            "mid":   mid,
            "last":  last,
            "vol":   int(vol) if vol is not None else None,
            "delta": float(delta) if delta is not None else None,
            "theta": float(theta) if theta is not None else None,
        }
        ticks.append(tick)

        ts_str = now.strftime("%H:%M:%S.%f")[:-3]
        bid_s  = f"{bid:6.2f}" if bid  is not None else "  n/a "
        ask_s  = f"{ask:6.2f}" if ask  is not None else "  n/a "
        mid_s  = f"{mid:6.2f}" if mid  is not None else "  n/a "
        dlt_s  = f"{delta:+.3f}" if delta is not None else "  n/a"
        print(f"[{ts_str}]  bid={bid_s}  ask={ask_s}  mid={mid_s}  delta={dlt_s}  #{len(ticks)}")
        sys.stdout.flush()


def print_summary() -> None:
    n = len(ticks)
    print("\n" + "=" * 60)
    print(f"  Summary: SPXW 0DTE Put @ {TARGET_STRIKE}")
    print("=" * 60)
    print(f"  Total ticks received : {n}")

    if n < 2:
        print("  Not enough ticks to compute frequency.")
        return

    first_ts = ticks[0]["ts"]
    last_ts  = ticks[-1]["ts"]
    elapsed  = (last_ts - first_ts).total_seconds()

    intervals = [
        (ticks[i]["ts"] - ticks[i - 1]["ts"]).total_seconds()
        for i in range(1, n)
    ]
    avg_interval = sum(intervals) / len(intervals)
    min_interval = min(intervals)
    max_interval = max(intervals)

    print(f"  Observation window   : {elapsed:.1f}s  ({first_ts.strftime('%H:%M:%S')} – {last_ts.strftime('%H:%M:%S')} CT)")
    print(f"  Update rate          : {n / elapsed:.2f} ticks/sec  ({60 * n / elapsed:.1f}/min)")
    print(f"  Avg interval         : {avg_interval:.3f}s")
    print(f"  Min / Max interval   : {min_interval:.3f}s / {max_interval:.3f}s")

    last = ticks[-1]
    print(f"\n  Last quote  bid={last['bid']}  ask={last['ask']}  mid={last['mid']}  delta={last['delta']}")
    print("=" * 60)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    if not CREDS_PATH.exists():
        print(f"ERROR: credentials not found at {CREDS_PATH}")
        return

    with open(CREDS_PATH) as f:
        creds = json.load(f)

    print(f"Connecting to Schwab (test account)…")
    client = easy_client(
        api_key=creds["api_key"],
        app_secret=creds["api_secret"],
        callback_url=creds.get("callback_url", "https://127.0.0.1"),
        token_path=str(TOKEN_PATH),
        asyncio=True,
        enforce_enums=False,
    )

    expiry = today_expiry()
    symbol = make_symbol(expiry, "P", TARGET_STRIKE)
    print(f"Target symbol: {symbol}")
    print(f"Running for {RUN_SECONDS}s — will print each tick as it arrives.\n")

    stream = StreamClient(client)
    await stream.login()
    stream.add_level_one_option_handler(handle_option_update)
    await stream.level_one_option_subs([symbol])

    print(f"Subscribed. Listening…\n")
    print(f"{'Timestamp':14}  {'Bid':>6}  {'Ask':>6}  {'Mid':>6}  {'Delta':>7}  Tick#")
    print("-" * 62)

    deadline = time.monotonic() + RUN_SECONDS
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                await asyncio.wait_for(stream.handle_message(), timeout=remaining)
            except asyncio.TimeoutError:
                break
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        await stream.logout()

    print_summary()


if __name__ == "__main__":
    asyncio.run(main())
