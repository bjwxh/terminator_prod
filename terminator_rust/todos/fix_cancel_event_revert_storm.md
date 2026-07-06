# Fix: Stale-Order Cancellation Events Blindly Revert All Sub-Strategy Optimistic Updates

## Observed Symptoms (2026-06-26 session)

1. **Flickering sim trades at 08:57:55** — 5 REBALANCE trades appeared in the Sim Trades
   panel then disappeared immediately after the GAP_RECON popup was dismissed.
2. **Sim positions stuck** — strat_0835, strat_0840, strat_0845 never committed their
   rebalance; positions stayed frozen at the pre-rebalance state.

## Root Cause

### Bug 1 (primary): `process_account_event` reverts ALL sub-strategies on ANY cancellation

Location: `terminator_rust/src/strategy.rs`, function `process_account_event`, lines ~2116–2122.

```rust
if event.status == "Cancelled" || event.status == "Rejected" {
    for s in strats.values_mut() {
        if s.previous_portfolio.is_some() {
            s.finalize_partial_fill().await;   // ← reverts optimistic update
        }
    }
}
```

Order `1006927770935` (a stale working order being chased in the UI) was generating
`ExecutionCreated: Cancelled` + `OrderUROutCompleted: Cancelled` events at ~4-second
intervals throughout the 13:57 minute. Every event hit the above branch and called
`finalize_partial_fill` (which is identical to `revert_optimistic_update`) for every
sub-strategy that had a pending `previous_portfolio`.

These sub-strategies (strat_0835/0840/0845) had NO connection to order `...935` — their
optimistic updates were REBALANCE trades staged by `tick()`, waiting for a GAP_RECON
order to be placed. The cancel events from an unrelated chased order destroyed them.

The resulting cycle:
```
tick() adds optimistic REBALANCE trades for strat_0835/0840/0845
  → live_portfolio rebuilt → trades appear in Sim Trades panel
    → cancel event for ...935 fires
      → finalize_partial_fill for ALL strats → optimistic trades reverted
        → live_portfolio rebuilt → trades disappear from Sim Trades panel
          → reconciliation detects gap again → GAP_RECON popup
            → user dismisses → dismiss_trade reverts again (Bug 2 below)
              → next tick() re-adds REBALANCE trades → cycle repeats
```

### Bug 2 (amplifier): `dismiss_trade` for GAP_RECON re-reverts sub-strategies

Location: `dismiss_trade` function, lines ~2437–2442.

Dismissing the GAP_RECON popup also calls `revert_optimistic_update` on all sub-strategies.
This is correct in principle (no orders were placed, so the optimistic state must not persist).
However, because Bug 1 already reverted the same strategies moments earlier, this call is
redundant. More importantly, it means even if Bug 1 were not present, a single user
dismissal would wipe the optimistic REBALANCE trades from the sim history.

### Why `active_order_id` does not help today

`SubStrategy` has an `active_order_id: Option<String>` field (line 64) but it is never
populated beyond the initial `None`. The cancellation handler has no way to filter which
strategies are actually waiting for fills from a specific order.

## Fix Plan

### Fix 1: Scope cancellation reverts to strategies waiting for the cancelled order's symbols

**File:** `terminator_rust/src/strategy.rs`
**Function:** `process_account_event`

The key insight is that a sub-strategy with a pending optimistic update is only
affected by an order cancellation if that order contains legs whose symbols overlap with
the sub-strategy's pending fill diff (i.e., `portfolio.position_qty_for(sym) !=
previous_portfolio.position_qty_for(sym)`).

#### Current code (lines ~2116–2122):
```rust
if event.status == "Cancelled" || event.status == "Rejected" {
    warn!("⚠️ Order {} was cancelled/rejected. Finalizing partial fills or reverting.", event.order_id);
    for s in strats.values_mut() {
        if s.previous_portfolio.is_some() {
            s.finalize_partial_fill().await;
        }
    }
}
```

#### New code:
```rust
if event.status == "Cancelled" || event.status == "Rejected" {
    warn!("⚠️ Order {} was cancelled/rejected. Finalizing partial fills or reverting.", event.order_id);
    if event.legs.is_empty() {
        // No leg data (e.g. OrderUROutCompleted) — cannot determine which strategies
        // are affected. Skip to avoid reverting unrelated optimistic updates.
        // The paired ExecutionCreated event (which does carry legs) already handled any
        // needed revert for this order.
    } else {
        let cancelled_symbols: std::collections::HashSet<&str> =
            event.legs.iter().map(|l| l.symbol.as_str()).collect();
        for s in strats.values_mut() {
            if let Some(ref prev) = s.previous_portfolio {
                let port = s.portfolio.lock().await;
                let affects_this_strategy = cancelled_symbols.iter().any(|sym| {
                    port.position_qty_for(sym) != prev.position_qty_for(sym)
                });
                drop(port);
                if affects_this_strategy {
                    s.finalize_partial_fill().await;
                }
            }
        }
    }
}
```

This ensures that a cancel event for order `...935` (whose leg symbol is, e.g., a 7370
CALL) does not revert strat_0835 unless strat_0835 is actually waiting for fills in that
symbol.

### Fix 2 (follow-up / longer term): Populate `active_order_id` for precise order tracking

The symbol-overlap approach in Fix 1 is safe but imprecise: if two strategies both need
the same symbol and only one order is cancelled, both strategies get reverted even though
only one was assigned fills from that order.

The correct long-term solution is to populate `SubStrategy.active_order_id` when
orders are placed:

1. In `confirm_trade` (live mode, after the `execute_trade` call), distribute the returned
   order IDs to the sub-strategies that are waiting for fills from the matching symbols.
   Set `s.active_order_id = Some(order_id.clone())` for each matched strategy.

2. In `process_account_event` cancellation handler, replace the symbol check with:
   ```rust
   if s.active_order_id.as_deref() == Some(&event.order_id) {
       s.active_order_id = None;
       s.finalize_partial_fill().await;
   }
   ```

3. Clear `active_order_id` in `revert_optimistic_update` and `check_and_finalize_fill`.

This is a bigger refactor; Fix 1 unblocks the immediate flickering/stall without it.

### Fix 3 (related): `OrderUROutCompleted` events should not trigger a second revert

Currently, the same order `...935` fires two Cancelled events in sequence:
- `ExecutionCreated, Cancelled, Legs count = 1` (the actual cancel)
- `OrderUROutCompleted, Cancelled, Legs count = 0` (confirmation the cancel completed)

Fix 1 already handles this: the zero-legs guard skips the revert for `OrderUROutCompleted`.
No additional change needed once Fix 1 is in place.

## Files to Change

| File | Change |
|------|--------|
| `src/strategy.rs` | `process_account_event`: add symbol-overlap guard for cancellation reverts |
| `src/strategy.rs` | (optional / Fix 2) populate `active_order_id` in `confirm_trade`; use it in cancel handler |

## Tests

1. **Unit test**: In `tests/strategy_tests.rs`, add a case where a Cancelled event for an
   order whose symbol does NOT overlap with any sub-strategy's pending diff is processed.
   Assert that no sub-strategy's `previous_portfolio` is cleared and positions remain
   in the optimistic state.

2. **Unit test**: Cancelled event whose symbol DOES overlap with a pending sub-strategy
   diff → that sub-strategy reverts; unaffected strategies stay.

3. **All existing tests must still pass** (`cargo test`).

## Verification (manual, live session)

1. Enter a state where strat_XXXX has an optimistic REBALANCE update (positions appear in
   Sim Trades panel with new timestamp).
2. Chase an existing working order (triggers repeated cancel events).
3. Confirm that the REBALANCE sim trades do NOT flicker / disappear during the chase.
4. Confirm positions update correctly after the GAP_RECON confirmation is sent.

## Related Todo

See also `fix_dry_run_trade_history.md` — that bug causes the REBALANCE trade record to
be overwritten by `DRY_RUN_FILL` stubs when `check_and_finalize_fill` commits. It is a
separate bug that surfaces in `dry_run: true` mode, but shares the same
`previous_portfolio` ↔ `portfolio.trades` overwrite mechanism. Fix that one after this.
