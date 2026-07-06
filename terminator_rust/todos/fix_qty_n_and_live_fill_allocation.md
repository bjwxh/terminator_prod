# Fix: Qty:N Partial Fill + Live FIFO Fill Allocation

## Scenario That Exposed the Bugs

Three strategies each held an iron condor. The app correctly cancelled 3 stale orders and
proposed two new orders:
- **Order #1**: Qty:2 — both strategies wanted identical strikes (7460 long call)
- **Order #2**: Qty:1 — one strategy had a different long call (7465)

After confirm, only 2 of the 3 condors were established. Schwab partially filled the Qty:2
order (1 of 2 units), leaving the second identical condor missing. The app correctly detected
the gap via GAP_RECON and re-prompted.

---

## Bug 1 — Qty:N Batching Creates Partial Fill Risk

### Root Cause

`get_smart_chunks` (lines 911–928) merges identical iron condor units into a single Qty:N
exchange order. The grouping key is `(symbol, instruction)` per leg.

Two strategies wanting the same strikes → same key → `roll_legs` combines them into one chunk
with quantity 2 → one API call to Schwab for Qty:2.

Schwab does NOT guarantee all-or-none fills on multi-unit complex options spreads. It fills
unit-by-unit. If the market moves between units, only 1 of the 2 requested fills. The second
condor is left missing.

### Fix

Remove the combo merging step from `get_smart_chunks`. Return each unrolled iron condor
unit as its own Qty:1 chunk instead of rolling them together.

**File:** `terminator_rust/src/strategy.rs`

**Change:** In `get_smart_chunks`, instead of the grouping/merging block (lines 911–929),
directly return `found_combos` with any leftover legs appended.

```rust
// REMOVE the grouped HashMap and roll_legs merging entirely.
// Replace with:

let mut final_chunks: Vec<Vec<OptionLeg>> = found_combos;

if !leftover_rolled.is_empty() {
    for chunk in leftover_rolled.chunks(4) {
        final_chunks.push(chunk.to_vec());
    }
}

final_chunks
```

**Effect:** Three identical iron condors → three separate Qty:1 exchange orders, each
independently fillable and independently traceable.

**Trade-off:** Slightly higher commission (one commission per order vs one for a Qty:N batch).
Acceptable for a user-gated system where each order is confirmed manually.

---

## Bug 2 — Live FIFO Fill Allocation Is Completely Broken

### Root Cause

The parser (`parser.rs` lines 162–176) handles `ExecutionCreated` fill events with a fallback
that extracts `ExecutionQuantity` but sets `symbol: String::new()`.

In `process_account_event`, the FIFO allocation loop does:
```rust
let symbol = &leg.symbol;  // = "" for all live fills
let target_qty = port.position_qty_for("");  // always 0
let diff = 0 - 0 = 0;  // never enters the waiting list
```

**Result**: No strategy ever gets a fill allocated via FIFO in live mode. Strategies remain stuck
with `previous_portfolio` set indefinitely. Rebalance and exit signals are permanently blocked
after the first live trade of the day.

The system appears to work because `broker_portfolio` is updated separately via the REST API
fast-sync (`get_today_filled_orders`), and `check_reconciliation` uses `broker_portfolio` to
detect gaps. But the per-strategy `previous_portfolio` is never cleared, so the strategy guard
`if s.previous_portfolio.is_some() { skip }` blocks all future signals.

### Fix

When a fill event arrives, after the FIFO loop fails (because symbol is empty), fall back to
comparing `broker_portfolio` with each strategy's `previous_portfolio` to detect whether a
strategy's fills have landed. 

**Practical approach:** After the REST fast-sync updates `broker_portfolio`, in
`check_and_finalize_fill` (or in the reconciliation loop), also check whether each strategy's
optimistic positions are now reflected in `broker_portfolio`. If yes, clear `previous_portfolio`.

**Alternatively:** Fix the parser to extract the symbol from `ExecutionCreated` events.
The Schwab `ExecutionCreated` event payload has the leg information under
`ExecutionCreatedEventExecutionInfo` → look for a symbol field there (e.g.,
`/BaseEvent/ExecutionCreatedEventExecutionInfo/Security/Symbol`). If that path exists in
Schwab's protobuf schema, populate `leg.symbol` and `leg.buy_sell` from it, making the
FIFO allocation work.

**Recommended approach:** Fix the parser first (correct at the source). If Schwab's
`ExecutionCreated` doesn't carry the symbol, use the broker_portfolio comparison as fallback.

**Interim mitigation in check_reconciliation:** After updating `broker_portfolio` via REST,
check each strategy's `previous_portfolio`. For any strategy where `previous_portfolio` positions
are now fully reflected in `broker_portfolio.positions`, call `check_and_finalize_fill` (it will
return true since the optimistic portfolio already matches the broker state after REST sync
updates it). This unblocks the strategy.

```rust
// After broker_portfolio.sync_from_broker / REST update:
let broker_port = self.broker_portfolio.lock().await;
for s in strats.values_mut() {
    if s.previous_portfolio.is_some() {
        // Check if broker has confirmed all the optimistic positions
        let port = s.portfolio.lock().await;
        let all_confirmed = port.positions.iter().all(|pos| {
            broker_port.position_qty_for(&pos.symbol).abs() >= pos.quantity.abs()
        });
        if all_confirmed {
            drop(port);
            s.revert_optimistic_update().await; // or check_and_finalize_fill
        }
    }
}
```

Note: this is a simplification — proper implementation should account for shared broker
positions across multiple strategies (since broker has aggregate positions, not per-strategy).
A correct implementation would aggregate all strategies' target positions and compare with
broker's aggregate.

---

---

## Bug 3 — create_execution_plan double-counts coverage after Qty:N fix (FIXED)

### Root Cause

After Bug 1's fix, `execute_trade` sends `leg.quantity = total contracts` (e.g. 2).
Schwab stores this and mirrors it back in `quantity` (order-level) too, so both fields = 2.

`create_execution_plan` (line 1553) was: `working_qty = qty * mult * order_qty = 2 * mult * 2 = 4`.
For a 2-contract working order this inflated coverage to 4, making legs appear
over-covered. `to_fill` went opposite-sign and got dropped — leaving only
the one uncovered leg (+1C7465) in remaining_legs.

### Fix Applied

Line 1553: `let working_qty = qty * mult;` — drop `* order_qty`.
Schwab's `leg.quantity` is already the total; multiplying by `order_qty` double-counts.

---

## Verification

1. With Bug 1 fix: confirm modal now shows 3 separate Qty:1 orders (not Qty:2 + Qty:1).
   All 3 are submitted independently. No partial fill risk.

2. With Bug 2 fix: after a live entry fills:
   - strategy state transitions to `Working` within one reconciliation cycle
   - Rebalance conditions fire normally on subsequent ticks
   - Exit fires at end of day without being blocked by `previous_portfolio`

3. `cargo test` must pass.
