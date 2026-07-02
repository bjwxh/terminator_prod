# Changelog 004: REST Fast-Sync Coalescing & Rebalance Unblocking Fixes

## Asynchronous Fast-Sync Queue & Coalescing (Race Condition Mitigation)
*   **MPSC Channel Queue**: Replaced the parallel task spawning model with a serialized background worker loop (`run_fast_sync_loop`) listening on an MPSC channel (`fast_sync_tx` and `fast_sync_rx`). This ensures all REST fast-sync operations run sequentially in order, completely eliminating the risk of network-jitter-induced out-of-order state overwrites.
*   **Debouncing & Coalescing**: Added a 50ms sleep debounce inside the worker task. Rapidly arriving WebSocket activity events (such as leg-by-leg fills) are now coalesced into a single optimized REST API sync, significantly reducing unnecessary Schwab API requests.

## Rebalance Unblocking via REST Fills
*   **Sub-Strategy Reconciliation**: Implemented `apply_broker_fills_to_strategies` to match REST-synced filled trades back to pending sub-strategies. This resolves the issue where sub-strategies remained permanently stuck in the `previous_portfolio` pending state when Schwab WebSocket streams emitted zero-leg `OrderFillCompleted` events, which previously prevented any subsequent rebalance actions.
*   **Trades List Preservation**: Updated `check_and_finalize_fill` to copy the `trades` list from `previous_portfolio` into the main `portfolio` upon fill finalization. This retains filled trades (with order-id metadata) permanently inside the sub-strategy, enabling the deduplication logic to ignore already-allocated fills on subsequent sync cycles.
*   **Option Grid Lookups**: Looked up live mid-price, delta, and theta values from the options grid `self.grid` during fill allocation, bypassing the default `0.0` prices returned by the Schwab REST orders API to ensure accurate premium, cost basis, and Greek calculations.
*   **Leg Grouping in Sim Trades**: Aggregated allocated trade legs on a per-sub-strategy basis before constructing and adding the `Trade` object. This ensures that multi-leg orders (such as iron condors) are displayed as a single grouped order in the Sim Trades UI table, matching the display behavior of Live Trades.

## Working Orders Mark Price Fix
*   **Real-time Combo Mark Pricing**: Replaced the hardcoded `null` value for working order mark prices in `web.rs` with a dynamic calculation. The backend now looks up the current streaming WebSocket mid prices from the options grid `state.grid.quotes` for each active leg of a working order, computing the exact net-mid price of the combo.
*   **Correct Debit/Credit Signage**: Set the mark price value to `-net_flow` (net debit) to perfectly align with the UI's parsing logic (where negative is rendered as `Cr` and positive as `Db`), restoring accurate real-time mark tracking (e.g. `$3.80 Db` or `$1.30 Cr` instead of `--`) for working orders.
