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

---
---

# Bug Report: Soft Bootstrap Failure — Sim/Live Position Mismatch
**Date:** 2026-03-27 (Round 2 — post first-round fixes)
**Symptoms:**
- Live trades correctly updated (Bugs 1–3 above are fixed)
- Sim trades window shows a **single trade with 12 legs** instead of individual ICs per strategy
- Sim and live positions are **largely mismatched**
- Session Stats charts **start from 11:11am** (deployment time) instead of 8:30am

---

## Root Cause Summary

The bootstrap silently crashes before any portfolio state is restored. After the crash, all sub-strategy portfolios are empty, and the first live monitoring step fires hard model entries for all three strategies simultaneously — producing a single 12-leg netted sim trade at wrong strikes. Three separate bugs in `_run_historical_simulation` are responsible.

---

## Bug 4 — CRITICAL: Bootstrap crashes due to timezone mismatch in chart-padding code

**File:** `server/core/monitor.py` — `_run_historical_simulation`, ~L2235–2262

**What happens:**

```python
# L2235: dt_series is created from the RAW column — tz-naive at this point
dt_series = pd.to_datetime(data['datetime'])

# L2236-2239: data['datetime'] is converted to tz-aware IN PLACE
if dt_series.dt.tz is None:
    data['datetime'] = dt_series.dt.tz_localize('America/Chicago')
# ← dt_series still points to the OLD tz-naive series; it is NOT updated

# L2244: groups are keyed by tz-AWARE timestamps
groups = data.groupby('datetime')

# L2255: KeyError — tz-naive key does not exist in tz-aware group index
first_snap = groups.get_group(dt_series.iloc[0])  # ← TypeError / KeyError

# L2262: TypeError — can't compare offset-naive and offset-aware datetimes
while curr_pad < first_avail_ts:  # curr_pad is tz-aware, first_avail_ts is tz-naive
```

This code only runs when `first_avail_ts.time() > 08:30` (L2259), i.e. when the DB data starts after market open. After a mid-day deployment the DB has no data before 11:11am — so this branch always fires, always raises, and the exception propagates to `_monitoring_loop` L562 where it is caught and logged. **The bootstrap silently exits, leaving all portfolios empty.**

**Cascade:**
```
Bootstrap crashes → all portfolios stay empty (reset at L542–547 but never re-populated)
→ broker sync repopulates live_combined_portfolio correctly
→ combined_portfolio and sub-strategy portfolios remain empty
→ First _monitor_step fires: all 3 sub-strategies call _check_entry simultaneously
→ net_trades() collapses 3×4-leg model ICs into 1 net 12-leg trade at wrong strikes
→ session_history starts collecting at 11:11am (charts start at 11:11am)
```

**Fix:**

`dt_series` must be read from `data['datetime']` AFTER the tz conversion, not before. Replace L2255–2257 with:

```python
# Read from the already-converted column, not from the stale tz-naive series
first_avail_ts = data['datetime'].iloc[0]                    # tz-aware Chicago Timestamp
first_snap = groups.get_group(first_avail_ts)                # key matches group index
first_spx = self.estimate_spx_price(first_snap) if not data.empty else None
```

The `while curr_pad < first_avail_ts` comparison at L2262 then becomes tz-aware vs. tz-aware, which is valid.

---

## Bug 5 — HIGH: BROKER IC mapping silently discards ICs filled before `trade_start_time`

**File:** `server/core/monitor.py` — `_run_historical_simulation`, ~L2320–2340

**What happens:**

```python
# Assignment condition for an untagged BROKER IRON_CONDOR:
for s_id in available_strat_ids:
    s_obj = self.sub_strategies[s_id]
    if not s_obj.has_traded_today and ts.time() >= s_obj.trade_start_time:
        target_sid = s_id
        break
# ↑ ts is the DB SNAPSHOT time, NOT the IC fill time.
# If the IC was filled before the sub-strategy's trade_start_time,
# AND the DB snapshot at that fill time is also before trade_start_time,
# no sub-strategy passes the condition.

# Falls through to:
else:
    self.combined_portfolio.add_trade(sim_trade)  # L2339 — combined only, no sub-strategy
    # sub-strategy portfolio stays empty
```

After `_reconcile_combined_simulation()` fires (during live monitoring), it rebuilds `combined_portfolio.positions` from sub-strategy portfolios — which are empty. The combined portfolio loses all its positions, causing full mismatch with `live_combined_portfolio`.

**Why it fires:**
A broker IC is popped from `pending_live_trades` when `pending_live_trades[0].timestamp <= ts` (the current DB snapshot). If the IC was filled at 9:14am and the strategy's `trade_start_time` is 9:15am, the DB snapshot processing it could be 9:14am. `ts.time() (9:14) < trade_start_time (9:15)` → condition fails → IC discarded into combined only.

Even more severely: if the DB starts from 11:11am (Bug 4 scenario, after Bootstrap crash), this code never runs. But if Bug 4 is fixed and the DB has early data, Bug 5 would still fire for any IC filled 1–5 minutes before its strategy's designated start time.

**Fix:**

Remove the `ts.time() >= s_obj.trade_start_time` guard. The ICs are already sorted chronologically by `pending_live_trades`, and `available_strat_ids` is sorted by `trade_start_time`. Sequential assignment is unambiguous and correct:

```python
# Replace:
if not s_obj.has_traded_today and ts.time() >= s_obj.trade_start_time:

# With:
if not s_obj.has_traded_today:
```

An IC filled before a strategy's designated start time is still definitively that strategy's opening trade — the strategy simply entered slightly early. The sequential ordering is the correct signal, not the start-time gate.

---

## Bug 6 — MEDIUM: Fuzzy leg-signature deduplication drops valid ICs from different strategies

**File:** `server/core/monitor.py` — `_run_historical_simulation`, ~L2188–2196

**What happens:**

```python
leg_sig = tuple(sorted([(l.symbol, l.strike, l.side, l.quantity) for l in t.legs]))
ts_fuzzy = t.timestamp.replace(second=0, microsecond=0)  # truncate to the minute
sig_key = (ts_fuzzy, leg_sig)

if sig_key in seen_signatures:
    self.logger.info("Bootstrap [SOFT]: Skipping fuzzy duplicate...")
    continue  # ← trade is dropped
seen_signatures.add(sig_key)
```

When two sub-strategies have the same delta targets (producing the same strikes), and their ICs fill within the same minute, both ICs share the same `(minute, leg_sig)` key. The second IC is silently dropped. That sub-strategy gets no opening trade during bootstrap.

**This is more likely than it appears:** if the config has multiple strategies with identical `init_s_delta` and `init_l_delta` (e.g., for split-lot scaling), all their same-day ICs will have identical leg signatures.

**Fix:**

The Order ID deduplication at L2182–2186 is already sufficient and correct. Remove the fuzzy signature block entirely:

```python
# Remove the entire block:
# leg_sig = tuple(sorted(...))
# ts_fuzzy = t.timestamp.replace(second=0, microsecond=0)
# sig_key = (ts_fuzzy, leg_sig)
# if sig_key in seen_signatures: continue
# seen_signatures.add(sig_key)

# Also remove seen_signatures = set() from initialization at L2178.
# Rely solely on Order ID deduplication (already correct).
```

If two different strategies have genuinely identical ICs (same strikes, same fills, different orders), they have different `order_id` values — the order ID check correctly preserves both.

---

## Fix Checklist (Round 2)

| # | Priority | File | Change |
|---|----------|------|--------|
| 4 | **Critical** | `monitor.py` `_run_historical_simulation` ~L2255 | Use `data['datetime'].iloc[0]` (tz-aware) instead of `dt_series.iloc[0]` (tz-naive) for chart-padding group lookup and comparison |
| 5 | **High** | `monitor.py` `_run_historical_simulation` ~L2322 | Remove `ts.time() >= s_obj.trade_start_time` from BROKER IC mapping condition |
| 6 | **Medium** | `monitor.py` `_run_historical_simulation` ~L2190–2196 | Remove fuzzy leg-signature deduplication block; rely on order ID dedup only |

Fix in order. Bug 4 alone explains all three symptoms. Bugs 5 and 6 are residual correctness issues that would surface after Bug 4 is fixed.

---

## How these bugs interact (failure cascade)

```
Bug 4: DB data starts after 8:30am (mid-day deployment)
    → chart-padding block fires
    → groups.get_group(tz-naive key) raises TypeError
    → _monitoring_loop catches the exception, bootstrap exits empty
    → All portfolios reset but never repopulated by sim
    → Broker sync repopulates live_combined_portfolio only
    → First _monitor_step: 3 strategies all fire _check_entry simultaneously
    → net_trades() → 1 × 12-leg model trade at arbitrary strikes
    → session_history empty → charts start at 11:11am

Bug 5 (after Bug 4 is fixed):
    → IC filled 1–5 min before trade_start_time
    → ts.time() < trade_start_time → no match found
    → IC replayed into combined_portfolio only (not sub-strategy)
    → _reconcile_combined_simulation() rebuilds combined from empty sub-strategies
    → combined_portfolio.positions wiped → mismatch with live

Bug 6 (after Bug 4 is fixed):
    → Two strategies have identical strikes, ICs fill same minute
    → Second IC dropped by fuzzy dedup
    → One sub-strategy has no opening trade
    → Mismatch: live has 2 ICs, sim has 1
```
