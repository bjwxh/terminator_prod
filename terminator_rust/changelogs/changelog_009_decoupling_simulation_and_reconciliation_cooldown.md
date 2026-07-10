# Changelog 009: Decoupling Simulation and Reconciliation Cooldown

## Changes

### 1. Decoupled Simulation Portfolios from Live Trading
- Removed obsolete fields (`previous_portfolio`, `snapshot_trade_count`, `last_update_ts`, `cancelled_at`) from `SubStrategy`.
- Removed obsolete methods (`check_and_finalize_fill`, `revert_optimistic_update`, `finalize_partial_fill`) from `SubStrategy`.
- Simplified `StrategySupervisor::tick` so sub-strategies transition directly to `Working`/`Idle` and fill instantly in their simulated portfolios on entry/exit/rebalance events without waiting for broker events.

### 2. Added Manual Reconciliation Cooldown
- Added `last_dismissed_at: Mutex<Option<Instant>>` to `StrategySupervisor`.
- Updated `check_reconciliation` to sync the broker positions snapshot and immediately return if a manual reconciliation popup was dismissed less than 10 seconds ago.
- Deleted the obsolete "Internal Netting math" block in `check_reconciliation` as sub-strategies portfolios are now decoupled and net automatically when combined.
- Updated `dismiss_trade` to record the dismissal timestamp in `last_dismissed_at` on any manual dismissal of `GAP_RECON`.

### 3. Cleaned Up Dead Code
- Deleted `process_pending_finalizations` from `StrategySupervisor`.
- Cleaned up `process_account_event` to remove all optimistic update, revert, and fill allocation logic.
- Simplified `confirm_trade`'s dry-run branch to directly sync the simulated portfolio with the broker portfolio.
- Deleted `apply_broker_fills_to_strategies` from `StrategySupervisor`.

### 4. Updated Unit Tests
- Updated `test_strategy_supervisor_tick` to check that simulated portfolio fills instantly and transitions to `Working`.
- Removed obsolete unit tests (`test_process_account_event_order_id_matching`, `test_cancel_event_symbol_overlap_filtering`, `test_has_traded_today_on_finalize`, `test_net_zero_roll_rebalance_guard`).
