# Fix: Dry-Run Simulation Deletes Trade History on Finalization

## Root Cause

When a REBALANCE (or EXIT) trade fires in dry-run mode, the following sequence destroys the trade record:

1. `tick()` fires REBALANCE:
   - `s.previous_portfolio` ← snapshot of `s.portfolio` (pre-rebalance positions + trades)
   - `s.portfolio.add_trade(&rebal_trade)` → portfolio now holds the REBALANCE trade + post-rebalance positions

2. `check_reconciliation()` detects a gap (sim is ahead of broker, which never executes in dry-run):
   - Injects `DRY_RUN_FILL` stubs into `s.previous_portfolio` leg-by-leg via `prev_port.add_trade(...)`
   - `s.previous_portfolio` now has: **pre-rebalance trades + DRY_RUN_FILL stubs**, post-rebalance positions

3. `check_and_finalize_fill()` sees positions match → line 117:
   ```rust
   *self.portfolio.lock().await = prev_port;
   ```
   This **replaces `s.portfolio` entirely** with `prev_port`. The REBALANCE trade in `portfolio.trades` is overwritten by DRY_RUN_FILL stubs. Trade history is permanently lost.

## Disagreement with Developer's Proposed Fix Location

The developer proposed fixing this by making `s.previous_portfolio` "retain the original target trades." This is the wrong place to fix it.

`previous_portfolio` is correctly populated with `DRY_RUN_FILL` stubs — that is precisely how `check_and_finalize_fill` detects that all legs are confirmed. Changing what goes into `previous_portfolio` would break the fill-detection logic.

The fix belongs in `check_and_finalize_fill` itself.

## The Fix

**File:** `terminator_rust/src/strategy.rs`

**Function:** `SubStrategy::check_and_finalize_fill` (line ~115)

**Change:** Instead of replacing the entire `s.portfolio` with `prev_port`, only update `positions` and `cash`. Preserve `portfolio.trades` so REBALANCE/EXIT records with correct purpose and metadata are never overwritten.

### Before (line 115–125):
```rust
if fully_filled {
    let prev_port = self.previous_portfolio.take().unwrap();
    *self.portfolio.lock().await = prev_port;
    self.last_update_ts = None;

    let has_positions = !self.portfolio.lock().await.positions.is_empty();
    if has_positions {
        self.state = StrategyState::Working;
    } else {
        self.state = StrategyState::Idle;
    }
    true
}
```

### After:
```rust
if fully_filled {
    let prev_port = self.previous_portfolio.take().unwrap();
    {
        let mut port = self.portfolio.lock().await;
        port.positions = prev_port.positions;
        port.cash = prev_port.cash;
        // port.trades is intentionally preserved: it holds the canonical REBALANCE/EXIT
        // records with correct purpose, metadata, and strategy_id. DRY_RUN_FILL and
        // BROKER_FILL stubs from prev_port are not trade-history records.
    }
    self.last_update_ts = None;

    let has_positions = !self.portfolio.lock().await.positions.is_empty();
    if has_positions {
        self.state = StrategyState::Working;
    } else {
        self.state = StrategyState::Idle;
    }
    true
}
```

## Trade-off for Live Trading

For live trades, the current behavior writes actual fill prices (from BROKER_FILL events) into `portfolio.trades`. After this fix, `portfolio.trades` will retain the optimistic (mid-price) REBALANCE entry rather than per-leg fill prices.

This is acceptable because:
- Actual fill prices are already tracked at the `broker_portfolio` level via `process_account_event`
- The `portfolio.trades` list in sub-strategies is used for P&L estimation and UI display, not execution accounting
- Losing the REBALANCE trade record entirely (current bug) is strictly worse than keeping it at an estimated price

## The "Aggregated Positions Live Qty" Issue is Not a Bug

The developer's explanation also flags that the "Live Qty" column in the Aggregated Positions table stays flat in dry-run. This is **expected behavior** — that column reflects the real Schwab broker snapshot, which correctly shows no positions because no real order was ever sent. If the intent is to show simulated positions in that column during dry-run, that is a separate UI feature request, not a backend bug.

## Verification

1. In dry-run mode, trigger a REBALANCE condition. Confirm the REBALANCE trade appears in the strategy's trade history and does not disappear on the next tick.
2. Confirm `s.portfolio.positions` correctly reflect the post-rebalance state after finalization.
3. Run `cargo test` — all 6 existing strategy tests must pass.
4. In live mode, confirm an entry/exit/rebalance still transitions state correctly (`Working` → `Exiting` → `Idle`) and positions are accurate after fills.
