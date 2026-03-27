# Code Review: Schwab API Lean Sync Optimization
**Date:** 2026-03-27
**Reviewer:** Claude
**Scope:** `server/core/monitor.py` — changes related to the 2-call broker sync pattern, Iron Condor support, and auto-reconnect logic documented in `changelogs/2026-03-27.md`

---

## Issue 1 — CRITICAL: `self.working_orders` can be set to `None`, crashing trade execution

**File:** `server/core/monitor.py`
**Lines:** ~1185, ~2597

**Problem:**
`get_working_orders()` returns `None` on failure (client not initialized, or any exception). That `None` is directly assigned to `self.working_orders`:

```python
# Line ~1185 in execute_net_trade()
self.working_orders = await self.get_working_orders()
plan = self.create_execution_plan(trade)
```

`create_execution_plan()` then iterates `self.working_orders` unconditionally:

```python
# Line ~2597 in create_execution_plan()
for o in self.working_orders:  # TypeError: 'NoneType' is not iterable
```

A network hiccup right before a trade execution will crash the entire execution path.

**Fix:**
Guard against `None` and fall back to the existing cached value:

```python
refreshed = await self.get_working_orders()
if refreshed is not None:
    self.working_orders = refreshed
# else: proceed with the last known working_orders from the sync loop
plan = self.create_execution_plan(trade)
```

---

## Issue 2 — HIGH: `get_working_orders()` makes an extra (3rd) API call, breaking the 2-call optimization

**File:** `server/core/monitor.py`
**Lines:** ~1185, ~1699

**Problem:**
The lean sync refactor was designed to cap broker API usage at 2 calls per cycle (`get_account` + `get_orders_for_account`). However, `get_working_orders()` still issues its own independent `get_orders_for_account` call, and is invoked on every trade execution at line ~1185. This adds a 3rd call and can push the system toward the 60 calls/min rate limit during active trading periods.

**Fix:**
Since `self.working_orders` is already kept fresh by `_sync_broker_data` (5s cadence), the pre-execution refresh should simply use the cached value instead of making a new API call. The `get_working_orders()` method can be retained as a utility for on-demand manual use (e.g. the diagnostic tool), but should not be called on the hot path.

Remove or replace the pre-trade refresh call:

```python
# In execute_net_trade() — remove this line:
# self.working_orders = await self.get_working_orders()

# working_orders is already fresh from the sync loop; proceed directly:
plan = self.create_execution_plan(trade)
```

If a real-time snapshot is still desired before execution, reuse the data already fetched by `_sync_broker_data` rather than issuing a new call.

---

## Issue 3 — HIGH: `ord_data` not validated as a list before iteration

**File:** `server/core/monitor.py`
**Lines:** ~638–647 in `_sync_broker_data()`

**Problem:**
`resp_ord.json()` is assumed to always return a list. If Schwab returns an error body or an empty JSON object (`{}`), iterating it raises `TypeError: 'dict' is not iterable`, which is then caught by the outer `except`, counts as a heartbeat failure, and — after 3 failures — sets `self.client = None` and sends an urgent notification. A single malformed API response would incorrectly trigger the reconnect alarm.

```python
ord_data = resp_ord.json()  # could be {} or {"error": "..."} on non-200 that slipped through
filled_orders = [o for o in ord_data if o.get('status') == 'FILLED']  # crashes
```

**Fix:**
Add a type guard immediately after parsing:

```python
ord_data = resp_ord.json()
if not isinstance(ord_data, list):
    self.logger.warning(f"Unexpected orders response format: {type(ord_data)} — {str(ord_data)[:200]}")
    ord_data = []
```

---

## Issue 4 — MEDIUM: Per-leg `price` and `entry_price` set to order-level net price

**File:** `server/core/monitor.py`
**Lines:** ~1598–1599 in `_convert_order_to_trade()`

**Problem:**
`order.get('price', 0)` is the **net** credit/debit for the entire order (e.g. `2.50` for the whole Iron Condor), not the fill price of each individual leg. Every `OptionLeg` created in this function receives this same net value as both `price` and `entry_price`:

```python
legs.append(OptionLeg(
    ...
    price=order.get('price', 0),       # wrong: this is the order's net premium
    entry_price=order.get('price', 0)  # wrong: same issue
))
```

The per-leg execution prices are available in `orderActivityCollection → executionLegs`, which the function already parses (lines ~1621–1632) to compute `actual_net_cash` — but never uses to set individual leg prices.

**Fix:**
Build a `legId → execution price` map during the activity loop and apply it back to each `OptionLeg`:

```python
leg_fill_prices = {}  # legId (str) -> fill price
for activity in activities:
    if activity.get('activityType') == 'EXECUTION':
        for exec_leg in activity.get('executionLegs', []):
            leg_fill_prices[str(exec_leg.get('legId'))] = exec_leg.get('price', 0.0)

# Then when building OptionLeg objects, map legId to fill price:
for idx, oleg in enumerate(order.get('orderLegCollection', [])):
    leg_id = str(oleg.get('legId'))
    fill_price = leg_fill_prices.get(leg_id, order.get('price', 0))
    legs.append(OptionLeg(..., price=fill_price, entry_price=fill_price))
```

---

## Issue 5 — MEDIUM: `self.client = None` and `self.heartbeat_failures` mutated outside `_data_lock`

**File:** `server/core/monitor.py`
**Lines:** ~674, ~683 in the `except` block of `_sync_broker_data()`

**Problem:**
Both mutations happen outside the data lock, while other coroutines (e.g. `execute_net_trade`, `_broker_sync_loop`) may be checking or using `self.client` concurrently:

```python
except Exception as e:
    self.heartbeat_failures += 1      # no lock
    ...
    if self.heartbeat_failures >= 3:
        self.broker_connected = False
        ...
        self.client = None            # no lock — race condition with other awaiters
```

A coroutine that checks `self.client` is not None and then awaits may find `self.client` is None by the time it executes its next line.

**Fix:**
Wrap both mutations in `_data_lock`:

```python
except Exception as e:
    with self._data_lock:
        self.heartbeat_failures += 1
        if self.heartbeat_failures >= 3:
            self.broker_connected = False
            if self.heartbeat_failures == 3:
                # send notification outside lock to avoid blocking
                pass
            self.client = None
    if self.heartbeat_failures == 3:
        await notify_all(self.config, "Broker Connection Lost!", title="Terminator Critical", priority="urgent")
    self._broadcast_alert("error", "Broker Sync Error", str(e))
```

---

## Issue 6 — LOW: `get_working_orders()` uses a timezone-naive datetime

**File:** `server/core/monitor.py`
**Line:** ~1698 in `get_working_orders()`

**Problem:**
The rest of the codebase uses `CHICAGO`-aware datetimes for API calls. `get_working_orders()` uses a naive `datetime.combine(date.today(), time(0, 0))` with no timezone, which Schwab likely interprets as UTC. This means the query window is offset from the intended Chicago midnight by 5–6 hours depending on DST.

```python
from_time = datetime.combine(date.today(), time(0, 0))  # naive — no timezone
```

**Fix:**
Apply the same pattern used in `_sync_broker_data`:

```python
from_time = datetime.now(CHICAGO).replace(hour=0, minute=0, second=0, microsecond=0)
```

---

## Summary

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| 1 | **Critical** | `execute_net_trade` ~L1185 | `working_orders = None` crashes `create_execution_plan` |
| 2 | **High** | `execute_net_trade` ~L1185 | `get_working_orders()` makes an extra 3rd API call on every trade |
| 3 | **High** | `_sync_broker_data` ~L647 | `ord_data` not guarded against non-list API response |
| 4 | **Medium** | `_convert_order_to_trade` ~L1598 | Per-leg `price`/`entry_price` set to order-level net premium |
| 5 | **Medium** | `_sync_broker_data` ~L674–683 | `client = None` and `heartbeat_failures` mutated without lock |
| 6 | **Low** | `get_working_orders` ~L1698 | Timezone-naive datetime vs. CHICAGO-aware datetime elsewhere |

Fix priority: Issues 1 and 3 should be addressed first as they can crash the system or trigger false reconnect alarms under normal operating conditions. Issue 2 is a quick win that directly protects the rate limit optimization. Issues 4–6 can follow.
