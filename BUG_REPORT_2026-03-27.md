# Bug Report: Post-Deployment Live Data Issues
**Date:** 2026-03-27
**Symptoms:**
- Web UI shows no live trades despite multiple broker fills since morning
- Web UI shows no working order updates from broker
- Live PnL calculated as $0 / incorrect
- Sim position (initial Iron Condor) incorrect after soft bootstrap

---

## Root Cause Summary

All four symptoms share a single upstream failure: **`_sync_broker_data` silently processes an empty `ord_data` list and overwrites `live_combined_portfolio.trades = []` every 5 seconds**, erasing any correctly-filled trade data. The bootstrap issue is a cascade of this same failure.

There are three distinct bugs. Fix them in the order listed.

---

## Bug 1 — CRITICAL: isinstance check masks API failures, allowing "healthy" sync with empty data

**File:** `server/core/monitor.py` — `_sync_broker_data`, ~L627–664

**What happens:**
```python
# Step 1: Both responses are 200 → system is marked CONNECTED, failures = 0
if resp_acc.status_code == 200 and resp_ord.status_code == 200:
    with self._data_lock:
        self.broker_connected = True      # ← Marked healthy BEFORE data is parsed
        self.heartbeat_failures = 0

# Step 2: The isinstance check fires → ord_data silently becomes []
ord_data = resp_ord.json()
if not isinstance(ord_data, list):
    self.logger.warning(...)  # ← Only a warning; no failure is recorded
    ord_data = []             # ← Silent reset

# Step 3: Sync "succeeds" with empty data
with self._data_lock:
    filled_orders = [o for o in ord_data if ...]  # → []
    self.live_combined_portfolio.trades = []       # ← Wipes all live trades
    self.working_orders = []                       # ← Wipes all working orders
```

The system reports `broker_connected = True` and `heartbeat_failures = 0` to the UI, while the actual trade and order data is empty. There is no alert.

**Why would `resp_ord.json()` return a non-list?**

The most likely cause is a **paginated API response**. The Schwab API may return orders in a dict wrapper like `{"orders": [...], "nextPageKey": "..."}` when the result set is large (many orders since 8:30 AM across multiple strategies). The isinstance check was intended as a safety guard but instead silently kills real data.

Other triggers: rate-limit or authentication error responses (`{"message": "..."}`), which the library passes through as dicts on a 200 status in some edge cases.

**Fix:**

Move the isinstance check into the failure path instead of the success path. If the response is not a list, it is a sync failure — do not mark the connection as healthy.

```python
# After both 200 checks pass, parse ord_data FIRST:
ord_data = resp_ord.json()
if not isinstance(ord_data, list):
    self.logger.error(f"Unexpected orders response (type={type(ord_data).__name__}): {str(ord_data)[:300]}")
    raise Exception(f"Malformed orders response: expected list, got {type(ord_data).__name__}")
    # This raises into the except block → increments heartbeat_failures → user sees alert

# Only THEN mark connection as healthy:
with self._data_lock:
    self.broker_connected = True
    self.heartbeat_failures = 0
```

---

## Bug 2 — HIGH: `live_combined_portfolio.trades` is replaced, not merged

**File:** `server/core/monitor.py` — `_sync_broker_data`, ~L659

**What happens:**
```python
self.live_combined_portfolio.trades = broker_trades  # Full replace every 5 seconds
```

Even if Bug 1 is fixed, a single 5-second window where the API returns no results (due to a transient network hiccup, rate limit, or temporary auth issue) wipes ALL live trades from the portfolio. This causes:
- Live PnL to spike to $0 for up to 5 seconds
- The session file, if saved during that window, to record empty trades → corrupted bootstrap on next startup

**Fix:**

Merge incoming filled trades with the existing set instead of replacing. Use `order_id` as the deduplication key:

```python
# Replace:
self.live_combined_portfolio.trades = broker_trades

# With:
existing_ids = {t.order_id for t in self.live_combined_portfolio.trades if t.order_id}
new_trades = [t for t in broker_trades if t.order_id not in existing_ids]
if new_trades:
    self.live_combined_portfolio.trades.extend(new_trades)
    self.logger.info(f"Added {len(new_trades)} new filled trade(s) from broker sync.")
# Also recalculate cash from the full combined list:
self.live_combined_portfolio.cash = sum(t.credit for t in self.live_combined_portfolio.trades)
```

This ensures a momentary empty response does not destroy accumulated trade history. Trades are additive (filled orders don't un-fill), so this merge is safe.

---

## Bug 3 — HIGH: Bootstrap reads `live_combined_portfolio.trades` before broker sync has run

**File:** `server/core/monitor.py` — `_monitoring_loop`, ~L534

**What happens:**

All tasks in the `TaskGroup` start concurrently, but `_monitoring_loop` runs its synchronous setup (including the `broker_trades` capture at L535) before any `await` yields control to `_broker_sync_loop`. The first time the bootstrap captures trades, `_broker_sync_loop` has never run.

```python
# _monitoring_loop startup (synchronous — no await yet):
with self._data_lock:
    broker_trades = list(self.live_combined_portfolio.trades)  # ← always [] on first start
```

If there is a valid session for today, `restore_monitor` at L522 would populate `live_combined_portfolio.trades` from the saved session — **but only if the session was saved with valid trades**. If Bug 2 caused the session to be saved during a window when `live_combined_portfolio.trades = []`, the session itself is corrupt and restores empty trades.

**Cascade:** Bootstrap gets `broker_trades = []` → `_run_historical_simulation` runs in soft mode with no live ICs → the soft bootstrap assigns no `soft_ics` to any sub-strategy → each sub-strategy's `_create_sync_entry` is never called → the sim builds a pure model IC at arbitrary strikes → sim position diverges from real position.

**Fix:**

Perform an explicit initial broker sync BEFORE reading `broker_trades` for the bootstrap. Add a dedicated "pre-bootstrap sync" call at startup:

```python
# At the start of _monitoring_loop, before the bootstrap:
self.logger.info("Performing initial broker sync before bootstrap...")
await self._sync_broker_data()  # Ensure live_combined_portfolio is populated from API

# Then capture:
with self._data_lock:
    broker_trades = list(self.live_combined_portfolio.trades)
```

This guarantees the bootstrap always uses fresh API data, regardless of session state. It also fixes the case where the session had corrupt/empty trades from Bug 2.

---

## Secondary Issue: `to_entered_datetime` timezone inconsistency

**File:** `server/core/monitor.py` — `_sync_broker_data` vs `get_live_trades`

`_sync_broker_data` uses `to_entered_datetime=now_chi` (CHICAGO-aware datetime). `get_live_trades` uses `to_entered_datetime=now` (UTC-aware datetime). If the schwab-py library serializes these differently, the sync window may be off.

This is unlikely to be a primary cause, but to be consistent and safe, use UTC for the `to_entered_datetime` parameter in `_sync_broker_data`, matching `get_live_trades`:

```python
# Replace:
now_chi = datetime.now(CHICAGO)
# ...
to_entered_datetime=now_chi

# With:
now_utc = datetime.now(timezone.utc)
now_chi = now_utc.astimezone(CHICAGO)
# ...
to_entered_datetime=now_utc
```

`today_830` can remain CHICAGO-aware since it's a fixed time anchor, not a "now" timestamp.

---

## Fix Checklist (in priority order)

| # | Priority | File | Change |
|---|----------|------|--------|
| 1 | **Critical** | `monitor.py` `_sync_broker_data` | Move isinstance check before heartbeat-success block; raise on non-list |
| 2 | **High** | `monitor.py` `_sync_broker_data` | Change trades from replace (`=`) to merge (`extend`) using `order_id` dedup |
| 3 | **High** | `monitor.py` `_monitoring_loop` | Add `await self._sync_broker_data()` before bootstrap reads `broker_trades` |
| 4 | **Low** | `monitor.py` `_sync_broker_data` | Standardize `to_entered_datetime` to use UTC (`datetime.now(timezone.utc)`) |

---

## How these bugs interact (failure cascade)

```
Bug 1: API returns non-list (paginated/error response)
    → isinstance check fires silently
    → ord_data = []
    → broker_connected = True (no alert shown)
    → live_combined_portfolio.trades = []  ← Bug 2 makes this permanent/persistent
    → working_orders = []

Bug 2: trades replaced (not merged) every 5s
    → Session saved with empty trades during bad window
    → Next startup: session restores empty trades

Bug 3: Bootstrap reads trades before any sync
    → Gets [] from corrupted session
    → Soft bootstrap skips IC anchoring
    → Sim IC built at wrong strikes / missing entirely
    → Sim PnL diverges from live from the start of the day
```
