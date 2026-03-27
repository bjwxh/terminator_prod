# Terminator — Code Review: 2026-03-25

Full review of bugs, performance issues, and scalability analysis (3 → 250 sub-strategies).
All file references are relative to `server/`.

---

## Bugs

### Bug 1 — `set_trading_enabled` crashes before monitor starts
**File:** `core/monitor.py:164`
**Severity:** High — crashes on toggle if monitor hasn't started yet.

`self.order_queue` is initialised to `None` in `__init__` and only replaced with a real `asyncio.Queue` inside `run_live_monitor()`. If the `/api/trading/toggle` endpoint is called before the monitor starts (e.g. on a fresh server start before the background task is up), calling `self.order_queue.empty()` throws `AttributeError: 'NoneType' object has no attribute 'empty'`.

**Fix:** Guard the queue-drain block:
```python
if self.order_queue is not None:
    while not self.order_queue.empty():
        ...
```

---

### Bug 2 — `entry_price` not serialised in session file
**File:** `core/session_manager.py:119-130`
**Severity:** High — silently corrupts unrealised PnL after every restart.

`_serialize_leg` does not include `entry_price` in the dict it writes. When positions are restored via `_restore_leg`, `entry_price` defaults to `0.0`. The `unrealized_pnl` property (`(price - entry_price) * qty * 100`) then reports the full current option premium as profit rather than the gain since entry.

**Fix:** Add `entry_price` to `_serialize_leg` and `_restore_leg`:
```python
# _serialize_leg
'entry_price': l.entry_price,

# _restore_leg
entry_price=l_data.get('entry_price', l_data['price']),  # fallback to price for old files
```

---

### Bug 3 — `max_margin` peak history reset every 30 seconds
**File:** `core/monitor.py:2476-2501` (`_reconcile_combined_simulation`)
**Severity:** Medium — max_margin stat is always inaccurate mid-session.

`_reconcile_combined_simulation` creates a brand-new `Portfolio()` on every call and rebuilds it from sub-strategy positions. A fresh `Portfolio` starts with `max_margin = 0.0`. The old portfolio's intraday peak is silently discarded. Since the method is called every 30s tick, `combined_portfolio.max_margin` never retains its intraday high for more than one tick.

**Fix:** Transfer `max_margin` from the old portfolio before replacing it:
```python
new_port.max_margin = max(
    self.combined_portfolio.max_margin,
    new_port.calculate_standard_margin()
)
self.combined_portfolio = new_port
```

---

### Bug 4 — `_update_stats` uses wrong divisor for average duration
**File:** `core/monitor.py:663`
**Severity:** Low — session stats displayed in the UI are wrong.

`total_dur` accumulates duration only for *closed* strategies (those with no open positions), but the divisor is `len([s for s in self.sub_strategies.values() if s.has_traded_today])`, which counts *all* strategies that have traded today regardless of whether they are still open. For any mix of open and closed strategies, the reported average duration is too low.

**Fix:** Count only closed strategies in the divisor:
```python
closed_count = len([s for s in self.sub_strategies.values() if s.has_traded_today and not s.portfolio.positions])
if closed_count > 0:
    self.stats.avg_duration_minutes = total_dur / closed_count
```

---

### Bug 5 — `active_order_signals` guard is dead code
**File:** `core/monitor.py:935-936`
**Severity:** Medium — intended duplicate-signal protection silently does nothing.

In `_monitor_step`, the guard `if sid in self.active_order_signals: continue` is meant to prevent the monitor from generating a new signal for a strategy that already has one pending in the UI. However, there is no code path in the current reconciliation/execution flow that *adds* a strategy ID to `self.active_order_signals` before this check runs. `signal_completed()` only *removes* from it. The set is always empty, so the check never triggers.

**Fix:** Populate `active_order_signals` when a trade is added to the order queue. In `_check_reconciliation` (and anywhere else `order_queue.put_nowait` is called):
```python
self.order_queue.put_nowait(recon_trade)
self.active_order_signals.add(recon_trade.strategy_id)
```
And ensure `signal_completed()` is called (it already exists) after the order execution loop finishes processing a trade.

---

### Bug 6 — `net_trades` mislabels all combined trades as `"GAP_SYNC"`
**File:** `core/monitor.py:2471`
**Severity:** Low — trade history in the UI is misattributed.

`net_trades()` hardcodes `strategy_id="GAP_SYNC"` for every netted trade it produces. Every trade written into `combined_portfolio.trades` therefore appears to come from `"GAP_SYNC"` rather than from the actual constituent strategies. The `/api/trades` endpoint and the UI trade history tab show all combined trades as reconciliation events.

**Fix:** Use a more meaningful label, e.g. `"combined"`, or derive it from the constituent strategies:
```python
strategy_id="combined",
```

---

### Bug 7 — Portfolio objects read outside the data lock in WebSocket broadcast
**File:** `api/ws.py:72-124`
**Severity:** Medium — potential torn read / iteration error under concurrent writes.

`sim_p` and `live_p` are captured as *reference pointers* (not deep copies) under `_data_lock`, then used extensively outside the lock: iterating `sim_p.positions`, calling `sim_p.calculate_standard_margin()`, iterating `live_p.trades`, etc. Concurrently, `_broker_sync_loop` can call `_update_live_portfolio` which replaces `live_combined_portfolio.positions` with a new list while the broadcast is iterating the old one. This can cause `RuntimeError: list changed size during iteration` or silent torn reads.

**Fix:** Either snapshot the data you need (not just the reference) while under the lock, or take shallow copies of the lists:
```python
with monitor._data_lock:
    sim_positions = list(sim_p.positions)
    sim_trades    = list(sim_p.trades)
    live_positions = list(live_p.positions)
    live_trades    = list(live_p.trades)
    sim_cash       = sim_p.cash
    # ... etc.
```
Then build the JSON from these copies outside the lock.

---

### Bug 8 — `_check_reconciliation` accesses asyncio Queue internals
**File:** `core/monitor.py:1754`
**Severity:** Low — forward-compatibility risk, may break on Python minor version upgrades.

```python
any(t.purpose == TradePurpose.RECONCILIATION for t in list(self.order_queue._queue))
```
`._queue` is the private internal deque of `asyncio.Queue`. It is not part of the public API and has broken before across Python versions.

**Fix:** Maintain an explicit `set` of purposes currently in the queue (or a counter), updated whenever items are put/got:
```python
# On put:
self._queued_purposes.add(trade.purpose)

# On task_done / after get:
# Recompute from remaining items or use a Counter
```
Alternatively, since the code already tracks `active_order_signals`, use that set to determine if a reconciliation is already pending.

---

### Bug 9 — `get_live_positions` sets bid == ask == price for all live positions
**File:** `core/monitor.py:1357-1359`
**Severity:** Low — display only, but live position spread is always shown as zero.

All three fields are derived from `marketValue / (qty * 100)`:
```python
'price': pos.get('marketValue', 0) / (qty * 100) if qty != 0 else 0,
'bid':   pos.get('marketValue', 0) / (qty * 100) if qty != 0 else 0,
'ask':   pos.get('marketValue', 0) / (qty * 100) if qty != 0 else 0,
```
The Schwab account positions response does not include bid/ask directly. The correct approach is to populate bid/ask from the option chain snap (already available at call sites) or leave them as 0 and fill them in `_update_live_portfolio` from `_greek_cache` / `_last_snap`.

**Fix:** Leave bid/ask as `0` in `get_live_positions` and fill them from the snap in `_update_live_portfolio`, which already has access to `_last_snap`:
```python
# In _update_live_portfolio, after writing delta/theta from snap:
if not r.empty:
    ...
    p.bid_price = float(r['bidprice'].iloc[0])
    p.ask_price = float(r['askprice'].iloc[0])
```

---

### Bug 10 — `_run_historical_simulation` returns bare `None` on database error
**File:** `core/monitor.py:1902`
**Severity:** Low — caller handles None gracefully, but the error is swallowed silently.

On a database exception, the function does `return` (bare return = `None`) instead of `return []`. The caller at line 476 writes `self.session_history = history if history else []`, so it doesn't crash — but the charts start empty with no visible indication of *why*, and the log message says "Database error" while the status bar shows normal "Running". This can hide misconfigured `db_path`.

**Fix:**
```python
except Exception as e:
    self.logger.error(f"Database error during historical simulation: {e}")
    return []   # not bare return
```

---

## Performance Issues

### P1 — `calculate_standard_margin()` called on every `add_trade()`
**File:** `core/models.py:236`
**Impact:** O(positions²) per trade add; catastrophic at 250 strategies.

`Portfolio.add_trade()` unconditionally calls `calculate_standard_margin()` on every invocation. `_reconcile_combined_simulation` calls `add_trade()` once per open position per sub-strategy on every 30s tick. At 3 strategies × 4 positions = 12 calls today; at 250 strategies × 4 positions = 1,000 calls per tick of an O(n²) method.

**Fix:** Make margin calculation lazy — cache the result and only recompute when positions actually change:
```python
def add_trade(self, trade: Trade):
    ...
    self._margin_dirty = True   # invalidate cache
    # Don't call calculate_standard_margin() here

@property
def current_margin(self) -> float:
    if self._margin_dirty:
        self._cached_margin = self.calculate_standard_margin()
        self._margin_dirty = False
    return self._cached_margin
```
Update `max_margin` in the property setter rather than in `add_trade`.

---

### P2 — `_update_all_pricing` uses O(n) DataFrame boolean mask scan per position
**File:** `core/monitor.py:1297-1301`
**Impact:** Redundant full-scan per position; partially fixed in live loop but not here.

`_update_all_pricing` filters the snap DataFrame with a boolean mask for each position:
```python
r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
```
`_monitor_step` already builds `snap_indexed = snap.set_index(['strike_int', 'side'])` for the live strategy loop (PERF-1 fix), but `_update_all_pricing` doesn't receive it and still does the slow path.

**Fix:** Pass `snap_indexed` into `_update_all_pricing` and use `.loc` for O(1) lookups:
```python
def _update_all_pricing(self, quotes, snap=None, snap_indexed=None):
    for portfolio in [...]:
        for p in portfolio.positions:
            key = (int(round(p.strike)), p.side)
            if snap_indexed is not None and key in snap_indexed.index:
                row = snap_indexed.loc[[key]].iloc[0]
                ...
```

---

### P3 — Historical simulation position updates use O(n) scans per position per timestamp
**File:** `core/monitor.py:1953-1958`, `2003-2008`
**Impact:** Startup replay becomes extremely slow at 250 strategies.

The historical replay loop iterates every active strategy's positions and does a raw boolean mask scan against the snap DataFrame for each position, at every timestamp:
```python
r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
```
The live `_monitor_step` loop uses `snap_indexed` for O(1) lookups but the historical path does not. At 250 strategies × 4 positions × 780 timestamps ≈ 780,000 DataFrame scans at startup.

**Fix:** Pre-index the snap inside the `for ts, snap in groups:` loop before the strategy iteration:
```python
for ts, snap in groups:
    snap = snap.reset_index(drop=True)
    snap['strike_int'] = snap['strike_price'].round().astype(int)
    snap_indexed = snap.set_index(['strike_int', 'side'])

    for sid, s in self.sub_strategies.items():
        for p in s.portfolio.positions:
            key = (int(round(p.strike)), p.side)
            if key in snap_indexed.index:
                row = snap_indexed.loc[[key]].iloc[0]
                p.delta = row['delta']
                ...
```

---

### P4 — `_reconcile_combined_simulation` rebuilds entire combined portfolio every 30s
**File:** `core/monitor.py:2476-2501`
**Impact:** Unnecessary allocation + O(n) work every tick even when nothing changed.

The method discards the existing combined portfolio, creates a new `Portfolio()`, and re-adds every position from every sub-strategy as a dummy trade. This allocates hundreds of objects, calls `add_trade()` (and thus `calculate_standard_margin()` — see P1) for every position, and copies the full trade list — all unconditionally every tick.

**Fix:** Only rebuild when positions have actually changed. Introduce a `_sim_dirty` flag set to `True` when any sub-strategy trade is applied, and skip the reconcile if it's `False`:
```python
# After applying t_trades in _monitor_step:
self._sim_dirty = True

# In _reconcile_combined_simulation:
if not self._sim_dirty:
    return
self._sim_dirty = False
... # rebuild as before
```
A more thorough fix is to maintain the combined portfolio incrementally (apply the netted trade directly) and only use `_reconcile_combined_simulation` as a periodic correctness check (e.g. every 5 minutes).

---

### P5 — WebSocket broadcast serialises full CONFIG dict every 500ms
**File:** `api/ws.py:157`
**Impact:** Minor but constant — a static dict serialised 2× per second per client.

`"config": CONFIG` is included in every state broadcast. CONFIG never changes at runtime.

**Fix:** Send CONFIG once on WebSocket connect, not in every broadcast:
```python
# In websocket_endpoint, after connect:
await websocket.send_text(json.dumps({"type": "config", "config": CONFIG}))

# Remove from broadcast_state payload.
```

---

### P6 — Option cache: wrong design for the access pattern
**File:** `core/monitor.py:2044-2085` (`_find_option`)
**Impact:** Eviction spikes every tick at 250 strategies; unnecessary complexity.

Three compounding problems:

1. **The cache key includes `timestamp`**, which means every entry from the previous 30s tick is a guaranteed miss. The cache is functionally a per-tick deduplicator — it just doesn't know it.

2. **Batch eviction pattern** — when the cache exceeds 2,000 entries it evicts 1,000 at once in a `while` loop. This is O(1,000) work in a single call rather than O(1) per insertion. At 250 strategies the cache fills and triggers this spike on roughly every tick.

3. **Rebalance lookups have strategy-specific `short_strike`**, making their keys unique per strategy. With 250 strategies × 2 sides × 2 legs = up to 1,000 unique entries per tick. These fill the cache and cause entry lookups (which *are* shared across all 250 strategies) to get evicted.

**Fix:** Replace the bounded global LRU with a plain `dict` cleared at the start of each tick. Since entries are only valid for the current snap, there is no benefit to retaining them across ticks.

In `_monitor_step`, just before the strategy loop:
```python
self._option_cache = {}    # all entries are snap-relative; clear per tick
```

In `_run_historical_simulation`, at the top of the `for ts, snap in groups:` loop:
```python
self._option_cache = {}    # clear per timestamp group
```

In `_find_option`, drop `timestamp` from the key and remove the entire eviction block:
```python
def _find_option(self, snap, target, side, timestamp, max_diff=None, short_strike=None):
    target = target if side == 'CALL' else -abs(target)
    cache_key = (round(target, 4), side, max_diff, short_strike)   # no timestamp
    if cache_key in self._option_cache:
        return self._option_cache[cache_key]

    # ... search logic unchanged ...

    self._option_cache[cache_key] = res   # no eviction needed
    return res
```

The `isinstance(self._option_cache, OrderedDict)` guard at line 2074 and the entire eviction loop (lines 2077-2082) can be deleted. The cache initialisation in `__init__` can become a plain `{}`.

**Benefit at 250 strategies:** Entry lookups (4 unique keys, shared by all 250 strategies at the same tick) still get 249 cache hits. Rebalance lookups (up to 1,000 unique keys) write to the dict in O(1) with no eviction pressure. Historical replay clears cleanly per timestamp group.

---

## Scalability to 250 Sub-Strategies

### Summary verdict: Not scalable in the current design.

The bottlenecks are severe enough to cause the 30s tick to exceed its window, block the WebSocket feed, and turn startup into a multi-minute operation.

| Area | 3 strategies (current) | 250 strategies | Verdict |
|------|------------------------|----------------|---------|
| `_reconcile_combined_simulation` | 12 `add_trade` + margin calls per tick | 1,000 calls × O(n²) margin | **Breaks** — seconds of CPU per tick |
| `_data_lock` hold time in `_monitor_step` | ~12 position updates, 3 strategy checks | ~1,000 position updates, 250 checks | **Blocks** WS + broker sync loops |
| WebSocket broadcast payload | ~3 strategy objects, ~12 positions | ~250 strategy objects, ~1,000 positions | **Bloats** — hundreds of KB per 500ms per client |
| Historical replay at startup | 780 ts × 12 positions, no indexing | 780 ts × 1,000 positions, no indexing | **Unacceptable** startup latency (minutes) |
| Session file JSON write (every 30s) | Small | ~250 strategies × all trades | MB-scale writes every 30s |
| Option cache (cap 2,000) | Rarely evicted | Cache thrashes every tick | Eviction spikes (fixed by P6) |

### Required design changes for 250 strategies

**1. Pre-build a snap lookup dict once per tick** (covers P2, P3, P6 partially)

Build a `{(strike_int, side): row}` Python dict from the snap *once*, before any strategy loop, and pass it into all inner loops. This converts all per-position snap access from O(DataFrame scan) to O(1):
```python
snap_dict = {
    (int(row['strike_int']), row['side']): row
    for _, row in snap.iterrows()
}
```
Use this in `_update_all_pricing`, in the historical replay loop, and in `_reconcile_combined_simulation`.

**2. Remove `_reconcile_combined_simulation` from the hot path** (covers P1, P4)

Stop rebuilding the combined portfolio every tick. Options in order of effort:
- **Short term:** Add a `_sim_dirty` flag (see P4 fix above).
- **Medium term:** Apply the netted trade directly to `combined_portfolio` and only run the full reconcile as a periodic integrity check (e.g. every 5 minutes or on explicit request).
- **Long term:** Make `combined_portfolio` a computed view over sub-strategy portfolios rather than an independently maintained object.

**3. Make `calculate_standard_margin` lazy** (covers P1)

Cache the result on the Portfolio object and only recompute when `add_trade` is called (see P1 fix above). This eliminates the O(n²) calls from the hot path entirely.

**4. Split the WebSocket payload into fast and slow tiers**

At 250 strategies, a full state broadcast at 500ms is impractical (hundreds of KB per message, multiple clients):

- **Fast tier (500ms):** SPX price, aggregate sim/live PnL, broker status, alerts, log tail.
- **Slow tier (5–10s):** Per-strategy positions, history, trade details.

```python
# Alternate between fast and full broadcasts:
if tick_count % 10 == 0:
    await manager.broadcast(build_full_state(monitor))
else:
    await manager.broadcast(build_summary_state(monitor))
```

**5. Release `_data_lock` in batches inside `_monitor_step`**

The `with self._data_lock:` block at line 928 currently holds the lock for the entire strategy loop (potentially 250 iterations of option lookups, position updates, entry/rebalance checks). This starves the broker sync and WS broadcast loops.

Process strategies in batches of ~10, yielding between batches:
```python
strategy_items = list(self.sub_strategies.items())
for batch_start in range(0, len(strategy_items), 10):
    batch = strategy_items[batch_start:batch_start + 10]
    with self._data_lock:
        for sid, s in batch:
            ...  # process strategy
    await asyncio.sleep(0)   # yield to other tasks between batches
```

**6. Event-driven session saves**

Stop saving the session file every 30s tick. Instead save on trade events only (entry, rebalance, exit) and do a periodic safety checkpoint every 5 minutes. At 250 strategies the JSON file can be several MB and writing it on every tick adds I/O contention.

**7. Scale the option cache** (covered by P6 fix — per-tick clear makes size irrelevant)

The per-tick dict approach in P6 eliminates the need to tune cache size for strategy count.

---

## Follow-up: Incomplete Fixes (2026-03-25 Implementation Review)

The following items were either not implemented, partially implemented, or a new bug was introduced during the fix pass. Each section states the current state and the exact remaining change required.

---

### Bug 4 — Average duration numerator still includes open strategies
**File:** `core/monitor.py:666-671`
**Current state:** The denominator was corrected to count only closed strategies (`total`). However `total_dur += dur` sits outside the `if is_closed:` block, so running strategies still inflate the numerator.

**Remaining fix:** Move the duration block inside `if is_closed:`. The `last_ts` branch for open strategies is also no longer needed once we only count closed ones.

```python
# Replace lines 666-671 with:
if is_closed and s.portfolio.trades:
    entry_ts = s.portfolio.trades[0].timestamp
    last_ts = s.portfolio.trades[-1].timestamp
    dur = (last_ts - entry_ts).total_seconds() / 60.0
    total_dur += dur
```

---

### Bug 5 — `active_order_signals` never populated
**File:** `core/monitor.py:1868`
**Current state:** The guard `if sid in self.active_order_signals: continue` exists in `_monitor_step` (line 960) but `active_order_signals.add()` is never called anywhere. The set is always empty.

**Remaining fix:** Add the strategy ID to the set when its trade is enqueued, and ensure it is removed after execution. Both changes belong in `_check_reconciliation` and `run_order_execution_loop` respectively.

```python
# In _check_reconciliation, immediately after put_nowait (line 1868):
self.order_queue.put_nowait(recon_trade)
self.active_order_signals.add(recon_trade.strategy_id)   # ADD THIS LINE
```

The removal already happens via `signal_completed()` which calls `active_order_signals.discard(sid)` — verify that `signal_completed()` is called at the end of `run_order_execution_loop` after `task_done()`.

---

### Bug 8 — `_queued_purposes` never populated
**File:** `core/monitor.py:1868`
**Current state:** `_queued_purposes` is initialised (line 116), `.discard()` is called on dequeue (line 377), and the presence check `TradePurpose.RECONCILIATION in self._queued_purposes` is used at line 1784 to prevent duplicate reconciliation trades. However `.add()` is never called when items are put to the queue, so the set is always empty and the duplicate-prevention check never fires.

**Remaining fix:** Add the purpose to the set immediately after `put_nowait`. This is the same site as Bug 5:

```python
# In _check_reconciliation, immediately after put_nowait (line 1868):
self.order_queue.put_nowait(recon_trade)
self.active_order_signals.add(recon_trade.strategy_id)    # Bug 5 fix
self._queued_purposes.add(recon_trade.purpose)             # Bug 8 fix — ADD THIS LINE
```

With this in place, the existing check at line 1784 (`in_queue = TradePurpose.RECONCILIATION in self._queued_purposes`) and the existing `.discard()` at line 377 will work correctly without further changes.

---

### Bug 9 — Live bid/ask still not from market data
**File:** `core/monitor.py:1388-1389`
**Current state:** `bid = MV/qty - 0.05`, `ask = MV/qty + 0.05` is an improvement over bid==ask==price, but the spread is still fabricated. The Schwab account positions endpoint does not return bid/ask, so the correct source is `_last_snap`.

**Remaining fix:** Leave `bid` and `ask` as `0` in `get_live_positions` and fill them from `_last_snap` inside `_update_live_portfolio`, which already has a `_last_snap` fallback for Greeks:

```python
# In get_live_positions, simplify to:
'bid': 0,
'ask': 0,
```

```python
# In _update_live_portfolio, after the Greek cache block (around line 1722),
# add a snap lookup for bid/ask when _last_snap is available:
bid_price = bp.get('bid', 0)
ask_price = bp.get('ask', 0)
if (bid_price == 0 or ask_price == 0) and self._last_snap is not None:
    r = self._last_snap[
        (self._last_snap['strike_price'].round().astype(int) == int(round(bp['strike']))) &
        (self._last_snap['side'] == bp['side'])
    ]
    if not r.empty:
        bid_price = float(r['bidprice'].iloc[0])
        ask_price = float(r['askprice'].iloc[0])

self.live_combined_portfolio.positions.append(OptionLeg(
    ...
    bid_price=bid_price,
    ask_price=ask_price,
    ...
))
```

---

### P2 — `_update_all_pricing` causes TypeError on every monitor tick (new bug)
**File:** `core/monitor.py:926` and `core/monitor.py:1319`
**Current state:** The call at line 926 passes `snap_indexed=snap_indexed` as a keyword argument, but the function signature at line 1319 does not declare that parameter. This raises `TypeError: _update_all_pricing() got an unexpected keyword argument 'snap_indexed'` on every 30s tick, making the monitor non-functional.

**Remaining fix:** Add `snap_indexed` to the function signature and replace the inner boolean mask scan with an O(1) `.loc` lookup when `snap_indexed` is provided. The existing `snap` fallback is retained for compatibility.

```python
# Line 1319 — update signature:
def _update_all_pricing(self, quotes: Dict[str, Dict], snap: Optional[pd.DataFrame] = None, snap_indexed=None):
    """Update both sim and live portfolios with latest quotes using robust matching"""
    for portfolio in [self.combined_portfolio, self.live_combined_portfolio]:
        for p in portfolio.positions:
            found = False

            # 1. Fast path: O(1) indexed snap lookup (P2 fix)
            if snap_indexed is not None:
                key = (int(round(p.strike)), p.side)
                if key in snap_indexed.index:
                    row = snap_indexed.loc[[key]].iloc[0]
                    p.bid_price = row['bidprice']
                    p.ask_price = row['askprice']
                    p.price = row['mid_price']
                    delta_val = row['delta']
                    p.delta = float(delta_val) if not pd.isna(delta_val) else 0.0
                    p.theta = float(row['theta']) if not pd.isna(row['theta']) else 0.0
                    self._greek_cache[(p.symbol, int(round(p.strike)), p.side)] = (p.delta, p.theta)
                    found = True

            # 2. Slow fallback: boolean mask (only when snap_indexed unavailable)
            elif snap is not None and not snap.empty:
                r = snap[(snap['strike_price'].round().astype(int) == int(round(p.strike))) & (snap['side'] == p.side)]
                if not r.empty:
                    row = r.iloc[0]
                    p.bid_price = row['bidprice']
                    p.ask_price = row['askprice']
                    p.price = row['mid_price']
                    delta_val = row['delta']
                    p.delta = float(delta_val) if not pd.isna(delta_val) else 0.0
                    p.theta = float(row['theta']) if not pd.isna(row['theta']) else 0.0
                    self._greek_cache[(p.symbol, int(round(p.strike)), p.side)] = (p.delta, p.theta)
                    found = True

            # 3. Symbol-based quote fallback
            if not found:
                q = quotes.get(p.symbol)
                if not q:
                    norm_sym = " ".join(p.symbol.split())
                    for sym, data in quotes.items():
                        if " ".join(sym.split()) == norm_sym:
                            q = data
                            break
                if q:
                    p.bid_price = q.get('bid', 0)
                    p.ask_price = q.get('ask', 0)
                    p.price = q.get('mid', 0)
                    p.delta = q.get('delta', 0)
                    p.theta = q.get('theta', 0)
```

The call site at line 926 already passes `snap_indexed` correctly and requires no change.

---

### P3 — Residual boolean scan in historical sim portfolio pricing
**File:** `core/monitor.py:2046-2052`
**Current state:** The inner strategy position loop (lines 1996-2002) correctly uses `snap_indexed`. However after that loop, a second pricing pass over `combined_portfolio` and `live_combined_portfolio` (lines 2046-2052) still does raw boolean mask scans. `snap_indexed` is in scope at that point (built at the top of the `for ts, snap in groups:` loop).

**Remaining fix:** Replace the boolean scan with the indexed lookup:

```python
# Replace lines 2046-2052 with:
for port in [self.combined_portfolio, self.live_combined_portfolio]:
    for p in port.positions:
        key = (int(round(p.strike)), p.side)
        if key in snap_indexed.index:
            row = snap_indexed.loc[[key]].iloc[0]
            p.delta = float(row['delta']) if not pd.isna(row['delta']) else 0.0
            p.price = row['mid_price']
            p.theta = float(row['theta']) if not pd.isna(row['theta']) else 0.0
```

---

### P4 — `_sim_dirty` never set to `True` after initial state
**File:** `core/monitor.py:992-1008`
**Current state:** `_sim_dirty` is initialised to `True` and the guard in `_reconcile_combined_simulation` checks and clears it. However `_sim_dirty = True` is never set after the initial value, so after the first reconcile the flag stays `False` permanently. The call at line 1008 bypasses the guard entirely (it calls the method directly, which runs the guard internally and returns early on the second call — but since no one sets it dirty again, subsequent calls from the reconciliation loop at line 353 will be no-ops even when trades *have* happened).

**Remaining fix:** Set `_sim_dirty = True` immediately after trades are applied to sub-strategies, before calling `_reconcile_combined_simulation`:

```python
# In _monitor_step, inside the `with self._data_lock:` block at line 992,
# after the t_trades application loop and before _reconcile_combined_simulation:

for sid, trades in t_trades.items():
    if sid in self.sub_strategies:
        for tr in trades:
            self.sub_strategies[sid].portfolio.add_trade(tr)
            self.sub_strategies[sid].has_traded_today = True

if t_trades:                       # ADD: only mark dirty if trades actually occurred
    self._sim_dirty = True         # ADD THIS LINE

netted = self.net_trades(t_trades)
for nt in netted:
    self.combined_portfolio.add_trade(nt)

self._reconcile_combined_simulation()   # now correctly sees _sim_dirty=True
```

With this in place, the reconciliation loop call at line 353 (`self._reconcile_combined_simulation()`) will be a cheap early-return on ticks with no new trades, and a real rebuild only on ticks where trades occurred.
